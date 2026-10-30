"""Failure messages of the manual URL flow and the three independent competitor states (offline, FakeSite)."""
from __future__ import annotations

import pytest
from streamlit.testing.v1 import AppTest

import price_analytics as analytics
from price_connectors import ConnectorError
from price_web import FAILURES, WebPriceSearch, failure_reason, mismatch_reason
from tests.test_price_review_fixes import _freeze
from tests.web_fixtures import BASE, PRADA, FakeSite, competitor_id, make_search, page, prada_page, url_of

SCRIPT = "from app_pages.price_comparator import render_price_comparator_page\nrender_price_comparator_page()"
EAN = "8056597875349"
OTHER = "8056597877329"
GOOD = url_of(PRADA[EAN]["slug"])


def _setup(tmp_path):
    site = FakeSite()
    store, search = make_search(tmp_path, site)
    return site, store, search, competitor_id(store)


def _stored(store, cid):
    with store._connect() as connection:
        return [dict(r) for r in connection.execute(
            "SELECT url,status,last_price FROM competitor_product_urls WHERE competitor_id=?", (cid,))]


def _offers(store) -> int:
    with store._connect() as connection:
        return connection.execute("SELECT COUNT(*) FROM offers").fetchone()[0]


def _comparable(store, search) -> dict[str, int]:
    summary, _ = store.dashboard(store.price_expiry_days())
    pairs = analytics.comparable_pairs(summary, store.current_offers(store.price_expiry_days()))
    return pairs["competitor"].value_counts().to_dict() if not pairs.empty else {}


def _state(search, **kwargs):
    return search.competitor_states(**kwargs).set_index("name").loc["Visio Factory"]


# --- 404/410, wrong product, variant, EAN ------------------------------------------------------
@pytest.mark.parametrize("status", [404, 410])
def test_missing_page_asks_for_another_url_and_stores_nothing(tmp_path, status):
    site, store, search, cid = _setup(tmp_path)
    site.add(GOOD, "", status=status)
    sub = search.submit_url(EAN, cid, GOOD)
    assert (sub.state, sub.reason, sub.action) == ("error", "gone", "url")
    assert "404/410" in sub.message and "Facilite la URL" in sub.message
    assert _stored(store, cid) == [] and search.needs_url(EAN, cid)


def test_wrong_product_can_be_replaced_without_touching_history(tmp_path):
    site, store, search, cid = _setup(tmp_path)
    wrong = f"{BASE}/gafas-de-sol/otra.html"
    site.add(wrong, page(name="Prada PR 99ZS 11A22B", price=100, gtin=None))
    sub = search.submit_url(EAN, cid, wrong)
    assert (sub.state, sub.reason, sub.action) == ("rejected", "wrong_product", "url")
    assert "histórico" in sub.message and _offers(store) == 0
    site.add(GOOD, prada_page(EAN))
    assert search.submit_url(EAN, cid, GOOD).state == "verified"
    states = {r["url"]: r["status"] for r in _stored(store, cid)}
    assert states == {wrong: "rejected", GOOD: "verified"}  # the wrong page stays on record
    assert _offers(store) == 1


def test_same_model_with_other_caliber_is_a_variant_problem(tmp_path):
    site, store, search, cid = _setup(tmp_path)
    site.add(GOOD, page(name="Prada PR 27ZS 16K08Z 52-20", price=239, gtin=EAN))
    sub = search.submit_url(EAN, cid, GOOD)
    assert (sub.state, sub.reason) == ("needs_confirmation", "variant_differs")
    assert "otro color o calibre" in sub.message and _offers(store) == 0


def test_contradicting_ean_is_reported_as_ean_conflict(tmp_path):
    site, store, search, cid = _setup(tmp_path)
    site.add(GOOD, page(name="Prada PR 27ZS 16K08Z 54-21", price=239, gtin=OTHER))
    sub = search.submit_url(EAN, cid, GOOD)
    assert (sub.state, sub.reason, sub.action) == ("rejected", "ean_conflict", "url")
    assert "EAN distinto" in sub.message and _offers(store) == 0


