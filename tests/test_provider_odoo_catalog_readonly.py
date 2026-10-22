"""#75: the Odoo preview reads image coverage from the existing catalog without initialising it."""
import sqlite3

import pytest

import provider_odoo_orders as po
from image_repository import ImageRepository
from marcolin_images import DEFAULTS, MarcolinConnector
from provider_images import ProviderService, ProviderStore
from provider_support import EANS, make_ean, photo
from test_provider_odoo_orders import RANGE, odoo_fixture


def tree(path):
    return sorted(str(p.relative_to(path)) for p in path.rglob("*")) if path.exists() else None


@pytest.fixture
def blank(tmp_path, monkeypatch):
    """Provider planning state exists; the image repository does NOT (nothing may create it)."""
    monkeypatch.setenv("IMAGE_REPOSITORY_STATE_ROOT", str(tmp_path / "r"))
    monkeypatch.setenv("IMAGE_REPOSITORY_ROOT", str(tmp_path / "i"))
    monkeypatch.setattr(ImageRepository, "__init__", lambda *a, **k: pytest.fail("ImageRepository constructed"))

    class NoRepo:
        pass

    service = ProviderService(ProviderStore(tmp_path / "s"), NoRepo(), DEFAULTS)
    po.save_link(tmp_path / "s", "marcolin", [573], reviewer="ana")
    return MarcolinConnector(service), tmp_path / "s", tmp_path


def test_missing_catalog_creates_nothing_and_is_unknown_not_absent(blank):
    connector, root, base = blank
    preview = po.demand_preview(odoo_fixture(), connector, root, *RANGE)
    assert not (base / "r").exists() and not (base / "i").exists()
    assert preview["catalog"]["status"] == "inaccesible"
    states = {r["Imágenes"] for r in preview["rows"] if r["Estado"] == po.ELIGIBLE}
    assert states == {po.IMAGES_UNKNOWN}
    assert po.pick_pilot(preview["rows"]) == []  # unknown coverage is never proposed as "without images"


def build_catalog(tmp_path, monkeypatch, populate):
    real = ImageRepository.__init__
    monkeypatch.undo()  # restore the real constructor just to prepare synthetic data
    repo = ImageRepository(tmp_path / "i", state=tmp_path / "r", backups=tmp_path / "b")
    populate(repo)
    monkeypatch.setenv("IMAGE_REPOSITORY_STATE_ROOT", str(tmp_path / "r"))
    monkeypatch.setenv("IMAGE_REPOSITORY_ROOT", str(tmp_path / "i"))
    return real


def test_existing_catalog_is_read_without_modification_and_declares_basis(tmp_path, monkeypatch):
    def populate(repo):
        repo.save(EANS[1], "a.png", photo(), provider="kering", origin="t", view="frontal")
    build_catalog(tmp_path, monkeypatch, populate)
    monkeypatch.setattr(ImageRepository, "__init__", lambda *a, **k: pytest.fail("ImageRepository constructed"))
    service = ProviderService(ProviderStore(tmp_path / "s"), object(), DEFAULTS)
    po.save_link(tmp_path / "s", "marcolin", [573], reviewer="ana")
    connector = MarcolinConnector(service)
    before = tree(tmp_path / "r"), (tmp_path / "r" / ".catalog.sqlite3").read_bytes()
    preview = po.demand_preview(odoo_fixture(), connector, tmp_path / "s", *RANGE)
    assert (tree(tmp_path / "r"), (tmp_path / "r" / ".catalog.sqlite3").read_bytes()) == before
    by = {r["EAN"]: r for r in preview["rows"] if r["Estado"] == po.ELIGIBLE}
    assert by[EANS[1]]["Imágenes"] == po.IMAGES_PARTIAL
    assert by[EANS[0]]["Imágenes"] == po.IMAGES_NONE
    assert all("metadatos" in r["Base"] for r in by.values())
    assert po.pick_pilot(preview["rows"])[-1] == EANS[1]


def test_catalog_with_records_but_no_valid_views_is_distinct_from_no_records(tmp_path, monkeypatch):
    def populate(repo):
        repo.save(EANS[1], "a.png", photo(), provider="kering", origin="t", view="frontal")
        with sqlite3.connect(tmp_path / "r" / ".catalog.sqlite3") as db:
            db.execute("UPDATE representations SET view='desconocida'")
    build_catalog(tmp_path, monkeypatch, populate)
    cov = po.catalog_coverage([EANS[1], EANS[0]], required_views=DEFAULTS.required_views)["coverage"]
    assert cov[EANS[0]]["label"] == "sin_registros"
    assert cov[EANS[1]]["label"] == "sin_vistas_validas"


def test_upc12_and_ean13_forms_are_one_product_in_the_preview():
    upc = make_ean("003600029145")[1:]
    lines = [po.OrderLine(upc, "R1", "p", "s", "O1", 1, "2026-09-02"),
             po.OrderLine("0" + upc, "R1", "p", "s", "O2", 2, "2026-09-01")]
    preview = po.build_preview(lines, {})
    assert [r["Estado"] for r in preview["rows"]] == [po.ELIGIBLE, po.DUPLICATE]
    assert preview["rows"][0]["EAN"] == upc  # the literal code is preserved
    live = po.build_preview(lines[:1], {}, live_keys=frozenset({po.ean_key("0" + upc)}))
    assert live["rows"][0]["Motivo"] == "solicitud en curso"
