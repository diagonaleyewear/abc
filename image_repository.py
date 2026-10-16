from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from typing import Callable
from zipfile import ZipFile

from filelock import FileLock
from PIL import Image, ImageOps

from db_config import load_env_file
from service_stop import acquire, checkpoint, request_timeout
from repository_storage import (
    state_root, backup_root, check_image_root, require_distinct_replica, check_write_path, validate_state_root,
    excluded_staging_path,
    staging_root,
)


LOGGER = logging.getLogger(__name__)
IMAGE_REPOSITORY_ROOT_ENV = "IMAGE_REPOSITORY_ROOT"
IMAGE_VIEWS = ("frontal", "lateral", "perspectiva", "detalle")
VIEW_ALIASES = {"V1": "frontal", "V2": "perspectiva", "V3": "lateral"}


def repository_root() -> Path:
    load_env_file()
    root = Path(os.getenv(IMAGE_REPOSITORY_ROOT_ENV, "").strip() or "repo/images")
    if not root.is_absolute():
        root = Path(__file__).resolve().parent / root
    return root.resolve()


def validate_ean(ean: str) -> None:
    if not ean or not ean.isascii() or not ean.isdigit() or len(ean) > 20:
        raise ValueError("ean_invalido")


def validate_name(name: str) -> None:
    if not name or name in {".", ".."} or any(ch in name for ch in '\\/:*?"<>|\x00'):
        raise ValueError("nombre_imagen_invalido")


def image_fingerprint(content: bytes) -> str:
    if len(content) > 30 * 1024 * 1024:
        raise ValueError("imagen_invalida")
    with Image.open(BytesIO(content)) as image:
        image.verify()
    with Image.open(BytesIO(content)) as image:
        if image.format not in {"JPEG", "PNG", "WEBP"} or min(image.size) < 600:
            raise ValueError("imagen_invalida")
        pixels = ImageOps.exif_transpose(image).convert("RGB")
        return hashlib.sha256(pixels.resize((64, 64)).tobytes()).hexdigest()


def atomic_write(path: Path, content: bytes, *, guard: Callable[[], None] | None = None) -> None:
    if guard is not None:
        guard()
    check_write_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".image-", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        check_write_path(path)
        if guard is not None:
            guard()
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def collision_name(name: str, checksum: str, index: int = 0) -> str:
    path = Path(name)
    extra = f"-{index}" if index else ""
    return f"{path.stem}__{checksum}{extra}{path.suffix}"


def file_checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_verified(source: Path, destination: Path, expected: str, *, guard: Callable[[], None] | None = None) -> None:
    if guard is not None:
        guard()
    check_write_path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if file_checksum(destination) != expected:
            raise ValueError(f"Conflicto sin sobrescritura: {destination}")
        return
    descriptor, temporary = tempfile.mkstemp(dir=destination.parent)
    os.close(descriptor)
    try:
        shutil.copyfile(source, temporary)
        if file_checksum(Path(temporary)) != expected:
            raise ValueError(f"El origen cambio durante la copia: {source}")
        with open(temporary, "r+b") as stream:
            os.fsync(stream.fileno())
        check_write_path(destination)
        if guard is not None:
            guard()
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@dataclass(frozen=True)
class ImageRecord:
    id: int
    ean: str
    checksum: str
    pixels: str | None
    path: Path
    provider: str
    origin: str
    view: str
    market: str
    name: str
    date: str
    metadata: dict


