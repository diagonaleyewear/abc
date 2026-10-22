"""Shared core for supplier image connectors (Marcolin, NuORDER, Sáfilo; issue #59).

Portal-independent: durable request store with a transactional state machine, demand
from explicit EAN lists (optionally from Odoo purchase orders), file/EAN matching with explicit evidence,
and import into the common ImageRepository. Portals plug in through ``ProviderPortal``;
nothing here touches the network and no credentials are handled.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import random
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Protocol
from zipfile import BadZipFile, ZipFile

from image_naming import classified_view, sanitize_filename
from image_repository import IMAGE_VIEWS, VIEW_ALIASES, ImageRepository
from kering_images import (
    AccessNotConfirmed, InterventionRequired, LoginFailed, PortalUnavailable, read_orders,
)

READY, SUBMITTING, SUBMITTED, PENDING, AVAILABLE = "ready", "submitting", "submitted", "pending", "available"
DOWNLOADED, IMPORTED, PARTIAL, FAILED, NEEDS_REVIEW, CANCELLED = (
    "downloaded", "imported", "partial", "failed", "needs_review", "cancelled")
TERMINAL = {IMPORTED, PARTIAL, FAILED, CANCELLED}
TRANSITIONS = {
    READY: {SUBMITTING, SUBMITTED, CANCELLED},
    SUBMITTING: {SUBMITTED, READY, FAILED, NEEDS_REVIEW},
    SUBMITTED: {PENDING, AVAILABLE, FAILED, NEEDS_REVIEW},
    PENDING: {PENDING, AVAILABLE, FAILED, NEEDS_REVIEW},
    AVAILABLE: {DOWNLOADED, FAILED, NEEDS_REVIEW},
    DOWNLOADED: {IMPORTED, PARTIAL, FAILED, NEEDS_REVIEW},
    NEEDS_REVIEW: {READY, PENDING, AVAILABLE, IMPORTED, PARTIAL, FAILED, CANCELLED},
    IMPORTED: set(), PARTIAL: set(), FAILED: set(), CANCELLED: set(),
}
PROVIDERS = ("marcolin", "nuorder", "safilo")
VALID_VIEWS = {*IMAGE_VIEWS, "unknown"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
MANIFEST_NAMES = {"manifest.csv", "manifest.json", "metadata.csv", "metadata.json"}
EAN_LENGTHS = {8, 12, 13, 14}
MAX_ZIP_ENTRIES = 5000
MAX_ZIP_BYTES = 2 * 1024**3
MAX_FILE_BYTES = 30 * 1024 * 1024


class RequestStateError(Exception):
    pass


class DefiniteNotSent(Exception):
    """Raised by adapters when a failure is guaranteed to have happened before sending."""


NOT_SENT_ERRORS = (DefiniteNotSent, LoginFailed, InterventionRequired, PortalUnavailable, AccessNotConfirmed)


def ean_valid(ean: str) -> bool:
    if not ean or not ean.isascii() or not ean.isdigit() or len(ean) not in EAN_LENGTHS:
        return False
    digits = [int(ch) for ch in ean[::-1]]
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(digits[1:]))
    return (10 - total % 10) % 10 == digits[0]


def normalize_eans(values) -> tuple[list[str], list[str]]:
    """Unique valid EANs in input order and the rejected raw values (bad length/checksum)."""
    valid, invalid, seen = [], [], set()
    for raw in values:
        ean = str(raw or "").strip()
        if not ean or ean in seen:
            continue
        seen.add(ean)
        (valid if ean_valid(ean) else invalid).append(ean)
    return valid, invalid


def ean_key(ean: str) -> str:
    return ean.zfill(14)


@dataclass(frozen=True)
class ProviderSettings:
    provider: str
    supplier_ids: tuple[int, ...] = ()
    max_eans_per_request: int = 50
    max_requests_per_day: int = 5
    pilot_cap: int = 0
    required_views: tuple[str, ...] = ("frontal", "lateral", "perspectiva")
    allow_filename_ean: bool = True
    max_attempts: int = 3
    poll_base_seconds: float = 900.0
    poll_cap_seconds: float = 6 * 3600.0

    @property
    def automation_enabled(self) -> bool:
        return os.getenv(f"{self.provider.upper()}_AUTOMATION_ENABLED", "false").strip().lower() == "true"


@dataclass
class PortalFile:
    name: str
    content: bytes
    ean: str | None = None
    view: str | None = None
    evidence: str | None = None


@dataclass
class PortalPackage:
    files: list[PortalFile] = field(default_factory=list)
    manifest: bytes | None = None
    manifest_name: str = "manifest.csv"


@dataclass(frozen=True)
class PortalStatus:
    state: str  # pending | available | failed | unknown
    detail: str = ""


class ProviderPortal(Protocol):
    def submit(self, eans: list[str]) -> str: ...
    def status(self, remote_id: str) -> PortalStatus: ...
    def download(self, remote_id: str) -> PortalPackage: ...


class UnverifiedProviderPortal:
    """Default portal: access method, terms and restrictions are not confirmed."""

    def submit(self, eans):
        raise PortalUnavailable()

    def status(self, remote_id):
        raise PortalUnavailable()

    def download(self, remote_id):
        raise PortalUnavailable()


def parse_manifest(content: bytes, name: str = "manifest.csv") -> dict[str, dict]:
    """filename -> {ean, view}; a filename repeated with different EANs is marked conflicting."""
    text = content.decode("utf-8-sig", errors="replace")
    if name.lower().endswith(".json") or text.lstrip().startswith(("[", "{")):
        data = json.loads(text)
        rows = data.get("files", data.get("items", [])) if isinstance(data, dict) else data
    else:
        sample = text[:2048]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        rows = list(csv.DictReader(io.StringIO(text), dialect=dialect))
    entries: dict[str, dict] = {}
    for row in rows:
        lowered = {str(k).strip().lower(): str(v).strip() for k, v in row.items() if k is not None and v is not None}
        file_name = next((lowered[k] for k in ("filename", "file", "archivo", "nombre") if lowered.get(k)), "")
        ean = next((lowered[k] for k in ("ean", "barcode", "gtin", "upc") if lowered.get(k)), "")
        view = next((lowered[k] for k in ("view", "vista", "angle") if lowered.get(k)), "")
        if not file_name or not ean:
            continue
        key = Path(file_name.replace("\\", "/")).name
        entry = {"ean": ean, "view": view}
        previous = entries.get(key)
        if previous and (previous.get("conflict") or ean_key(previous["ean"]) != ean_key(ean)):
            entries[key] = {**previous, "conflict": True}
        else:
            entries[key] = entry
    return entries


def read_zip_package(content: bytes) -> PortalPackage:
    """Safe in-memory read: basenames only, bounded entries/size, images and manifest only."""
    package = PortalPackage()
    try:
        archive = ZipFile(io.BytesIO(content))
    except BadZipFile as error:
        raise ValueError("zip_invalido") from error
    with archive:
        infos = [i for i in archive.infolist() if not i.is_dir()]
        if len(infos) > MAX_ZIP_ENTRIES or sum(i.file_size for i in infos) > MAX_ZIP_BYTES:
            raise ValueError("zip_demasiado_grande")
        for info in infos:
            parts = info.filename.replace("\\", "/").split("/")
            base = parts[-1]
            if not base or "__MACOSX" in parts or base.startswith("."):
                continue
            lowered = base.lower()
            if info.file_size > MAX_FILE_BYTES and lowered not in MANIFEST_NAMES:
                continue
            if lowered in MANIFEST_NAMES:
                package.manifest, package.manifest_name = archive.read(info), lowered
            elif Path(lowered).suffix in IMAGE_SUFFIXES:
                package.files.append(PortalFile(name=base, content=archive.read(info)))
    return package


def match_file(file: PortalFile, requested: dict[str, str], manifest: dict[str, dict],
               allow_filename_ean: bool) -> tuple[str | None, str | None, str, str | None]:
    """-> (ean, evidence, view, problem). Never infers by order or by brand."""
    entry = manifest.get(file.name)
    ean = evidence = None
    view = ""
    if entry is not None:
        if entry.get("conflict"):
            return None, None, "unknown", "manifiesto_contradictorio"
        ean, evidence, view = requested.get(ean_key(entry["ean"])), "manifest", entry.get("view", "")
        if ean is None:
            return None, None, "unknown", "ean_no_solicitado"
    elif file.ean:
        ean, evidence, view = requested.get(ean_key(file.ean)), file.evidence or "portal_metadata", file.view or ""
        if ean is None:
            return None, None, "unknown", "ean_no_solicitado"
    elif allow_filename_ean:
        found = {requested[ean_key(t)] for t in re.findall(r"(?<!\d)\d{8,14}(?!\d)", file.name)
                 if ean_key(t) in requested}
        if len(found) > 1:
            return None, None, "unknown", "varios_ean_en_nombre"
        if found:
            ean, evidence = next(iter(found)), "filename_ean"
    if ean is None:
        return None, None, "unknown", "sin_evidencia_de_ean"
    view = VIEW_ALIASES.get(view, view)
    if view not in VALID_VIEWS:
        view = "unknown"
    if view == "unknown":
        view = file.view if file.view in IMAGE_VIEWS else classified_view(file.name)
    return ean, evidence, view, None


SCHEMA = """
CREATE TABLE IF NOT EXISTS requests(
  id INTEGER PRIMARY KEY, provider TEXT NOT NULL, mode TEXT NOT NULL DEFAULT 'portal',
  remote_id TEXT, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 1, polls INTEGER NOT NULL DEFAULT 0,
  last_error TEXT, retry_of INTEGER, created_at REAL NOT NULL, updated_at REAL NOT NULL, next_poll_at REAL);
