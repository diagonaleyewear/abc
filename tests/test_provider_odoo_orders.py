import json
from datetime import date

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

import provider_odoo_orders as po
from image_repository import ImageRepository
from marcolin_images import MarcolinConnector, DEFAULTS
from provider_images import ProviderService, ProviderStore
from provider_support import EANS, photo

BAD_EAN = "8400000000999"


def odoo_fixture():
    """Orders as read from Odoo 15: states, commercial contacts, missing/invalid/duplicate barcodes."""
    engine = create_engine("sqlite://")
    with engine.begin() as c:
        for ddl in (
            "CREATE TABLE res_partner(id INTEGER PRIMARY KEY, name TEXT, active BOOLEAN DEFAULT 1,"
            " supplier_rank INTEGER DEFAULT 0, commercial_partner_id INTEGER)",
            "CREATE TABLE purchase_order(id INTEGER PRIMARY KEY, name TEXT, partner_id INTEGER, date_order TEXT, state TEXT)",
            "CREATE TABLE purchase_order_line(id INTEGER PRIMARY KEY, order_id INTEGER, product_id INTEGER,"
            " name TEXT, product_qty REAL, display_type TEXT)",
            "CREATE TABLE product_product(id INTEGER PRIMARY KEY, barcode TEXT, default_code TEXT)",
        ):
            c.execute(text(ddl))
        c.execute(text("INSERT INTO res_partner VALUES (573,'MARCOLIN IBERICA, S.A.',1,1,573),"
                       "(574,'Marcolin contacto',1,0,573),(900,'Otro proveedor',1,1,900)"))
        orders = [  # id, name, partner, date, state, [(barcode, ref)]
            (1, "P00001", 573, "2026-09-01 10:00:00", "purchase", [(EANS[0], "MCL-1"), (EANS[1], "MCL-2")]),
            (2, "P00002", 574, "2026-09-20 10:00:00", "done", [(EANS[2], "MCL-3"), (EANS[0], "MCL-1")]),
            (3, "P00003", 573, "2026-09-15 10:00:00", "cancel", [(EANS[3], "MCL-4")]),
            (4, "P00004", 573, "2026-09-16 10:00:00", "draft", [(EANS[4], "MCL-5")]),
            (5, "P00005", 900, "2026-09-17 10:00:00", "purchase", [(EANS[5], "OTR-1")]),
            (6, "P00006", 573, "2026-09-18 10:00:00", "purchase", [(None, "MCL-6"), (BAD_EAN, "MCL-7")]),
        ]
        line = 0
        for oid, name, partner, when, state, items in orders:
            c.execute(text("INSERT INTO purchase_order VALUES (:i,:n,:p,:d,:s)"),
                      {"i": oid, "n": name, "p": partner, "d": when, "s": state})
            for barcode, ref in items:
                line += 1
                c.execute(text("INSERT INTO product_product VALUES (:i,:b,:r)"), {"i": line, "b": barcode, "r": ref})
                c.execute(text("INSERT INTO purchase_order_line VALUES (:l,:o,:p,:n,1,NULL)"),
                          {"l": line, "o": oid, "p": line, "n": f"Producto {ref}"})
    return engine


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo = ImageRepository(tmp_path / "i", state=tmp_path / "r", backups=tmp_path / "b")
    monkeypatch.setenv("IMAGE_REPOSITORY_STATE_ROOT", str(tmp_path / "r"))
    monkeypatch.setenv("IMAGE_REPOSITORY_ROOT", str(tmp_path / "i"))
    root = tmp_path / "s"
    service = ProviderService(ProviderStore(root), repo, DEFAULTS)
    return MarcolinConnector(service), repo, root


RANGE = (date(2026, 9, 1), date(2026, 9, 30))


def counts(engine):
    with engine.connect() as c:
        return [c.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() for t in
                ("purchase_order", "purchase_order_line", "product_product", "res_partner")]


def test_reads_only_confirmed_orders_with_commercial_contacts_and_does_not_write():
    engine = odoo_fixture()
    before = counts(engine)
    lines, truncated = po.read_order_lines(engine, [573], *RANGE)
    assert not truncated and counts(engine) == before
    assert {l.order for l in lines} == {"P00001", "P00002", "P00006"}  # cancel, draft and partner 900 excluded
    assert any(l.supplier == "Marcolin contacto" for l in lines)  # contact 574 grouped under 573
    assert lines[0].date >= lines[-1].date  # most recent first
    assert next(l for l in lines if l.ean == EANS[1]).reference == "MCL-2"


def test_read_is_bounded():
    lines, truncated = po.read_order_lines(odoo_fixture(), [573], *RANGE, max_lines=2)
    assert len(lines) == 2 and truncated


def test_association_must_be_configured_and_reviewed(env, tmp_path):
    connector, _, root = env
    with pytest.raises(po.AssociationNotConfigured):
        po.demand_preview(odoo_fixture(), connector, root, *RANGE)
    with pytest.raises(ValueError):
        po.save_link(root, "marcolin", [573], reviewer=" ")
    with pytest.raises(ValueError):
        po.save_link(root, "marcolin", [], reviewer="ana")
    link = po.save_link(root, "marcolin", [573], reviewer="ana", brands=["Marcolin"])
    assert link["confirmed"] and po.confirmed_partner_ids(root, "marcolin") == (573,)
    with pytest.raises(po.AssociationNotConfigured):  # other portals are not implied
        po.confirmed_partner_ids(root, "safilo")


