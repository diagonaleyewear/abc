"""Manual tools of the price comparator: JSON observations, reference search, catalogue sync, coverage.

Nothing here is scheduled or networked except ``sync-catalog``, which reads Odoo read-only.
``import`` and ``sync-catalog`` simulate on a copy of the database unless ``--apply`` is given.

    python price_observations.py import observaciones.json            # dry run
    python price_observations.py import observaciones.json --apply
    python price_observations.py search "BOTTEGA VENETA BV1274S 005"
    python price_observations.py sync-catalog [--apply]
    python price_observations.py coverage [--load docs/Precios/cobertura_competidores.json] [--apply]
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
from typing import Any, Callable, Iterable

import pandas as pd

from price_comparator import (
    DEFAULT_MARKET, ImportReport, PriceComparatorStore, normalize_gtin, resolve_database_path,
)
from price_references import ReferenceIndex, search_plan

SCHEMA = "cloud-price-observations/1"
# estado of the observation -> match state used when it carries no price
NO_PRICE_STATES = {"no_encontrada": "not_found", "error": "error", "bloqueada": "error",
                   "sin_conector": "not_found"}
_DESCRIPTION = re.compile(r"\s*\(.*\)\s*$")


def _clean(value: Any) -> str:
    return "" if value is None else _DESCRIPTION.sub("", str(value)).strip()


def _tristate(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return "sí" if value else "no"
    return str(value)


def catalog_index(store: PriceComparatorStore) -> ReferenceIndex:
    products = store.products()
    return ReferenceIndex(products.to_dict("records") if not products.empty else [])


def observations_to_frame(data: dict, index: ReferenceIndex) -> tuple[pd.DataFrame, list[str]]:
    """Translate the JSON observations to the rows of ``PriceComparatorStore.import_offers``."""
    if data.get("schema") != SCHEMA:
        return pd.DataFrame(), [f"Esquema no soportado: se esperaba '{SCHEMA}'"]
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for number, obs in enumerate(data.get("observaciones", []), start=1):
        label = f"Observación {number}"
        ean = _clean(obs.get("ean_consultado"))
        if not ean and obs.get("referencia"):
            level, found = index.lookup(str(obs["referencia"]))
            if len(found) != 1:
                errors.append(f"{label}: la referencia '{obs['referencia']}' "
                              f"{'es ambigua' if found else 'no está en el catálogo'}")
                continue
            ean = found[0]["ean"]
        if not ean:
            errors.append(f"{label}: falta ean_consultado o referencia")
            continue
        state = _clean(obs.get("estado")) or "observada"
        price = obs.get("precio_venta")
        match = _clean(obs.get("coincidencia")) or "manual_review"
        note = _clean(obs.get("evidencia")) or _clean(obs.get("validacion")) or _clean(obs.get("detalle"))
        if state in NO_PRICE_STATES:
            price, match = None, NO_PRICE_STATES[state]
        elif price is not None and match == "exact_match" and obs.get("iva_incluido") is None:
            # An exact match whose VAT treatment is unknown cannot be compared safely.
            match = "manual_review"
            note += " | IVA no determinado: pendiente de revisión"
        rows.append({
            "EAN": ean, "Competidor": obs.get("competidor", ""),
            "Precio": "" if price is None else price,
            "Precio promocional": obs.get("precio_promocional") or "",
            "Precio anterior": obs.get("precio_anterior") or "",
            "Coste envío": obs.get("coste_envio") or "",
            "Moneda": obs.get("moneda", "EUR"), "Mercado": obs.get("mercado") or DEFAULT_MARKET,
            "URL": obs.get("url", ""), "Disponibilidad": obs.get("disponibilidad") or "unknown",
            "Estado coincidencia": match, "Evidencia": note or state,
            "Método": obs.get("metodo") or "Importación JSON",
            "Fecha consulta": obs.get("fecha_observacion") or "",
            "Marca": _clean(obs.get("marca")), "Modelo": _clean(obs.get("modelo")),
            "Color": _clean(obs.get("color")), "Calibre": _clean(obs.get("calibre")),
            "EAN publicado": _clean(obs.get("ean_publicado")), "Título": _clean(obs.get("titulo")),
            "IVA incluido": _tristate(obs.get("iva_incluido")), "Tipo IVA": obs.get("tipo_iva") or "",
        })
    return pd.DataFrame(rows), errors


def _simulation_copy(database: Path) -> tuple[Path, Path]:
    folder = Path(tempfile.mkdtemp(prefix="price-sim-"))
    target = folder / "copy.sqlite3"
    if database.exists():
        with closing(sqlite3.connect(database)) as source, closing(sqlite3.connect(target)) as copy:
            source.backup(copy)
    return folder, target


def import_observations(path: Path, database: Path, *, apply: bool = False) -> dict[str, Any]:
    """Import (or simulate importing) a JSON file. The real database is untouched unless ``apply``."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    folder = None
    try:
        if apply:
            target = database
        else:
            folder, target = _simulation_copy(database)
        store = PriceComparatorStore(target)
        frame, errors = observations_to_frame(data, catalog_index(store))
        report = store.import_offers(frame) if not frame.empty else ImportReport(0, 0, ())
        return {"applied": apply, "database": str(target if apply else database),
                "observations": len(data.get("observaciones", [])), "inserted": report.imported,
                "unchanged_checked": report.updated, "errors": errors + list(report.rejected)}
    finally:
        if folder:
            shutil.rmtree(folder, ignore_errors=True)


