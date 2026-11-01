"""Web finder: extraction, access rules, location, recording and integration with the comparator."""
import pandas as pd
import pytest
from datetime import timedelta

import price_analytics as pa
from price_connectors import ConnectorError
from price_web import WebFichaConnector, WebPriceSearch, WebSearchError
from price_web_extract import extract_product
from tests.test_price_review_fixes import _freeze
from tests.web_fixtures import (BASE, NOW, PRADA, FakeSite, competitor_id, index_url, make_search, page, prada_page,
                                url_of)


@pytest.fixture(autouse=True)
def frozen(monkeypatch):
    _freeze(monkeypatch)


def _open_access(search, cid):
    search.check_access(cid)
    assert search.config(cid)["access_status"] == "robots_allowed"


@pytest.fixture
def pilot(tmp_path):
    site = FakeSite()
    store, search = make_search(tmp_path, site)
    cid = competitor_id(store)
    for ean, data in PRADA.items():
        site.add(url_of(data["slug"]), prada_page(ean))
        index_url(store, cid, ean, url_of(data["slug"]))
    _open_access(search, cid)
    return site, store, search, cid


# --- extraction -------------------------------------------------------------------------------
def test_json_ld_extraction_with_vat_proved_by_pretax_price():
    html = prada_page("8056597875349")
    found = extract_product(html, url_of("x.html"))
    assert (found.price, found.currency, found.availability) == (239.0, "EUR", "available")
    assert found.ean_published == "8056597875349" and found.caliber == "54-21"
    assert found.includes_tax is True and found.vat_rate == 21.0 and found.market == "ES"


def test_vat_stays_unknown_without_evidence():
    html = page(name="Prada PR 27ZS 16K08Z 54-21", price=239, gtin="8056597875349")
    found = extract_product(html, url_of("x.html"))
    assert found.includes_tax is None
    assert extract_product(html, url_of("x.html"), tax_policy="included").includes_tax is True
    unrelated = page(name="Prada PR 27ZS 16K08Z 54-21", price=239, pretax=200)  # ratio 1.195: not a VAT rate
    assert extract_product(unrelated, url_of("x.html")).includes_tax is None


def test_market_comes_from_the_page_never_from_the_domain():
    html = page(name="Prada PR 27ZS 16K08Z 54-21", price=239, lang=None, market=None)
    assert extract_product(html, "https://www.visiofactory.es/x.html").market == ""
    assert extract_product(page(name="n", price=1, lang="fr-BE", market=None), "https://x.es/a").market == ""
    assert extract_product(page(name="n", price=1, lang="fr", market="BE"), "https://x.es/a").market == "BE"


def test_strikethrough_price_is_not_the_selling_price():
    found = extract_product(page(name="Prada PR 27ZS 16K08Z 54-21", price=239, previous=312), url_of("x.html"))
    assert found.price == 239.0 and found.previous_price == 312.0


def test_multiple_offers_are_flagged_ambiguous():
    html = page(name="Prada PR 27ZS 16K08Z 54-21", price=239,
                extra_offer={"@type": "Offer", "priceCurrency": "EUR", "price": "259"})
    assert extract_product(html, url_of("x.html")).ambiguous is True


def test_html_selector_fallback():
    html = ('<html lang="es-ES"><title>Prada PR 27ZS</title><span class="price">239,00 €</span>'
            '<p class="stock">Agotado</p></html>')
    found = extract_product(html, url_of("x.html"), price_selector="span.price", availability_selector="p.stock")
    assert found.price == 239.0 and found.availability == "unavailable" and found.method == "selector"


# --- pilot: three Prada ------------------------------------------------------------------------
def test_pilot_three_prada_prices_stock_and_percentages(pilot):
    site, store, search, cid = pilot
    outcomes = search.run(list(PRADA), [cid])
    assert [o.state for o in outcomes] == ["exact"] * 3
    assert [o.page.price for o in outcomes] == [239.0, 187.0, 219.0]
    assert [o.page.availability for o in outcomes] == ["available", "unavailable", "available"]
    summary, _ = store.dashboard(30)
    rows = summary.set_index("ean")
    assert rows.loc["8056597875349", "difference_eur"] == pytest.approx(73)
    assert rows.loc["8056597875349", "difference_pct"] == pytest.approx(23.397, abs=1e-3)
    assert rows.loc["8056597875226", "difference_eur"] == pytest.approx(54)
    assert rows.loc["8056597875226", "difference_pct"] == pytest.approx(19.780, abs=1e-3)
    assert pd.isna(rows.loc["8056597877329", "competitor_min_eur"])  # out of stock: excluded
    pairs = pa.comparable_pairs(summary, store.current_offers(30))
    assert set(pairs["ean"]) == {"8056597875349", "8056597875226"}