CREATE TABLE IF NOT EXISTS request_eans(
  request_id INTEGER NOT NULL REFERENCES requests(id), provider TEXT NOT NULL, ean TEXT NOT NULL,
  result TEXT NOT NULL DEFAULT '', live INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(request_id, ean));
CREATE UNIQUE INDEX IF NOT EXISTS one_live_request_per_ean ON request_eans(provider, ean) WHERE live = 1;
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, request_id INTEGER, at REAL NOT NULL, from_state TEXT, to_state TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS review_items(
  id INTEGER PRIMARY KEY, request_id INTEGER NOT NULL, provider TEXT NOT NULL, name TEXT NOT NULL,
  sha256 TEXT NOT NULL, reason TEXT NOT NULL, record_id INTEGER, ean TEXT, staged TEXT,
  status TEXT NOT NULL DEFAULT 'open', resolved_by TEXT, resolution TEXT, created_at REAL NOT NULL,
  UNIQUE(request_id, sha256, name));
CREATE TRIGGER IF NOT EXISTS events_append_only_update BEFORE UPDATE ON events
  BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_append_only_delete BEFORE DELETE ON events
  BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
"""


class ProviderStore:
    def __init__(self, root: Path, clock: Callable[[], float] = time.time):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "provider_requests.sqlite"
        self.staging = self.root / "provider_staging"
        self.clock = clock
        with closing(self.connect()) as connection:
            connection.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _event(self, connection, request_id, from_state, to_state, detail=""):
        connection.execute("INSERT INTO events(request_id,at,from_state,to_state,detail) VALUES(?,?,?,?,?)",
                           (request_id, self.clock(), from_state, to_state, str(detail)[:500]))

    def plan(self, settings: ProviderSettings, eans, *, mode: str = "portal") -> dict:
        """Create ready requests for EANs with no live request; never duplicates pending ones."""
        valid, invalid = normalize_eans(eans)
        created, skipped = [], []
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            live = {r[0] for r in connection.execute(
                "SELECT ean FROM request_eans WHERE provider=? AND live=1", (settings.provider,))}
            fresh = [e for e in valid if e not in live]
            skipped = [e for e in valid if e in live]
            size = max(1, settings.max_eans_per_request)
            now = self.clock()
            for start in range(0, len(fresh), size):
                chunk = fresh[start:start + size]
                cursor = connection.execute(
                    "INSERT INTO requests(provider,mode,state,created_at,updated_at) VALUES(?,?,?,?,?)",
                    (settings.provider, mode, READY, now, now))
                rid = cursor.lastrowid
                connection.executemany(
                    "INSERT INTO request_eans(request_id,provider,ean) VALUES(?,?,?)",
                    [(rid, settings.provider, e) for e in chunk])
                self._event(connection, rid, None, READY, f"{len(chunk)} EAN")
                created.append(rid)
            connection.execute("COMMIT")
        return {"created": created, "already_live": skipped, "invalid": invalid}

    def get(self, request_id: int) -> dict:
        with closing(self.connect()) as connection:
            row = connection.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
            if row is None:
                raise KeyError(request_id)
            result = dict(row)
            result["eans"] = [dict(r) for r in connection.execute(
                "SELECT ean,result,live FROM request_eans WHERE request_id=? ORDER BY ean", (request_id,))]
        return result

    def list_requests(self, provider: str | None = None, states: set[str] | None = None) -> list[dict]:
        query, args = "SELECT r.*, (SELECT COUNT(*) FROM request_eans e WHERE e.request_id=r.id) AS ean_count " \
                      "FROM requests r WHERE 1=1", []
        if provider:
            query, args = query + " AND provider=?", args + [provider]
        if states:
            query += f" AND state IN ({','.join('?' * len(states))})"
            args += sorted(states)
        with closing(self.connect()) as connection:
            return [dict(r) for r in connection.execute(query + " ORDER BY id DESC", args)]

    def events(self, request_id: int) -> list[dict]:
        with closing(self.connect()) as connection:
            return [dict(r) for r in connection.execute(
                "SELECT at,from_state,to_state,detail FROM events WHERE request_id=? ORDER BY id", (request_id,))]

    def transition(self, request_id: int, new_state: str, *, expect: str | None = None, detail: str = "",
                   remote_id: str | None = None, error: str | None = None, next_poll_at: float | None = None,
                   count_poll: bool = False) -> None:
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT state FROM requests WHERE id=?", (request_id,)).fetchone()
            if row is None:
                raise KeyError(request_id)
            state = row["state"]
            if (expect is not None and state != expect) or new_state not in TRANSITIONS[state]:
                connection.execute("ROLLBACK")
                raise RequestStateError(f"{state} -> {new_state}")
            connection.execute(
                "UPDATE requests SET state=?, updated_at=?, remote_id=COALESCE(?,remote_id), last_error=?,"
                " next_poll_at=?, polls=polls+? WHERE id=?",
                (new_state, self.clock(), remote_id, error, next_poll_at, 1 if count_poll else 0, request_id))
            if new_state in TERMINAL:
                connection.execute("UPDATE request_eans SET live=0 WHERE request_id=?", (request_id,))
            self._event(connection, request_id, state, new_state, detail or error or "")
            connection.execute("COMMIT")

    def recover_interrupted(self) -> int:
        """A crash between 'submitting' and the portal answer is ambiguous: never resend."""
        stuck = [r["id"] for r in self.list_requests(states={SUBMITTING})]
        for rid in stuck:
            self.transition(rid, NEEDS_REVIEW, expect=SUBMITTING, error="envio_interrumpido_ambiguo")
        return len(stuck)

    def reconcile(self, request_id: int, *, remote_id: str | None, sent: bool) -> None:
        """Operator verified the portal's request list: link the remote id or release for resend."""
        if sent:
            if not remote_id:
                raise ValueError("remote_id_requerido")
            self.transition(request_id, PENDING, expect=NEEDS_REVIEW, remote_id=remote_id, detail="reconciliada")
        else:
            self.transition(request_id, READY, expect=NEEDS_REVIEW, detail="confirmado_no_enviado")

    def cancel(self, request_id: int) -> None:
        self.transition(request_id, CANCELLED)

    def retry(self, settings: ProviderSettings, request_id: int) -> int | None:
        request = self.get(request_id)
        if request["state"] not in {FAILED, PARTIAL} or request["attempts"] >= settings.max_attempts:
            return None
        pending = [e["ean"] for e in request["eans"] if e["result"] != "imported"]
        if not pending:
            return None
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            live = {r[0] for r in connection.execute(
                "SELECT ean FROM request_eans WHERE provider=? AND live=1", (request["provider"],))}
            pending = [e for e in pending if e not in live]
            if not pending:
                connection.execute("ROLLBACK")
                return None
            now = self.clock()
            rid = connection.execute(
                "INSERT INTO requests(provider,mode,state,attempts,retry_of,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (request["provider"], request["mode"], READY, request["attempts"] + 1, request_id, now, now)).lastrowid
            connection.executemany("INSERT INTO request_eans(request_id,provider,ean) VALUES(?,?,?)",
                                   [(rid, request["provider"], e) for e in pending])
            self._event(connection, rid, None, READY, f"reintento de {request_id}")
            connection.execute("COMMIT")
        return rid

    def requests_today(self, provider: str) -> int:
        now = self.clock()
        local = time.localtime(now)
        midnight = now - (local.tm_hour * 3600 + local.tm_min * 60 + local.tm_sec)
        with closing(self.connect()) as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM events e JOIN requests r ON r.id=e.request_id "
                "WHERE r.provider=? AND e.to_state=? AND e.at>=?", (provider, SUBMITTING, midnight)).fetchone()[0]

    def review_items(self, provider: str | None = None, status: str = "open") -> list[dict]:
        query, args = "SELECT * FROM review_items WHERE status=?", [status]
        if provider:
            query, args = query + " AND provider=?", args + [provider]
        with closing(self.connect()) as connection:
            return [dict(r) for r in connection.execute(query + " ORDER BY id", args)]


