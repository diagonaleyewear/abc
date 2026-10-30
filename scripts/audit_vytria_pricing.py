"""Read-only Odoo audit to identify where the Vytria (Spain) selling price comes from.

Reuses Cloud's existing connection settings (DB_* via db_config) and only runs SELECT
statements inside a READ ONLY transaction. Output is structural/aggregate plus a small
sample of EANs; it never prints credentials. Run it where the existing Odoo connection
already exists, e.g. from the running Cloud container:

    docker exec -i abcd-control python - --sample 15 < scripts/audit_vytria_pricing.py

or, with an explicit EAN list taken from the published shop:

    python scripts/audit_vytria_pricing.py --ean 8056262672563 --ean 036000291452
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

try:
    from db_config import load_db_config
except ImportError:  # executed from stdin inside the container: /app is the cwd
    sys.path.insert(0, str(Path.cwd()))
    from db_config import load_db_config

STATEMENT_TIMEOUT_MS = 30000

PRICE_COLUMN_HINT = "(column_name ILIKE '%price%' OR column_name ILIKE '%pvp%' OR column_name ILIKE '%discount%' OR column_name ILIKE '%descuento%' OR column_name ILIKE '%tarifa%')"

QUERIES = {
    "pricelists": """
        SELECT id, name::text AS name, active, currency_id, company_id
        FROM product_pricelist ORDER BY active DESC, id""",
    "pricelist_items": """
        SELECT pricelist_id, applied_on, compute_price, base, COUNT(*) AS items,
               MIN(date_start) AS first_start, MAX(date_end) AS last_end
        FROM product_pricelist_item GROUP BY 1, 2, 3, 4 ORDER BY 1, 2, 3""",
    "sale_taxes": """
        SELECT id, name::text AS name, amount, amount_type, price_include, active, company_id
        FROM account_tax WHERE type_tax_use = 'sale' ORDER BY active DESC, id""",
    "website_channels": """
        SELECT siteweb_id, name, COUNT(*) AS products,
               SUM(CASE WHEN published THEN 1 ELSE 0 END) AS published
        FROM diagonal_product_website GROUP BY 1, 2 ORDER BY 1""",
    "website_columns": """
        SELECT column_name, data_type FROM information_schema.columns
        WHERE table_name = 'diagonal_product_website' ORDER BY ordinal_position""",
    "custom_price_columns": f"""
        SELECT table_name, column_name, data_type FROM information_schema.columns
        WHERE table_schema = 'public' AND {PRICE_COLUMN_HINT}
          AND (table_name LIKE 'diagonal%' OR table_name LIKE 'x\\_%' OR table_name LIKE 'product\\_%'
               OR table_name LIKE 'website\\_%' OR table_name LIKE 'prestashop%')
        ORDER BY table_name, column_name""",
}

SAMPLE_SELECTION = """
    SELECT pp.id AS product_id, pp.barcode, pp.default_code, pp.active AS variant_active,
           pt.id AS template_id, pt.active AS template_active, pt.sale_ok, pt.list_price
    FROM product_product pp JOIN product_template pt ON pt.id = pp.product_tmpl_id
    {where}
    ORDER BY pp.id DESC LIMIT :limit"""

SAMPLE_TAXES = """
    SELECT tx.id, tx.name::text AS name, tx.amount, tx.price_include
    FROM product_taxes_rel rel JOIN account_tax tx ON tx.id = rel.tax_id
    WHERE rel.prod_id = :template_id AND tx.type_tax_use = 'sale' AND tx.active"""

SAMPLE_ITEMS = """
    SELECT pricelist_id, applied_on, compute_price, base, fixed_price, percent_price,
           price_discount, price_surcharge, min_quantity, date_start, date_end
    FROM product_pricelist_item
    WHERE (product_id = :product_id OR product_tmpl_id = :template_id)
    ORDER BY pricelist_id, min_quantity"""

SAMPLE_CHANNELS = """
    SELECT siteweb_id, name, product_website_id, published, updated_website_date, archived_website_date
    FROM diagonal_product_website WHERE product_id = :product_id ORDER BY siteweb_id"""


def build_engine():
    config = load_db_config()
    url = URL.create("postgresql+psycopg2", username=config.user, password=config.password,
                     host=config.host, port=config.port, database=config.database)
    return create_engine(url, connect_args={
        "options": f"-c default_transaction_read_only=on -c statement_timeout={STATEMENT_TIMEOUT_MS}",
        "connect_timeout": 10,
    })


def run(connection, name: str, sql: str, params: dict | None = None) -> list[dict]:
    """One SELECT; a failing query (for example a table that does not exist) is reported, not fatal."""
    savepoint = connection.begin_nested()
    try:
        rows = [{k: _plain(v) for k, v in row.items()} for row in connection.execute(text(sql), params or {}).mappings()]
        savepoint.commit()
        return rows
    except Exception as exc:  # noqa: BLE001
        savepoint.rollback()
        return [{"error": f"{name}: {type(exc).__name__}"}]


def _plain(value):
    return value if isinstance(value, (str, int, float, bool)) or value is None else str(value)


def audit(connection, eans: list[str], sample: int) -> dict:
    connection.execute(text("SET TRANSACTION READ ONLY"))
    report = {name: run(connection, name, sql) for name, sql in QUERIES.items()}
    if eans:
        selection = run(connection, "sample", SAMPLE_SELECTION.format(
            where="WHERE pp.barcode = ANY(:eans)"), {"eans": eans, "limit": len(eans)})
    else:
        selection = run(connection, "sample", SAMPLE_SELECTION.format(
            where="WHERE pp.barcode IS NOT NULL AND pp.barcode <> '' AND pp.active AND pt.sale_ok"),
            {"limit": sample})
    products = []
    for row in selection:
        if "error" in row:
            products.append(row)
            continue
        params = {"product_id": row["product_id"], "template_id": row["template_id"]}
        products.append({
            **row,
            "sale_taxes": run(connection, "taxes", SAMPLE_TAXES, params),
            "pricelist_items": run(connection, "items", SAMPLE_ITEMS, params),
            "channels": run(connection, "channels", SAMPLE_CHANNELS, params),
        })
    report["sample_products"] = products
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ean", action="append", default=[], help="EAN/UPC to inspect (repeatable)")
    parser.add_argument("--sample", type=int, default=10, help="products to sample when no --ean is given")
    args = parser.parse_args(argv)
    engine = build_engine()
    with engine.connect() as connection:
        report = audit(connection, [e.strip() for e in args.ean if e.strip()], max(1, min(args.sample, 50)))
        connection.rollback()
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