# --- price / VAT / market ------------------------------------------------------------------------
def test_page_without_price_keeps_the_url_and_asks_for_extractor_review(tmp_path):
    site, store, search, cid = _setup(tmp_path)
    site.add(GOOD, page(name="Prada PR 27ZS 16K08Z 54-21", price=None, gtin=EAN))
    sub = search.submit_url(EAN, cid, GOOD)
    assert (sub.state, sub.reason, sub.action) == ("verified", "no_price", "review")
    assert "no otra URL" in sub.message
    assert _stored(store, cid)[0]["status"] == "verified" and _offers(store) == 0
    assert not search.needs_url(EAN, cid)


def test_unconfirmed_vat_is_stored_but_never_comparable(tmp_path):
    site, store, search, cid = _setup(tmp_path)
    site.add(GOOD, prada_page(EAN, pretax=None))
    sub = search.submit_url(EAN, cid, GOOD)
    assert (sub.reason, sub.action) == ("vat_unconfirmed", "review")
    assert "IVA no está confirmado" in sub.message
    assert _comparable(store, search) == {}


@pytest.mark.parametrize("overrides", [{"lang": None}, {"currency": "USD"}])
def test_unverified_market_or_foreign_currency_is_not_comparable(tmp_path, overrides):
    site, store, search, cid = _setup(tmp_path)
    site.add(GOOD, prada_page(EAN, **overrides))
    sub = search.submit_url(EAN, cid, GOOD)
    assert sub.reason == "market_currency" and "moneda o mercado" in sub.message.lower()
    assert _comparable(store, search) == {}


def test_confirmed_offer_has_no_reason_and_is_comparable(tmp_path):
    site, store, search, cid = _setup(tmp_path)
    site.add(GOOD, prada_page(EAN))
    sub = search.submit_url(EAN, cid, GOOD)
    assert (sub.state, sub.reason) == ("verified", "")
    assert _comparable(store, search) == {"Visio Factory": 1}


# --- access ----------------------------------------------------------------------------------------
def test_robots_txt_restriction_keeps_the_url_without_fetching_the_page(tmp_path):
    site, store, search, cid = _setup(tmp_path)
    site.add(f"{BASE}/robots.txt", "User-agent: *\nDisallow: /gafas-de-sol\n")
    site.add(GOOD, prada_page(EAN))
    sub = search.submit_url(EAN, cid, GOOD)
    assert (sub.reason, sub.action) == ("robots", "authorization")
    assert GOOD not in site.calls and _stored(store, cid)[0]["status"] == "pending_verification"


@pytest.mark.parametrize("status", [403, 429])
def test_http_block_is_not_a_missing_product_and_is_not_retried(tmp_path, status):
    site, store, search, cid = _setup(tmp_path)
    site.add(GOOD, "", status=status)
    sub = search.submit_url(EAN, cid, GOOD)
    assert (sub.state, sub.reason, sub.action) == ("blocked", "http_blocked", "wait")
    assert "no es un producto inexistente" in sub.message.lower()
    assert _stored(store, cid)[0]["status"] == "pending_verification"
    calls = len(site.calls)
    again = search.submit_url(EAN, cid, f"{GOOD}?x=1")
    assert again.reason == "http_blocked" and len(site.calls) == calls


def test_captcha_is_reported_as_such_and_keeps_the_url(tmp_path):
    site, store, search, cid = _setup(tmp_path)
    site.add(GOOD, "<html><body>Please complete the CAPTCHA to continue</body></html>")
    sub = search.submit_url(EAN, cid, GOOD)
    assert (sub.state, sub.reason, sub.action) == ("blocked", "captcha", "wait")
    assert _stored(store, cid)[0]["status"] == "pending_verification"
    assert search.submit_url(EAN, cid, f"{GOOD}?x=1").reason == "captcha"  # later gate message keeps the cause


@pytest.mark.parametrize("body", [ConnectorError("Error de conexión: Timeout"), None])
def test_temporary_error_stores_nothing_and_does_not_block_the_competitor(tmp_path, body):
    site, store, search, cid = _setup(tmp_path)
    site.add(GOOD, body if body else "", status=200 if body else 503)
    sub = search.submit_url(EAN, cid, GOOD)
    assert (sub.state, sub.reason, sub.action) == ("error", "temporary", "retry")
    assert _stored(store, cid) == [] and search.config(cid)["access_status"] == "robots_allowed"


