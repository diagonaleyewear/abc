from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import json
import hashlib
import logging
import os
import tempfile
import math
import sqlite3
import uuid
import time as clock
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Thread, BoundedSemaphore
from contextlib import contextmanager

from cryptography.fernet import Fernet
from filelock import FileLock, Timeout
from PIL import Image
from sqlalchemy import bindparam, text
from image_repository import ImageRepository, image_fingerprint
from image_naming import kering_filename, classified_view


PORTAL_URL = "https://my.keringeyewear.com/keringeyewear/es/login"
VIEWS = ("frontal", "lateral", "perspectiva")
IMAGE_VIEWS = VIEWS + ("detalle",)
IN_PROGRESS = "En proceso"
BEGIN_WRITE = "BEGIN IMMEDIATE"
STATES = ("Pendiente", IN_PROGRESS, "Completo", "Parcial", "Error", "Interrumpido")
DEFAULT_SETTINGS = {
    "cutoff_date": "2026-09-01", "auto_enabled": False, "auto_interval_hours": 6.0,
    "test_mode": True, "test_order_ids": [], "full_lot_validated": False,
}


class PortalUnavailable(Exception):
    pass


class AccessNotConfirmed(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class InterventionRequired(Exception):
    pass


class PortalFailure(Exception):
    code = "fallo_portal"
    retryable = True


class LoginFailed(PortalFailure):
    code = "login_fallido"
    retryable = False


class SessionExpired(PortalFailure):
    code = "sesion_caducada"


class PortalTimeout(PortalFailure):
    code = "timeout_portal"


class ProductNotFound(PortalFailure):
    code = "producto_no_encontrado"
    retryable = False


def displayed_views(valid) -> tuple[str, ...]:
    return VIEWS


class UnverifiedPortal:
    def fetch(self, ean: str, pending: tuple[str, ...]) -> dict[str, bytes]:
        raise PortalUnavailable()


def read_orders(engine, supplier_id: int, start: date, end: date, order_ids=None,
                include_commercial_contacts: bool = False) -> list[dict]:
    lower, upper = utc_interval(start, end)
    parameters = {"supplier": int(supplier_id), "lower": lower, "upper": upper}
    ids_filter = ""
    if order_ids is not None:
        selected = tuple(dict.fromkeys(int(value) for value in order_ids))
        if not selected:
            return []
        parameters["order_ids"] = selected
        ids_filter = "AND po.id IN :order_ids"
    supplier_filter = "po.partner_id = :supplier"
    if include_commercial_contacts:
        supplier_filter = """po.partner_id IN (
            SELECT contact.id FROM res_partner contact
            JOIN res_partner selected ON selected.id = :supplier
            WHERE COALESCE(contact.commercial_partner_id, contact.id)
                = COALESCE(selected.commercial_partner_id, selected.id)
        )"""
    query = text(f"""
        SELECT po.id, po.name, po.partner_id, po.date_order, po.state,
               pol.id AS line_id, pol.product_id, pp.barcode, pol.name AS product_name, pol.product_qty
        FROM purchase_order po
        LEFT JOIN purchase_order_line pol ON pol.order_id = po.id
          AND COALESCE(pol.display_type, '') = '' AND pol.product_id IS NOT NULL
        LEFT JOIN product_product pp ON pp.id = pol.product_id
        WHERE {supplier_filter} AND po.state != 'cancel'
          AND po.date_order >= :lower AND po.date_order < :upper
          {ids_filter}
        ORDER BY po.id, pol.id
    """)
    if order_ids is not None:
        query = query.bindparams(bindparam("order_ids", expanding=True))
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            if engine.dialect.name == "postgresql":
                connection.execute(text("SET TRANSACTION READ ONLY"))
            rows = connection.execute(query, parameters).mappings().all()
        finally:
            transaction.rollback()
    orders = {}
    for row in rows:
        order = orders.setdefault(row["id"], {
            "id": row["id"], "name": row["name"], "supplier_id": row["partner_id"],
            "date_order": str(row["date_order"]), "state": row["state"], "lines": [],
        })
        if row["line_id"] is not None:
            barcode = row["barcode"]
            order["lines"].append(purchase_line(row, barcode))
    return list(orders.values())


def purchase_line(row, barcode):
    return {"id": row["line_id"], "product_id": row["product_id"],
            "ean": barcode.strip() if isinstance(barcode, str) else "",
            "product_name": row["product_name"] or "",
            "quantity": str(row["product_qty"]) if row["product_qty"] is not None else ""}


def configured_orders(engine, config, start: date, end: date, order_ids=None):
    cutoff = date.fromisoformat(config.get("cutoff_date", DEFAULT_SETTINGS["cutoff_date"]))
    start = max(start, cutoff)
    if end < start:
        return []
    return read_orders(engine, config["supplier_id"], start, end, order_ids,
                       include_commercial_contacts=config.get("include_commercial_contacts", False))


def read_suppliers(engine) -> list[dict]:
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(text(
            "SELECT id, name, COALESCE(commercial_partner_id, id) AS commercial_partner_id "
            "FROM res_partner WHERE active = TRUE AND supplier_rank > 0 ORDER BY name"
        )).mappings()]