def test_pilot_is_idempotent_and_history_has_no_duplicates(pilot):
    site, store, search, cid = pilot
    search.run(list(PRADA), [cid])
    search.run(list(PRADA), [cid])
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 3
    assert len(store.offer_history("8056597875349")) == 1


def test_price_and_availability_changes_create_history_points(pilot):
    site, store, search, cid = pilot
    ean = "8056597875349"
    search.run([ean], [cid])
    for step, (price, stock) in enumerate([(229, True), (229, False)], start=1):
        search._now = lambda step=step: NOW + timedelta(hours=step)
        site.add(url_of(PRADA[ean]["slug"]), prada_page(ean, price=price, stock=stock))
        search.run([ean], [cid])
    history = store.offer_history(ean).sort_values("captured_at")
    assert list(history["price"]) == [239.0, 229.0, 229.0]
    assert list(history["availability"]) == ["available", "available", "unavailable"]
    summary, _ = store.dashboard(30)
    assert pd.isna(summary.set_index("ean").loc[ean, "competitor_min_eur"])  # no fallback to the older price


def test_unknown_vat_is_stored_but_not_comparable(tmp_path):
    site = FakeSite()
    store, search = make_search(tmp_path, site)
    cid = competitor_id(store)
    ean = "8056597875349"
    site.add(url_of(PRADA[ean]["slug"]), prada_page(ean, pretax=None))
    index_url(store, cid, ean, url_of(PRADA[ean]["slug"]))
    _open_access(search, cid)
    outcome = search.run([ean], [cid])[0]
    assert outcome.state == "exact" and outcome.page.includes_tax is None
    summary, _ = store.dashboard(30)
    assert pd.isna(summary.set_index("ean").loc[ean, "competitor_min_eur"])
    _, frame = search.stored_results(ean)
    assert "IVA sin confirmar (no comparable)" in set(frame["Estado"])


def test_market_not_verified_on_page_means_stored_noncomparable(tmp_path):
    site = FakeSite()
    store, search = make_search(tmp_path, site)
    cid = competitor_id(store)
    ean = "8056597875349"
    site.add(url_of(PRADA[ean]["slug"]), prada_page(ean, market=None))
    index_url(store, cid, ean, url_of(PRADA[ean]["slug"]))
    _open_access(search, cid)
    outcome = search.run([ean], [cid])[0]
    assert "Mercado sin evidencia comercial verificada" in outcome.detail
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 1
    assert store.dashboard()[0].set_index("ean").loc[ean, "competitor_count"] == 0


def test_incompatible_market_is_rejected_by_the_importer(tmp_path):
    site = FakeSite()
    store, search = make_search(tmp_path, site)
    cid = competitor_id(store)
    search.confirm_market(cid, "ES", "EUR")
    ean = "8056597875349"
    site.add(url_of(PRADA[ean]["slug"]), prada_page(ean, currency="GBP"))
    index_url(store, cid, ean, url_of(PRADA[ean]["slug"]))
    _open_access(search, cid)
    outcome = search.run([ean], [cid])[0]
    assert "GBP" in outcome.detail or "moneda" in outcome.detail.lower()
    summary, _ = store.dashboard(30)
    assert pd.isna(summary.set_index("ean").loc[ean, "competitor_min_eur"])


# --- identification ----------------------------------------------------------------------------
def test_wrong_colour_is_rejected_but_wrong_caliber_does_not_change_identity(tmp_path):
    site = FakeSite()
    store, search = make_search(tmp_path, site)
    cid = competitor_id(store)
    ean = "8056597875349"
    other_color = url_of("pr27zs-1ab0a-54-21.html")
    other_caliber = url_of("pr27zs-16k08z-57-21.html")
    site.add(other_color, page(name="Prada PR 27ZS 1AB0A0 54-21", price=200, pretax=165.29, gtin=None))
    site.add(other_caliber, page(name="Prada PR 27ZS 16K08Z 57-21", price=210, pretax=173.55, gtin=None))
    for url in (other_color, other_caliber):
        index_url(store, cid, ean, url)
    _open_access(search, cid)
    outcome = search.locate(ean, cid)
    assert outcome.state == "exact" and outcome.url == other_caliber
    search.record(outcome)
    summary, _ = store.dashboard(30)
    assert summary.set_index("ean").loc[ean, "competitor_min_eur"] == 210