def test_extraction_failure_keeps_the_url_and_does_not_ask_for_another(tmp_path):
    site, store, search, cid = _setup(tmp_path)
    site.add(GOOD, "<html><body><h1>Prada</h1></body></html>")
    sub = search.submit_url(EAN, cid, GOOD)
    assert (sub.state, sub.reason, sub.action) == ("needs_review", "no_product_data", "review")
    assert "revisión del extractor" in sub.message
    assert [r["url"] for r in _stored(store, cid)] == [GOOD] and not search.needs_url(EAN, cid)


def test_off_domain_redirect_asks_for_the_final_url(tmp_path):
    site, store, search, cid = _setup(tmp_path)
    site.add(GOOD, "", status=302)
    sub = search.submit_url(EAN, cid, GOOD)
    assert sub.reason in {"redirect", "temporary"} and sub.state == "error"
    assert _stored(store, cid) == []


# --- mapping helpers ---------------------------------------------------------------------------------
@pytest.mark.parametrize("status,reasons,expected", [
    ("rejected", ["EAN publicado distinto: 123"], "ean_conflict"),
    ("rejected", ["Distinto marca X, modelo Y"], "wrong_product"),
    ("rejected", ["Distinto color 99"], "variant_differs"),
    ("rejected", ["No se identifican modelo y color"], "wrong_product"),
    ("probable", ["EAN idéntico pero contradice: calibre: revisar"], "variant_differs"),
    ("probable", ["EAN idéntico pero contradice: modelo PR 1: revisar"], "wrong_product"),
    ("probable", ["Falta evidencia de la marca: revisar"], "ambiguous")])
def test_mismatch_reason(status, reasons, expected):
    assert mismatch_reason(status, reasons) == expected


def test_every_reason_explains_and_has_an_action():
    assert {"gone", "wrong_product", "variant_differs", "ean_conflict", "no_price", "vat_unconfirmed",
            "market_currency", "robots", "http_blocked", "captcha", "temporary"} <= set(FAILURES)
    assert all(text and action in {"url", "review", "wait", "authorization", "retry"} for text, action in FAILURES.values())
    assert failure_reason(ConnectorError("x")) == "temporary"


# --- three independent states -------------------------------------------------------------------------
def test_new_competitor_starts_untested_pending_review_without_offers(tmp_path):
    site, store, search, cid = _setup(tmp_path)
    states = search.competitor_states()
    assert set(states["location"]) == {"none"} and set(states["extraction"]) == {"untested"}
    assert set(states["operability"]) == {"pending_review"} and states["offers"].sum() == 0


def test_working_extraction_does_not_mean_authorised(tmp_path):
    site, store, search, cid = _setup(tmp_path)
    site.add(GOOD, prada_page(EAN))
    search.submit_url(EAN, cid, GOOD)
    state = _state(search, comparable=_comparable(store, search))
    assert (state["location"], state["extraction"], state["operability"]) == ("verified", "verified", "pending_review")
    assert (state["offers"], state["comparable"]) == (1, 1)
    search.update_config(cid, terms_reviewed=True, terms_note="Revisadas por Ana el 2026-11-01")
    assert _state(search)["operability"] == "authorized"


def test_incomplete_failed_and_blocked_states(tmp_path):
    site, store, search, cid = _setup(tmp_path)
    site.add(GOOD, page(name="Prada PR 27ZS 16K08Z 54-21", price=None, gtin=EAN))
    search.submit_url(EAN, cid, GOOD)
    assert _state(search)["extraction"] == "incomplete"
    other = f"{BASE}/gafas-de-sol/otra.html"
    site.add(other, page(name="Prada PR 99ZS 11A22B", price=100, gtin=None))
    search.submit_url(OTHER, cid, other)
    assert _state(search)["extraction"] == "incomplete"  # the verified-without-price page still wins
    site2, store2, search2, cid2 = _setup(tmp_path / "b")
    site2.add(other, page(name="Prada PR 99ZS 11A22B", price=100, gtin=None))
    search2.submit_url(OTHER, cid2, other)
    assert _state(search2)["extraction"] == "failed"
    site2.add(GOOD, "", status=429)
    search2.submit_url(EAN, cid2, GOOD)
    blocked = _state(search2)
    assert blocked["operability"] == "blocked" and blocked["location"] == "candidate"


