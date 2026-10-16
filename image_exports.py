"""Bounded, on-demand exports of current repository images (not historical copies)."""
from contextlib import contextmanager
from dataclasses import dataclass
import argparse
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
from zipfile import ZipFile, ZIP_STORED
from typing import BinaryIO
from PIL import Image, ImageOps

from image_naming import sanitize_filename
from image_repository import (
    ImageRecord,
    ImageRepository,
    atomic_write,
    collision_name,
    file_checksum,
    image_fingerprint,
    validate_ean,
)
from repository_storage import check_image_root


MAX_EXPORT_BYTES = 256 * 1024 * 1024
MAX_EXPORT_FILES = 2000
REQUIRED_VIEWS = ("frontal", "lateral", "perspectiva")
COMMERCIAL_VIEWS = {"frontal": "V1", "perspectiva": "V2", "lateral": "V3"}
COMMERCIAL_MARKETS = {"Farfetch": ".png", "Miinto": ".jpeg"}
SAFE_MARKET_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


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


@dataclass(frozen=True)
class CommercialExportEntry:
    ean: str
    market: str
    view: str
    file_name: str
    relative_path: str
    source: ImageRecord
    source_checksum: str
    checksum: str
    size: int
    write_required: bool


@dataclass
class CommercialExportPlan:
    eans: tuple[str, ...]
    market_ids: dict[str, dict[str, str]]
    entries: list[CommercialExportEntry]
    missing_ids: dict[str, list[str]]
    missing_views: dict[str, list[str]]
    issues: list[str]
    blockers: list[str]
    signature: str
    total_bytes: int
    bytes_to_write: int


def _commercial_content(repository: ImageRepository, record: ImageRecord, market: str) -> bytes:
    content = repository.verified_bytes(record)
    with Image.open(BytesIO(content)) as image:
        if image.format == "PNG":
            return content
        if market == "Miinto":
            raise ValueError("Miinto requiere conservar los bytes PNG historicos")
        image = ImageOps.exif_transpose(image)
        output = BytesIO()
        image.convert("RGBA" if "A" in image.getbands() else "RGB").save(output, format="PNG")
        return output.getvalue()


def _commercial_target(repository: ImageRepository, relative_path: str) -> Path:
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Ruta de exportacion comercial invalida")
    candidate = repository.root / relative
    current = repository.root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("Ruta de exportacion comercial contiene un enlace simbolico")
    target = candidate.resolve()
    if repository.root not in target.parents:
        raise ValueError("Ruta de exportacion comercial fuera del repositorio")
    return target


def _commercial_signature(
    repository: ImageRepository,
    eans: tuple[str, ...],
    market_ids: dict[str, dict[str, str]],
) -> str:
    records = []
    for ean in eans:
        for record in repository.records(ean):
            try:
                stat = record.path.stat()
                file_state = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            except FileNotFoundError:
                file_state = None
            records.append((
                record.id, record.ean, record.checksum, record.view, record.market, record.name,
                str(record.path), file_state,
            ))
    payload = {"eans": eans, "market_ids": market_ids, "records": records}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


def build_commercial_export_plan(
    repository: ImageRepository,
    market_ids: dict[str, dict[str, str]],
    eans: list[str] | tuple[str, ...] | None = None,
) -> CommercialExportPlan:
    records_by_ean: dict[str, list[ImageRecord]] = {}
    for record in repository.records():
        records_by_ean.setdefault(record.ean, []).append(record)
    selected_eans = tuple(sorted(set(eans if eans is not None else records_by_ean)))
    for ean in selected_eans:
        validate_ean(ean)

    entries: list[CommercialExportEntry] = []
    missing_ids: dict[str, list[str]] = {}
    missing_views: dict[str, list[str]] = {}
    issues: list[str] = []
    blockers: list[str] = []
    total_bytes = 0
    bytes_to_write = 0

    for ean in selected_eans:
        original_records = [
            record for record in records_by_ean.get(ean, [])
            if not record.market and record.view in COMMERCIAL_VIEWS
        ]
        candidates: dict[str, dict[str, tuple[ImageRecord, str]]] = {}
        for record in original_records:
            try:
                pixels = image_fingerprint(repository.verified_bytes(record))
            except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
                issues.append(f"invalid_image:{ean}:{record.id}")
                continue
            if record.pixels != pixels:
                issues.append(f"pixel_fingerprint_mismatch:{ean}:{record.id}")
                continue
            candidates.setdefault(record.view, {})[record.checksum] = (record, pixels)

        selected: dict[str, ImageRecord] = {}
        seen_pixels: set[str] = set()
        for view in ("frontal", "perspectiva", "lateral"):
            variants = candidates.get(view, {})
            if len(variants) > 1:
                issues.append(f"ambiguous_view:{ean}:{view}")
                continue
            if not variants:
                continue
            record, pixels = next(iter(variants.values()))
            if pixels in seen_pixels:
                issues.append(f"duplicate_view_pixels:{ean}:{view}")
                continue
            seen_pixels.add(pixels)
            selected[view] = record

        ids_for_ean = market_ids.get(ean, {})
        for market, extension in COMMERCIAL_MARKETS.items():
            market_id = str(ids_for_ean.get(market, "")).strip()
            if not market_id:
                missing_ids.setdefault(ean, []).append(market)
                continue
            if not SAFE_MARKET_ID.fullmatch(market_id) or market_id in {".", ".."}:
                blockers.append(f"invalid_market_id:{ean}:{market}")
                continue
            available_views = set()
            for view, view_code in COMMERCIAL_VIEWS.items():
                record = selected.get(view)
                if record is None:
                    continue
                try:
                    content = _commercial_content(repository, record, market)
                except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
                    issues.append(f"unsupported_image:{ean}:{market}:{view}")
                    continue
                file_name = f"{market_id}_{view_code}{extension}"
                relative_path = Path("commercial_exports", market, ean, file_name).as_posix()
                try:
                    target = _commercial_target(repository, relative_path)
                    exists = target.exists()
                    if exists and file_checksum(target) != hashlib.sha256(content).hexdigest():
                        blockers.append(f"destination_conflict:{relative_path}")
                        continue
                except (OSError, ValueError):
                    blockers.append(f"unsafe_destination:{relative_path}")
                    continue
                checksum = hashlib.sha256(content).hexdigest()
                entries.append(CommercialExportEntry(
                    ean=ean,
                    market=market,
                    view=view,
                    file_name=file_name,
                    relative_path=relative_path,
                    source=record,
                    source_checksum=record.checksum,
                    checksum=checksum,
                    size=len(content),
                    write_required=not exists,
                ))
                available_views.add(view_code)
                total_bytes += len(content)
                if not exists:
                    bytes_to_write += len(content)
            missing = [
                view_code for view_code in COMMERCIAL_VIEWS.values()
                if view_code not in available_views
            ]
            if missing:
                missing_views[f"{market}:{ean}"] = missing

    return CommercialExportPlan(
        eans=selected_eans,
        market_ids=market_ids,
        entries=entries,
        missing_ids=missing_ids,
        missing_views=missing_views,
        issues=issues,
        blockers=blockers,
        signature=_commercial_signature(repository, selected_eans, market_ids),
        total_bytes=total_bytes,
        bytes_to_write=bytes_to_write,
    )


