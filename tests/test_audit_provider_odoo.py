import json
import sys
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from audit_provider_odoo import run_audit  # noqa: E402
from provider_support import EANS, fake_odoo


def test_audit_reports_counts_only_and_separates_partners():
    engine = fake_odoo({573: [(1, "P1", EANS[0]), (2, "P2", None)], 574: [(3, "P3", EANS[1]), (5, "P5", "123")],
                        900: [(4, "P4", EANS[1])]})
    with engine.begin() as c:
        c.execute(text("INSERT INTO product_supplierinfo VALUES (1, 900, 4)"))
    report = run_audit(engine, {"marcolin": 573, "other": 900, "ghost": 12345}, ["otro", "zzz"])
    marcolin = report["partners"]["marcolin"]
    assert marcolin["family_size"] == 2
    assert marcolin["via_purchase_orders"] == {"lines": 4, "without_barcode": 1, "distinct_ean": 3, "invalid_ean": 1}
    assert report["partners"]["ghost"] == {"partner_id": 12345, "found": False}
    assert report["shared_eans"]["marcolin/other"] == 2  # EAN via PO plus another via supplierinfo; surfaced, never resolved by name
    assert report["candidates_to_confirm"][0]["partners"] == [{"id": 900, "name": "Otro"}]
    assert all("_eans" not in p for p in report["partners"].values())
    json.dumps(report)