def read_supplier_contacts(engine, supplier_id: int) -> list[dict]:
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(text("""
            SELECT contact.id, contact.name, contact.active,
                   COALESCE(contact.commercial_partner_id, contact.id) AS commercial_partner_id
            FROM res_partner contact JOIN res_partner selected ON selected.id = :supplier
            WHERE COALESCE(contact.commercial_partner_id, contact.id)
                = COALESCE(selected.commercial_partner_id, selected.id)
            ORDER BY contact.id
        """), {"supplier": int(supplier_id)}).mappings()]


def kering_image_name(view: str) -> str:
    if view not in IMAGE_VIEWS:
        raise ValueError("ean_o_vista_invalida")
    return f"{view}.img"


def kering_zip_name(view: str, content: bytes) -> str:
    with Image.open(BytesIO(content)) as image:
        extension = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}[image.format]
    return f"{view}.{extension}"


class ImageStore:
    def __init__(self, root: Path):
        self.root = root
        self.repository = ImageRepository()
        root.mkdir(parents=True, exist_ok=True)
        self.database = root / "history.sqlite3"
        with FileLock(root / "schema.lock", timeout=30), self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS images (
                    ean TEXT, view TEXT, digest TEXT, pixels TEXT, PRIMARY KEY(ean, view)
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, user TEXT, supplier INTEGER, start_date TEXT,
                    end_date TEXT, started REAL, ended REAL, heartbeat REAL, status TEXT
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    run_id TEXT, order_id INTEGER, number TEXT, supplier INTEGER,
                    order_date TEXT, attempt INTEGER, status TEXT, snapshot TEXT,
                    results TEXT DEFAULT '{}', error TEXT DEFAULT '',
                    PRIMARY KEY(run_id, order_id)
                );
                CREATE TABLE IF NOT EXISTS schedule_state (
                    id INTEGER PRIMARY KEY CHECK(id=1), last_started REAL, last_finished REAL,
                    next_run REAL, result TEXT, run_id TEXT
                );
            """)
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(runs)")}
            if "origin" not in columns:
                connection.execute("ALTER TABLE runs ADD COLUMN origin TEXT NOT NULL DEFAULT 'Manual'")

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.database, timeout=30)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                yield connection
        finally:
            connection.close()

    def path(self, ean: str, view: str) -> Path:
        if not ean.isascii() or not ean.isdigit() or len(ean) > 20 or view not in IMAGE_VIEWS:
            raise ValueError("ean_o_vista_invalida")
        valid = self.valid_views(ean)
        if view in valid:
            return valid[view]
        records = [record for record in self.repository.records(ean) if record.view == view]
        return records[0].path if records else self.repository.root / ean / kering_image_name(view)

    def valid_views(self, ean: str) -> dict[str, Path]:
        return self.repository.valid_views(ean)

    def available_image_count(self, ean: str) -> int:
        checksums = set()
        for record in self.repository.records(ean):
            if record.checksum in checksums:
                continue
            try:
                image_fingerprint(self.repository.verified_bytes(record))
                checksums.add(record.checksum)
            except (OSError, ValueError, Image.DecompressionBombError, SyntaxError):
                logging.getLogger(__name__).warning("kering_image_not_reusable record_id=%s", record.id)
        return len(checksums)

    def save_view(self, ean: str, view: str, content: bytes, identity: dict | None = None) -> bool:
        with self.repository.lock(ean):
            existing = self.valid_views(ean)
            if view in existing:
                return False
            fingerprint = image_fingerprint(content)
            if any(image_fingerprint(path.read_bytes()) == fingerprint for path in existing.values()):
                return False
            with Image.open(BytesIO(content)) as image:
                extension = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}[image.format]
            identity = identity or {}
            name = kering_filename(identity.get("model", ean), identity.get("color", "unknown"),
                                   view, extension, f"{view}{extension}")
            self.repository.save(ean, name, content, provider="Kering", origin="kering_portal",
                                 view=classified_view(name), metadata={"modelo": identity.get("model", ""),
                                                                      "identity": identity})
            return True

    def recover(self, now=None) -> None:
        timestamp = clock.time() if now is None else now
        with self.connect() as connection:
            connection.execute(BEGIN_WRITE)
            connection.execute("""UPDATE attempts SET status='Interrumpido', error='ejecucion_interrumpida'
                WHERE run_id IN (SELECT id FROM runs WHERE ended IS NULL AND heartbeat < ?)
                AND status IN ('Pendiente', 'En proceso')""", (timestamp - 300,))
            connection.execute("""UPDATE runs SET status='Interrumpido', ended=?
                WHERE ended IS NULL AND heartbeat < ?""", (timestamp, timestamp - 300))

    def recover_abandoned(self, now=None):
        timestamp = clock.time() if now is None else now
        with self.connect() as connection:
            connection.execute(BEGIN_WRITE)
            connection.execute("""UPDATE attempts SET status='Interrumpido', error='ejecucion_interrumpida'
                WHERE run_id IN (SELECT id FROM runs WHERE ended IS NULL)
                AND status IN ('Pendiente','En proceso')""")
            connection.execute("UPDATE runs SET status='Interrumpido', ended=? WHERE ended IS NULL", (timestamp,))

    def create_run(self, orders: list[dict], user: str, supplier: int, start: date, end: date,
                   origin: str = "Manual") -> str:
        run_id = uuid.uuid4().hex
        now = clock.time()
        with self.connect() as connection:
            connection.execute(BEGIN_WRITE)
            connection.execute("""INSERT INTO runs
                (id,user,supplier,start_date,end_date,started,ended,heartbeat,status,origin)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                               (run_id, user, supplier, str(start), str(end), now, None, now, "Pendiente", origin))
            self.insert_orders(connection, run_id, supplier, orders)
        return run_id

    def insert_orders(self, connection, run_id, supplier, orders):
        for order in orders:
            self.repository.associate_order("Kering", str(order["id"]),
                                            [line["ean"] for line in order["lines"] if line["ean"]])
            previous = connection.execute("SELECT COALESCE(MAX(attempt),0) FROM attempts WHERE order_id=?",
                                          (order["id"],)).fetchone()[0]
            connection.execute("""INSERT INTO attempts
                (run_id, order_id, number, supplier, order_date, attempt, status, snapshot, results)
                VALUES(?,?,?,?,?,?,?,?,?)""", (run_id, order["id"], order["name"], supplier,
                order["date_order"], previous + 1, "Pendiente", json.dumps(order), json.dumps({
                    line["ean"]: {"views": dict.fromkeys(VIEWS, "Pendiente"), "reason": "", "tries": 0}
                    for line in order["lines"] if line["ean"]
                })))

    def add_orders(self, run_id, orders):
        with self.connect() as connection:
            connection.execute(BEGIN_WRITE)
            supplier = connection.execute("SELECT supplier FROM runs WHERE id=?", (run_id,)).fetchone()[0]
            self.insert_orders(connection, run_id, supplier, orders)

    def executions(self):
        with self.connect() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM runs ORDER BY started DESC")]

    def history(self) -> list[dict]:
        self.recover()
        with self.connect() as connection:
            return [dict(row) for row in connection.execute("""
                SELECT attempts.*, runs.user, runs.started, runs.ended, runs.start_date, runs.end_date, runs.origin
                FROM attempts JOIN runs ON runs.id=attempts.run_id ORDER BY started DESC, order_id
            """)]

    def run_history(self, run_id: str) -> list[dict]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute("""
                SELECT attempts.*, runs.user, runs.started, runs.ended, runs.start_date, runs.end_date, runs.origin
                FROM attempts JOIN runs ON runs.id=attempts.run_id
                WHERE attempts.run_id=? ORDER BY order_id
            """, (run_id,))]

    def zip_order(self, order: dict) -> bytes:
        from image_exports import prepare_order_zip
        with prepare_order_zip(self.repository, order) as export:
            return export.stream.read()