def stage_original(store: ProviderStore, provider: str, request_id: int, sha: str, content: bytes) -> str:
    folder = store.staging / provider / str(request_id)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / sha
    if not target.exists():
        temporary = target.with_suffix(".tmp")
        temporary.write_bytes(content)
        os.replace(temporary, target)
    return str(target)


class ProviderService:
    def __init__(self, store: ProviderStore, repository: ImageRepository, settings: ProviderSettings,
                 *, rng: random.Random | None = None):
        self.store, self.repository, self.settings = store, repository, settings
        self.rng = rng or random.Random()

    def _metadata(self, request: dict, file_name: str, sha: str, evidence: str, entry: dict | None) -> dict:
        return {"provider": self.settings.provider, "request_id": request["id"], "remote_id": request["remote_id"],
                "source_name": file_name, "source_sha256": sha, "evidence": evidence,
                "manifest_entry": entry or {}}

    def import_package(self, request_id: int, package: PortalPackage) -> dict:
        """Import with demonstrable file↔EAN evidence; anything else goes to review, never guessed."""
        request = self.store.get(request_id)
        if request["state"] in (AVAILABLE, NEEDS_REVIEW, PENDING, SUBMITTED, READY):
            if request["state"] == READY:
                self.store.transition(request_id, SUBMITTED, detail="enviada_manualmente")
            if request["state"] != AVAILABLE:
                self.store.transition(request_id, AVAILABLE, detail="paquete_manual")
            self.store.transition(request_id, DOWNLOADED, expect=AVAILABLE)
        elif request["state"] != DOWNLOADED:
            raise RequestStateError(request["state"])
        requested = {ean_key(e["ean"]): e["ean"] for e in request["eans"]}
        manifest = parse_manifest(package.manifest, package.manifest_name) if package.manifest else {}
        summary = {"imported": 0, "reused": 0, "review": 0, "errors": []}
        saved: dict[str, int] = {}
        now = self.store.clock()
        for file in package.files:
            sha = hashlib.sha256(file.content).hexdigest()
            name = sanitize_filename(Path(file.name.replace("\\", "/")).name)
            ean, evidence, view, problem = match_file(file, requested, manifest, self.settings.allow_filename_ean)
            if problem:
                self._queue_review(request, name, sha, problem, None, None, file.content, now)
                summary["review"] += 1
                continue
            try:
                record, created = self.repository.save(
                    ean, name, file.content, provider=self.settings.provider,
                    origin=f"{self.settings.provider}:{request_id}", view=view,
                    metadata=self._metadata(request, file.name, sha, evidence, manifest.get(file.name)))
            except (ValueError, OSError, SyntaxError) as error:
                summary["errors"].append(f"{name}: {error}")
                self._queue_review(request, name, sha, f"imagen_rechazada:{error}", None, ean, file.content, now)
                continue
            saved[ean] = saved.get(ean, 0) + 1
            summary["imported" if created else "reused"] += 1
            if view == "unknown":
                self._queue_review(request, name, sha, "vista_sin_evidencia", record.id, ean, None, now)
                summary["review"] += 1
        with closing(self.store.connect()) as connection:
            for ean in saved:
                connection.execute("UPDATE request_eans SET result='imported' WHERE request_id=? AND ean=?",
                                   (request_id, ean))
        summary["state"] = self.finalize(request_id)
        return summary

    def _queue_review(self, request, name, sha, reason, record_id, ean, content, now):
        staged = stage_original(self.store, self.settings.provider, request["id"], sha, content) if content else None
        with closing(self.store.connect()) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO review_items(request_id,provider,name,sha256,reason,record_id,ean,staged,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (request["id"], self.settings.provider, name, sha, reason, record_id, ean, staged, now))

    def finalize(self, request_id: int) -> str:
        request = self.store.get(request_id)
        with closing(self.store.connect()) as connection:
            open_items = connection.execute(
                "SELECT COUNT(*) FROM review_items WHERE request_id=? AND status='open'", (request_id,)).fetchone()[0]
        done = sum(1 for e in request["eans"] if e["result"] == "imported")
        if open_items:
            target = NEEDS_REVIEW
        elif done == len(request["eans"]):
            target = IMPORTED
        elif done:
            target = PARTIAL
        else:
            target = FAILED
        if request["state"] != target:
            self.store.transition(request_id, target, detail=f"{done}/{len(request['eans'])} EAN con imagen",
                                  error="sin_imagenes" if target == FAILED else None)
        return target

    def resolve_review(self, item_id: int, *, reviewer: str, reason: str, ean: str | None = None,
                       view: str | None = None, reject: bool = False) -> None:
        if not reviewer.strip() or not reason.strip():
            raise ValueError("La revision requiere revisor y motivo")
        with closing(self.store.connect()) as connection:
            item = connection.execute("SELECT * FROM review_items WHERE id=? AND status='open'", (item_id,)).fetchone()
        if item is None:
            raise KeyError(item_id)
        request = self.store.get(item["request_id"])
        if reject:
            resolution = "rechazada"
        elif item["record_id"] is not None:
            if view not in IMAGE_VIEWS:
                raise ValueError("vista_invalida")
            self.repository.review_view(item["record_id"], view, reviewer=reviewer, reason=reason,
                                        evidence={"review_item": item_id, "request": item["request_id"]})
            resolution = f"vista={view}"
        else:
            requested = {e["ean"] for e in request["eans"]}
            if ean not in requested or view not in VALID_VIEWS:
                raise ValueError("ean_o_vista_invalidos")
            content = Path(item["staged"]).read_bytes()
            if hashlib.sha256(content).hexdigest() != item["sha256"]:
                raise ValueError("original_en_cuarentena_alterado")
            self.repository.save(ean, item["name"], content, provider=self.settings.provider,
                                 origin=f"{self.settings.provider}:{item['request_id']}", view=view,
                                 metadata={**self._metadata(request, item["name"], item["sha256"],
                                                             "manual_review", None), "reviewer": reviewer})
            with closing(self.store.connect()) as connection:
                connection.execute("UPDATE request_eans SET result='imported' WHERE request_id=? AND ean=?",
                                   (item["request_id"], ean))
            resolution = f"ean={ean};vista={view}"
        with closing(self.store.connect()) as connection:
            connection.execute("UPDATE review_items SET status=?, resolved_by=?, resolution=? WHERE id=?",
                               ("rejected" if reject else "resolved", reviewer, f"{resolution}: {reason}", item_id))
        if request["state"] == NEEDS_REVIEW:
            self.finalize(item["request_id"])

    def _next_poll(self, polls: int) -> float:
        delay = min(self.settings.poll_base_seconds * 2 ** polls, self.settings.poll_cap_seconds)
        return self.store.clock() + delay * (1 + self.rng.uniform(0, 0.2))

    def submit_ready(self, portal: ProviderPortal, *, dry_run: bool = True) -> list[dict]:
        out = []
        budget = self.settings.max_requests_per_day - self.store.requests_today(self.settings.provider)
        for request in reversed(self.store.list_requests(self.settings.provider, {READY})):
            if request["mode"] != "portal":
                continue
            eans = [e["ean"] for e in self.store.get(request["id"])["eans"]]
            cap = self.settings.pilot_cap
            if cap and len(eans) > cap:
                out.append({"id": request["id"], "action": "excede_tope_piloto"})
                continue
            if dry_run or budget <= 0:
                out.append({"id": request["id"], "action": "dry_run" if dry_run else "limite_diario"})
                continue
            budget -= 1
            self.store.transition(request["id"], SUBMITTING, expect=READY)
            try:
                remote_id = portal.submit(eans)
            except NOT_SENT_ERRORS as error:
                self.store.transition(request["id"], READY, expect=SUBMITTING, error=type(error).__name__)
                out.append({"id": request["id"], "action": "no_enviada", "error": type(error).__name__})
                break
            except Exception as error:
                self.store.transition(request["id"], NEEDS_REVIEW, expect=SUBMITTING,
                                      error=f"envio_ambiguo:{type(error).__name__}")
                out.append({"id": request["id"], "action": "ambigua"})
                continue
            self.store.transition(request["id"], SUBMITTED, expect=SUBMITTING, remote_id=str(remote_id))
            self.store.transition(request["id"], PENDING, expect=SUBMITTED,
                                  next_poll_at=self._next_poll(0))
            out.append({"id": request["id"], "action": "enviada", "remote_id": str(remote_id)})
        return out

    def poll_pending(self, portal: ProviderPortal) -> list[dict]:
        out, now = [], self.store.clock()
        for request in self.store.list_requests(self.settings.provider, {PENDING}):
            if request["next_poll_at"] and request["next_poll_at"] > now:
                continue
            try:
                status = portal.status(request["remote_id"])
            except (LoginFailed, InterventionRequired, PortalUnavailable, AccessNotConfirmed) as error:
                out.append({"id": request["id"], "action": "sondeo_bloqueado", "error": type(error).__name__})
                break
            except Exception as error:
                self.store.transition(request["id"], PENDING, error=type(error).__name__, count_poll=True,
                                      next_poll_at=self._next_poll(request["polls"] + 1))
                out.append({"id": request["id"], "action": "error_transitorio"})
                continue
            if status.state == "available":
                self.store.transition(request["id"], AVAILABLE, detail=status.detail, count_poll=True)
            elif status.state == "failed":
                self.store.transition(request["id"], FAILED, error=status.detail or "rechazada_por_portal",
                                      count_poll=True)
            elif status.state == "pending":
                self.store.transition(request["id"], PENDING, detail=status.detail, count_poll=True,
                                      next_poll_at=self._next_poll(request["polls"] + 1))
            else:
                self.store.transition(request["id"], NEEDS_REVIEW, error=f"estado_desconocido:{status.detail}")
            out.append({"id": request["id"], "action": status.state})
        return out

    def download_available(self, portal: ProviderPortal) -> list[dict]:
        out = []
        for request in self.store.list_requests(self.settings.provider, {AVAILABLE}):
            try:
                package = portal.download(request["remote_id"])
            except (LoginFailed, InterventionRequired, PortalUnavailable, AccessNotConfirmed) as error:
                out.append({"id": request["id"], "action": "descarga_bloqueada", "error": type(error).__name__})
                break
            except Exception as error:
                self.store.transition(request["id"], NEEDS_REVIEW, error=f"descarga_fallida:{type(error).__name__}")
                out.append({"id": request["id"], "action": "descarga_fallida"})
                continue
            out.append({"id": request["id"], "action": "importada", **self.import_package(request["id"], package)})
        return out

    def run_cycle(self, portal: ProviderPortal, *, dry_run: bool = True) -> dict:
        """One scheduler tick. Does nothing live unless <PROVIDER>_AUTOMATION_ENABLED=true."""
        if not dry_run and not self.settings.automation_enabled:
            return {"skipped": "automatizacion_desactivada"}
        self.store.recover_interrupted()
        return {"submitted": self.submit_ready(portal, dry_run=dry_run),
                "polled": [] if dry_run else self.poll_pending(portal),
                "downloaded": [] if dry_run else self.download_available(portal)}