def publish_commercial_export_plan(
    repository: ImageRepository,
    plan: CommercialExportPlan,
) -> list[Path]:
    if plan.blockers:
        raise ValueError("Publicacion bloqueada: " + ", ".join(plan.blockers))
    if plan.signature != _commercial_signature(repository, plan.eans, plan.market_ids):
        raise ValueError("El repositorio cambio; vuelva a preparar la exportacion comercial")

    from contextlib import ExitStack

    published: list[Path] = []
    with ExitStack() as locks:
        for ean in plan.eans:
            locks.enter_context(repository.lock(ean))
        if plan.signature != _commercial_signature(repository, plan.eans, plan.market_ids):
            raise ValueError("El repositorio cambio; vuelva a preparar la exportacion comercial")
        for entry in plan.entries:
            target = _commercial_target(repository, entry.relative_path)
            if target.exists() and file_checksum(target) != entry.checksum:
                raise ValueError(f"Conflicto sin sobrescritura: {entry.relative_path}")
        for entry in plan.entries:
            target = _commercial_target(repository, entry.relative_path)
            content = _commercial_content(repository, entry.source, entry.market)
            if hashlib.sha256(content).hexdigest() != entry.checksum:
                raise ValueError(f"El contenido cambio: {entry.relative_path}")
            if not target.exists():
                atomic_write(target, content, guard=lambda: check_image_root(repository.root))
            if file_checksum(target) != entry.checksum:
                raise ValueError(f"Fallo verificando exportacion: {entry.relative_path}")
            published.append(target)
    return published


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prepara exportaciones comerciales desde originales del repositorio comun."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Solo valida y muestra el plan (predeterminado).")
    mode.add_argument("--apply", action="store_true", help="Publica archivos sin sobrescribir destinos distintos.")
    parser.add_argument("--ean", action="append", help="Limita la preparacion a este EAN; se puede repetir.")
    arguments = parser.parse_args(argv)

    repository = ImageRepository()
    selected_eans = sorted(set(arguments.ean)) if arguments.ean else None
    eans_to_query = set(selected_eans) if selected_eans is not None else {
        record.ean for record in repository.records()
    }
    from graph_mail_downloader import _get_market_ids_by_ean
    market_ids = _get_market_ids_by_ean(eans_to_query)
    plan = build_commercial_export_plan(repository, market_ids, selected_eans)
    print(json.dumps({
        "mode": "apply" if arguments.apply else "dry-run",
        "files": [
            {"ean": entry.ean, "market": entry.market, "view": COMMERCIAL_VIEWS[entry.view],
             "name": entry.file_name, "bytes": entry.size, "already_present": not entry.write_required}
            for entry in plan.entries
        ],
        "total_bytes": plan.total_bytes,
        "bytes_to_write": plan.bytes_to_write,
        "available_bytes": shutil.disk_usage(repository.root).free,
        "missing_ids": plan.missing_ids,
        "missing_views": plan.missing_views,
        "issues": plan.issues,
        "blockers": plan.blockers,
    }, ensure_ascii=False, indent=2))
    if arguments.apply:
        if plan.blockers:
            print("Publicacion bloqueada; no se escribieron exportaciones.", file=sys.stderr)
            return 2
        if not plan.entries:
            print("No hay archivos comerciales publicables.", file=sys.stderr)
            return 2
        if plan.bytes_to_write > shutil.disk_usage(repository.root).free:
            print("Espacio insuficiente; no se escribieron exportaciones.", file=sys.stderr)
            return 2
        published = publish_commercial_export_plan(repository, plan)
        print(f"Exportaciones verificadas: {len(published)}")
    return 0


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


if __name__ == "__main__":
    raise SystemExit(main())