def line_signature(order):
    return sorted((line["id"], line["product_id"], line["ean"], line.get("product_name", ""),
                   str(line.get("quantity", ""))) for line in order["lines"])


def purchase_status(store, order, latest=None, views_cache=None):
    cache = {} if views_cache is None else views_cache
    complete = count_complete_lines(store, order, cache)
    pending = len(order["lines"]) - complete
    changed = latest is not None and line_signature(order) != line_signature(json.loads(latest["snapshot"]))
    if latest is None:
        status = "No procesado"
    elif latest["status"] in {"Pendiente", IN_PROGRESS} and latest["ended"] is None:
        status = IN_PROGRESS
    elif not pending and not changed and latest["status"] == "Completo":
        status = "Procesado"
    elif latest["status"] in {"Error", "Interrumpido"} and not changed:
        status = latest["status"]
    else:
        status = "Parcial"
    return {"status": status, "complete": complete, "pending": pending, "changed": changed,
            "last_processed": latest["ended"] or latest["started"] if latest else None,
            "needs_processing": status not in {"Procesado", IN_PROGRESS}}


def count_complete_lines(store, order, cache):
    complete = 0
    for line in order["lines"]:
        ean = line["ean"]
        if ean and ean not in cache:
            cache[ean] = store.valid_views(ean)
        complete += int(bool(ean) and set(VIEWS) <= cache.get(ean, {}).keys())
    return complete