def test_unchecked_page_is_only_a_candidate(tmp_path):
    site, store, search, cid = _setup(tmp_path)
    search.add_known_url(cid, EAN, GOOD)
    state = _state(search)
    assert (state["location"], state["extraction"]) == ("candidate", "untested")


# --- Streamlit ---------------------------------------------------------------------------------------------
@pytest.fixture
def ui(tmp_path, monkeypatch):
    _freeze(monkeypatch)
    site = FakeSite()
    store, search = make_search(tmp_path, site)
    monkeypatch.setenv("PRICE_COMPARATOR_DB_PATH", str(store.database_path))
    monkeypatch.setattr(WebPriceSearch, "_default_factory", staticmethod(site.factory))
    return site, store, search, competitor_id(store)


def _find(app):
    app.text_input(key="web_text").set_value(EAN).run(timeout=90)
    app.multiselect(key="web_competitors").set_value(["Visio Factory"])
    app.button(key="web_search_button").click()
    return app.run(timeout=90)


def _submit(app, cid, url):
    app.text_input(key=f"web_url_{EAN}_{cid}").set_value(url).run(timeout=90)
    app.button(key=f"web_submit_{EAN}_{cid}").click()
    return app.run(timeout=90)


def _states_frame(app):
    return next(f.value for f in app.dataframe if "Operatividad" in f.value.columns).set_index("Competidor")


def test_competitors_tab_shows_three_independent_states_for_the_fourteen(ui):
    app = AppTest.from_string(SCRIPT).run(timeout=90)
    assert not app.exception
    frame = _states_frame(app)
    assert {"Localización", "Extracción", "Operatividad", "Ofertas", "Comparables"} <= set(frame.columns)
    assert set(frame["Operatividad"]) == {"Pendiente de revisión de condiciones"}
    assert "Operativo" not in set(frame["Operatividad"]) | set(frame["Extracción"])


def test_competitors_tab_after_a_verified_page_is_not_authorised(ui):
    site, store, search, cid = ui
    site.add(GOOD, prada_page(EAN))
    search.submit_url(EAN, cid, GOOD)
    row = _states_frame(AppTest.from_string(SCRIPT).run(timeout=90)).loc["Visio Factory"]
    assert (row["Localización"], row["Extracción"], row["Operatividad"]) == (
        "Ficha verificada", "Verificada con datos reales", "Pendiente de revisión de condiciones")
    assert (row["Ofertas"], row["Comparables"]) == (1, 1)


def test_ui_missing_page_message_asks_for_another_url(ui):
    site, store, search, cid = ui
    site.add(GOOD, "", status=404)
    app = _submit(_find(AppTest.from_string(SCRIPT).run(timeout=90)), cid, GOOD)
    assert any("404/410" in w.value and "Facilite la URL" in w.value for w in app.warning)
    assert app.text_input(key=f"web_url_{EAN}_{cid}") is not None


def test_ui_blocked_site_keeps_the_url_and_hides_the_url_field(ui):
    site, store, search, cid = ui
    site.add(GOOD, "", status=429)
    app = _submit(_find(AppTest.from_string(SCRIPT).run(timeout=90)), cid, GOOD)
    assert any("no es un producto inexistente" in w.value.lower() for w in app.warning)
    assert not [t for t in app.text_input if t.key == f"web_url_{EAN}_{cid}"]


def test_ui_extraction_problem_does_not_ask_for_another_url(ui):
    site, store, search, cid = ui
    site.add(GOOD, "<html><body>vacío</body></html>")
    app = _submit(_find(AppTest.from_string(SCRIPT).run(timeout=90)), cid, GOOD)
    assert any("revisión del extractor" in w.value for w in app.warning)
    assert not [t for t in app.text_input if t.key == f"web_url_{EAN}_{cid}"]


def test_ui_verified_page_with_unconfirmed_vat_is_a_warning_not_success(ui):
    site, store, search, cid = ui
    site.add(GOOD, prada_page(EAN, pretax=None))
    app = _submit(_find(AppTest.from_string(SCRIPT).run(timeout=90)), cid, GOOD)
    assert any("IVA no está confirmado" in w.value for w in app.warning)
    assert not any("URL verificada y guardada" in s.value for s in app.success)