def test_exact_by_model_color_caliber_without_published_ean(tmp_path):
    site = FakeSite()
    store, search = make_search(tmp_path, site)
    cid = competitor_id(store)
    ean = "8056597875349"
    url = url_of(PRADA[ean]["slug"])
    site.add(url, prada_page(ean, gtin=None))
    index_url(store, cid, ean, url)
    _open_access(search, cid)
    outcome = search.locate(ean, cid)
    assert outcome.state == "exact" and outcome.page.ean_published == ""


def test_published_ean_of_another_product_requires_manual_review(tmp_path):
    site = FakeSite()
    store, search = make_search(tmp_path, site)
    cid = competitor_id(store)
    ean = "8056597875349"
    url = url_of(PRADA[ean]["slug"])
    site.add(url, prada_page(ean, gtin="8056597875226"))
    index_url(store, cid, ean, url)
    _open_access(search, cid)
    outcome = search.locate(ean, cid)
    assert outcome.state == "probable" and outcome.reason == "ean_conflict"


# --- discovery ---------------------------------------------------------------------------------
def test_sitemap_discovery_finds_and_indexes_the_page(tmp_path):
    site = FakeSite()
    store, search = make_search(tmp_path, site)
    cid = competitor_id(store)
    ean = "8056597875349"
    url = url_of(PRADA[ean]["slug"])
    site.add(f"{BASE}/sitemap.xml", f"<urlset><url><loc>{url}</loc></url><url><loc>{url_of('otro-modelo.html')}</loc>"
                                    "</url></urlset>")
    site.add(url, prada_page(ean))
    _open_access(search, cid)
    outcome = search.locate(ean, cid)
    assert outcome.state == "exact" and outcome.method.startswith("web:")
    assert [u for u in site.calls if u.endswith(".html")] == [url]  # only the matching candidate is fetched
    with store._connect() as connection:
        assert connection.execute("SELECT status FROM competitor_product_urls WHERE ean=?", (ean,)).fetchone()[0] \
               == "verified"


def test_internal_search_is_used_only_when_configured_and_allowed(tmp_path):
    site = FakeSite()
    store, search = make_search(tmp_path, site)
    cid = competitor_id(store)
    ean = "8056597875349"
    url = url_of(PRADA[ean]["slug"])
    site.add(f"{BASE}/buscar?q=8056597875349", f'<a href="{url}">Prada PR 27ZS 16K08Z</a>')
    site.add(url, prada_page(ean))
    search.update_config(cid, locate_strategy="site_search", search_url_template=f"{BASE}/buscar?q={{query}}",
                         search_verified=True, search_evidence="GET público autorizado offline")
    _open_access(search, cid)
    assert search.locate(ean, cid).state == "exact"
    site.add(f"{BASE}/robots.txt", "User-agent: *\nDisallow: /buscar\n")
    store2_cid = cid
    search.check_access(store2_cid)
    assert search.locate(ean, cid).state in {"exact", "not_allowed"}


def test_not_found_is_reported_honestly(tmp_path):
    site = FakeSite()
    store, search = make_search(tmp_path, site)
    cid = competitor_id(store)
    site.add(f"{BASE}/sitemap.xml", "<urlset></urlset>")
    _open_access(search, cid)
    outcome = search.locate("8056597875349", cid)
    assert outcome.state == "not_found" and "No se localizó" in outcome.detail


def test_dead_url_is_marked_for_relocation_and_history_kept(pilot):
    site, store, search, cid = pilot
    ean = "8056597875349"
    search.run([ean], [cid])
    site.add(url_of(PRADA[ean]["slug"]), "", status=404)
    outcome = search.locate(ean, cid)
    assert outcome.state in {"not_found", "error"}
    with store._connect() as connection:
        assert connection.execute("SELECT status FROM competitor_product_urls WHERE ean=?", (ean,)).fetchone()[0] \
               == "needs_relocation"
    assert len(store.offer_history(ean)) == 1


# --- access rules ------------------------------------------------------------------------------
def test_unchecked_or_restricted_sites_are_never_queried(tmp_path):
    site = FakeSite()
    store, search = make_search(tmp_path, site)
    cid = competitor_id(store)
    ean = "8056597875349"
    index_url(store, cid, ean, url_of(PRADA[ean]["slug"]))
    assert search.locate(ean, cid).state == "not_allowed" and site.calls == []
    site.add(f"{BASE}/robots.txt", "User-agent: *\nDisallow: /\n")
    search.check_access(cid)
    assert search.config(cid)["access_status"] == "robots_restricted"
    calls = len(site.calls)
    assert search.locate(ean, cid).state == "not_allowed" and len(site.calls) == calls


