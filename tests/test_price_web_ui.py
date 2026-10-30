"""Streamlit: «Buscar precios en la competencia» and per-competitor web configuration."""
import pytest
from streamlit.testing.v1 import AppTest

from price_web import WebPriceSearch
from app_pages.price_comparator import WEB_STATUS_TEXT
from tests.test_price_review_fixes import _freeze
from tests.web_fixtures import PRADA, FakeSite, competitor_id, index_url, make_search, prada_page, url_of

SCRIPT = "from app_pages.price_comparator import render_price_comparator_page\nrender_price_comparator_page()"


@pytest.fixture
def ui(tmp_path, monkeypatch):
    _freeze(monkeypatch)
    site = FakeSite()
    store, search = make_search(tmp_path, site)
    cid = competitor_id(store)
    for ean, data in PRADA.items():
        site.add(url_of(data["slug"]), prada_page(ean))
        index_url(store, cid, ean, url_of(data["slug"]))
    monkeypatch.setenv("PRICE_COMPARATOR_DB_PATH", str(store.database_path))
    monkeypatch.setattr(WebPriceSearch, "_default_factory", staticmethod(site.factory))
    return site, store, search, cid


def _run():
    app = AppTest.from_string(SCRIPT).run(timeout=90)
    assert not app.exception
    return app


def _search(app, text, *, mode="Almacenados", competitors=(), **flags):
    app.text_input(key="web_text").set_value(text).run(timeout=90)  # the button enables once the text is committed
    app.radio(key="web_mode").set_value(mode)
    app.multiselect(key="web_competitors").set_value(list(competitors))
    for key, value in flags.items():
        app.checkbox(key=key).set_value(value)
    app.button(key="web_search_button").click()
    return app.run(timeout=90)


def _frames(app):
    return [frame.value for frame in app.dataframe]


def test_finder_is_present_and_search_is_disabled_without_text(ui):
    app = _run()
    assert any(s.value == "Buscar precios en la competencia" for s in app.subheader)
    assert app.button(key="web_search_button").disabled


def test_stored_search_by_ean_shows_price_and_percentage_against_vytria(ui):
    site, store, search, cid = ui
    search.check_access(cid)
    search.run(list(PRADA), [cid])
    app = _search(_run(), "8056597875349", competitors=["Visio Factory"])
    assert not app.exception and not app.error
    frame = next(f for f in _frames(app) if "Diferencia %" in f.columns)
    row = frame.iloc[0]
    assert row["Competidor"] == "Visio Factory" and row["Precio"] == 239.0
    assert row["Estado"] == "Precio almacenado" and round(row["Diferencia %"], 1) == -23.4


def test_stored_search_by_model_and_colour_and_normalised_reference(ui):
    site, store, search, cid = ui
    app = _search(_run(), "PR 27ZS 16K08Z")
    frame = next(f for f in _frames(app) if "Diferencia %" in f.columns)
    assert set(frame["EAN"]) == {"8056597875349"}
    app = _search(_run(), "BOTTEGA VENETA BV1274S 005")
    assert set(next(f for f in _frames(app) if "Diferencia %" in f.columns)["EAN"]) == {"0889652637075"}
    app = _search(_run(), "BV1274S 005")
    assert set(next(f for f in _frames(app) if "Diferencia %" in f.columns)["EAN"]) == {"0889652637075"}


def test_unknown_reference_reports_no_match_without_querying(ui):
    site, *_ = ui
    app = _search(_run(), "REFERENCIA INEXISTENTE 999")
    assert any("Sin coincidencias" in w.value for w in app.warning)
    assert site.calls == []


def test_web_mode_requires_a_bounded_competitor_selection(ui):
    site, *_ = ui
    app = _search(_run(), "8056597875349", mode="Consultar webs")
    assert any("Elija entre 1 y 3" in e.value for e in app.error)
    assert site.calls == []


