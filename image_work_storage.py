"""Bounded archive staging, separate from image bytes and local SQLite."""
from contextlib import contextmanager
import os
from pathlib import Path
import shutil

from filelock import FileLock

from repository_storage import (
    state_root, check_image_root, require_distinct_storage, filesystem_mount, check_staging_role,
    check_production_directory,
)
from scripts.nfs_repository_guard import require_nfs
from service_stop import acquire, checkpoint


def positive_setting(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} debe ser positivo")
    return value


def archive_limit() -> int:
    return positive_setting("GRAPH_IMAGE_MAX_ARCHIVE_BYTES", 8 * 1024**3)


def check_work_root(root: Path) -> None:
    if check_staging_role(root, "work"):
        return
    if check_production_directory(root, "work"):
        return
    if root.resolve() != root:
        raise ValueError("Temporales no pueden redirigirse mediante enlaces simbolicos")
    source = os.getenv("IMAGE_REPOSITORY_WORK_NFS_SOURCE", "").strip()
    if source:
        configured = os.getenv("IMAGE_REPOSITORY_WORK_MOUNT_ROOT", "").strip()
        if not configured:
            raise ValueError("Temporales NFS requieren IMAGE_REPOSITORY_WORK_MOUNT_ROOT")
        mount = Path(configured).absolute()
        if root != mount and mount not in root.parents:
            raise ValueError("Temporales fuera del montaje configurado")
        require_nfs(mount, source)
        actual = filesystem_mount(root)
        if actual is None or actual[0] != mount or actual[1] not in {"nfs", "nfs4"} or actual[2] != source:
            raise ValueError("Temporales cubiertos por un montaje distinto")
    else:
        mount = filesystem_mount(root)
        if mount and mount[1] in {"nfs", "nfs4"}:
            raise ValueError("Temporales NFS requieren exportacion esperada explicita")


def work_root(images: Path) -> Path:
    configured = os.getenv("IMAGE_REPOSITORY_WORK_ROOT", "").strip()
    if not configured and os.getenv("IMAGE_REPOSITORY_NFS_SOURCE", "").strip():
        raise ValueError("Imagenes NFS requieren temporales independientes explicitos")
    root = Path(configured).absolute() if configured else state_root(images) / ".incoming"
    if configured:
        require_distinct_storage(images, root)
        require_distinct_storage(state_root(images), root)
    check_work_root(root)
    return root


def require_capacity(root: Path, required: int = 0) -> None:
    check_work_root(root)
    floor = positive_setting("IMAGE_REPOSITORY_WORK_RESERVE_BYTES", 2 * 1024**3)
    if shutil.disk_usage(root).free < floor + required:
        raise OSError("Espacio insuficiente en temporales de imagenes; reserva protegida")


@contextmanager
def incoming_lock(images: Path):
    checkpoint()
    check_image_root(images)
    root = work_root(images)
    root.mkdir(parents=True, exist_ok=True)
    with acquire(FileLock(root / ".incoming.lock", timeout=120)):
        require_capacity(root, archive_limit())
        yield root