@pytest.mark.parametrize("status", [403, 429])
def test_block_is_recorded_and_not_retried(pilot, status):
    site, store, search, cid = pilot
    site.add(url_of(PRADA["8056597875349"]["slug"]), "", status=status)
    first = search.run(["8056597875349", "8056597875226"], [cid])
    assert first[0].state == "blocked" and len(first) == 1  # the run stops at the first block
    assert search.config(cid)["access_status"] == "blocked"
    calls = len(site.calls)
    again = search.run(["8056597875226"], [cid])
    assert again[0].state == "not_allowed" and len(site.calls) == calls
    assert search.configs().set_index("name").loc["Visio Factory", "connector_status"] == "blocked"


def test_captcha_page_stops_the_site(pilot):
    site, store, search, cid = pilot
    site.add(url_of(PRADA["8056597875349"]["slug"]), "<html>Please verify you are human (captcha)</html>")
    assert search.run(["8056597875349"], [cid])[0].state == "blocked"
    assert search.config(cid)["access_status"] == "blocked"


def test_timeout_is_an_error_with_a_single_retry_and_recorded(pilot):
    site, store, search, cid = pilot
    site.add(url_of(PRADA["8056597875349"]["slug"]), ConnectorError("Error de conexión: Timeout"))
    outcome = search.run(["8056597875349"], [cid])[0]
    assert outcome.state == "error"
    assert sum(1 for u in site.calls if u.endswith("54-21.html")) == 2  # retries=1 by default
    config = search.config(cid)
    assert config["error_count"] == 1 and "Timeout" in config["last_error"]
    assert config["access_status"] == "robots_allowed"


def test_request_budget_is_enforced(pilot):
    site, store, search, cid = pilot
    search.update_config(cid, max_requests=1)
    outcomes = search.run(list(PRADA), [cid])
    assert any("Límite de consultas" in o.detail for o in outcomes)


def test_concurrent_run_is_refused(pilot):
    site, store, search, cid = pilot
    held = search._lock(cid)
    held.acquire()
    try:
        outcome = search.run(["8056597875349"], [cid])[0]
    finally:
        held.release()
    assert outcome.state == "error" and "Otra ejecución" in outcome.detail


# --- configuration -----------------------------------------------------------------------------
def test_fourteen_competitors_start_disabled_and_not_operational(tmp_path):
    from price_comparator import PriceComparatorStore

    store = PriceComparatorStore(tmp_path / "all.sqlite3")
    store.seed_defaults()
    configs = WebPriceSearch(store).configs()
    assert len(configs) == 14 and configs["name"].is_unique
    assert set(configs["connector_status"]) == {"implemented"}
    assert not configs["terms_reviewed"].any() and not configs["scheduled"].fillna(0).astype(bool).any()


def test_config_validation(tmp_path):
    site = FakeSite()
    store, search = make_search(tmp_path, site)
    cid = competitor_id(store)
    for bad in ({"pause_seconds": 0}, {"max_requests": 999}, {"concurrency": 4}, {"tax_policy": "x"},
                {"search_url_template": "http://x/{query}"}, {"search_url_template": "https://x/nope"},
                {"locate_strategy": "scrape"}, {"unknown": 1}, {"terms_reviewed": True}):
        with pytest.raises(WebSearchError):
            search.update_config(cid, **bad)
    search.update_config(cid, terms_reviewed=True, terms_note="Revisado por Vytria el 2026-10-30")
    with pytest.raises(WebSearchError):
        search.confirm_market(cid, "es", "EUR")


def test_operational_needs_real_offer_terms_review_and_robots(pilot):
    site, store, search, cid = pilot
    status = lambda: search.configs().set_index("name").loc["Visio Factory", "connector_status"]  # noqa: E731
    assert status() == "implemented"
    search.run(["8056597875349"], [cid])
    assert status() == "verified"
    search.update_config(cid, terms_reviewed=True, terms_note="Revisado")
    assert status() == "operational"


