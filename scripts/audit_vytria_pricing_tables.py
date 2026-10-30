"""Read-only Odoo audit, phase 2: relations and prices of the Vytria/PrestaShop integration tables.

Self-contained so it can be piped into the running Cloud container, which already has the
Odoo connection (DB_* settings):

    docker exec -i abcd-control python - --ean 889652637075 --ean 8056597981361 \
        < scripts/audit_vytria_pricing_tables.py

Only SELECT statements, in a READ ONLY session with a statement timeout. Column names of the
integration tables are discovered from the catalog, not assumed. Secrets are redacted.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

sys.path.insert(0, str(Path.cwd()))
from db_config import load_db_config  # noqa: E402

TABLES = (
    "diagonal_vytria_int_catalog_item",
    "diagonal_vytria_int_catalog_item_pricelist",
    "prestashop_pricelist_item",
    "prestashop_pricelist_item_pricelist",
    "diagonal_saleschannel_int",
    "diagonal_saleschannel_int_product_pricelist_rel",
)
DEFAULT_PRICELISTS = (17, 59)
ROW_LIMIT = 200
IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")
SENSITIVE_KEY = re.compile(r"pass|pwd|token|secret|api|key|auth|credential|signature|cookie|session", re.I)

COLUMNS = """SELECT table_name, column_name, data_type FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = ANY(:tables) ORDER BY table_name, ordinal_position"""
FOREIGN_KEYS = """SELECT c.conrelid::regclass::text AS table_name, a.attname AS column_name,
           c.confrelid::regclass::text AS ref_table
    FROM pg_constraint c JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
    WHERE c.contype = 'f' AND array_length(c.conkey, 1) = 1 AND c.conrelid::regclass::text = ANY(:tables)"""
PRICELISTS = """SELECT id, name::text AS name, active, currency_id, company_id, to_jsonb(p) - 'name' AS detail
    FROM product_pricelist p WHERE id = ANY(:ids)"""
PRICELIST_ITEM_SUMMARY = """SELECT pricelist_id, applied_on, compute_price, base, COUNT(*) AS items,
           MIN(date_start) AS first_start, MAX(date_end) AS last_end
    FROM product_pricelist_item WHERE pricelist_id = ANY(:ids) GROUP BY 1, 2, 3, 4 ORDER BY 1, 2"""
PRODUCTS = """SELECT pp.id AS product_id, pp.barcode, pp.default_code, pp.active AS variant_active,
           pt.id AS template_id, pt.active AS template_active, pt.sale_ok, pt.list_price
    FROM product_product pp JOIN product_template pt ON pt.id = pp.product_tmpl_id
    WHERE pp.barcode = ANY(:forms) ORDER BY pp.barcode, pp.id"""
TAXES = """SELECT tx.id, tx.name::text AS name, tx.amount, tx.amount_type, tx.price_include, tx.company_id
    FROM product_taxes_rel rel JOIN account_tax tx ON tx.id = rel.tax_id
    WHERE rel.prod_id = :template_id AND tx.type_tax_use = 'sale' AND tx.active"""
PRODUCT_ITEMS = """SELECT pricelist_id, applied_on, compute_price, base, fixed_price, percent_price,
           price_discount, price_surcharge, min_quantity, date_start, date_end
    FROM product_pricelist_item
    WHERE pricelist_id = ANY(:ids) AND (product_id = :product_id OR product_tmpl_id = :template_id)
    ORDER BY pricelist_id, min_quantity"""


def barcode_forms(ean: str) -> list[str]:
    """Odoo may store a UPC-12 with or without its leading zero."""
    digits = ean.strip()
    forms = {digits}
    if len(digits) == 12:
        forms.add("0" + digits)
    if len(digits) == 13 and digits.startswith("0"):
        forms.add(digits[1:])
    return sorted(forms)


def redact(value, key: str = ""):
    if SENSITIVE_KEY.search(key):
        return "<redacted>"
    if isinstance(value, dict):
        return {k: redact(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, key) for v in value][:20]
    if isinstance(value, str):
        value = re.sub(r"(://)[^/@\s]*@", r"\1", value).split("?")[0]
        return value if len(value) <= 200 else value[:200] + "…"
    return value if value is None or isinstance(value, (int, float, bool)) else str(value)


def safe_identifier(name: str) -> str:
    if not IDENTIFIER.match(name):
        raise ValueError("unexpected identifier")
    return '"' + name + '"'


def build_engine():
    config = load_db_config()
    url = URL.create("postgresql+psycopg2", username=config.user, password=config.password,
                     host=config.host, port=config.port, database=config.database)
    return create_engine(url, connect_args={
        "options": "-c default_transaction_read_only=on -c statement_timeout=30000", "connect_timeout": 10})


def run(connection, name: str, sql: str, params: dict | None = None) -> list[dict]:
    savepoint = connection.begin_nested()
    try:
        rows = [redact(dict(row)) for row in connection.execute(text(sql), params or {}).mappings()]
        savepoint.commit()
        return rows
    except Exception as exc:  # noqa: BLE001 - one failing probe must not stop the audit
        savepoint.rollback()
        return [{"error": f"{name}: {type(exc).__name__}"}]


def fetch_rows(connection, table: str, column: str, ids: list) -> list[dict]:
    if not ids:
        return []
    sql = (f"SELECT to_jsonb(t) AS row FROM {safe_identifier(table)} t "
           f"WHERE t.{safe_identifier(column)} = ANY(:ids) LIMIT {ROW_LIMIT}")
    return [redact(r["row"]) if "row" in r else r for r in run(connection, table, sql, {"ids": ids})]


def dump_rows(connection, table: str) -> list[dict]:
    sql = f"SELECT to_jsonb(t) AS row FROM {safe_identifier(table)} t LIMIT {ROW_LIMIT}"
    return [redact(r["row"]) if "row" in r else r for r in run(connection, table, sql)]


def integration_tables(connection, product_ids: list, template_ids: list, pricelists: list) -> dict:
    fks: dict[str, list[tuple[str, str]]] = {}
    for row in run(connection, "foreign_keys", FOREIGN_KEYS, {"tables": list(TABLES)}):
        if "error" not in row:
            fks.setdefault(row["table_name"], []).append((row["column_name"], row["ref_table"]))
    fetched: dict[str, list[dict]] = {}
    out: dict = {}
    for table in TABLES * 2:  # second pass resolves tables that depend on later ones
        if table in out and out[table].get("rows"):
            continue
        links = fks.get(table, [])
        rows, via = [], "full_dump"
        for column, ref in links:
            if ref == "product_product":
                rows, via = fetch_rows(connection, table, column, product_ids), f"{column}->product_product"
            elif ref == "product_template":
                rows, via = fetch_rows(connection, table, column, template_ids), f"{column}->product_template"
            else:
                continue
            break
        else:
            for column, ref in links:
                parent = fetched.get(ref)
                if parent and any("id" in r for r in parent):
                    rows = fetch_rows(connection, table, column, [r["id"] for r in parent if "id" in r])
                    via = f"{column}->{ref}"
                    break
            else:
                pricelist_links = [c for c, ref in links if ref == "product_pricelist"]
                if pricelist_links:
                    rows, via = fetch_rows(connection, table, pricelist_links[0], pricelists), f"{pricelist_links[0]}->product_pricelist"
                elif not links:
                    rows = dump_rows(connection, table)
        fetched[table] = rows
        out[table] = {"foreign_keys": links, "linked_via": via, "rows": rows}
    return out


def audit(connection, eans: list[str], pricelists: list[int]) -> dict:
    connection.execute(text("SET TRANSACTION READ ONLY"))
    report: dict = {"pricelists": run(connection, "pricelists", PRICELISTS, {"ids": pricelists}),
                    "pricelist_item_summary": run(connection, "item_summary", PRICELIST_ITEM_SUMMARY, {"ids": pricelists})}
    report["columns"] = run(connection, "columns", COLUMNS, {"tables": list(TABLES)})
    report["row_counts"] = {t: run(connection, t, f"SELECT COUNT(*) AS rows FROM {safe_identifier(t)}")[0] for t in TABLES}
    forms = sorted({form for ean in eans for form in barcode_forms(ean)})
    products = run(connection, "products", PRODUCTS, {"forms": forms})
    good = [p for p in products if "error" not in p]
    for product in good:
        params = {"product_id": product["product_id"], "template_id": product["template_id"], "ids": pricelists}
        product["sale_taxes"] = run(connection, "taxes", TAXES, params)
        product["pricelist_items"] = run(connection, "items", PRODUCT_ITEMS, params)
    report["products"] = products
    report["missing_barcodes"] = sorted(set(forms) - {p["barcode"] for p in good})
    report["integration_tables"] = integration_tables(
        connection, [p["product_id"] for p in good], sorted({p["template_id"] for p in good}), pricelists)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ean", action="append", default=[], required=True)
    parser.add_argument("--pricelist", action="append", type=int)
    args = parser.parse_args(argv)
    with build_engine().connect() as connection:
        report = audit(connection, [e.strip() for e in args.ean if e.strip()], args.pricelist or list(DEFAULT_PRICELISTS))
        connection.rollback()
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