def test_name_candidates_are_proposals_not_associations(env):
    _, _, root = env
    found = po.propose_partners(odoo_fixture(), "marcolin")
    assert [p["id"] for p in found] == [573] and found[0]["orders"] >= 1
    assert po.load_links(root) == {}
    with pytest.raises(ValueError):
        po.propose_partners(odoo_fixture(), "ma")


def test_preview_statuses_images_and_duplicates(env):
    connector, repo, root = env
    po.save_link(root, "marcolin", [573], reviewer="ana")
    repo.save(EANS[1], "a.png", photo(), provider="kering", origin="t", view="frontal")
    for view in ("frontal", "lateral", "perspectiva"):
        repo.save(EANS[2], f"{view}.png", photo(), provider="luxottica", origin="t", view=view)
    connector.plan_eans([EANS[0]])  # already has a live request
    preview = po.demand_preview(odoo_fixture(), connector, root, *RANGE)
    by = {(r["EAN"], r["Pedido"]): r for r in preview["rows"]}
    assert by[(EANS[1], "P00001")]["Imágenes"] == po.IMAGES_PARTIAL and by[(EANS[1], "P00001")]["Estado"] == po.ELIGIBLE
    assert by[(EANS[2], "P00002")]["Imágenes"] == po.IMAGES_COMPLETE and by[(EANS[2], "P00002")]["Estado"] == po.COMPLETE
    assert by[(EANS[0], "P00002")]["Estado"] == po.DUPLICATE or by[(EANS[0], "P00001")]["Estado"] == po.DUPLICATE
    reviews = [r for r in preview["rows"] if r["Estado"] == po.REVIEW]
    assert {r["Motivo"] for r in reviews} == {"sin EAN en Odoo", "EAN inválido"}
    assert preview["summary"]["sin_ean"] == 1 and preview["summary"]["ean_invalido"] == 1
    assert sum(1 for r in preview["rows"] if r["EAN"] == EANS[0] and r["Estado"] == po.DUPLICATE) >= 1


def test_pilot_proposal_prefers_missing_then_partial_recent_first_and_caps_at_ten(env):
    connector, repo, root = env
    po.save_link(root, "marcolin", [573], reviewer="ana")
    repo.save(EANS[1], "a.png", photo(), provider="kering", origin="t", view="frontal")
    preview = po.demand_preview(odoo_fixture(), connector, root, *RANGE)
    picks = po.pick_pilot(preview["rows"], size=50)
    assert picks[-1] == EANS[1]  # partial last
    assert set(picks) == {EANS[0], EANS[1], EANS[2]} and len(po.pick_pilot(preview["rows"], 2)) == 2
    many = [{"EAN": f"{i}", "Estado": po.ELIGIBLE, "Imágenes": po.IMAGES_NONE, "Fecha": "2026-09-01"} for i in range(30)]
    assert len(po.pick_pilot(many, 99)) == 10


def test_create_requests_only_planner_only_eligible_no_portal_and_logged(env):
    connector, repo, root = env
    po.save_link(root, "marcolin", [573], reviewer="ana")
    preview = po.demand_preview(odoo_fixture(), connector, root, *RANGE)
    result = po.create_requests(connector, root, preview["rows"], [EANS[0], EANS[1], BAD_EAN, EANS[3]], approved_by="ana")
    assert result["not_eligible"] == [BAD_EAN, EANS[3]]  # invalid and cancelled-order EANs cannot be forced in
    requests = connector.service.store.list_requests("marcolin")
    assert len(requests) == 1 and requests[0]["state"] == "ready" and requests[0]["remote_id"] is None
    log = [json.loads(l) for l in (root / po.SELECTION_LOG).read_text(encoding="utf-8").splitlines()]
    assert log[0]["approved_by"] == "ana" and {e["ean"] for e in log[0]["eans"]} == {EANS[0], EANS[1]}
    again = po.create_requests(connector, root, preview["rows"], [EANS[0]], approved_by="ana")
    assert again["created"] == [] and len(connector.service.store.list_requests("marcolin")) == 1
    with pytest.raises(ValueError):
        po.create_requests(connector, root, preview["rows"], [EANS[2]], approved_by="")


class DownEngine:
    class dialect:
        name = "postgresql"

    def connect(self):
        raise OperationalError("SELECT 1", {}, Exception("timeout"))


def test_odoo_down_is_reported_and_manual_flow_still_works(env):
    connector, _, root = env
    po.save_link(root, "marcolin", [573], reviewer="ana")
    connector.engine = DownEngine()
    with pytest.raises(po.OdooUnavailable):
        po.demand_preview(DownEngine(), connector, root, *RANGE)
    with pytest.raises(po.OdooUnavailable):
        po.propose_partners(DownEngine(), "marcolin")
    result = connector.plan_eans(f"{EANS[0]}, {EANS[1]}")
    assert len(result["created"]) == 1
