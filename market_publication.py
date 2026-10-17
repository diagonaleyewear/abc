"""Prepare and publish verified commercial image copies from the canonical repository."""
import argparse
from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Protocol

from filelock import FileLock
from PIL import Image, ImageOps

from image_naming import market_view_from_image_name
from image_repository import ImageRecord, image_fingerprint, validate_ean
from kering_media import POSITIONAL_PROVENANCE, POSITIONAL_RULE, REVIEW_CONFLICT
from repository_storage import check_image_root, excluded_staging_path, state_root


DEFAULT_SOURCE_ROOT = Path("/mnt/cloud-imagenes/images")
DEFAULT_PUBLICATION_ROOT = Path("/mnt/cloud-imagenes/market-publication")
MANIFEST_NAME = ".market-publication-manifest.json"
LOCK_NAME = ".market-publication.lock"
MANAGED_BY = "market_publication.py"
MARKETS = {
    "Farfetch": (".png", "PNG"),
    "Miinto": (".jpeg", "JPEG"),
}
VIEW_NAMES = {"frontal": "V1", "perspectiva": "V2", "lateral": "V3"}
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


class ImageRepositoryReader(Protocol):
    root: Path

    def records(self, ean: str | None = None) -> list[ImageRecord]: ...

    def verified_bytes(self, record: ImageRecord) -> bytes: ...


class KeringReviewReader(Protocol):
    def has_blocking_review(self, ean: str, records: list[ImageRecord]) -> bool: ...


class UnsupportedPublicationFormat(ValueError):
    pass


class ReadOnlyImageRepository:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        check_image_root(self.root)
        if not self.root.is_dir():
            raise FileNotFoundError(f"origen_canonico_no_disponible: {self.root}")
        self.state_root = state_root(self.root)
        self.database = self.state_root / ".catalog.sqlite3"
        if not self.database.is_file():
            raise FileNotFoundError(f"catalogo_canonico_no_disponible: {self.database}")

    def _connect(self) -> sqlite3.Connection:
        check_image_root(self.root)
        return sqlite3.connect(f"{self.database.as_uri()}?mode=ro", uri=True, timeout=30)

    def records(self, ean: str | None = None) -> list[ImageRecord]:
        if ean is not None:
            validate_ean(ean)
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            query = """
                SELECT r.*, a.ean, a.checksum, a.pixels, a.path FROM representations r
                JOIN assets a ON a.id=r.asset_id
            """
            rows = connection.execute(
                query + (" WHERE a.ean=?" if ean is not None else "") + " ORDER BY r.id",
                (ean,) if ean is not None else (),
            ).fetchall()
        records = []
        for row in rows:
            relative = Path(row["path"])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"ruta_catalogo_invalida: {row['path']}")
            if excluded_staging_path(relative):
                continue
            path = self.root / relative
            resolved = path.resolve()
            if path.absolute() != resolved or self.root not in resolved.parents:
                raise ValueError(f"ruta_catalogo_fuera_del_repositorio: {row['path']}")
            if excluded_staging_path(resolved.relative_to(self.root)):
                continue
            records.append(ImageRecord(
                row["id"], row["ean"], row["checksum"], row["pixels"], path,
                row["provider"], row["origin"], row["view"], row["market"], row["name"],
                row["date"], json.loads(row["metadata"]),
            ))
        return records

    def verified_bytes(self, record: ImageRecord) -> bytes:
        check_image_root(self.root)
        path = record.path
        resolved = path.resolve()
        if path.absolute() != resolved or self.root not in resolved.parents:
            raise ValueError("ruta_original_modificada")
        content = resolved.read_bytes()
        if sha256(content).hexdigest() != record.checksum:
            raise ValueError(f"checksum_incorrecto: {record.path}")
        return content