def latest_attempts(store):
    latest = {}
    for attempt in store.history():
        latest.setdefault(attempt["order_id"], attempt)
    return latest


def trial_validated(store, config):
    ids = config.get("test_order_ids", [])
    if not ids or len(ids) > 2:
        return False
    history = store.history()
    downloaded = False
    for order_id in ids:
        attempts = [row for row in history if row["order_id"] == order_id and row["status"] == "Completo"]
        if len(attempts) < 2:
            return False
        snapshot = json.loads(attempts[0]["snapshot"])
        if not snapshot["lines"] or purchase_status(store, snapshot, attempts[0])["status"] != "Procesado":
            return False
        outcomes = [json.loads(row["results"]) for row in attempts]
        reused = any(result and all(value == "Reutilizada" for entry in result.values()
                                   for value in entry["views"].values()) for result in outcomes)
        downloaded = downloaded or any(value == "Descargada" for result in outcomes
                                       for entry in result.values() for value in entry["views"].values())
        if not reused:
            return False
    return downloaded


def batch_lock(store):
    return FileLock(store.root / "batch.lock", timeout=0, thread_local=False)


def batch_busy(store):
    try:
        with batch_lock(store):
            return False
    except Timeout:
        return True


def process_ean(store: ImageStore, portal, ean: str) -> dict:
    if not ean.isascii() or not ean.isdigit() or len(ean) > 20:
        return {"views": dict.fromkeys(VIEWS, "Pendiente"), "reason": "ean_invalido", "tries": 0}
    try:
        with store.repository.lock(ean).acquire(timeout=0):
            valid = store.valid_views(ean)
            results = {view: "Reutilizada" if view in valid else "Pendiente" for view in displayed_views(valid)}
            pending = tuple(view for view in displayed_views(valid) if view not in valid)
            if set(VIEWS) <= valid.keys():
                available = store.available_image_count(ean)
                return {"views": results, "reason": "", "tries": 0,
                        "acquisition": {"available_files": available, "downloaded_files": 0, "reused_files": available}}
            if hasattr(portal, "repository"):
                portal.repository = store.repository
            images, reason, tries = fetch_pending(portal, ean, pending)
            reason, downloaded = save_pending(store, ean, pending, images, results, reason, portal)
            available = store.available_image_count(ean)
            if available and not valid and set(results.values()) == {"Pendiente"} and reason == "vistas_no_disponibles":
                reason = "vistas_sin_identificar"
            return {
                "views": results,
                "reason": reason if "Pendiente" in results.values() else "",
                "tries": tries,
                "identity": getattr(portal, "identities", {}).get(ean, {}),
                "acquisition": {"available_files": available, "downloaded_files": downloaded,
                                "reused_files": max(0, available - downloaded)},
                **({"access_failure": dict(portal.access_failure)}
                   if getattr(portal, "access_failure", None) else {}),
            }
    except Timeout:
        return {"views": dict.fromkeys(VIEWS, "Pendiente"), "reason": "ean_en_proceso", "tries": 0}


