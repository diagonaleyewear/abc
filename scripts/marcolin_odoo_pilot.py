"""Autonomous, strictly read-only Marcolin pilot audit (#59).

Standalone on purpose: it only needs ``sqlalchemy``, ``kering_jobs`` and ``image_repository`` (the modules
present in production) and can run from stdin, e.g.

  docker exec -i -w /app <container> python - --partner 573 --upc-file /app/data/in/upcs.txt \
      --out /app/data/audits < scripts/marcolin_odoo_pilot.py

It does NOT: write to Odoo, instantiate ImageRepository (its constructor creates directories and runs DDL),
create or modify the SQLite catalog, contact the Marcolin portal, create requests or save supplier associations.
Reports go only to ``--out`` (a writable volume; the container rootfs is read-only).

Odoo: PostgreSQL ``SET TRANSACTION READ ONLY`` + statement timeout + rollback, bounded by ``--max-lines``.
Catalog: ``file:<path>?mode=ro`` + ``PRAGMA query_only``; never ``immutable=1`` (that would hide rows that only
live in an active WAL). A missing/unreadable/incompatible catalog is reported as ``desconocida``, never as absent.
Without ``--verify-files`` coverage rests on catalog metadata only and is labelled as such.

Exit codes: 0 complete; 2 Odoo unavailable / invalid input; 3 partner is not the expected supplier;
4 report not written; 5 missing dependency; 6 partial (catalog blocker, report written).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib
import json
import os
import re
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SCHEMA = "marcolin-pilot-audit/2"
REQUIRED_MODULES = ("sqlalchemy", "kering_jobs", "image_repository")
EAN_LENGTHS = (8, 12, 13, 14)
CONFIRMED_STATES = ("purchase", "done")
DEFAULT_REQUIRED_VIEWS = ("frontal", "lateral", "perspectiva")  # repository taxonomy, not a Marcolin policy
DEFAULT_MAX_LINES, HARD_MAX_LINES = 2000, 10000
MAX_FILE_BYTES = 30 * 1024 * 1024
CATALOG_TABLES = {
    "assets": {"id", "ean", "checksum", "pixels", "path"},
    "representations": {"id", "asset_id", "provider", "origin", "view", "market", "name", "date", "metadata"},
}
RANK = {"sin_registros": 0, "sin_vistas_validas": 0, "parciales": 1, "desconocida": 2}
REASONS = {
    "sin_registros": "sin registros en el catálogo consultado (no prueba ausencia de archivos sin catalogar)",
    "sin_vistas_validas": "hay registros pero ninguna vista válida",
    "parciales": "vistas incompletas respecto a las vistas requeridas indicadas",
    "desconocida": "cobertura desconocida: catálogo no consultable",
}
FAMILY = """po.partner_id IN (
    SELECT contact.id FROM res_partner contact JOIN res_partner selected ON selected.id = :partner
    WHERE COALESCE(contact.commercial_partner_id, contact.id) = COALESCE(selected.commercial_partner_id, selected.id))"""


class Blocker(Exception):
    """A concrete, reportable stop. ``detail`` never carries secrets or raw driver messages."""

    def __init__(self, code: str, detail=None):
        super().__init__(code)
        self.code, self.detail = code, detail


# --- dependencies -------------------------------------------------------------------------

def require_modules(names=REQUIRED_MODULES) -> dict:
    loaded, missing = {}, []
    for name in names:
        try:
            loaded[name] = importlib.import_module(name)
        except ImportError as error:
            missing.append(name if getattr(error, "name", None) in (None, name) else f"{name} ({error.name})")
    if missing:
        raise Blocker("dependencia_ausente", {"missing": missing})
    return loaded


# --- codes (UPC-12 / EAN kept literally) --------------------------------------------------

def gtin_valid(code: str) -> bool:
    if not (code.isascii() and code.isdigit() and len(code) in EAN_LENGTHS):
        return False
    digits = [int(ch) for ch in reversed(code)]
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(digits[1:]))
    return (10 - total % 10) % 10 == digits[0]


def code_key(code: str) -> str:
    return code.zfill(14)


def code_variants(code: str) -> list[str]:
    """The literal code plus zero-padded/unpadded equivalents, for lookups only."""
    key = code_key(code)
    forms = [key[-n:] for n in EAN_LENGTHS if not key[:14 - n].strip("0")]
    return [code, *[f for f in dict.fromkeys(forms) if f != code]]


def split_codes(*texts: str) -> list[str]:
    return [t for text_ in texts for t in re.split(r"[\s,;]+", text_ or "") if t]


def normalize_codes(tokens) -> dict:
    valid, invalid, equivalent, seen_literal, seen_key, repeated = [], [], [], set(), {}, 0
    for token in tokens:
        if token in seen_literal:
            repeated += 1
            continue
        seen_literal.add(token)
        if not gtin_valid(token):
            invalid.append(token)
        elif code_key(token) in seen_key:
            equivalent.append({"kept": seen_key[code_key(token)], "equivalent": token})
        else:
            seen_key[code_key(token)] = token
            valid.append(token)
    return {"valid": valid, "invalid": invalid, "repeated": repeated, "equivalent_forms": equivalent}


# --- portal grouping (model/colour reference as returned by the portal) -------------------

def group_by_portal_ref(requested: list[str], payload: dict) -> dict:
    """Keep every UPC under the portal's own reference. Status is recorded verbatim, never used to drop codes.

    payload: {"groups": [{"portal_ref": "...", "status": "...", "upcs": ["..."]}]} supplied by the operator.
    """
    wanted = {code_key(c): c for c in requested}
    groups, assigned = [], {}
    for entry in payload.get("groups", []):
        upcs = [str(u) for u in entry.get("upcs", [])]
        ref = str(entry.get("portal_ref", ""))
        groups.append({"portal_ref": ref, "status": str(entry.get("status", "")), "upcs": upcs})
        for upc in upcs:
            assigned.setdefault(code_key(upc), []).append(ref)
    return {
        "groups": groups,
        "statuses": {s: sum(1 for g in groups if g["status"] == s) for s in sorted({g["status"] for g in groups})},
        "unassigned_upcs": [c for k, c in wanted.items() if k not in assigned],
        "multi_assigned": {k: refs for k, refs in assigned.items() if len(refs) > 1},
        "not_requested": sorted({u for g in groups for u in g["upcs"] if code_key(u) not in wanted}),
        "empty_groups": [g["portal_ref"] for g in groups if not g["upcs"]],
        "ref_by_upc": {u: g["portal_ref"] for g in groups for u in g["upcs"]},
    }


# --- catalog (read-only) ------------------------------------------------------------------

def resolve_catalog(explicit: str | None, env=None) -> Path | None:
    env = os.environ if env is None else env
    if explicit:
        return Path(explicit)
    base = (env.get("IMAGE_REPOSITORY_STATE_ROOT") or "").strip() or (env.get("IMAGE_REPOSITORY_ROOT") or "").strip()
    return Path(base) / ".catalog.sqlite3" if base else None


def open_catalog(path: Path | None):
    """Open an existing catalog read-only. Never creates files, directories, tables or locks."""
    if path is None:
        raise Blocker("catalogo_no_configurado")
    if not path.is_file():
        raise Blocker("catalogo_inexistente")
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
                raise Blocker("esquema_incompatible", {"table": table, "missing": sorted(required - columns)})
    except sqlite3.Error as error:
        if connection:
            connection.close()
        raise Blocker("catalogo_inaccesible", {"error_class": error.__class__.__name__})
    except Blocker:
        connection.close()
        raise
    wal = Path(str(path) + "-wal")
    return connection, {"journal_mode": mode, "wal_file_present": wal.exists(),
                        "wal_bytes": wal.stat().st_size if wal.exists() else 0}


def fetch_records(connection, codes: list[str]) -> dict[str, list[dict]]:
    """Catalog rows by zero-insensitive code key. Codes are looked up literally and in equivalent forms."""
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
        raise Blocker("catalogo_inaccesible", {"error_class": error.__class__.__name__})
    return found


def check_record(record: dict, helpers: dict, image_root: Path | None, verify: bool):
    """-> (view, pixels, issue). Same path/staging/view rules as ImageRepository; files only read when verifying."""
    relative = Path(record["path"])
    if relative.is_absolute() or ".." in relative.parts or "staging" in relative.parts:
        return None, None, "ruta_excluida"
    view = helpers["aliases"].get(record["view"], record["view"])
    if view not in helpers["views"]:
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
        pixels = helpers["fingerprint"](content)
    except Exception:  # image libraries raise several unrelated types for corrupt files
        return None, None, "imagen_invalida"
    if not record["pixels"] or pixels != record["pixels"]:
        return None, None, "huella_distinta"
    return view, pixels, None


def coverage(records: list[dict], required, helpers, image_root, verify) -> dict:
    views, issues, seen = {}, [], set()
    for record in records:
        view, pixels, issue = check_record(record, helpers, image_root, verify)
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
    return {"label": label, "basis": "archivos_verificados" if verify else "solo_metadatos_del_catalogo",
            "views": sorted(views), "missing_views": missing, "records": len(records), "issues": issues}


# --- Odoo (read-only) ---------------------------------------------------------------------

def interval(start: date, end: date):
    if end < start:
        raise ValueError("intervalo_invalido")
    madrid = ZoneInfo("Europe/Madrid")
    return tuple(datetime.combine(day, time.min, madrid).astimezone(timezone.utc).replace(tzinfo=None)
                 for day in (start, end + timedelta(days=1)))


def odoo_read(engine, partner_id: int, start: date, end: date, *, codes=None, order_names=None,
              max_lines: int = DEFAULT_MAX_LINES, timeout_ms: int = 20000) -> dict:
    from sqlalchemy import bindparam, text  # noqa: PLC0415
    lower, upper = interval(start, end)
    max_lines = max(1, min(int(max_lines), HARD_MAX_LINES))
    parameters = {"partner": int(partner_id), "states": list(CONFIRMED_STATES), "lower": lower, "upper": upper,
                  "limit": max_lines + 1}
    extra, expanding = "", ["states"]
    if codes:
        parameters["codes"] = list(dict.fromkeys(v for c in codes for v in code_variants(c)))
        extra += " AND pp.barcode IN :codes"
        expanding.append("codes")
    if order_names:
        parameters["orders"] = list(dict.fromkeys(order_names))
        extra += " AND po.name IN :orders"
        expanding.append("orders")
    lines_query = text(f"""
        SELECT po.id AS order_id, po.name AS order_name, po.date_order, po.state, partner.name AS supplier,
               pp.barcode, pp.default_code, pol.name AS line_name
        FROM purchase_order po JOIN res_partner partner ON partner.id = po.partner_id
        JOIN purchase_order_line pol ON pol.order_id = po.id
          AND COALESCE(pol.display_type, '') = '' AND pol.product_id IS NOT NULL
        JOIN product_product pp ON pp.id = pol.product_id
        WHERE {FAMILY} AND po.state IN :states AND po.date_order >= :lower AND po.date_order < :upper{extra}
        ORDER BY po.date_order DESC, po.id DESC, pol.id LIMIT :limit""").bindparams(
        *[bindparam(name, expanding=True) for name in expanding])
    family_query = text("""
        SELECT contact.id, contact.name, contact.active, contact.supplier_rank, contact.commercial_partner_id
        FROM res_partner contact JOIN res_partner selected ON selected.id = :partner
        WHERE COALESCE(contact.commercial_partner_id, contact.id) = COALESCE(selected.commercial_partner_id, selected.id)
        ORDER BY contact.id""")
    states_query = text(f"SELECT po.state, COUNT(*) FROM purchase_order po WHERE {FAMILY} GROUP BY po.state")
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                if engine.dialect.name == "postgresql":
                    connection.execute(text("SET TRANSACTION READ ONLY"))
                    connection.execute(text(f"SET LOCAL statement_timeout = {int(timeout_ms)}"))
                family = [dict(r) for r in connection.execute(family_query, {"partner": int(partner_id)}).mappings()]
                by_state = {r[0]: r[1] for r in connection.execute(states_query, {"partner": int(partner_id)})}
                rows = [dict(r) for r in connection.execute(lines_query, parameters).mappings()]
            finally:
                transaction.rollback()
    except Exception as error:  # class only: driver messages can embed connection details
        raise Blocker("odoo_no_disponible", {"error_class": error.__class__.__name__})
    main = next((p for p in family if p["id"] == int(partner_id)), None)
    return {"family": family, "orders_by_state": by_state, "partner_name": main["name"] if main else None,
            "lines": rows[:max_lines], "truncated": len(rows) > max_lines}


# --- report -------------------------------------------------------------------------------

def repository_helpers() -> dict:
    module = require_modules(("image_repository",))["image_repository"]
    return {"views": tuple(module.IMAGE_VIEWS), "aliases": dict(module.VIEW_ALIASES),
            "fingerprint": module.image_fingerprint}


def default_engine():
    return require_modules(("sqlalchemy", "kering_jobs"))["kering_jobs"].db_engine()


def date_digits(value: str) -> int:
    return int(value.replace("-", "") or 0)


def build_report(args, *, engine=None, helpers=None, today: date | None = None) -> tuple[dict, int]:
    end = today or date.today()
    start = end - timedelta(days=args.days)
    report = {"schema": SCHEMA, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "scope": {"odoo": "read_only_rollback", "catalog": "mode=ro_query_only", "portal_contacted": False,
                        "requests_created": 0, "association_saved": False,
                        "coverage_basis": "archivos_verificados" if args.verify_files else "solo_metadatos_del_catalogo"},
              "period": [start.isoformat(), end.isoformat()], "partner_id": args.partner}
    explicit = split_codes(*(args.upc or []), *(Path(p).read_text(encoding="utf-8") for p in (args.upc_file or [])))
    if len(explicit) > args.max_codes:
        report["blocker"] = {"code": "demasiados_codigos", "limit": args.max_codes, "received": len(explicit)}
        return report, 2
    requested = normalize_codes(explicit)
    report["input"] = {"mode": "codigos_indicados" if explicit else "descubrimiento_por_pedidos",
                       "received": len(explicit), "valid": len(requested["valid"]), "invalid": requested["invalid"],
                       "repeated": requested["repeated"], "equivalent_forms": requested["equivalent_forms"]}
    if explicit and not requested["valid"]:
        report["blocker"] = {"code": "sin_codigos_validos"}
        return report, 2
    odoo = odoo_read(engine if engine is not None else default_engine(), args.partner, start, end,
                     codes=requested["valid"] or None, order_names=args.order_name,
                     max_lines=args.max_lines, timeout_ms=args.timeout_ms)
    expected = (args.expect_name or "marcolin").lower()
    by_name = bool(odoo["partner_name"] and expected in odoo["partner_name"].lower())
    report["identity"] = {"found": odoo["partner_name"] is not None, "name_matches_expected": by_name,
                          "family_size": len(odoo["family"]), "family": odoo["family"],
                          "orders_by_state": odoo["orders_by_state"]}
    if not by_name:
        report["blocker"] = {"code": "partner_no_coincide"}
        return report, 3
    candidates = {}
    for line in odoo["lines"]:
        barcode = (line["barcode"] or "").strip()
        if not barcode:
            continue
        info = candidates.setdefault(code_key(barcode), {"upc": barcode, "refs": set(), "orders": [], "date": "",
                                                         "product": "", "supplier": ""})
        info["refs"].add((line["default_code"] or "").strip())
        info["orders"].append(line["order_name"])
        info["date"] = max(info["date"], str(line["date_order"])[:10])
        info["product"] = info["product"] or line["line_name"] or ""
        info["supplier"] = info["supplier"] or line["supplier"] or ""
    report["odoo"] = {"lines_read": len(odoo["lines"]), "truncated": odoo["truncated"],
                      "lines_without_barcode": sum(1 for line in odoo["lines"] if not (line["barcode"] or "").strip())}
    if requested["valid"]:
        codes = requested["valid"]
        report["odoo"]["requested_found"] = {c: code_key(c) in candidates for c in codes}
    else:
        discovered = normalize_codes(c["upc"] for c in candidates.values())
        report["input"].update({"valid": len(discovered["valid"]), "invalid": discovered["invalid"]})
        codes = discovered["valid"][:args.max_codes]
    if args.portal_groups:
        payload = json.loads(Path(args.portal_groups).read_text(encoding="utf-8"))
        report["portal_groups"] = group_by_portal_ref(codes, payload)
    return finish_with_catalog(report, args, codes, candidates, helpers)


def finish_with_catalog(report, args, codes, candidates, helpers):
    helpers = helpers or repository_helpers()
    required = tuple(args.required_view or DEFAULT_REQUIRED_VIEWS)
    root_text = args.image_root or os.environ.get("IMAGE_REPOSITORY_ROOT", "")
    image_root = Path(root_text) if root_text else None
    partial, labels = False, {}
    try:
        if args.verify_files and image_root is None:
            raise Blocker("raiz_imagenes_no_configurada")
        connection, info = open_catalog(resolve_catalog(args.catalog))
        try:
            report["catalog"] = {"status": "consultado", **info, "required_views": list(required)}
            found = fetch_records(connection, codes)
        finally:
            connection.close()
        for code in codes:
            labels[code] = coverage(found.get(code_key(code), []), required, helpers, image_root, args.verify_files)
    except Blocker as blocker:
        partial = True
        report["catalog"] = {"status": "inaccesible", "blocker": blocker.code, "detail": blocker.detail,
                             "note": "cobertura desconocida: no equivale a imágenes ausentes"}
        labels = {c: {"label": "desconocida", "basis": "catalogo_no_consultado", "views": [], "missing_views": [],
                      "records": 0, "issues": []} for c in codes}
    report["coverage"] = labels
    report["coverage_summary"] = {k: sum(1 for v in labels.values() if v["label"] == k)
                                  for k in ("completas", "parciales", "sin_registros", "sin_vistas_validas",
                                            "desconocida")}
    refs = (report.get("portal_groups") or {}).get("ref_by_upc", {})
    ranked = sorted((c for c in codes if labels[c]["label"] in RANK and code_key(c) in candidates),
                    key=lambda c: (RANK[labels[c]["label"]], -date_digits(candidates[code_key(c)]["date"])))
    report["proposal"] = [{
        "UPC": c, "Referencia": ", ".join(sorted(r for r in candidates[code_key(c)]["refs"] if r)),
        "Pedido": ", ".join(dict.fromkeys(candidates[code_key(c)]["orders"])), "Fecha": candidates[code_key(c)]["date"],
        "Producto": candidates[code_key(c)]["product"], "Proveedor": candidates[code_key(c)]["supplier"],
        "Referencia_portal": refs.get(c, ""), "Cobertura": labels[c]["label"], "Base_cobertura": labels[c]["basis"],
        "Motivo": REASONS[labels[c]["label"]]} for c in ranked[:max(1, min(int(args.size), 10))]]
    report["status"] = "propuesta_pendiente_de_aprobacion"
    if partial:
        report["blocker"] = {"code": report["catalog"]["blocker"], "note": "informe parcial"}
    return report, 6 if partial else 0


def write_report(report: dict, out_dir: Path) -> Path:
    """Exclusive, owner-only files in an explicitly allowed volume. Never overwrites."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = out_dir / f"marcolin_pilot_{stamp}_{os.urandom(3).hex()}.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False, default=sorted)
    if report.get("proposal"):
        descriptor = os.open(path.with_suffix(".csv"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(report["proposal"][0]))
            writer.writeheader()
            writer.writerows(report["proposal"])
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--partner", type=int, default=573)
    parser.add_argument("--expect-name", default="marcolin", help="fragment the Odoo partner name must contain")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--upc", action="append", help="code to confirm (literal); repeatable")
    parser.add_argument("--upc-file", action="append", help="file with codes separated by newlines/commas")
    parser.add_argument("--order-name", action="append", help="restrict to purchase order names; repeatable")
    parser.add_argument("--portal-groups", help="JSON with the portal's model/colour groups (kept outside GitHub)")
    parser.add_argument("--catalog", help="path to .catalog.sqlite3 (default: state/images root from env)")
    parser.add_argument("--image-root", help="images root; required with --verify-files")
    parser.add_argument("--verify-files", action="store_true", help="read files and verify sha256/fingerprint")
    parser.add_argument("--required-view", action="append", help="views counted as complete (taxonomy, not policy)")
    parser.add_argument("--size", type=int, default=8)
    parser.add_argument("--max-codes", type=int, default=300)
    parser.add_argument("--max-lines", type=int, default=DEFAULT_MAX_LINES)
    parser.add_argument("--timeout-ms", type=int, default=20000)
    parser.add_argument("--out", help="writable output directory (or env MARCOLIN_AUDIT_OUT)")
    return parser


def summary(report: dict) -> dict:
    keep = ("schema", "generated_at", "scope", "period", "partner_id", "blocker", "status", "coverage_summary")
    out = {k: report[k] for k in keep if k in report}
    out["catalog"] = {k: v for k, v in report.get("catalog", {}).items() if k in ("status", "blocker", "journal_mode")}
    out["input"] = {k: (len(v) if isinstance(v, list) else v) for k, v in report.get("input", {}).items()}
    out["proposal_size"] = len(report.get("proposal", []))
    return out


def main(argv=None, *, engine=None, helpers=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        require_modules()
        report, code = build_report(args, engine=engine, helpers=helpers)
    except Blocker as blocker:
        report = {"schema": SCHEMA, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "partner_id": args.partner, "blocker": {"code": blocker.code, "detail": blocker.detail},
                  "scope": {"portal_contacted": False, "requests_created": 0, "association_saved": False}}
        code = 5 if blocker.code == "dependencia_ausente" else 2
    out_dir = args.out or os.environ.get("MARCOLIN_AUDIT_OUT")
    path = None
    if out_dir:
        try:
            path = write_report(report, Path(out_dir))
        except OSError as error:
            report["blocker"] = {"code": "salida_no_escribible", "error_class": error.__class__.__name__}
            code = 4
    else:
        code = code or 4
    print(json.dumps(summary(report), indent=2, ensure_ascii=False, default=sorted))
    print(f"Informe: {path}" if path else "Informe no guardado: indica --out en un volumen con escritura")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