class ReadOnlyKeringReviewReader:
    def __init__(self, root: Path | None = None):
        self.root = Path(root or os.getenv(
            "KERING_DATA_ROOT",
            str(Path(__file__).resolve().parent / "masterdata_data" / "kering"),
        )).resolve()
        self.database = self.root / "history.sqlite3"

    def has_blocking_review(self, ean: str, records: list[ImageRecord]) -> bool:
        if not self.database.is_file():
            raise FileNotFoundError(f"historial_kering_no_disponible: {self.database}")
        with closing(sqlite3.connect(
            f"{self.database.as_uri()}?mode=ro", uri=True, timeout=30,
        )) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT review, created FROM view_reviews WHERE ean=?", (ean,)
            ).fetchone()
        if not row:
            return False
        review = json.loads(row["review"])
        if REVIEW_CONFLICT not in review.get("reasons", []):
            return False
        reviewed = {
            record.origin
            for record in records
            if (
                (record.metadata.get("view_review") or {}).get("reviewer")
                not in {None, "kering-adapter"}
                and record.metadata["view_review"].get("date", "") >= row["created"]
            )
        }
        pending = [
            item for item in review.get("contradictions", [])
            if item.get("url") not in reviewed
        ]
        return bool(pending or not review.get("contradictions"))


@dataclass(frozen=True)
class PublicationEntry:
    ean: str
    market: str
    view: str
    relative_path: str
    source_id: int
    source_path: str
    source_checksum: str
    output_checksum: str
    provider: str
    output_format: str
    action: str
    record: ImageRecord