def fetch_pending(portal, ean, pending):
    reason = "fallo_portal"
    for tries in range(1, 4):
        try:
            return portal.fetch(ean, pending), "vistas_no_disponibles", tries
        except InterventionRequired:
            return {}, "intervencion_captcha_o_mfa", tries
        except PortalUnavailable:
            return {}, "portal_no_verificado", tries
        except PortalFailure as error:
            reason = error.code
            if not error.retryable or getattr(portal, "access_failure", None):
                return {}, reason, tries
        except Exception:
            pass
    return {}, reason, 3


def save_pending(store, ean, pending, images, results, reason, portal=None):
    identity = getattr(portal, "identities", {}).get(ean, {})
    media = getattr(portal, "media", {})
    downloaded = set()
    downloaded_checksums = set()
    for view, content in images.items():
        try:
            if view in media:
                info = media[view]
                record, _ = store.repository.save(
                    ean, info["name"], content, provider="Kering", origin=info["url"],
                    view=info["view"], metadata={"modelo": identity.get("model", ""),
                                                "identity": identity, "view_detection": info["detection"]})
                downloaded.add(record.view)
                downloaded_checksums.add(record.checksum)
            elif store.save_view(ean, view, content, identity):
                if view not in pending:
                    continue
                results[view] = "Descargada"
                downloaded.add(view)
                downloaded_checksums.add(hashlib.sha256(content).hexdigest())
            else:
                reason = "vista_duplicada"
        except (OSError, ValueError, Image.DecompressionBombError, SyntaxError):
            reason = "imagen_invalida"
    valid = store.valid_views(ean)
    for view in pending:
        results[view] = ("Descargada" if view in downloaded else "Reutilizada") if view in valid else "Pendiente"
    return reason, len(downloaded_checksums)