CALIBER_SQL = """
    SELECT pp.barcode, pt.modelo::text AS model, pt.color_proveedor::text AS color,
           pt.size_width_puente::text AS bridge, pav.name::text AS lens
    FROM product_product pp JOIN product_template pt ON pt.id=pp.product_tmpl_id
    LEFT JOIN product_variant_combination pvc ON pvc.product_product_id=pp.id
    LEFT JOIN product_template_attribute_value ptav ON ptav.id=pvc.product_template_attribute_value_id
    LEFT JOIN product_attribute_value pav ON pav.id=ptav.product_attribute_value_id
    LEFT JOIN product_attribute pa ON pa.id=pav.attribute_id AND pa.name::text='Calibre'
    WHERE pp.barcode IN :barcodes AND (pa.id IS NOT NULL OR pvc.product_product_id IS NULL)
"""


def read_odoo_references(eans: Iterable[str]) -> list[dict[str, str]]:
    """Read-only lookup of model, colour and calibre ('lens-bridge') in Odoo for the given EANs."""
    from sqlalchemy import bindparam, text

    from vytria_price_sync import _barcode_forms, build_readonly_engine

    forms = sorted({form for ean in eans for form in _barcode_forms(ean)})
    records: list[dict[str, str]] = []
    statement = text(CALIBER_SQL).bindparams(bindparam("barcodes", expanding=True))
    with build_readonly_engine().connect() as connection:
        for row in connection.execute(statement, {"barcodes": forms}).mappings():
            lens, bridge = (row["lens"] or "").strip(), (row["bridge"] or "").strip()
            records.append({"ean": normalize_gtin(row["barcode"]), "model": row["model"] or "",
                            "color": row["color"] or "",
                            "caliber": f"{lens}-{bridge}" if lens and bridge else lens})
    return records


def sync_catalog(database: Path, *, apply: bool = False,
                 reader: Callable[[Iterable[str]], list[dict[str, str]]] = read_odoo_references) -> list[dict]:
    store = PriceComparatorStore(database)
    eans = list(store.products()["ean"])
    return store.update_catalog_references(reader(eans), apply=apply)


def search_report(store: PriceComparatorStore, text: str) -> dict[str, Any]:
    index = catalog_index(store)
    level, found = index.lookup(text)
    return {"query": text, "level": level or "sin resultado",
            "products": [{k: p[k] for k in ("ean", "brand", "model", "color", "caliber")} for p in found],
            "ambiguous": len(found) > 1,
            "plan": [{"level": lvl, "query": query} for product in found[:1] for lvl, query in search_plan(product)]}


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=None, help="SQLite del comparador (por defecto, la configurada)")
    commands = parser.add_subparsers(dest="command", required=True)
    imp = commands.add_parser("import")
    imp.add_argument("file", type=Path)
    imp.add_argument("--apply", action="store_true")
    search = commands.add_parser("search")
    search.add_argument("text")
    sync = commands.add_parser("sync-catalog")
    sync.add_argument("--apply", action="store_true")
    coverage = commands.add_parser("coverage")
    coverage.add_argument("--load", type=Path)
    coverage.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    database = (args.db or resolve_database_path()).resolve()

    if args.command == "import":
        result = import_observations(args.file, database, apply=args.apply)
        _print(result)
        return 1 if result["errors"] else 0
    if args.command == "search":
        _print(search_report(PriceComparatorStore(database), args.text))
        return 0
    if args.command == "sync-catalog":
        changes = sync_catalog(database, apply=args.apply)
        _print({"applied": args.apply, "changes": changes})
        return 0
    store = PriceComparatorStore(database)
    if args.load:
        records = json.loads(args.load.read_text(encoding="utf-8"))["competidores"]
        if args.apply:
            store.save_coverage(records)
        else:
            print(f"Simulación: {len(records)} competidores en {args.load}; use --apply para guardarlos.")
    print(store.coverage_table().to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