@dataclass(frozen=True)
class PublicationPlan:
    root: Path
    entries: tuple[PublicationEntry, ...]
    skipped: tuple[dict, ...]
    stale_files: tuple[dict, ...]
    previous_manifest_checksum: str | None

    def report(self) -> dict:
        return {
            "destination": str(self.root),
            "create": sum(entry.action == "create" for entry in self.entries),
            "unchanged": sum(entry.action == "unchanged" for entry in self.entries),
            "skipped": list(self.skipped),
            "stale_files": list(self.stale_files),
            "files": [
                {
                    "path": entry.relative_path,
                    "action": entry.action,
                    "source_sha256": entry.source_checksum,
                    "output_sha256": entry.output_checksum,
                    "provider": entry.provider,
                    "view": entry.view,
                }
                for entry in self.entries
            ],
            "previous_manifest_sha256": self.previous_manifest_checksum,
        }

    def manifest_bytes(self) -> bytes:
        payload = {
            "schema_version": 1,
            "managed_by": MANAGED_BY,
            "files": [
                {
                    "path": entry.relative_path,
                    "ean": entry.ean,
                    "market": entry.market,
                    "view": entry.view,
                    "provider": entry.provider,
                    "source_record_id": entry.source_id,
                    "source": entry.source_path,
                    "source_sha256": entry.source_checksum,
                    "output_sha256": entry.output_checksum,
                    "output_format": entry.output_format,
                }
                for entry in self.entries
            ],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")

    def signature(self) -> str:
        payload = json.dumps(self.report(), ensure_ascii=False, sort_keys=True).encode("utf-8")
        return sha256(payload).hexdigest()


def _root_path(repository: ImageRepositoryReader, root: Path) -> Path:
    destination = Path(root).expanduser().absolute()
    resolved = destination.resolve()
    source = repository.root.resolve()
    if destination != resolved:
        raise ValueError("raiz_publicacion_con_alias_o_enlace")
    if destination == source or destination in source.parents or source in destination.parents:
        raise ValueError("raices_canonica_y_publicacion_solapadas")
    if destination.exists() and not destination.is_dir():
        raise ValueError("raiz_publicacion_no_es_directorio")
    return destination


def _target_path(root: Path, relative_path: str) -> Path:
    target = root / Path(relative_path)
    if target.absolute() != target.resolve():
        raise ValueError(f"destino_con_alias_o_enlace: {relative_path}")
    if root not in target.parents:
        raise ValueError(f"destino_fuera_de_raiz: {relative_path}")
    return target


def _manual_review_is_valid(record: ImageRecord) -> bool:
    review = record.metadata.get("view_review", {})
    reviewer = review.get("reviewer")
    reason = review.get("reason")
    return bool(
        isinstance(reviewer, str) and reviewer.strip() and reviewer != "kering-adapter"
        and isinstance(reason, str) and reason.strip()
        and review.get("view") == record.view
    )


def _has_unresolved_kering_conflict(records: list[ImageRecord]) -> bool:
    for record in records:
        if record.provider != "Kering":
            continue
        detection = record.metadata.get("view_detection", {})
        review = detection.get("review", {})
        if REVIEW_CONFLICT in review.get("reasons", []) and review.get("contradicts_position"):
            if not _manual_review_is_valid(record):
                return True
    return False


def _is_accredited(record: ImageRecord) -> bool:
    if record.market or record.view not in VIEW_NAMES:
        return False
    expected_view = VIEW_NAMES[record.view]
    if record.provider == "Luxoptica":
        return market_view_from_image_name(record.name) == expected_view
    if record.provider != "Kering":
        return False
    if _manual_review_is_valid(record):
        return True
    detection = record.metadata.get("view_detection", {})
    if detection.get("normalized_view") != record.view:
        return False
    if detection.get("rule") == "luxoptica_original_filename_v1":
        return True
    return (
        detection.get("rule") == POSITIONAL_RULE
        and detection.get("provenance") == POSITIONAL_PROVENANCE
    )


def _output_bytes(content: bytes, market: str) -> tuple[bytes, str]:
    _, expected_format = MARKETS[market]
    with Image.open(BytesIO(content)) as image:
        source_format = image.format
        if source_format not in {"JPEG", "PNG", "WEBP"}:
            raise UnsupportedPublicationFormat(f"formato_imagen_no_admitido: {source_format}")
        if market == "Miinto" and source_format == "WEBP":
            raise UnsupportedPublicationFormat("formato_miinto_no_aprobado: WEBP")
        if source_format == expected_format or (market == "Miinto" and source_format == "PNG"):
            return content, source_format
        converted = ImageOps.exif_transpose(image)
        if expected_format == "JPEG":
            if "A" in converted.getbands() or "transparency" in converted.info:
                rgba = converted.convert("RGBA")
                background = Image.new("RGB", rgba.size, "white")
                background.paste(rgba, mask=rgba.getchannel("A"))
                converted = background
            else:
                converted = converted.convert("RGB")
        stream = BytesIO()
        converted.save(stream, format=expected_format, optimize=False, progressive=False)
        return stream.getvalue(), expected_format


def _market_id(value: object, market: str, ean: str) -> str | None:
    if value is None or not str(value).strip():
        return None
    identifier = str(value).strip()
    if not ID_PATTERN.fullmatch(identifier):
        raise ValueError(f"id_comercial_invalido: {market}/{ean}")
    return identifier


def _read_existing_manifest(root: Path) -> tuple[dict[str, str], str | None]:
    path = root / MANIFEST_NAME
    if path.is_symlink():
        raise ValueError("manifiesto_publicacion_con_enlace")
    if not path.exists():
        return {}, None
    try:
        content = path.read_bytes()
        manifest = json.loads(content.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("manifiesto_publicacion_no_legible") from error
    if not isinstance(manifest, dict) or manifest.get("managed_by") != MANAGED_BY \
            or manifest.get("schema_version") != 1:
        raise ValueError("colision_con_manifiesto_no_gestionado")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ValueError("manifiesto_publicacion_invalido")
    managed = _manifest_file_map(root, files)
    return managed, sha256(content).hexdigest()


def _manifest_file_map(root: Path, files: list) -> dict[str, str]:
    managed: dict[str, str] = {}
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("manifiesto_publicacion_invalido")
        relative = item.get("path")
        checksum = item.get("output_sha256")
        if not isinstance(relative, str) or not isinstance(checksum, str) \
                or not re.fullmatch(r"[0-9a-f]{64}", checksum):
            raise ValueError("manifiesto_publicacion_invalido")
        if not re.fullmatch(
            r"(Farfetch/[0-9]{1,20}/[A-Za-z0-9][A-Za-z0-9_-]{0,127}_V[123]\.png"
            r"|Miinto/[0-9]{1,20}/[A-Za-z0-9][A-Za-z0-9_-]{0,127}_V[123]\.jpeg)",
            relative,
        ):
            raise ValueError("ruta_manifiesto_publicacion_invalida")
        target = _target_path(root, relative)
        if relative in managed:
            raise ValueError(f"ruta_duplicada_en_manifiesto: {relative}")
        if target.is_symlink():
            raise ValueError(f"destino_con_enlace: {relative}")
        managed[relative] = checksum
    return managed


def _publication_lock_path(root: Path) -> Path:
    path = root / LOCK_NAME
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("lock_publicacion_invalido")
    return path


def _kering_review_skip_reason(
    ean: str,
    records: list[ImageRecord],
    kering_reviews: KeringReviewReader | None,
) -> str | None:
    kering_records = [record for record in records if record.provider == "Kering"]
    if _has_unresolved_kering_conflict(records):
        return "revision_kering_pendiente"
    if kering_records and kering_reviews is None:
        return "historia_revision_no_disponible"
    if kering_records and kering_reviews.has_blocking_review(ean, records):
        return "revision_kering_pendiente"
    return None


def _select_accredited_views(
    repository: ImageRepositoryReader,
    ean: str,
    records: list[ImageRecord],
    kering_reviews: KeringReviewReader | None,
) -> tuple[dict[str, tuple[ImageRecord, bytes]], list[dict]]:
    skipped: list[dict] = []
    review_skip_reason = _kering_review_skip_reason(ean, records, kering_reviews)
    if review_skip_reason:
        return {}, [{"ean": ean, "reason": review_skip_reason}]

    selected: dict[str, tuple[ImageRecord, bytes]] = {}
    seen_pixels: set[str] = set()
    for record in records:
        if record.market:
            continue
        reason = _record_skip_reason(record)
        if reason:
            skipped.append({"ean": ean, "source": record.name, "reason": reason})
            continue
        content = repository.verified_bytes(record)
        try:
            pixels = image_fingerprint(content)
        except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
            skipped.append({"ean": ean, "source": record.name, "reason": "imagen_no_valida"})
            continue
        if record.view in selected:
            skipped.append({"ean": ean, "source": record.name, "reason": "vista_duplicada"})
            continue
        if pixels in seen_pixels:
            skipped.append({"ean": ean, "source": record.name, "reason": "pixeles_duplicados"})
            continue
        selected[record.view] = (record, content)
        seen_pixels.add(pixels)
    return selected, skipped


def _entries_for_market(
    source_root: Path,
    root: Path,
    ean: str,
    market: str,
    market_id: str,
    selected: dict[str, tuple[ImageRecord, bytes]],
) -> tuple[list[PublicationEntry], list[dict]]:
    extension, _ = MARKETS[market]
    entries = []
    skipped = []
    for view, (record, content) in selected.items():
        try:
            output, output_format = _output_bytes(content, market)
        except UnsupportedPublicationFormat as error:
            skipped.append({
                "ean": ean,
                "market": market,
                "view": view,
                "source": record.name,
                "reason": "formato_no_admitido",
                "detail": str(error),
            })
            continue
        relative = f"{market}/{ean}/{market_id}_{VIEW_NAMES[view]}{extension}"
        target = _target_path(root, relative)
        checksum = sha256(output).hexdigest()
        if target.exists():
            if sha256(target.read_bytes()).hexdigest() != checksum:
                raise ValueError(f"colision_destino: {relative}")
            action = "unchanged"
        else:
            action = "create"
        entries.append(PublicationEntry(
            ean=ean,
            market=market,
            view=view,
            relative_path=relative,
            source_id=record.id,
            source_path=record.path.relative_to(source_root).as_posix(),
            source_checksum=record.checksum,
            output_checksum=checksum,
            provider=record.provider,
            output_format=output_format,
            action=action,
            record=record,
        ))
    return entries, skipped


def _missing_views(ean: str, selected: dict[str, tuple[ImageRecord, bytes]]) -> list[dict]:
    return [
        {"ean": ean, "view": view, "reason": "vista_no_disponible"}
        for view in VIEW_NAMES
        if view not in selected
    ]


def _plan_for_ean(
    repository: ImageRepositoryReader,
    root: Path,
    ean: str,
    records: list[ImageRecord],
    market_ids: dict[str, dict[str, str]],
    kering_reviews: KeringReviewReader | None,
) -> tuple[list[PublicationEntry], list[dict]]:
    selected, skipped = _select_accredited_views(repository, ean, records, kering_reviews)
    skipped.extend(_missing_views(ean, selected))
    ids = market_ids.get(ean, {})
    if not isinstance(ids, dict):
        raise ValueError(f"ids_comerciales_invalidos: {ean}")

    entries = []
    for market in MARKETS:
        market_id = _market_id(ids.get(market), market, ean)
        if market_id is None:
            skipped.extend({
                "ean": ean, "market": market, "view": view, "reason": "id_comercial_pendiente",
            } for view in selected)
            continue
        market_entries, market_skipped = _entries_for_market(
            repository.root, root, ean, market, market_id, selected
        )
        entries.extend(market_entries)
        skipped.extend(market_skipped)
    return entries, skipped


def _record_skip_reason(record: ImageRecord) -> str | None:
    if record.view not in VIEW_NAMES:
        return "vista_no_publicable"
    if not _is_accredited(record):
        return "vista_no_acreditada"
    return None


def create_publication_plan(
    repository: ImageRepositoryReader,
    market_ids_by_ean: dict[str, dict[str, str]],
    destination: Path,
    *,
    kering_reviews: KeringReviewReader | None = None,
) -> PublicationPlan:
    root = _root_path(repository, destination)
    managed_files, previous_manifest_checksum = (
        _read_existing_manifest(root) if root.exists() else ({}, None)
    )

    entries: list[PublicationEntry] = []
    skipped: list[dict] = []
    records_by_ean: dict[str, list[ImageRecord]] = {}
    for record in repository.records():
        records_by_ean.setdefault(record.ean, []).append(record)
    for ean in sorted(records_by_ean):
        validate_ean(ean)
        records = records_by_ean[ean]
        ean_entries, ean_skipped = _plan_for_ean(
            repository, root, ean, records, market_ids_by_ean, kering_reviews
        )
        entries.extend(ean_entries)
        skipped.extend(ean_skipped)

    relative_paths: dict[str, str] = {}
    for entry in entries:
        target = _target_path(root, entry.relative_path)
        key = os.path.normcase(str(target))
        previous = relative_paths.setdefault(key, entry.output_checksum)
        if previous != entry.output_checksum:
            raise ValueError(f"colision_en_plan: {entry.relative_path}")
    entries.sort(key=lambda entry: (entry.market, entry.ean, entry.view))
    wanted_paths = {entry.relative_path for entry in entries}
    stale_files = []
    for relative, expected_checksum in managed_files.items():
        if relative in wanted_paths:
            continue
        target = _target_path(root, relative)
        if target.exists():
            actual_checksum = sha256(target.read_bytes()).hexdigest()
            stale_files.append({
                "path": relative,
                "expected_sha256": expected_checksum,
                "actual_sha256": actual_checksum,
                "reason": "stale" if actual_checksum == expected_checksum else "modified",
            })
    return PublicationPlan(
        root, tuple(entries), tuple(skipped), tuple(stale_files), previous_manifest_checksum
    )


def _validate_sources(repository: ImageRepositoryReader, plan: PublicationPlan) -> None:
    for entry in plan.entries:
        content = repository.verified_bytes(entry.record)
        output, _ = _output_bytes(content, entry.market)
        if sha256(output).hexdigest() != entry.output_checksum:
            raise ValueError(f"fuente_cambiada_durante_publicacion: {entry.relative_path}")


def _validate_simulated_destinations(root: Path, plan: PublicationPlan) -> None:
    _, manifest_checksum = _read_existing_manifest(root)
    if manifest_checksum != plan.previous_manifest_checksum:
        raise ValueError("manifiesto_cambio_despues_de_simulacion")
    for entry in plan.entries:
        target = _target_path(root, entry.relative_path)
        if (entry.action == "create" and target.exists()) \
                or (entry.action == "unchanged" and not target.exists()):
            raise ValueError(f"destino_cambio_despues_de_simulacion: {entry.relative_path}")
        if target.exists() and sha256(target.read_bytes()).hexdigest() != entry.output_checksum:
            raise ValueError(f"colision_destino: {entry.relative_path}")


def _atomic_publication_write(path: Path, content: bytes, root: Path) -> bool:
    if path.absolute() != path.resolve() or root not in path.parents:
        raise ValueError(f"destino_con_alias_o_fuera_de_raiz: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.absolute() != path.resolve() or root not in path.parents:
        raise ValueError(f"destino_con_alias_o_fuera_de_raiz: {path}")
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".market-publication-", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if path == root / MANIFEST_NAME:
            os.replace(temporary, path)
            return True
        else:
            try:
                os.link(temporary, path)
            except FileExistsError:
                if sha256(path.read_bytes()).digest() != sha256(content).digest():
                    raise ValueError(f"colision_destino_durante_escritura: {path.relative_to(root)}")
                return False
            return True
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def publish_market_images(repository: ImageRepositoryReader, plan: PublicationPlan) -> dict:
    root = _root_path(repository, plan.root)
    if plan.stale_files:
        raise ValueError("salidas_obsoletas_requieren_revision_manual")
    _validate_sources(repository, plan)
    root.mkdir(parents=True, exist_ok=True)
    # This lock is intentionally inside the separately approved publication root.
    with FileLock(_publication_lock_path(root), timeout=120):
        _validate_simulated_destinations(root, plan)
        _validate_sources(repository, plan)

        created = 0
        unchanged = 0
        for entry in plan.entries:
            target = _target_path(root, entry.relative_path)
            content = repository.verified_bytes(entry.record)
            output, _ = _output_bytes(content, entry.market)
            if sha256(output).hexdigest() != entry.output_checksum:
                raise ValueError(f"fuente_cambiada_durante_publicacion: {entry.relative_path}")
            if target.exists():
                unchanged += 1
                continue
            if _atomic_publication_write(target, output, root):
                created += 1
            else:
                unchanged += 1

        manifest_path = root / MANIFEST_NAME
        manifest = plan.manifest_bytes()
        if manifest_path.exists() and manifest_path.read_bytes() == manifest:
            manifest_unchanged = True
        else:
            _read_existing_manifest(root)
            _atomic_publication_write(manifest_path, manifest, root)
            manifest_unchanged = False
    return {
        "created": created,
        "unchanged": unchanged,
        "manifest_unchanged": manifest_unchanged,
        "skipped": len(plan.skipped),
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Simula o publica copias comerciales verificadas.")
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--destination", type=Path, default=DEFAULT_PUBLICATION_ROOT)
    parser.add_argument("--apply", action="store_true", help="Escribe copias locales; por defecto solo simula.")
    parser.add_argument("--expected-plan-sha256", help="Firma obtenida en una simulacion previa.")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.apply and not args.expected_plan_sha256:
        raise ValueError("--apply requiere --expected-plan-sha256 de una simulacion previa")
    if not args.apply and args.expected_plan_sha256:
        raise ValueError("--expected-plan-sha256 solo se admite junto con --apply")
    source_root = args.source_root.resolve()
    destination = args.destination.resolve()
    if source_root != DEFAULT_SOURCE_ROOT.resolve() or destination != DEFAULT_PUBLICATION_ROOT.resolve():
        raise ValueError("El comando solo admite las raices de publicacion autorizadas")
    if not source_root.is_dir():
        raise FileNotFoundError(f"origen_canonico_no_disponible: {source_root}")

    repository = ReadOnlyImageRepository(source_root)
    eans = {record.ean for record in repository.records() if not record.market}
    from graph_mail_downloader import _get_market_ids_by_ean

    ids = _get_market_ids_by_ean(eans)
    plan = create_publication_plan(
        repository, ids, destination, kering_reviews=ReadOnlyKeringReviewReader()
    )
    if args.apply:
        if plan.signature() != args.expected_plan_sha256:
            raise ValueError("el plan actual no coincide con la simulacion previa")
        result = publish_market_images(repository, plan)
        print(json.dumps({
            "mode": "apply", "plan_sha256": plan.signature(), **result,
        }, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({
            "mode": "dry-run", "plan_sha256": plan.signature(), **plan.report(),
        }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
