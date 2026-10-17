"""Regenerable local thumbnails; never an authority for original availability."""
from contextlib import contextmanager
import hashlib
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import shutil
import tempfile
import time

from filelock import FileLock
from PIL import Image, ImageOps

from image_repository import ImageRecord, ImageRepository
from repository_storage import check_image_root, require_local, filesystem_mount, excluded_staging_path


LOGGER = logging.getLogger(__name__)
GENERATOR_VERSION = "exif-fit-rgb-v1"
# Library cards: ~2x of the largest visual size (4:3 frame), flat light frame colour, high quality.
UI_DIMENSIONS = (640, 480)
UI_BACKGROUND = "#f4f4f2"
UI_QUALITY = 90
UI_VERSION = "ui-lanczos-flat-v1"
DEFAULT_CAPACITY = 1024**3
DEFAULT_RESERVE = 2 * 1024**3


def setting(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} debe ser positivo")
    return value


def cache_root(repository: ImageRepository) -> Path:
    configured = os.getenv("IMAGE_THUMBNAIL_CACHE_ROOT", "").strip()
    if not configured and os.getenv("IMAGE_REPOSITORY_NFS_SOURCE", "").strip():
        raise ValueError("NFS requiere IMAGE_THUMBNAIL_CACHE_ROOT local explicita")
    root = Path(configured).absolute() if configured else repository.state_root.parent / "image-thumbnails"
    if root.resolve() != root or ".." in root.parts:
        raise ValueError("Cache no admite aliases")
    require_local(root)
    mount = filesystem_mount(root)
    if mount and mount[1] in {"tmpfs", "ramfs"}:
        raise ValueError("Cache requiere almacenamiento persistente, no tmpfs")
    # Do not resolve NAS paths here: these are already validated repository roots.
    protected = [repository.root, repository.state_root, repository.backup_root,
                 Path(tempfile.gettempdir()).absolute()]
    for name in ("IMAGE_REPOSITORY_WORK_ROOT", "KERING_DATA_ROOT", "MASTERDATA_DATA_ROOT"):
        value = os.getenv(name, "").strip()
        if value:
            protected.append(Path(value).absolute())
    for name in ("AUTH_STATE_PATH", "ORDER_ALERT_STATE_PATH", "PROCESS_ACTIVITY_PATH",
                 "MASTERDATA_DICTIONARY_PATH"):
        value = os.getenv(name, "").strip()
        if value:
            protected.append(Path(value).absolute().parent)
    for other in protected:
        if root == other or root in other.parents or other in root.parents:
            raise ValueError("Cache requiere raiz independiente de datos y temporales")
    return root


def generate(content: bytes, dimensions: tuple[int, int], image_format: str,
             background: str | None = None) -> bytes:
    """Derived preview; the original bytes are never modified.

    With `background` the image is converted to RGBA first (palette and alpha modes would otherwise be
    resampled with NEAREST, which jags the silhouette), resized with LANCZOS and composited on that flat
    colour instead of black, then saved with high quality and no chroma subsampling.
    """
    with Image.open(BytesIO(content)) as original:
        image = ImageOps.exif_transpose(original)
        if background is None:
            image.thumbnail(dimensions, Image.Resampling.LANCZOS)
            buffer = BytesIO()
            image.convert("RGB").save(buffer, format=image_format)
            return buffer.getvalue()
        image = image.convert("RGBA")
        image.thumbnail(dimensions, Image.Resampling.LANCZOS)
        flat = Image.new("RGBA", image.size, background)
        buffer = BytesIO()
        Image.alpha_composite(flat, image).convert("RGB").save(
            buffer, format=image_format, quality=UI_QUALITY, subsampling=0, optimize=True)
        return buffer.getvalue()


def validate_visible(repository: ImageRepository, records: list[ImageRecord]) -> dict:
    """Metadata only, once per visible selection/manual refresh, not per rerender."""
    check_image_root(repository.root)
    result = {}
    for record in records:
        try:
            path = repository.resolve(record.path.relative_to(repository.root).as_posix())
            if path != record.path or excluded_staging_path(path):
                raise ValueError("Alias o ruta staging excluida")
            if not path.is_file() or not os.access(path, os.R_OK):
                raise OSError("Original ausente o inaccesible")
            stat = path.stat()
            result[record.id] = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        except (OSError, ValueError) as error:
            LOGGER.warning("Original no accesible id=%s: %s", record.id, error)
            result[record.id] = None
    return result