def order_status(order: dict, results: dict) -> str:
    if not order["lines"]:
        return "Completo"
    complete = True
    available = False
    for line in order["lines"]:
        result = results.get(line["ean"], {})
        views = result.get("views", {})
        complete = complete and bool(line["ean"]) and all(views.get(view, "Pendiente") != "Pendiente" for view in VIEWS)
        available = available or any(value != "Pendiente" for value in views.values()) or bool(
            result.get("acquisition", {}).get("available_files", 0))
    if complete:
        return "Completo"
    return "Parcial" if available or any(not line["ean"] for line in order["lines"]) else "Error"


def execute_order(store, attempt, loader, portal, cache):
    run_id = attempt["run_id"]
    order = loader(attempt["order_id"])
    with store.connect() as connection:
        connection.execute("UPDATE attempts SET snapshot=?, order_date=? WHERE run_id=? AND order_id=?",
                           (json.dumps(order), order["date_order"], run_id, order["id"]))
    results = {}
    for line in order["lines"]:
        ean = line["ean"]
        if not ean or ean in results:
            continue
        valid = store.valid_views(ean)
        cached = cache.get(ean)
        cache_valid = cached and all(view in valid for view, outcome in cached["views"].items() if outcome != "Pendiente")
        if cache_valid:
            available = store.available_image_count(ean)
            results[ean] = {"views": {view: "Reutilizada" if view in valid else "Pendiente" for view in displayed_views(valid)},
                            "reason": cache[ean]["reason"], "tries": 0,
                            "identity": cache[ean].get("identity", {}),
                            "acquisition": {"available_files": available,
                                            "downloaded_files": 0, "reused_files": available}}
        else:
            results[ean] = process_ean(store, portal, ean)
            cache[ean] = results[ean]
        with store.connect() as connection:
            connection.execute("UPDATE attempts SET results=? WHERE run_id=? AND order_id=?",
                               (json.dumps(results), run_id, order["id"]))
        if results[ean].get("access_failure"):
            raise AccessNotConfirmed(results[ean]["reason"])
    status = order_status(order, results)
    error = "lineas_sin_ean" if any(not line["ean"] for line in order["lines"]) else ""
    with store.connect() as connection:
        connection.execute("""UPDATE attempts SET status=?, snapshot=?, results=?, error=?, order_date=?
            WHERE run_id=? AND order_id=?""", (status, json.dumps(order), json.dumps(results), error,
            order["date_order"], run_id, order["id"]))


def heartbeat(store, run_id, stopped):
    while not stopped.wait(30):
        with store.connect() as connection:
            connection.execute("UPDATE runs SET heartbeat=? WHERE id=? AND ended IS NULL", (clock.time(), run_id))


def execute_run(store: ImageStore, run_id: str, loader, portal) -> None:
    stopped = Event()
    keeper = Thread(target=heartbeat, args=(store, run_id, stopped), daemon=True)
    keeper.start()
    try:
        execute_batch(store, run_id, loader, portal)
    finally:
        stopped.set()
        keeper.join()


def execute_batch(store, run_id, loader, portal):
    cache = {}
    with store.connect() as connection:
        attempts = connection.execute("SELECT * FROM attempts WHERE run_id=?", (run_id,)).fetchall()
        connection.execute("UPDATE runs SET status='En proceso', heartbeat=? WHERE id=?", (clock.time(), run_id))
    for attempt in attempts:
        with store.connect() as connection:
            connection.execute("UPDATE runs SET heartbeat=? WHERE id=?", (clock.time(), run_id))
            connection.execute("UPDATE attempts SET status='En proceso' WHERE run_id=? AND order_id=?",
                               (run_id, attempt["order_id"]))
        try:
            execute_order(store, attempt, loader, portal, cache)
        except AccessNotConfirmed as error:
            with store.connect() as connection:
                connection.execute("UPDATE attempts SET status='Error', error=? WHERE run_id=? AND order_id=?",
                                   (error.code, run_id, attempt["order_id"]))
                connection.execute("UPDATE runs SET ended=?, heartbeat=?, status='Interrumpido' WHERE id=?",
                                   (clock.time(), clock.time(), run_id))
            return
        except Exception:
            with store.connect() as connection:
                connection.execute("UPDATE attempts SET status='Error', error='fallo_lectura_o_proceso' WHERE run_id=? AND order_id=?",
                                   (run_id, attempt["order_id"]))
    with store.connect() as connection:
        connection.execute("UPDATE runs SET ended=?, heartbeat=?, status='Finalizado' WHERE id=?",
                           (clock.time(), clock.time(), run_id))