class ImageRepository:
    def __init__(self, root: Path | None = None, *, state: Path | None = None, backups: Path | None = None):
        self.root = (root if root is not None else repository_root()).resolve()
        check_image_root(self.root)
        self.state_root = state.resolve() if state is not None else state_root(self.root)
        validate_state_root(self.root, self.state_root)
        self.backup_root = backups.resolve() if backups is not None else backup_root(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.database = self.state_root / ".catalog.sqlite3"
        if self.state_root != self.root and (self.root / ".catalog.sqlite3").exists() and not self.database.exists():
            raise ValueError("Catalogo legacy existente; separar mediante copia verificada antes de activar")
        if self.state_root != self.root:
            for name in (".mail_download_state.json", ".market_pending.json"):
                if (self.root / name).exists() and not (self.state_root / name).exists():
                    raise ValueError("Estado legacy existente; separar mediante copia verificada antes de activar")
        self._locks: dict[str, FileLock] = {}
        with acquire(self.maintenance_lock()), self.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS assets (
                    id INTEGER PRIMARY KEY, ean TEXT NOT NULL, checksum TEXT NOT NULL,
                    pixels TEXT, path TEXT NOT NULL UNIQUE, UNIQUE(ean, checksum)
                );
                CREATE TABLE IF NOT EXISTS representations (
                    id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL REFERENCES assets(id),
                    provider TEXT NOT NULL, origin TEXT NOT NULL, view TEXT NOT NULL,
                    market TEXT NOT NULL, name TEXT NOT NULL, date TEXT NOT NULL,
                    metadata TEXT NOT NULL,
                    UNIQUE(asset_id, provider, origin, view, market, name)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, date TEXT NOT NULL, ean TEXT NOT NULL,
                    action TEXT NOT NULL, details TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS order_eans (
                    source TEXT NOT NULL, order_id TEXT NOT NULL, ean TEXT NOT NULL,
                    PRIMARY KEY(source, order_id, ean)
                );
                CREATE TABLE IF NOT EXISTS migrations (
                    plan_id TEXT NOT NULL, source TEXT NOT NULL, checksum TEXT NOT NULL,
                    backup TEXT NOT NULL, destination TEXT, PRIMARY KEY(plan_id, source)
                );
            """)

    def maintenance_lock(self) -> FileLock:
        check_image_root(self.root)
        return FileLock(self.root / ".repository.lock", timeout=120)

    def lock(self, ean: str) -> FileLock:
        check_image_root(self.root)
        validate_ean(ean)
        directory = self.root / ".locks"
        directory.mkdir(exist_ok=True)
        # One instance per EAN permits save() inside the download critical section.
        lock = self._locks.setdefault(ean, FileLock(directory / f"{ean}.lock", timeout=120))
        return lock

    @contextmanager
    def connect(self):
        check_image_root(self.root)
        validate_state_root(self.root, self.state_root)
        connection = sqlite3.connect(self.database, timeout=request_timeout(120))
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            connection.create_function(
                "casefold", 1, lambda value: value.casefold() if isinstance(value, str) else "")
            with connection:
                yield connection
        finally:
            connection.close()

    def resolve(self, relative: str) -> Path:
        check_image_root(self.root)
        path = (self.root / relative).resolve()
        if self.root not in path.parents:
            raise ValueError("ruta_fuera_del_repositorio")
        if staging_root() is not None:
            check_write_path(path)
        return path

    def records(self, ean: str | None = None) -> list[ImageRecord]:
        if ean is not None:
            validate_ean(ean)
        with self.connect() as connection:
            rows = connection.execute("""
                SELECT r.*, a.ean, a.checksum, a.pixels, a.path FROM representations r
                JOIN assets a ON a.id=r.asset_id
            """ + (" WHERE a.ean=?" if ean is not None else "") + " ORDER BY r.id",
                (ean,) if ean is not None else ()).fetchall()
        return [ImageRecord(
            row["id"], row["ean"], row["checksum"], row["pixels"], self.resolve(row["path"]),
            row["provider"], row["origin"], row["view"], row["market"], row["name"],
            row["date"], json.loads(row["metadata"]),
        ) for row in rows if not excluded_staging_path(Path(row["path"])) and not
            excluded_staging_path((self.root / row["path"]).resolve().relative_to(self.root))]

    def verified_bytes(self, record: ImageRecord) -> bytes:
        check_image_root(self.root)
        current = self.resolve(record.path.relative_to(self.root).as_posix())
        if current != record.path:
            raise ValueError("ruta_original_modificada")
        content = record.path.read_bytes()
        if hashlib.sha256(content).hexdigest() != record.checksum:
            raise ValueError(f"checksum_incorrecto: {record.path}")
        return content

    def valid_views(self, ean: str) -> dict[str, Path]:
        valid: dict[str, Path] = {}
        seen: set[str] = set()
        for record in self.records(ean):
            if record.view not in IMAGE_VIEWS or record.view in valid or not record.pixels:
                continue
            try:
                pixels = image_fingerprint(self.verified_bytes(record))
            except (OSError, ValueError, Image.DecompressionBombError, SyntaxError) as error:
                LOGGER.warning("Imagen no reutilizable EAN %s: %s", ean, error)
                continue
            if pixels != record.pixels or pixels in seen:
                continue
            seen.add(pixels)
            valid[record.view] = record.path
        return valid

    def pending_views(self, ean: str, required: tuple[str, ...]) -> tuple[str, ...]:
        valid = self.valid_views(ean)
        return tuple(view for view in required if view not in valid)

    def catalog_rows(self, records: list[ImageRecord] | None = None) -> list[dict]:
        rows = []
        for record in self.catalog_records() if records is None else records:
            timestamp = datetime.fromisoformat(record.date).timestamp()
            rows.append({
                "Modelo": record.metadata.get("modelo", ""), "EAN": record.ean,
                "Mercado": record.market or "Original", "Archivo": record.name,
                "Ruta": str(record.path), "Descargada": timestamp,
                "Fecha": datetime.fromtimestamp(timestamp).strftime("%d/%m/%Y %H:%M"),
                "Origen": record.origin, "Proveedor": record.provider, "Vista": record.view,
                "Checksum": record.checksum,
                "RecordId": record.id,
            })
        return rows

    def catalog_records(self, ean: str | None = None, *, record_id: int | None = None,
                        record_ids: list[int] | None = None) -> list[ImageRecord]:
        """Local listing only; resolve/access/integrity belong to visible validation."""
        if ean is not None:
            validate_ean(ean)
        clauses, parameters = [], []
        if ean is not None:
            clauses.append("a.ean=?")
            parameters.append(ean)
        if record_id is not None:
            clauses.append("r.id=?")
            parameters.append(record_id)
        if record_ids is not None:
            if not record_ids:
                return []
            clauses.append("r.id IN (" + ",".join("?" for _ in record_ids) + ")")
            parameters.extend(record_ids)
        with self.connect() as connection:
            rows = connection.execute("""
                SELECT r.*, a.ean, a.checksum, a.pixels, a.path FROM representations r
                JOIN assets a ON a.id=r.asset_id
            """ + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY r.id",
                parameters).fetchall()
        records = []
        for row in rows:
            relative = Path(row["path"])
            if relative.is_absolute() or ".." in relative.parts or excluded_staging_path(relative):
                LOGGER.warning("Ruta de catalogo excluida: %s", relative)
                continue
            records.append(ImageRecord(
                row["id"], row["ean"], row["checksum"], row["pixels"], self.root / relative,
                row["provider"], row["origin"], row["view"], row["market"], row["name"],
                row["date"], json.loads(row["metadata"])))
        return records

    def search_eans(self, query: str, *, limit: int = 12, offset: int = 0) -> list[str]:
        """Search product metadata and locally associated Kering order numbers."""
        query = query.strip()
        if not query or len(query) > 120 or not 1 <= limit <= 50 or offset < 0:
            raise ValueError("Consulta o paginacion invalida")
        escaped = query.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        ean_query = query if query.isascii() and query.isdigit() else pattern
        with self.connect() as connection:
            rows = connection.execute("""
                WITH matches(ean) AS (
                    SELECT a.ean
                    FROM assets a
                    JOIN representations r ON r.asset_id=a.id
                    WHERE a.ean LIKE ? ESCAPE '\\'
                       OR casefold(r.metadata) LIKE ? ESCAPE '\\'
                    UNION
                    SELECT ean FROM order_eans
                    WHERE source='Kering' AND casefold(order_id) LIKE ? ESCAPE '\\'
                )
                SELECT DISTINCT ean FROM matches
                ORDER BY ean LIMIT ? OFFSET ?
            """, (ean_query, pattern, pattern, limit, offset)).fetchall()
        return [str(row["ean"]) for row in rows]

    def save(self, ean: str, name: str, content: bytes, *, provider: str, origin: str,
             view: str = "unknown", market: str = "", date: str | None = None,
             metadata: dict | None = None, allow_invalid: bool = False) -> tuple[ImageRecord, bool]:
        validate_ean(ean)
        validate_name(name)
        view = VIEW_ALIASES.get(view, view)
        checksum = hashlib.sha256(content).hexdigest()
        try:
            pixels = image_fingerprint(content)
        except (OSError, ValueError, Image.DecompressionBombError, SyntaxError) as error:
            if not allow_invalid:
                raise
            LOGGER.warning("Conservando imagen no valida EAN %s, nombre %s: %s", ean, name, error)
            pixels = None
        instant = datetime.fromisoformat(date) if date else datetime.now(timezone.utc)
        if instant.tzinfo is None:
            raise ValueError("fecha_imagen_sin_zona_horaria")
        timestamp = instant.astimezone(timezone.utc).isoformat()
        checkpoint()
        with acquire(self.lock(ean)), acquire(self.maintenance_lock()), self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            asset = connection.execute("SELECT * FROM assets WHERE ean=? AND checksum=?",
                                       (ean, checksum)).fetchone()
            created = asset is None
            destination = self.resolve(asset["path"]) if asset else self.resolve(f"{ean}/{name}")
            collision = False
            if destination.exists() and hashlib.sha256(destination.read_bytes()).hexdigest() != checksum:
                collision = True
                index = 0
                destination = self.resolve(f"{ean}/{collision_name(name, checksum)}")
                while destination.exists() and hashlib.sha256(destination.read_bytes()).hexdigest() != checksum:
                    index += 1
                    destination = self.resolve(f"{ean}/{collision_name(name, checksum, index)}")
            if not destination.exists():
                atomic_write(destination, content, guard=lambda: check_image_root(self.root))
            if file_checksum(destination) != checksum:
                raise ValueError(f"Fallo verificando escritura: {destination}")
            relative = destination.relative_to(self.root).as_posix()
            connection.execute("""
                INSERT INTO assets(ean,checksum,pixels,path) VALUES(?,?,?,?)
                ON CONFLICT(ean,checksum) DO UPDATE SET path=excluded.path,pixels=excluded.pixels
            """, (ean, checksum, pixels, relative))
            if asset and asset["path"] != relative:
                connection.execute("UPDATE migrations SET destination=? WHERE destination=? AND checksum=?",
                                   (relative, asset["path"], checksum))
            asset_id = connection.execute("SELECT id FROM assets WHERE ean=? AND checksum=?",
                                          (ean, checksum)).fetchone()[0]
            cursor = connection.execute("""
                INSERT OR IGNORE INTO representations
                    (asset_id,provider,origin,view,market,name,date,metadata) VALUES(?,?,?,?,?,?,?,?)
            """, (asset_id, provider, origin, view, market, name, timestamp,
                  json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)))
            if cursor.rowcount or collision:
                connection.execute("INSERT INTO events(date,ean,action,details) VALUES(?,?,?,?)",
                    (timestamp, ean, "conflict" if collision else "saved" if created else "reused",
                     json.dumps({"provider": provider, "origin": origin, "name": name,
                                 "path": relative, "checksum": checksum})))
            row_id = connection.execute("""
                SELECT id FROM representations WHERE asset_id=? AND provider=? AND origin=?
                    AND view=? AND market=? AND name=?
            """, (asset_id, provider, origin, view, market, name)).fetchone()[0]
        record = next(record for record in self.records(ean) if record.id == row_id)
        return record, created

    def associate_order(self, source: str, order_id: str, eans: list[str]) -> None:
        for ean in eans:
            validate_ean(ean)
        with self.maintenance_lock(), self.connect() as connection:
            connection.executemany("INSERT OR IGNORE INTO order_eans VALUES(?,?,?)",
                                   [(source, str(order_id), ean) for ean in set(eans)])

    def review_view(self, record_id: int, view: str, *, reviewer: str, reason: str,
                    evidence: dict | None = None) -> None:
        if view not in {*IMAGE_VIEWS, "unknown"} or not reviewer.strip() or not reason.strip():
            raise ValueError("La revision requiere vista, revisor y motivo")
        records = [record for record in self.records() if record.id == record_id]
        if not records:
            raise ValueError("Imagen no encontrada para revisar")
        record = records[0]
        with self.lock(record.ean), self.maintenance_lock(), self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute("SELECT * FROM representations WHERE id=?", (record_id,)).fetchone()
            metadata = json.loads(current["metadata"])
            review = {"previous_view": current["view"], "view": view, "reviewer": reviewer,
                      "reason": reason, "date": datetime.now(timezone.utc).isoformat(),
                      "evidence": evidence or {}}
            metadata["view_review"] = review
            connection.execute("UPDATE representations SET view=?, metadata=? WHERE id=?",
                               (view, json.dumps(metadata, ensure_ascii=False, sort_keys=True), record_id))
            connection.execute("INSERT INTO events(date,ean,action,details) VALUES(?,?,?,?)",
                               (review["date"], record.ean, "view_review",
                                json.dumps({"record_id": record_id, **review})))

    def review_view_for_user(self, record_id: int, view: str, *, role: str | None,
                             reviewer: str, reason: str, evidence: dict | None = None) -> None:
        if role != "admin":
            raise PermissionError("La revision de vistas requiere un usuario administrador")
        self.review_view(record_id, view, reviewer=reviewer, reason=reason, evidence=evidence)

    def order_records(self, source: str, order_id: str) -> list[ImageRecord]:
        with self.connect() as connection:
            eans = {row[0] for row in connection.execute(
                "SELECT ean FROM order_eans WHERE source=? AND order_id=?", (source, str(order_id)))}
        return [record for record in self.records() if record.ean in eans]

    def zip_eans(self, eans: list[str], *, market: str | None = None) -> bytes:
        buffer = BytesIO()
        names: dict[str, str] = {}
        with ZipFile(buffer, "w") as archive:
            for ean in dict.fromkeys(eans):
                with self.lock(ean):
                    for record in self.records(ean):
                        if market is not None and record.market != market:
                            continue
                        name = f"{ean}/{record.name}"
                        if name in names:
                            if names[name] == record.checksum:
                                continue
                            name = f"{ean}/{collision_name(record.name, record.checksum)}"
                            index = 0
                            while name in names and names[name] != record.checksum:
                                index += 1
                                name = f"{ean}/{collision_name(record.name, record.checksum, index)}"
                            if name in names:
                                continue
                        names[name] = record.checksum
                        archive.writestr(name, self.verified_bytes(record))
        return buffer.getvalue()

    def zip_eans_for_user(self, eans: list[str], *, role: str | None,
                          market: str | None = None) -> bytes:
        if role not in {"admin", "masterdata"}:
            raise PermissionError("La descarga requiere acceso a la biblioteca de imagenes")
        return self.zip_eans(eans, market=market)

    def sync_nas(self, target: Path) -> None:
        from process_activity import record_process
        with record_process("nas-sync") as receipt:
            count = self._sync_nas(target)
            receipt["counts"] = {"Archivos de imágenes verificados": count}

    def _sync_nas(self, target: Path) -> int:
        target = target.resolve()
        require_distinct_replica(self.root, target)
        if self.state_root != self.root or self.backup_root != self.root:
            raise ValueError("Replica completa requiere destino de estado/backup independiente; sync-nas legacy no permitido")
        target.mkdir(parents=True, exist_ok=True)
        with FileLock(target / ".repository.lock", timeout=120), self.maintenance_lock():
            seen: set[Path] = set()
            for record in self.records():
                if record.path in seen:
                    continue
                seen.add(record.path)
                content = self.verified_bytes(record)
                destination = target / record.path.relative_to(self.root)
                if destination.exists() and hashlib.sha256(destination.read_bytes()).hexdigest() != record.checksum:
                    raise ValueError(f"Conflicto en replica NAS: {destination}")
                if not destination.exists():
                    atomic_write(destination, content)
                if hashlib.sha256(destination.read_bytes()).hexdigest() != record.checksum:
                    raise ValueError(f"Fallo verificando NAS: {destination}")
            with self.connect() as connection:
                backups = list(connection.execute("SELECT backup,checksum FROM migrations"))
            for relative, digest in backups:
                if excluded_staging_path(Path(relative)):
                    LOGGER.warning("Backup staging excluido de replica: %s", relative)
                    continue
                source = self.resolve(relative)
                copy_verified(source, target / relative, digest)
            for plan in (self.root / ".migration").glob("*/plan.json"):
                copy_verified(plan, target / plan.relative_to(self.root), file_checksum(plan))
            descriptor, temporary = tempfile.mkstemp(dir=target, suffix=".sqlite3")
            os.close(descriptor)
            try:
                with self.connect() as source, closing(sqlite3.connect(temporary)) as backup:
                    source.backup(backup)
                    if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise ValueError("Catalogo NAS no integro")
                os.replace(temporary, target / ".catalog.sqlite3")
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            return len(seen)