def provider_demand(engine, repository: ImageRepository, settings: ProviderSettings,
                    start: date, end: date) -> dict:
    """EANs on non-cancelled purchase orders of the configured Odoo suppliers lacking full views.

    Membership comes only from purchase orders of verified partner ids (read-only query,
    commercial contacts grouped); brand or name is never used.
    """
    if not settings.supplier_ids:
        raise AccessNotConfirmed("proveedor_odoo_no_configurado")
    eans: list[str] = []
    for supplier in settings.supplier_ids:
        for order in read_orders(engine, supplier, start, end, include_commercial_contacts=True):
            eans.extend(line["ean"] for line in order["lines"])
    valid, invalid = normalize_eans(e for e in eans if e)
    missing = [e for e in valid if repository.pending_views(e, settings.required_views)]
    return {"eans": missing, "covered": len(valid) - len(missing), "invalid": invalid,
            "without_barcode": sum(1 for e in eans if not e)}


def load_settings(provider: str, root: Path, defaults: ProviderSettings | None = None) -> ProviderSettings:
    """Non-secret per-provider settings from <root>/provider_config.json (partner ids, caps, limits)."""
    base = defaults or ProviderSettings(provider)
    path = Path(root) / "provider_config.json"
    raw = json.loads(path.read_text(encoding="utf-8")).get(provider, {}) if path.exists() else {}
    allowed = set(ProviderSettings.__dataclass_fields__) - {"provider"}
    values = {k: v for k, v in raw.items() if k in allowed}
    for key in ("supplier_ids", "required_views"):
        if key in values:
            values[key] = tuple(values[key])
    return ProviderSettings(**{**{f: getattr(base, f) for f in allowed}, **values, "provider": provider})