def test_web_mode_refuses_sites_whose_access_was_not_checked(ui):
    site, *_ = ui
    app = _search(_run(), "8056597875349", mode="Consultar webs", competitors=["Visio Factory"])
    frame = next(f for f in _frames(app) if "Resultado" in f.columns)
    assert frame.iloc[0]["Resultado"] == "No consultable" and site.calls == []


def test_web_mode_queries_known_page_and_filters_out_of_stock(ui):
    site, store, search, cid = ui
    search.check_access(cid)
    app = _search(_run(), "PR 65ZS ZVN09T", mode="Consultar webs", competitors=["Visio Factory"], web_only_stock=True)
    result = next(f for f in _frames(app) if "Resultado" in f.columns)
    assert result.iloc[0]["Resultado"] == "Coincidencia exacta" and result.iloc[0]["Precio"] == 187.0
    assert result.iloc[0]["Stock"] == "unavailable"
    stored = [f for f in _frames(app) if "Diferencia %" in f.columns]
    assert not stored  # «Solo en stock» hides the out-of-stock offer
    assert any("No hay resultados" in i.value for i in app.info)


def test_web_mode_stops_after_a_block_and_reports_it(ui):
    site, store, search, cid = ui
    search.check_access(cid)
    site.add(url_of(PRADA["8056597875349"]["slug"]), "", status=429)
    app = _search(_run(), "8056597875349", mode="Consultar webs", competitors=["Visio Factory"])
    result = next(f for f in _frames(app) if "Resultado" in f.columns)
    assert result.iloc[0]["Resultado"] == "Bloqueado por el sitio"
    calls = len(site.calls)
    app = _search(_run(), "8056597875349", mode="Consultar webs", competitors=["Visio Factory"])
    assert len(site.calls) == calls


def test_competitors_tab_lists_fourteen_web_configs_none_operational(ui):
    app = _run()
    labels = [e.label for e in app.expander if e.label.endswith(WEB_STATUS_TEXT["implemented"])]
    assert len(labels) == 14
    assert not [e for e in app.expander if e.label.endswith(WEB_STATUS_TEXT["operational"])
                and e.label.split(" · ")[0] in {label.split(" · ")[0] for label in labels}]


def test_web_config_is_persisted_and_validated(ui):
    site, store, search, cid = ui
    app = _run()
    key = f"web_{cid}"
    app.number_input(key=f"{key}_pause").set_value(7.0)
    app.selectbox(key=f"{key}_tax").set_value("included")
    app.button(key=f"{key}_save").click()
    app = app.run(timeout=90)
    assert not app.exception and not app.error
    saved = search.config(cid)
    assert saved["pause_seconds"] == 7.0 and saved["tax_policy"] == "included"
    app.checkbox(key=f"{key}_terms").set_value(True)  # without a note the review is rejected
    app.button(key=f"{key}_save").click()
    app = app.run(timeout=90)
    assert any("nota" in e.value.lower() for e in app.error)
    assert not search.config(cid)["terms_reviewed"]


def test_robots_check_button_records_access(ui):
    site, store, search, cid = ui
    app = _run()
    app.button(key=f"web_{cid}_robots").click()
    app = app.run(timeout=90)
    assert not app.exception
    assert search.config(cid)["access_status"] == "robots_allowed"
    assert site.calls == ["https://www.visiofactory.es/robots.txt"]


def test_known_url_form_rejects_other_domains(ui):
    site, store, search, cid = ui
    app = _run()
    key = f"web_{cid}"
    app.text_input(key=f"{key}_url_ean").set_value("8056597875349")
    app.text_input(key=f"{key}_url_value").set_value("https://otra-tienda.example/ficha.html")
    position = list(search.configs()["competitor_id"]).index(cid)
    submit = [b for b in app.button if b.label == "Añadir ficha conocida"][position]
    submit.click()
    app = app.run(timeout=90)
    assert any("dominio" in e.value for e in app.error)