class BatchService:
    def __init__(self, store: ImageStore):
        self.store = store
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="kering")
        self.slots = BoundedSemaphore(2)

    def work(self, run_id, loader, portal, lock):
        try:
            execute_run(self.store, run_id, loader, portal)
        finally:
            try:
                close = getattr(portal, "close", None)
                if close:
                    close()
            finally:
                lock.release()
                self.slots.release()

    def submit(self, orders, user, supplier, start, end, loader, portal=None, origin="Manual") -> str:
        if not self.slots.acquire(blocking=False):
            raise ValueError("Hay lotes activos; espere a que terminen")
        lock = batch_lock(self.store)
        try:
            lock.acquire()
            self.store.recover_abandoned()
            run_id = self.store.create_run(orders, user, supplier, start, end, origin)
            self.executor.submit(self.work, run_id, loader, portal or UnverifiedPortal(), lock)
        except Exception:
            lock.release()
            self.slots.release()
            raise
        return run_id


def data_root() -> Path:
    return Path(os.getenv("KERING_DATA_ROOT", str(Path(__file__).parent / "masterdata_data" / "kering")))


def utc_interval(start: date, end: date) -> tuple[datetime, datetime]:
    if end < start:
        raise ValueError("Intervalo de fechas invalido")
    madrid = ZoneInfo("Europe/Madrid")
    return tuple(
        datetime.combine(day, time.min, madrid).astimezone(timezone.utc).replace(tzinfo=None)
        for day in (start, end + timedelta(days=1))
    )


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class ConfigStore:
    def __init__(self, root: Path):
        self.root = root

    def cipher(self) -> Fernet:
        key_file = os.getenv("KERING_ENCRYPTION_KEY_FILE", "")
        key = Path(key_file).read_text(encoding="ascii").strip() if key_file else os.getenv("KERING_ENCRYPTION_KEY", "")
        if not key:
            raise ValueError("Configure KERING_ENCRYPTION_KEY en el servidor")
        return Fernet(key.encode("ascii"))

    def load(self) -> dict:
        path = self.root / "config.enc"
        if not path.exists():
            return {**DEFAULT_SETTINGS, "url": PORTAL_URL, "username": "", "password": "", "supplier_id": 0}
        return {**DEFAULT_SETTINGS, **json.loads(self.cipher().decrypt(path.read_bytes()))}

    def save(self, config: dict) -> None:
        from urllib.parse import urlsplit

        parsed = urlsplit(config["url"])
        if parsed.scheme != "https" or parsed.hostname != "my.keringeyewear.com" or parsed.username or parsed.password:
            raise ValueError("URL Kering no permitida")
        if int(config["supplier_id"]) <= 0:
            raise ValueError("Seleccione un proveedor Odoo")
        config = {**DEFAULT_SETTINGS, **config}
        date.fromisoformat(config["cutoff_date"])
        interval = float(config["auto_interval_hours"])
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("El intervalo debe ser mayor que cero")
        ids = list(dict.fromkeys(int(value) for value in config["test_order_ids"]))
        if len(ids) > 2 or any(value <= 0 for value in ids):
            raise ValueError("Autorice uno o dos pedidos de prueba")
        if not config["test_mode"] and not config["full_lot_validated"]:
            raise ValueError("El lote completo requiere validar previamente descarga e historial")
        config["test_order_ids"] = ids
        atomic_write(self.root / "config.enc", self.cipher().encrypt(json.dumps(config).encode()))