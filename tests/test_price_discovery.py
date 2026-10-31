"""Supervised discovery and commercial evidence use only temporary databases and simulated HTTP."""
import json
from urllib.parse import quote

import pytest
from filelock import FileLock
from streamlit.testing.v1 import AppTest

from price_comparator import PriceComparatorStore
from price_discovery import PilotDiscovery
from price_pilot import PilotError, SAMPLE_EANS, pilot_scope
from price_scheduler import CompetitorScheduler
from price_web import Outcome, WebSearchError, product_search_urls
from price_web_extract import ProductPage, extract_product, resolve_market
from tests.test_price_pilot import known
from tests.web_fixtures import BASE, ROBOTS_OK, prada_page

pytest_plugins = ["tests.test_price_pilot"]
EAN = "8056597875349"


def commerce(store):
    with store._connect() as connection:
        return {table: [tuple(r) for r in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                for table in ("products", "offers", "offer_checks", "search_results", "vytria_prices",
                              "vytria_price_history", "competitor_schedule", "price_pilot")}


def configure(search, cid, **fields):
    search.update_config(cid, locate_strategy="index,site_search,sitemap",
                         search_mode="ean", search_url_template=BASE + "/search?q={query}",
                         search_verified=True, search_evidence="GET público autorizado, revisor offline, 2026-10-31",
                         **fields)


def only_pending(store, cid, eans):
    coverage = PilotDiscovery(store).coverage()
    with store._connect() as connection, connection:
        for row in coverage.to_dict("records"):
            if row["competitor_id"] != cid or row["ean"] not in eans:
                connection.execute("""INSERT INTO pilot_discovery(competitor_id,ean,state,updated_at)
                    VALUES(?,?,'not_found','offline')""", (row["competitor_id"], row["ean"]))


def route(site, query, *, ean=EAN, **overrides):
    url = BASE + "/pr27zs-16k08z.html"
    site.add(BASE + "/search?q=" + quote(query, safe=""), f'<a href="{url}">Prada PR27ZS 16K08Z {ean}</a>')
    site.add(url, prada_page(ean, **overrides))
    return url


def exact_page(market=""):
    return ProductPage(url=BASE + "/product.html", title="Prada Symbole PR27ZS 16K08Z 54-21 Negro",
                       brand="Prada", identifiers=["PR27ZS", "PR27ZS 16K08Z 54-21"], ean_published=EAN,
                       price=239, currency="EUR", includes_tax=True, vat_rate=21, availability="available",
                       market=market, market_source="ficha:eligibleRegion")


@pytest.mark.parametrize("verified,market,expected", [(True, "ES", "ES"), (False, "ES", ""), (False, "", "")])
def test_market_config_requires_manual_evidence_and_never_updates_products(pilot, verified, market, expected):
    _, store, search, site, cid, _ = pilot
    before = commerce(store)
    search.update_config(cid, sales_market=market, market_verified=verified,
                         market_evidence_url=BASE + "/delivery", market_note="Revisor: Ana, 2026-10-31")
    config = search.config(cid)
    assert resolve_market(exact_page(), config)[0] == expected
    assert config["market_verified_at"] if verified else config["market_verified_at"] is None
    assert commerce(store) == before and not site.calls


@pytest.mark.parametrize("fields", [
    {"sales_market": "ES", "market_verified": True},
    {"sales_market": "ES", "market_verified": True, "market_note": "Ana"},
    {"sales_market": "ES", "market_verified": True, "market_note": "Ana",
     "market_evidence_url": "https://evil.example/delivery"},
    {"sales_market": "es"}, {"market_verified": "yes"},
])
def test_invalid_market_verification_fails_explicitly(pilot, fields):
    with pytest.raises(WebSearchError):
        pilot[2].update_config(pilot[4], **fields)
    assert pilot[3].calls == []


@pytest.mark.parametrize("page_market,configured,expected,conflict", [
    ("ES", "ES", "ES", False), ("", "ES", "ES", False),
    ("FR", "ES", "FR", True), ("ES", "PT", "ES", True), ("ES", "", "ES", False),
])
def test_shared_market_resolution_and_pilot_commercial_validation(pilot, page_market, configured, expected, conflict):
    pilot_service, store, search, _, cid, _ = pilot
    search.update_config(cid, sales_market=configured, market_verified=bool(configured),
                         market_evidence_url=BASE + "/delivery", market_note="Ana, 2026-10-31")
    page = exact_page(page_market)
    market, evidence, contradictory = resolve_market(page, search.config(cid))
    assert (market, contradictory) == (expected, conflict)
    if not page_market and configured:
        assert "configuración verificada" in evidence
    elif not conflict:
        assert "ficha" in evidence
    outcome = Outcome(EAN, cid, "Visio Factory", "exact", page.url, method="web:json-ld", page=page)
    validation = {}
    reasons = pilot_service._record(search, search.config(cid), outcome, validation=validation)
    assert bool(reasons) == conflict
    assert validation["market_conflict"] == conflict
    assert validation["market"] == expected
    assert store.dashboard()[1]["confirmed"] == (0 if conflict else 1)


@pytest.mark.parametrize("lang,url", [("es-ES", BASE + "/es/a.html"), ("es", BASE + "/a.html"),
                                     ("fr-BE", "https://store.es/a")])
def test_locale_domain_and_path_never_establish_sales_market(lang, url):
    html = prada_page(EAN, lang=lang, market=None)
    assert extract_product(html, url).market == ""
    html += '<script>var prestashop={"locale":"es-ES"}</script>'
    assert extract_product(html, url).market == ""


def test_explicit_jsonld_and_metadata_markets_and_contradictions():
    product = {"@type": "Product", "name": "Prada PR27ZS 16K08Z", "offers": {
        "@type": "Offer", "price": 239, "priceCurrency": "EUR", "shippingDetails": {
            "shippingDestination": {"@type": "DefinedRegion", "addressCountry": "ES"}}}}
    html = '<script type="application/ld+json">' + json.dumps(product) + "</script>"
    assert extract_product(html, BASE).market == "ES"
    contradictory = extract_product(html + '<meta property="product:market" content="FR">', BASE)
    assert contradictory.market_conflict
    assert resolve_market(contradictory, {})[2]


def test_unknown_market_stored_noncomparable_and_verified_fallback_is_deduplicated(pilot):
    service, store, search, _, cid, _ = pilot
    with store._connect() as connection, connection:
        connection.execute("UPDATE competitors SET market='' WHERE id=?", (cid,))
    page = exact_page()
    outcome = Outcome(EAN, cid, "Visio Factory", "exact", page.url, method="web:json-ld", page=page)
    assert search.record(outcome) == ""
    assert store.dashboard()[0].set_index("ean").loc[EAN, "competitor_count"] == 0
    search.update_config(cid, sales_market="ES", market_verified=True,
                         market_evidence_url=BASE + "/delivery", market_note="Ana, 2026-10-31")
    assert service._record(search, search.config(cid), outcome) == []
    assert service._record(search, search.config(cid), outcome) == []
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 2
        rows = connection.execute("SELECT market,evidence FROM offers ORDER BY id").fetchall()
    assert rows[0]["market"] == "UNKNOWN"
    assert rows[1]["market"] == "ES" and "configuración verificada" in rows[1]["evidence"]


def test_twenty_by_six_identifies_114_missing_without_visiting_six_known(pilot):
    _, store, search, site, _, _ = pilot
    for row in pilot[0].competitors():
        search.add_known_url(row["id"], EAN, row["url"].rstrip("/") + "/pr27zs.html")
    discovery = PilotDiscovery(store)
    frame = discovery.coverage()
    assert len(frame) == 120 and set(frame.ean) == set(SAMPLE_EANS)
    assert len(frame.competitor_id.unique()) == 6
    assert len(discovery.pending()) == 114
    assert (frame.state == "known").sum() == 6 and site.calls == []


@pytest.mark.parametrize("mode,query", [("ean", EAN), ("model_color", "PRADA PR 27ZS 16K08Z")])
def test_supervised_search_exact_candidate_requires_approval_and_reuses_confirmed_url(pilot, mode, query):
    service, store, search, site, cid, _ = pilot
    configure(search, cid)
    search.update_config(cid, search_mode=mode)
    only_pending(store, cid, [EAN])
    url = route(site, query)
    snapshot = commerce(store)
    service.enable()
    before = commerce(store)
    discovery = PilotDiscovery(store)
    run_id = discovery.run(search=search)
    assert commerce(store) == before
    candidates = discovery.candidates()
    assert len(candidates) == 1 and candidates.iloc[0].state == "candidate_found"
    assert search._indexed(cid, EAN) == []  # No automatic approval.
    assert site.calls == [BASE + "/robots.txt", BASE + "/search?q=" + quote(query, safe=""), url]
    with store._connect() as connection:
        assert connection.execute("SELECT requests FROM pilot_budgets WHERE run_id=? AND competitor_id=?",
                                  (run_id, cid)).fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM pilot_http_log").fetchone()[0] == 3
    discovery.review(int(candidates.iloc[0].id), accept=True, note="Ana, identidad publicada revisada offline")
    assert search._indexed(cid, EAN)[0]["status"] == "verified"
    assert commerce(store) == before
    site.calls.clear()
    discovery.run(search=search)
    assert site.calls == []
    assert (discovery.coverage().state == "confirmed").sum() == 1
    service.run(search=search)
    assert site.calls == [BASE + "/robots.txt", url]
    assert store.dashboard()[1]["confirmed"] == 1
    assert len(snapshot["products"]) == 100


@pytest.mark.parametrize("overrides,state", [
    ({"gtin": None}, "pending_validation"),
    ({"gtin": "8056597875226"}, "rejected"),
    ({"name": "Prada PR27ZS ZVN09T 54-21"}, "rejected"),
    ({"name": "Prada PR26ZS 16K08Z 54-21"}, "rejected"),
    ({"extra_offer": {"price": "200", "priceCurrency": "EUR", "availability": "InStock"}}, "pending_validation"),
])
def test_wrong_or_doubtful_identity_is_never_approved(pilot, overrides, state):
    service, store, search, site, cid, _ = pilot
    configure(search, cid)
    only_pending(store, cid, [EAN])
    route(site, EAN, **overrides)
    service.enable()
    before = commerce(store)
    discovery = PilotDiscovery(store)
    discovery.run(search=search)
    candidate = discovery.candidates().iloc[0]
    assert candidate.state == state
    with pytest.raises(PilotError):
        discovery.review(int(candidate.id), accept=True, note="Revisor")
    assert not search._indexed(cid, EAN)
    assert commerce(store) == before
    if state == "pending_validation":
        discovery.review(int(candidate.id), accept=False, note="Descartada por falta de evidencia")
        assert discovery.candidates().iloc[0].state == "discarded"


def test_automatic_fallback_only_uses_authorized_methods(pilot):
    service, store, search, site, cid, _ = pilot
    configure(search, cid)
    search.update_config(cid, search_mode="auto", search_preferred="ean", search_alternatives="model_color")
    only_pending(store, cid, [EAN])
    site.add(BASE + "/search?q=" + EAN, "<html>No results</html>")
    route(site, "PRADA PR 27ZS 16K08Z")
    service.enable()
    discovery = PilotDiscovery(store)
    discovery.run(search=search)
    row = discovery.candidates().iloc[0]
    assert row.method == "model_color"
    assert site.calls[1:3] == [BASE + "/search?q=" + EAN,
                              BASE + "/search?q=" + quote("PRADA PR 27ZS 16K08Z", safe="")]
    assert not any("sitemap" in url for url in site.calls)


@pytest.mark.parametrize("mode,verified,terms", [("known", True, True), ("ean", False, True), ("ean", True, False)])
def test_no_unverified_search_or_unauthorized_competitor_queries(pilot, mode, verified, terms):
    service, store, search, site, cid, _ = pilot
    configure(search, cid)
    search.update_config(cid, search_mode=mode, search_verified=verified,
                         terms_reviewed=terms, terms_note="Offline authorization")
    only_pending(store, cid, [EAN])
    service.enable()
    PilotDiscovery(store).run(search=search)
    assert site.calls == []
    assert PilotDiscovery(store).coverage().query("ean == @EAN and competitor_id == @cid").iloc[0].state == "not_allowed"


def test_not_found_is_persistent_and_explicit_retry_does_not_do_http(pilot):
    service, store, search, site, cid, _ = pilot
    configure(search, cid)
    only_pending(store, cid, [EAN])
    site.add(BASE + "/search?q=" + EAN, "<html>No results</html>")
    service.enable()
    discovery = PilotDiscovery(store)
    discovery.run(search=search)
    assert discovery.coverage().query("ean == @EAN and competitor_id == @cid").iloc[0].state == "not_found"
    calls = list(site.calls)
    discovery.run(search=search)
    assert site.calls == calls
    discovery.reset(EAN, cid, note="Autorizar otro intento tras revisar mecanismo")
    assert (cid, EAN) in discovery.pending() and site.calls == calls


@pytest.mark.parametrize("body,status,state", [
    ("User-agent: *\nDisallow: /search", 200, "not_allowed"),
    (ROBOTS_OK, 403, "blocked"), (ROBOTS_OK, 429, "blocked"),
    ("<html>captcha verify you are human</html>", 200, "blocked"),
])
def test_robots_blocks_and_no_retries(pilot, body, status, state):
    service, store, search, site, cid, _ = pilot
    configure(search, cid)
    only_pending(store, cid, [EAN])
    if status == 200 and "Disallow" in body:
        site.add(BASE + "/robots.txt", body)
    else:
        site.add(BASE + "/search?q=" + EAN, body, status)
    service.enable()
    discovery = PilotDiscovery(store)
    discovery.run(search=search)
    assert discovery.coverage().query("ean == @EAN and competitor_id == @cid").iloc[0].state == state
    assert len(site.calls) <= 2
    assert discovery.candidates().empty


def test_http_budget_is_reserved_before_io_and_never_refunded_on_resume(pilot, monkeypatch):
    service, store, search, site, cid, _ = pilot
    configure(search, cid)
    only_pending(store, cid, [EAN, SAMPLE_EANS[1]])
    route(site, EAN)
    scope = pilot_scope(store)
    scope["competitors"][cid]["max_http_requests"] = 3
    service.save(scope["eans"], scope["competitors"])
    service.enable()
    before = commerce(store)
    discovery = PilotDiscovery(store)
    original = discovery.locate
    attempts = []

    def interrupt(*args):
        result = original(*args)
        attempts.append(args[0])
        if len(attempts) == 1:
            raise RuntimeError("Interrupción simulada después de guardar candidato")
        return result

    monkeypatch.setattr(PilotDiscovery, "locate", lambda self, *args: interrupt(*args))
    with pytest.raises(RuntimeError, match="Interrupción"):
        discovery.run(search=search)
    calls = list(site.calls)
    monkeypatch.setattr(PilotDiscovery, "locate", lambda self, *args: original(*args))
    reopened = PilotDiscovery(PriceComparatorStore(store.database_path))
    reopened.run(search=search, resume=True)
    assert site.calls == calls  # persistent budget exhausted, known candidate not re-fetched
    assert commerce(store) == before
    assert reopened.candidates().iloc[0].state == "candidate_found"
    assert _pending_job_count(store) == 0


def test_sitemap_is_an_explicit_auto_alternative_not_external_search(pilot):
    service, store, search, site, cid, _ = pilot
    search.update_config(cid, search_mode="auto", search_alternatives="sitemap", locate_strategy="index,sitemap")
    only_pending(store, cid, [EAN])
    url = BASE + "/pr27zs-16k08z.html"
    site.add(BASE + "/sitemap.xml", f"<urlset><url><loc>{url}</loc></url></urlset>")
    site.add(url, prada_page(EAN))
    search.external_search = lambda *args: pytest.fail("No external discovery")
    service.enable()
    PilotDiscovery(store).run(search=search)
    assert PilotDiscovery(store).candidates().iloc[0].method == "sitemap"
    assert site.calls == [BASE + "/robots.txt", BASE + "/sitemap.xml", url]


@pytest.mark.parametrize("published,state", [("8056597875226", "candidate_found"), (None, "pending_validation"),
                                            (EAN, "rejected")])
def test_edel_optics_ean_strategy_suggestion_never_proves_published_gtin(pilot, published, state):
    service, store, search, site, _, _ = pilot
    cid = next(row["id"] for row in service.competitors() if row["name"] == "Edel-Optics")
    config = search.config(cid)
    base = config["site_url"].rstrip("/")
    search.update_config(cid, locate_strategy="site_search", search_mode="ean", search_url_template=base + "/search?q={ean}",
                         search_verified=True, search_evidence="Mecanismo público GET verificado offline",
                         terms_reviewed=True, terms_note="Autorización offline", access_status="robots_allowed")
    only_pending(store, cid, ["8056597875226"])
    site.add(base + "/robots.txt", "User-agent: *\nDisallow:")
    site.add(base + "/search?q=8056597875226", '<a href="/pr26zs-16k08z.html">Prada PR 26ZS 16K08Z</a>')
    site.add(base + "/pr26zs-16k08z.html", prada_page("8056597875226", gtin=published))
    service.enable()
    discovery = PilotDiscovery(store)
    discovery.run(search=search)
    assert discovery.candidates().iloc[0].state == state
    assert site.calls == [base + "/robots.txt", base + "/search?q=8056597875226", base + "/pr26zs-16k08z.html"]
    assert search._indexed(cid, "8056597875226") == []


def test_ajax_search_is_reported_without_calling_endpoints(pilot):
    service, store, search, site, cid, _ = pilot
    configure(search, cid)
    only_pending(store, cid, [EAN])
    site.add(BASE + "/search?q=" + EAN, '<script>fetch("/internal/suggest?ean=8056597875349")</script>')
    service.enable()
    discovery = PilotDiscovery(store)
    discovery.run(search=search)
    row = discovery.coverage().query("ean == @EAN and competitor_id == @cid").iloc[0]
    assert row.state == "error" and "AJAX" in row.detail
    assert site.calls == [BASE + "/robots.txt", BASE + "/search?q=" + EAN]


def test_discovery_never_follows_off_domain_redirects(pilot):
    service, store, search, site, cid, _ = pilot
    configure(search, cid)
    only_pending(store, cid, [EAN])
    source = site.factory

    def factory(config):
        fetcher = source(config)
        transport = fetcher._transport

        def redirect(url, headers, timeout):
            if "/search?" in url:
                site.calls.append(url)
                return 302, {"Location": "https://evil.example/product.html"}, b""
            return transport(url, headers, timeout)
        fetcher._transport = redirect
        return fetcher

    search._factory = factory
    service.enable()
    PilotDiscovery(store).run(search=search)
    assert site.calls == [BASE + "/robots.txt", BASE + "/search?q=" + EAN]
    assert PilotDiscovery(store).candidates().empty


def test_auth_withdrawal_before_product_request_interrupts_resumably(pilot):
    service, store, search, site, cid, _ = pilot
    configure(search, cid)
    only_pending(store, cid, [EAN])
    route(site, EAN)
    factory = site.factory

    def revoke(config):
        fetcher = factory(config)
        transport = fetcher._transport

        def wrapped(url, headers, timeout):
            result = transport(url, headers, timeout)
            if "/search?" in url:
                search.update_config(cid, terms_reviewed=False)
            return result
        fetcher._transport = wrapped
        return fetcher

    search._factory = revoke
    service.enable()
    with pytest.raises(PilotError, match="Autorización"):
        PilotDiscovery(store).run(search=search)
    assert site.calls == [BASE + "/robots.txt", BASE + "/search?q=" + EAN]
    with store._connect() as connection:
        assert connection.execute("SELECT status FROM pilot_runs").fetchone()[0] == "interrupted"


def test_review_revalidates_catalogue_and_conflicts_are_never_overridden(pilot):
    service, store, search, site, cid, _ = pilot
    configure(search, cid)
    only_pending(store, cid, [EAN])
    route(site, EAN)
    service.enable()
    discovery = PilotDiscovery(store)
    discovery.run(search=search)
    candidate = discovery.candidates().iloc[0]
    with store._connect() as connection, connection:
        connection.execute("UPDATE products SET color='ZVN09T' WHERE ean=?", (EAN,))
    with pytest.raises(PilotError, match="identidad exacta"):
        discovery.review(int(candidate.id), accept=True, note="Revisor")
    assert search._indexed(cid, EAN) == []


def test_discard_retry_same_url_keeps_prior_evidence_and_new_candidate(pilot):
    service, store, search, site, cid, _ = pilot
    configure(search, cid)
    only_pending(store, cid, [EAN])
    route(site, EAN)
    service.enable()
    discovery = PilotDiscovery(store)
    discovery.run(search=search)
    old = int(discovery.candidates().iloc[0].id)
    discovery.review(old, accept=False, note="Revisor descarta temporalmente")
    discovery.reset(EAN, cid, note="Revisor autoriza repetir tras revisar")
    discovery.run(search=search)
    rows = discovery.candidates()
    assert len(rows) == 2 and set(rows.state) == {"discarded", "candidate_found"}
    assert rows.set_index("id").loc[old, "review_note"] == "Revisor descarta temporalmente"


def test_verified_market_overrides_unverified_legacy_value_without_mutation(pilot):
    service, store, search, _, cid, _ = pilot
    with store._connect() as connection, connection:
        connection.execute("UPDATE competitors SET market='PT' WHERE id=?", (cid,))
    search.update_config(cid, sales_market="ES", market_verified=True,
                         market_evidence_url=BASE + "/delivery", market_note="Ana, 2026-10-31")
    page = exact_page()
    outcome = Outcome(EAN, cid, "Visio Factory", "exact", page.url, method="web:json-ld", page=page)
    assert service._record(search, search.config(cid), outcome) == []
    assert store.dashboard()[0].set_index("ean").loc[EAN, "competitor_count"] == 1
    assert search.config(cid)["market"] == "PT"


def _pending_job_count(store):
    with store._connect() as connection:
        return connection.execute("SELECT COUNT(*) FROM pilot_jobs WHERE status IN ('pending','running')").fetchone()[0]


def test_no_parallel_operations_general_queries_or_outside_scope(pilot):
    service, store, search, site, cid, extras = pilot
    service.enable()
    discovery = PilotDiscovery(store)
    with FileLock(f"{store.database_path}.pilot.lock", timeout=0):
        with pytest.raises(PilotError):
            discovery.run(search=search)
    other = next(r["id"] for r in store.competitors().to_dict("records") if r["id"] not in pilot_scope(store)["competitors"])
    for ean, competitor in [(extras[0], cid), (EAN, other), (EAN, cid)]:
        with pytest.raises(PilotError):
            discovery.locate(ean, competitor, 1, search, search._factory(search.config(cid)))
        with pytest.raises(WebSearchError):
            search.locate(ean, competitor)
    assert CompetitorScheduler(store).run_once(force=True) == []
    assert len(discovery.coverage()) == 120 and len(pilot_scope(store)["eans"]) == 20
    assert len(pilot_scope(store)["competitors"]) == 6 and site.calls == []


@pytest.mark.parametrize("template", [
    "http://www.visiofactory.es/search?q={ean}", "https://evil.example/{ean}",
    BASE + "/search?q={unknown}", BASE + "/search?q={ean.__class__}",
    "https://{query}.visiofactory.es/search", BASE + "/search?q={ean!r}",
    BASE + "/search?q={ean:>10}", BASE + "/search?q={", BASE + "/search",
])
def test_template_variables_and_domain_are_controlled(pilot, template):
    with pytest.raises(WebSearchError):
        pilot[2].update_config(pilot[4], search_url_template=template)
    assert not pilot[3].calls


def test_exact_search_variables_and_normalized_manufacturer_code(pilot):
    _, _, search, _, cid, _ = pilot
    configure(search, cid)
    product = search._product(EAN)
    search.update_config(cid, search_mode="model_color", search_url_template=BASE + "/{model}/{color}?q={query}")
    assert product_search_urls(product, search.config(cid))[0] == (
        "model_color", BASE + "/PR%2027ZS/16K08Z?q=PRADA%20PR%2027ZS%2016K08Z")
    search.update_config(cid, search_mode="ean", search_url_template=BASE + "/search?q={ean}")
    assert product_search_urls(product, search.config(cid))[0][1] == BASE + "/search?q=" + EAN


def test_migration_preserves_legacy_values_and_defaults_to_unverified(pilot):
    _, store, search, _, cid, _ = pilot
    before = commerce(store)
    with store._connect() as connection, connection:
        for field in ("sales_market", "market_verified", "market_evidence_url", "market_note", "market_verified_at",
                      "search_mode", "search_preferred", "search_alternatives", "search_verified", "search_evidence"):
            connection.execute(f"ALTER TABLE competitor_web_config DROP COLUMN {field}")
        connection.execute("ALTER TABLE pilot_runs DROP COLUMN operation")
    reopened = PriceComparatorStore(store.database_path)
    assert commerce(reopened) == before
    config = type(search)(reopened).config(cid)
    assert config["market"] == "ES" and not config["market_verified"] and not config["search_verified"]
    assert config["sales_market"] == "" and config["search_mode"] == "auto"
    with reopened._connect() as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()


def test_ui_coverage_settings_and_approval_are_offline(pilot, monkeypatch):
    import app_pages.price_comparator as page
    _, store, search, site, cid, _ = pilot
    known(pilot)
    monkeypatch.setenv("PRICE_COMPARATOR_DB_PATH", str(store.database_path))
    monkeypatch.setattr(page, "WebPriceSearch", lambda store: search)
    app = AppTest.from_string("from app_pages.price_comparator import render_price_comparator_page\n"
                             "render_price_comparator_page()").run(timeout=60)
    assert not app.exception
    assert any(item.value == "Localización de fichas del piloto" for item in app.subheader)
    assert app.button(key="discovery_start").disabled
    assert app.selectbox(key=f"web_{cid}_market").value == ""
    assert app.selectbox(key=f"web_{cid}_search_mode").value == "auto"
    assert not site.calls
