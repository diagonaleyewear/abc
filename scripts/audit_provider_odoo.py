"""Read-only Odoo audit of supplier ↔ product relations for provider image connectors (#59).

Prints counts only (no personal data). Relations are measured through purchase orders and
product_supplierinfo for explicit partner ids; names are used solely to *propose* candidates.

    python scripts/audit_provider_odoo.py --partner marcolin=573 --candidate-name dita --candidate-name safilo
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from provider_images import ean_valid  # noqa: E402

FAMILY = """SELECT contact.id FROM res_partner contact JOIN res_partner selected ON selected.id = :partner
 WHERE COALESCE(contact.commercial_partner_id, contact.id) = COALESCE(selected.commercial_partner_id, selected.id)"""


def audit_partner(connection, partner_id: int) -> dict:
    family = [r[0] for r in connection.execute(text(FAMILY), {"partner": partner_id})]
    if not family:
        return {"partner_id": partner_id, "found": False}
    ids = {f"p{i}": v for i, v in enumerate(family)}
    marks = ",".join(f":{k}" for k in ids)
    po = [r[0] for r in connection.execute(text(
        f"SELECT pp.barcode FROM purchase_order po JOIN purchase_order_line pol ON pol.order_id = po.id "
        f"AND COALESCE(pol.display_type,'') = '' JOIN product_product pp ON pp.id = pol.product_id "
        f"WHERE po.partner_id IN ({marks}) AND po.state != 'cancel'"), ids)]
    info = [r[0] for r in connection.execute(text(
        f"SELECT pp.barcode FROM product_supplierinfo si JOIN product_product pp "
        f"ON pp.product_tmpl_id = si.product_tmpl_id WHERE si.partner_id IN ({marks})"), ids)]

    def quality(barcodes):
        clean = [(b or "").strip() for b in barcodes]
        unique = {b for b in clean if b}
        return {"lines": len(clean), "without_barcode": sum(1 for b in clean if not b),
                "distinct_ean": len(unique), "invalid_ean": sum(1 for b in unique if not ean_valid(b))}

    return {"partner_id": partner_id, "found": True, "family_size": len(family),
            "via_purchase_orders": quality(po), "via_supplierinfo": quality(info),
            "_eans": {(b or "").strip() for b in po + info if (b or "").strip()}}


def run_audit(engine, partners: dict[str, int], candidate_names=()) -> dict:
    report = {"partners": {}, "shared_eans": {}, "candidates_to_confirm": []}
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            if engine.dialect.name == "postgresql":
                connection.execute(text("SET TRANSACTION READ ONLY"))
            sets = {}
            for provider, partner in partners.items():
                result = audit_partner(connection, partner)
                sets[provider] = result.pop("_eans", set())
                report["partners"][provider] = result
            names = list(sets)
            for i, left in enumerate(names):
                for right in names[i + 1:]:
                    report["shared_eans"][f"{left}/{right}"] = len(sets[left] & sets[right])
            for name in candidate_names:
                rows = connection.execute(text(
                    "SELECT id, name FROM res_partner WHERE supplier_rank > 0 AND LOWER(name) LIKE :n ORDER BY id"),
                    {"n": f"%{name.lower()}%"}).mappings().all()
                report["candidates_to_confirm"].append({"search": name, "partners": [dict(r) for r in rows]})
        finally:
            transaction.rollback()
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--partner", action="append", default=[], metavar="provider=id")
    parser.add_argument("--candidate-name", action="append", default=[])
    args = parser.parse_args(argv)
    partners = {k: int(v) for k, v in (p.split("=", 1) for p in args.partner)}
    from kering_jobs import db_engine  # noqa: PLC0415

    print(json.dumps(run_audit(db_engine(), partners, args.candidate_name), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