class ProviderConnector:
    """Common connector surface used by the UI and CLI; portal specifics live in subclasses."""
    provider = ""
    default_mode = "portal"
    defaults: ProviderSettings

    def __init__(self, service: ProviderService, engine=None):
        self.service, self.engine = service, engine

    def plan_eans(self, eans, *, mode: str | None = None, limit: int | None = None) -> dict:
        """Plan supplied EANs without consulting Odoo; imported images still need evidence."""
        if isinstance(eans, str):
            eans = re.split(r"[,;\\s]+", eans.strip())
        valid, invalid = normalize_eans(eans)
        selected = valid[:limit] if limit is not None else valid
        result = self.service.store.plan(self.service.settings, selected,
                                         mode=mode or self.default_mode)
        return {**result, "invalid": invalid, "source": "manual_eans",
                "selected": len(selected)}

    def detect(self, start: date, end: date, *, mode: str | None = None, limit: int | None = None) -> dict:
        demand = provider_demand(self.engine, self.service.repository, self.service.settings, start, end)
        eans = demand["eans"][:limit] if limit else demand["eans"]
        return {**demand, **self.service.store.plan(self.service.settings, eans, mode=mode or self.default_mode)}

    def export_ean_list(self, request_id: int, separator: str = ",") -> str:
        return separator.join(e["ean"] for e in self.service.store.get(request_id)["eans"])

    def import_zip(self, request_id: int, content: bytes) -> dict:
        return self.service.import_package(request_id, read_zip_package(content))
