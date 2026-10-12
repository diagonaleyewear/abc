"""Bounded, on-demand exports of current repository images (not historical copies)."""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import tempfile
from zipfile import ZipFile, ZIP_STORED
from typing import BinaryIO
from PIL import Image

from image_naming import sanitize_filename
from image_repository import ImageRepository, collision_name, image_fingerprint


MAX_EXPORT_BYTES = 256 * 1024 * 1024
MAX_EXPORT_FILES = 2000
REQUIRED_VIEWS = ("frontal", "lateral", "perspectiva")


@dataclass
class ExportPlan:
    files: list
    manifest: dict
    signature: str
    filename: str


def repository_signature(repository: ImageRepository, order: dict) -> str:
    entries = []
    for ean in dict.fromkeys(line["ean"] for line in order["lines"] if line["ean"]):
        for record in repository.records(ean):
            try:
                stat = record.path.stat()
                state = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            except FileNotFoundError:
                state = None
            entries.append((record.id, record.checksum, record.view, record.market, record.name,
                            record.metadata, str(record.path), state))
    return hashlib.sha256(json.dumps({"order": order, "entries": entries}, sort_keys=True).encode()).hexdigest()


def order_export_plan(repository: ImageRepository, order: dict) -> ExportPlan:
    files = []
    products = []
    used = {}
    total = 0
    for ean in dict.fromkeys(line["ean"] for line in order["lines"] if line["ean"]):
        available = {}
        views = set()
        invalid = []
        for record in repository.records(ean):
            try:
                content = repository.verified_bytes(record)
                image_fingerprint(content)
            except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
                invalid.append(record.name)
                continue
            name = f"{ean}/{record.name}"
            if name in used and used[name] != record.checksum:
                name = f"{ean}/{collision_name(record.name, record.checksum)}"
                index = 0
                while name in used and used[name] != record.checksum:
                    index += 1
                    name = f"{ean}/{collision_name(record.name, record.checksum, index)}"
            if name not in used:
                total += len(content)
                if total > MAX_EXPORT_BYTES or len(files) >= MAX_EXPORT_FILES:
                    raise ValueError("Pedido demasiado grande para un ZIP (limite 256 MiB / 2000 fotos)")
                used[name] = record.checksum
                files.append((name, record))
            available.setdefault(name, []).append({
                "provider": record.provider, "origin": record.origin, "view": record.view,
                "market": record.market, "checksum": record.checksum,
                "date": record.date, "path": record.path.relative_to(repository.root).as_posix(),
                "view_detection": record.metadata.get("view_detection"),
                "view_review": record.metadata.get("view_review"),
            })
        # Distinct pixels and canonical views determine coverage, not market copies.
        views.update(repository.valid_views(ean))
        products.append({"ean": ean, "files": available, "missing_views": [
            view for view in REQUIRED_VIEWS if view not in views], "invalid_files": invalid})
    manifest = {"order": order["name"], "order_id": order["id"], "products": products,
                "lines_without_ean": sum(not line["ean"] for line in order["lines"]),
                "current_images": True, "partial": any(p["missing_views"] for p in products)
                or any(not line["ean"] for line in order["lines"])}
    signature = repository_signature(repository, order)
    return ExportPlan(files, manifest, signature, sanitize_filename(order["name"]) + ".zip")


@dataclass
class PreparedExport:
    stream: BinaryIO
    plan: ExportPlan


@contextmanager
def prepare_order_zip(repository: ImageRepository, order: dict, plan: ExportPlan | None = None):
    plan = plan or order_export_plan(repository, order)
    if plan.signature != repository_signature(repository, order):
        raise ValueError("Los archivos o el pedido cambiaron; vuelva a preparar el ZIP")
    if not plan.files:
        raise ValueError("El pedido no tiene fotos validas para exportar")
    with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b") as stream:
        with ZipFile(stream, "w", ZIP_STORED) as archive:
            for name, record in plan.files:
                with repository.lock(record.ean):
                    archive.writestr(name, repository.verified_bytes(record))
            archive.writestr("manifest.json", json.dumps(plan.manifest, ensure_ascii=False, indent=2))
        stream.seek(0)
        if plan.signature != repository_signature(repository, order):
            raise ValueError("Los archivos o el pedido cambiaron durante la preparacion")
        yield PreparedExport(stream, plan)