class ThumbnailCache:
    def __init__(self, repository: ImageRepository):
        self.repository = repository
        self.root = cache_root(repository)
        self.capacity = setting("IMAGE_THUMBNAIL_CACHE_MAX_BYTES", DEFAULT_CAPACITY)
        self.reserve = setting("IMAGE_THUMBNAIL_CACHE_RESERVE_BYTES", DEFAULT_RESERVE)
        self._current = None

    @contextmanager
    def batch(self, records: list[ImageRecord]):
        check_image_root(self.repository.root)
        self._current = {record.id: record for record in self.repository.catalog_records(
            record_ids=[record.id for record in records])}
        try:
            yield self
        finally:
            self._current = None

    def _atomic(self, path: Path, content: bytes) -> None:
        temporary = self.root / ".thumb-write.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            if os.name == "posix":
                directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @contextmanager
    def _locked(self):
        require_local(self.root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(self.root, 0o700)
        with FileLock(self.root / ".cache.lock", timeout=30, mode=0o600):
            index_path = self.root / "index.json"
            try:
                index = json.loads(index_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                index = {}
            except (ValueError, UnicodeError) as error:
                raise ValueError("Indice de miniaturas corrupto; retirar cache regenerable") from error
            if not isinstance(index, dict) or any(
                    not isinstance(entry, dict) or not {"size", "digest", "sources", "used"} <= entry.keys()
                    or len(key) != 64 or any(char not in "0123456789abcdef" for char in key)
                    or not isinstance(entry["size"], int) or entry["size"] < 0
                    or not isinstance(entry["used"], int) or not isinstance(entry["sources"], dict)
                    for key, entry in index.items()):
                raise ValueError("Indice de miniaturas invalido")
            # The global lock makes a fixed atomic-write temporary safe to recover.
            (self.root / ".thumb-write.tmp").unlink(missing_ok=True)
            yield index
            encoded = json.dumps(index, sort_keys=True).encode()
            if shutil.disk_usage(self.root).free < self.reserve + len(encoded):
                raise OSError("Reserva local protegida; no se guarda indice de miniaturas")
            self._atomic(index_path, encoded)

    def get(self, record: ImageRecord, access: tuple[int, int, int] | None,
            dimensions: tuple[int, int] = (360, 240), image_format: str = "JPEG",
            version: str = GENERATOR_VERSION, background: str | None = None) -> bytes:
        if access is None:
            raise OSError("Original no validado o inaccesible")
        if min(dimensions) <= 0 or image_format != "JPEG":
            raise ValueError("Dimensiones/formato de miniatura invalidos")
        if self._current is None:
            check_image_root(self.repository.root)
            current = self.repository.catalog_records(record.ean, record_id=record.id)
            authoritative = current[0] if current else None
        else:
            authoritative = self._current.get(record.id)
        if authoritative != record:
            raise ValueError("Imagen eliminada o modificada en el catalogo")
        key = hashlib.sha256(json.dumps(
            [record.checksum, dimensions, image_format, version, *([background] if background else [])]
        ).encode()).hexdigest()
        source = hashlib.sha256(str(record.path).encode()).hexdigest()
        with self._locked() as index:
            entry = index.get(key)
            path = self.root / f"{key}.jpg"
            proof = [record.checksum, *access]
            content = None
            if not entry or entry.get("sources", {}).get(source) != proof:
                # Read only a new/changed source, never a known warm original.
                content = self.repository.verified_bytes(record)
            if entry:
                try:
                    cached = path.read_bytes()
                    if hashlib.sha256(cached).hexdigest() != entry["digest"]:
                        raise ValueError("Miniatura corrupta")
                except (OSError, ValueError) as error:
                    LOGGER.warning("Regenerando miniatura %s: %s", key, error)
                    path.unlink(missing_ok=True)
                    del index[key]
                    entry = None
            if not entry:
                if content is None:
                    content = self.repository.verified_bytes(record)
                cached = (generate(content, dimensions, image_format, background) if background
                          else generate(content, dimensions, image_format))
                entry = {"size": len(cached), "digest": hashlib.sha256(cached).hexdigest(),
                         "sources": {}, "used": time.time_ns()}
            entry["sources"][source] = proof
            entry["used"] = time.time_ns()
            index[key] = entry
            encoded_size = len(json.dumps(index, sort_keys=True).encode())
            required = sum(item["size"] for item in index.values()) + encoded_size + 1
            free = shutil.disk_usage(self.root).free
            for victim in sorted(index, key=lambda item: index[item]["used"]):
                if required <= self.capacity and free >= self.reserve + len(cached) + encoded_size:
                    break
                if victim == key:
                    continue
                removed = index.pop(victim)
                (self.root / f"{victim}.jpg").unlink(missing_ok=True)
                required -= removed["size"]
                free = shutil.disk_usage(self.root).free
            encoded_size = len(json.dumps(index, sort_keys=True).encode())
            if sum(item["size"] for item in index.values()) + encoded_size + 1 > self.capacity:
                index.pop(key)
                path.unlink(missing_ok=True)
                # Persist evictions even when a single thumbnail cannot fit.
                if index:
                    self._atomic(self.root / "index.json", json.dumps(index, sort_keys=True).encode())
                else:
                    (self.root / "index.json").unlink(missing_ok=True)
                raise OSError("Miniatura supera la capacidad de cache")
            if free < self.reserve + len(cached) + encoded_size:
                raise OSError("Reserva local protegida; cache sin espacio")
            if not path.exists():
                # Register size before publishing bytes: a process crash can leave
                # a missing entry, but never an unaccounted payload/orphan.
                self._atomic(self.root / "index.json", json.dumps(index, sort_keys=True).encode())
                self._atomic(path, cached)
            return cached