# --- scheduler integration ---------------------------------------------------------------------
def test_web_connector_requires_review_access_and_a_real_offer(pilot):
    site, store, search, cid = pilot
    connector = WebFichaConnector(store, search)
    with pytest.raises(ConnectorError):
        connector.verify(f"{BASE}/")
    search.run(["8056597875349"], [cid])
    with pytest.raises(ConnectorError):
        connector.verify(f"{BASE}/")  # terms not reviewed
    search.update_config(cid, terms_reviewed=True, terms_note="Revisado")
    connector.verify(f"{BASE}/")
    observations = connector.fetch(f"{BASE}/", ["8056597875349"])
    assert float(observations[0].price) == 239.0 and observations[0].match_status == "exact_match"
    assert observations[0].tax_included is True


def test_scheduler_uses_the_web_connector_and_stays_disabled(pilot):
    from price_scheduler import CompetitorScheduler

    site, store, search, cid = pilot
    scheduler = CompetitorScheduler(store)
    assert "web_ficha" in scheduler.connectors
    assert not scheduler.overview()["enabled"].astype(bool).any()


# --- stored search -----------------------------------------------------------------------------
def test_stored_search_by_ean_model_and_normalised_reference(pilot):
    site, store, search, cid = pilot
    search.run(list(PRADA), [cid])
    level, by_ean = search.stored_results("8056597875349")
    assert level == "ean"
    row = by_ean[by_ean["Competidor"] == "Visio Factory"].iloc[0]
    assert row["Precio"] == 239.0
    assert row["Estado"] == "Precio almacenado" and row["Diferencia €"] == pytest.approx(-73)
    assert row["Diferencia %"] == pytest.approx(-23.397, abs=1e-3)
    _, model_color = search.stored_results("PR 27ZS 16K08Z")
    assert set(model_color["EAN"]) == {"8056597875349"}
    _, stock_only = search.stored_results("PR 65ZS ZVN09T", only_in_stock=True)
    assert stock_only.empty
    _, with_all = search.stored_results("PR 65ZS ZVN09T")
    assert set(with_all["Estado"]) >= {"Oferta sin stock"}
    _, comparable = search.stored_results("PR 65ZS ZVN09T", include_non_comparable=False)
    assert comparable.empty


def test_bottega_reference_variants_are_equivalent(pilot):
    site, store, search, cid = pilot
    _, long_form = search.stored_results("BOTTEGA VENETA BV1274S 005")
    _, short_form = search.stored_results("BV1274S 005")
    _, spaced = search.stored_results("bv1274s-005")
    ean = "0889652637075"  # catalogue EAN normalised to GTIN-13
    assert set(long_form["EAN"]) == set(short_form["EAN"]) == set(spaced["EAN"]) == {ean}
    _, other_colour = search.stored_results("BV1274S 006")
    assert other_colour.empty


def test_old_prices_are_flagged_as_expired_not_current(pilot, monkeypatch):
    site, store, search, cid = pilot
    search.run(["8056597875349"], [cid])
    from datetime import timedelta
    late = WebPriceSearch(store, fetcher_factory=site.factory, clock=lambda: NOW + timedelta(days=60))
    _, frame = late.stored_results("8056597875349")
    assert frame[frame["Competidor"] == "Visio Factory"].iloc[0]["Estado"] == "Precio caducado"


# --- command line ------------------------------------------------------------------------------
def test_cli_status_and_bounded_search(pilot, monkeypatch, capsys):
    import price_web

    site, store, search, cid = pilot
    monkeypatch.setenv("PRICE_COMPARATOR_DB_PATH", str(store.database_path))
    monkeypatch.setattr(WebPriceSearch, "_default_factory", staticmethod(site.factory))
    assert price_web.main(["status"]) == 0
    assert "Visio Factory" in capsys.readouterr().out
    assert price_web.main(["search", "--ean", "8056597875349", "--competitor", "Visio Factory", "--dry-run"]) == 0
    assert "exact | 239 EUR | available" in capsys.readouterr().out
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 0  # dry run writes nothing
    eans = [x for pair in zip(PRADA, PRADA) for x in ("--ean", pair[0])] * 2
    assert price_web.main(["search", *eans, "--competitor", "Visio Factory"]) == 2
    assert price_web.main(["search", "--ean", "8056597875349", "--competitor", "Nadie"]) == 2


def test_scheduler_observation_carries_the_market_declared_by_the_page(pilot):
    site, store, search, cid = pilot
    search.run(["8056597875349"], [cid])
    search.update_config(cid, terms_reviewed=True, terms_note="Revisado")
    observation = WebFichaConnector(store, search).fetch(f"{BASE}/", ["8056597875349"])[0]
    assert observation.market == "ES"
