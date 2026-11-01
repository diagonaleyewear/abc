"""Streamlit: the finder asks for the exact page when nothing is found, learns it and never asks twice."""
import pytest
from streamlit.testing.v1 import AppTest

from price_web import WebPriceSearch
from tests.test_price_review_fixes import _freeze
from tests.web_fixtures import PRADA, FakeSite, competitor_id, make_search, prada_page, url_of

SCRIPT = "from app_pages.price_comparator import render_price_comparator_page\nrender_price_comparator_page()"
EAN = "8056597875349"
OTHER = "8056597877329"
PROMPT = "No hemos localizado este producto en Visio Factory. ¿Puedes facilitarnos la URL?"


@pytest.fixture
def ui(tmp_path, monkeypatch):
    _freeze(monkeypatch)
    site = FakeSite()
    store, search = make_search(tmp_path, site)
    monkeypatch.setenv("PRICE_COMPARATOR_DB_PATH", str(store.database_path))
    monkeypatch.setattr(WebPriceSearch, "_default_factory", staticmethod(site.factory))
    return site, store, search, competitor_id(store)


def _search(app=None):
    app = app or AppTest.from_string(SCRIPT).run(timeout=90)
    app.selectbox(key="price_admin_section").set_value("Búsqueda manual").run(timeout=90)
    app.text_input(key="web_text").set_value(EAN).run(timeout=90)
    app.multiselect(key="web_competitors").set_value(["Visio Factory"])
    app.button(key="web_search_button").click()
    return app.run(timeout=90)


def _prompted(app) -> bool:
    return any(PROMPT in m.value for m in app.markdown)


def _submit(app, cid, url):
    app.text_input(key=f"web_url_{EAN}_{cid}").set_value(url).run(timeout=90)
    app.button(key=f"web_submit_{EAN}_{cid}").click()
    return app.run(timeout=90)


def test_prompt_appears_when_nothing_is_found_and_is_not_shown_without_competitor(ui):
    site, store, search, cid = ui
    app = _search()
    assert not app.exception and _prompted(app)
    assert any(b.label == "Verificar y guardar" for b in app.button)
    assert site.calls == []


def test_correct_url_is_verified_and_never_requested_again(ui):
    site, store, search, cid = ui
    url = url_of(PRADA[EAN]["slug"])
    site.add(url, prada_page(EAN))
    app = _submit(_search(), cid, url)
    assert not app.exception and any("URL verificada y guardada" in s.value for s in app.success)
    app = _search()
    assert not _prompted(app)
    frame = next(f.value for f in app.dataframe if "Ficha registrada" in f.value.columns)
    assert frame.iloc[0]["Ficha registrada"] == "URL verificada y guardada" and frame.iloc[0]["Precio"] == 239.0


def test_wrong_product_is_reported_and_the_prompt_stays(ui):
    site, store, search, cid = ui
    url = url_of(PRADA[OTHER]["slug"])
    site.add(url, prada_page(OTHER))
    app = _submit(_search(), cid, url)
    assert any("Coincidencia incorrecta" in w.value for w in app.warning)
    assert _prompted(app)


def test_other_domain_is_refused_without_any_request(ui):
    site, store, search, cid = ui
    app = _submit(_search(), cid, "https://www.edel-optics.es/prada.html")
    assert any("dominio autorizado" in w.value for w in app.warning)
    assert site.calls == []


def test_ambiguous_page_asks_for_confirmation_before_saving(ui):
    site, store, search, cid = ui
    url = url_of(PRADA[EAN]["slug"])
    extra = {"@type": "Offer", "priceCurrency": "EUR", "price": "199", "availability": "https://schema.org/InStock"}
    site.add(url, prada_page(EAN, extra_offer=extra))
    app = _submit(_search(), cid, url)
    assert any("probable o ambigua" in w.value for w in app.warning)
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM competitor_product_urls").fetchone()[0] == 0
    app.button(key=f"web_confirm_{EAN}_{cid}").click()
    app.run(timeout=90)
    assert any("pendiente de revisión" in i.value for i in app.info)
    with store._connect() as connection:
        assert connection.execute("SELECT status FROM competitor_product_urls").fetchone()[0] == "probable"
        assert connection.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 0


def test_blocked_site_keeps_the_url_pending(ui):
    site, store, search, cid = ui
    url = url_of(PRADA[EAN]["slug"])
    site.add(url, "", status=403)
    app = _submit(_search(), cid, url)
    assert any("pendiente de verificación" in w.value for w in app.warning)
    assert search.url_state(EAN, cid)["text"] == "Acceso bloqueado"
