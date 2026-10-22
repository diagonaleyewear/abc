"""Strictly read-only view of the image repository catalog (#59).

`ImageRepository(...)` creates directories, takes a lock and runs DDL, so it cannot be used to *look* at a catalog
that must not be touched (audits, previews). This module opens an existing SQLite catalog with
`file:<path>?mode=ro` and `PRAGMA query_only`; it never creates files or tables, never checkpoints and never uses
`immutable=1`, so rows that only live in an active WAL are visible.

Coverage labels:
  completas / parciales        – catalog has valid views for every / some of the required views
  sin_vistas_validas           – records exist but none is a valid view (bad path, staging, unknown view, ...)
  sin_registros                – nothing in the catalog (does NOT prove that uncatalogued files do not exist)
  desconocida                  – the catalog could not be read; never to be read as "no images"
`basis` states whether a label rests on catalog metadata only or on files that were opened and verified
(inside the root, size, SHA-256 and pixel fingerprint, duplicates).
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

from image_repository import IMAGE_VIEWS, VIEW_ALIASES, image_fingerprint

EAN_LENGTHS = (8, 12, 13, 14)
MAX_FILE_BYTES = 30 * 1024 * 1024
DEFAULT_REQUIRED_VIEWS = ("frontal", "lateral", "perspectiva")  # repository taxonomy, not a provider policy
LABELS = ("completas", "parciales", "sin_registros", "sin_vistas_validas", "desconocida")
BASIS_METADATA, BASIS_FILES, BASIS_NONE = "solo_metadatos_del_catalogo", "archivos_verificados", "catalogo_no_consultado"
CATALOG_TABLES = {
    "assets": {"id", "ean", "checksum", "pixels", "path"},
    "representations": {"id", "asset_id", "provider", "origin", "view", "market", "name", "date", "metadata"},
}


class CatalogUnavailable(Exception):
    """The catalog cannot be read. ``detail`` never carries paths from the driver or secrets."""

    def __init__(self, code: str, detail=None):
        super().__init__(code)
        self.code, self.detail = code, detail


def code_key(code: str) -> str:
    return code.zfill(14)


def code_variants(code: str) -> list[str]:
    """The literal code plus zero-padded/unpadded equivalents, for lookups only."""
    key = code_key(code)
    forms = [key[-n:] for n in EAN_LENGTHS if not key[:14 - n].strip("0")]
    return [code, *[f for f in dict.fromkeys(forms) if f != code]]


def resolve_catalog(explicit=None, env=None) -> Path | None:
    env = os.environ if env is None else env
    if explicit:
        return Path(explicit)
    base = (env.get("IMAGE_REPOSITORY_STATE_ROOT") or "").strip() or (env.get("IMAGE_REPOSITORY_ROOT") or "").strip()
    return Path(base) / ".catalog.sqlite3" if base else None


def open_catalog(path: Path | None):
    """Open an existing catalog read-only -> (connection, info). Never creates files, directories or tables."""
    if path is None:
        raise CatalogUnavailable("catalogo_no_configurado")
    path = Path(path)
    if not path.is_file():
        raise CatalogUnavailable("catalogo_inexistente")
    connection = None
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        for table, required in CATALOG_TABLES.items():
            columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
            if required - columns:
                raise CatalogUnavailable("esquema_incompatible", {"table": table, "missing": sorted(required - columns)})
    except sqlite3.Error as error:
        if connection:
            connection.close()
        raise CatalogUnavailable("catalogo_inaccesible", {"error_class": error.__class__.__name__})
    except CatalogUnavailable:
        connection.close()
        raise
    wal = Path(str(path) + "-wal")
    return connection, {"journal_mode": mode, "wal_file_present": wal.exists(),
                        "wal_bytes": wal.stat().st_size if wal.exists() else 0}


def fetch_records(connection, codes) -> dict[str, list[dict]]:
    """Catalog rows by zero-insensitive code key; codes are looked up literally and in equivalent forms."""
    forms = list(dict.fromkeys(v for c in codes for v in code_variants(c)))
    found: dict[str, list[dict]] = {}
    try:
        for start in range(0, len(forms), 400):
            chunk = forms[start:start + 400]
            rows = connection.execute(
                "SELECT r.id AS record_id, r.provider, r.origin, r.view, r.market, r.name, r.date, "
                "a.ean, a.checksum, a.pixels, a.path FROM representations r JOIN assets a ON a.id = r.asset_id "
                f"WHERE a.ean IN ({','.join('?' for _ in chunk)}) ORDER BY r.id", chunk).fetchall()
            for row in rows:
                found.setdefault(code_key(row["ean"]), []).append(dict(row))
    except sqlite3.Error as error:
        raise CatalogUnavailable("catalogo_inaccesible", {"error_class": error.__class__.__name__})
    return found


def check_record(record: dict, image_root: Path | None, verify: bool):
    """-> (view, pixels, issue). Same path/staging/view rules as ImageRepository; files only read when verifying."""
    relative = Path(record["path"])
    if relative.is_absolute() or ".." in relative.parts or "staging" in relative.parts:
        return None, None, "ruta_excluida"
    view = VIEW_ALIASES.get(record["view"], record["view"])
    if view not in IMAGE_VIEWS:
        return None, None, "vista_no_valida"
    if not verify:
        return view, record["pixels"], None
    root = image_root.resolve()
    target = (root / relative).resolve()
    if root not in target.parents:
        return None, None, "ruta_fuera_del_repositorio"
    if not target.is_file():
        return None, None, "archivo_ausente"
    if target.stat().st_size > MAX_FILE_BYTES:
        return None, None, "archivo_demasiado_grande"
    content = target.read_bytes()
    if hashlib.sha256(content).hexdigest() != record["checksum"]:
        return None, None, "checksum_incorrecto"
    try:
        pixels = image_fingerprint(content)
    except Exception:  # image libraries raise several unrelated types for corrupt files
        return None, None, "imagen_invalida"
    if not record["pixels"] or pixels != record["pixels"]:
        return None, None, "huella_distinta"
    return view, pixels, None


def coverage(records: list[dict], required, image_root: Path | None, verify: bool) -> dict:
    views, issues, seen = {}, [], set()
    for record in records:
        view, pixels, issue = check_record(record, image_root, verify)
        if issue:
            issues.append({"record_id": record["record_id"], "issue": issue})
        elif verify and pixels in seen:
            issues.append({"record_id": record["record_id"], "issue": "huella_duplicada"})
        elif view not in views:
            views[view] = record["record_id"]
            seen.add(pixels)
    missing = [v for v in required if v not in views]
    label = ("completas" if not missing else "parciales" if views else
             "sin_registros" if not records else "sin_vistas_validas")
    return {"label": label, "basis": BASIS_FILES if verify else BASIS_METADATA, "views": sorted(views),
            "missing_views": missing, "records": len(records), "issues": issues}


def unknown_coverage() -> dict:
    return {"label": "desconocida", "basis": BASIS_NONE, "views": [], "missing_views": [], "records": 0, "issues": []}


def catalog_coverage(codes, *, catalog=None, required_views=DEFAULT_REQUIRED_VIEWS, image_root=None,
                     verify_files: bool = False, env=None) -> dict:
    """Coverage per code without ever initialising the catalog.

    -> {"catalog": {"status": "consultado"|"inaccesible", ...}, "coverage": {code: {...}}}
    An inaccessible catalog yields `desconocida` for every code, never `sin_registros`.
    """
    codes, required = list(dict.fromkeys(codes)), tuple(required_views)
    try:
        if verify_files and image_root is None:
            raise CatalogUnavailable("raiz_imagenes_no_configurada")
        connection, info = open_catalog(resolve_catalog(catalog, env))
        try:
            found = fetch_records(connection, codes)
        finally:
            connection.close()
        root = Path(image_root) if image_root else None
        return {"catalog": {"status": "consultado", **info, "required_views": list(required)},
                "coverage": {c: coverage(found.get(code_key(c), []), required, root, verify_files) for c in codes}}
    except CatalogUnavailable as blocker:
        return {"catalog": {"status": "inaccesible", "blocker": blocker.code, "detail": blocker.detail,
                            "note": "cobertura desconocida: no equivale a imágenes ausentes"},
                "coverage": {c: unknown_coverage() for c in codes}}
