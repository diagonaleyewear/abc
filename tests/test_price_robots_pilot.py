"""Configuration checks never confer permission to fetch products. SQLite and HTTP are simulated."""
from urllib.parse import urlsplit

import pytest
from filelock import FileLock
from streamlit.testing.v1 import AppTest

from price_connectors import ConnectorError
from price_pilot import pilot_scope
from price_scheduler import CompetitorScheduler
from price_web import WebSearchError
from price_web_fetch import WebFetcher
from tests.test_price_pilot import known
from tests.web_fixtures import BASE, ROBOTS_OK

pytest_plugins = ["tests.test_price_pilot"]


def origin(search, cid):
    parsed = urlsplit(search.config(cid)["site_url"])
    return f"{parsed.scheme}://{parsed.netloc}"


def snapshots(store):
    with store._connect() as connection:
        return {table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                for table in ("products", "offers", "offer_checks", "search_results", "pilot_runs", "pilot_jobs",
                              "pilot_budgets", "price_pilot", "competitor_schedule", "scheduler_jobs")}


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("selected", [True, False])
def test_check_registered_competitor_with_any_pilot_state(pilot, enabled, selected, monkeypatch):
    service, store, search, site, cid, extras = pilot
    if enabled:
        service.enable()
    if not selected:
        cid = next(row["id"] for row in store.competitors().to_dict("records")
                   if row["id"] not in pilot_scope(store)["competitors"])
    url = origin(search, cid) + "/robots.txt"
    site.add(url, ROBOTS_OK)
    # Configure a large ordinary budget/retries: the dedicated check remains exactly one request.
    search.update_config(cid, retries=3, max_requests=200)
    state = snapshots(store)
    scope = pilot_scope(store)
    monkeypatch.setattr("price_web.extract_product", lambda *args, **kwargs: pytest.fail("No product extraction"))
    result = search.check_access(cid)
    assert result["access_status"] == "robots_allowed" and result["requests"] == 1
    assert site.calls == [url]
    config = search.config(cid)
    assert config["robots_checked_at"] and "robots.txt leído" in config["robots_summary"]
    assert config["access_status"] == "robots_allowed"
    assert config["terms_reviewed"] == (1 if selected else 0)  # Never grant authorization.
    assert snapshots(store) == state
    assert pilot_scope(store) == scope
    assert CompetitorScheduler(store).run_once(force=True) == []
    with pytest.raises(WebSearchError):
        search.locate(extras[0], cid)
    with pytest.raises(WebSearchError):
        search.locate(scope["eans"][0], cid)
    with pytest.raises(WebSearchError):
        search.run(scope["eans"], [cid])
    assert site.calls == [url]
    with store._connect() as c:
        assert c.execute("SELECT SUM(enabled) FROM competitor_schedule").fetchone()[0] == 0
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not c.execute("PRAGMA foreign_key_check").fetchall()


def test_domanci_and_all_fourteen_registered_competitors(pilot):
    service, store, search, site, _, _ = pilot
    service.enable()
    rows = store.competitors().to_dict("records")
    assert len(rows) == 14
    before = snapshots(store)
    for row in rows:
        url = origin(search, row["id"]) + "/robots.txt"
        site.add(url, ROBOTS_OK)
        result = search.check_access(row["id"])
        assert result["competitor"] == row["name"] and result["access_status"] == "robots_allowed"
        assert site.calls[-1] == url
    domanci = next(row["id"] for row in rows if row["name"] == "Domanci")
    assert search.config(domanci)["access_status"] == "robots_allowed"
    assert len(site.calls) == 14 and all(url.endswith("/robots.txt") for url in site.calls)
    assert snapshots(store) == before


@pytest.mark.parametrize("status,body,expected", [
    (200, "User-agent: *\nDisallow: /\n", "robots_restricted"),
    (200, ROBOTS_OK, "robots_allowed"),
    (404, "", "unknown"), (410, "", "unknown"), (500, "", "unknown"),
    (302, "", "unknown"), (403, "", "blocked"), (429, "", "blocked"),
    (200, "<html>Verify you are human captcha</html>", "blocked"),
    (200, "<html>Product 239 EUR</html>", "unknown"),
    (200, '<script type="application/ld+json">{"price":239}</script>', "unknown"),
    (200, "User-agent: *\nDisallow: /captcha\n", "robots_allowed"),
])
def test_result_status_errors_and_no_retries(pilot, status, body, expected):
    service, store, search, site, cid, _, = pilot
    url = BASE + "/robots.txt"
    search.update_config(cid, retries=3)
    site.add(url, body, status)
    before = snapshots(store)
    result = search.check_access(cid)
    assert result["access_status"] == expected
    assert result["requests"] == 1 and site.calls == [url]
    config = search.config(cid)
    assert config["access_status"] == expected
    assert config["robots_checked_at"] and config["robots_summary"]
    if expected in {"blocked", "unknown"}:
        assert config["last_error"]
    if expected == "blocked":
        assert config["blocked_at"]
        from datetime import datetime, timedelta
        search._now = lambda: datetime.fromisoformat(config["robots_checked_at"]) + timedelta(hours=1)
        repeated = search.check_access(cid)
        assert repeated["access_status"] == "blocked" and repeated["requests"] == 0
        assert search.config(cid)["blocked_at"] == config["blocked_at"]
        assert search.config(cid)["robots_checked_at"] != config["robots_checked_at"]
        assert site.calls == [url]
    assert snapshots(store) == before


@pytest.mark.parametrize("message", ["Error de conexión: Timeout", "No se pudo resolver el dominio del feed"])
def test_timeout_and_resolution_failure_are_recorded(pilot, message):
    service, store, search, site, cid, _ = pilot
    site.add(BASE + "/robots.txt", ConnectorError(message))
    result = search.check_access(cid)
    assert result["access_status"] == "unknown" and result["requests"] == 1
    assert message in search.config(cid)["last_error"]
    assert site.calls == [BASE + "/robots.txt"]


@pytest.mark.parametrize("target", [
    "https://www.visiofactory.es/product.html", "https://evil.example/robots.txt",
    "http://www.visiofactory.es/robots.txt", "https://www.visiofactory.es/robots.txt?product=1",
])
def test_robots_redirect_never_followed(pilot, target):
    service, store, search, site, cid, _ = pilot
    calls = []

    def redirect(url, headers, timeout):
        calls.append(url)
        return 302, {"Location": target}, b""

    source = WebFetcher(transport=redirect, retries=3, max_requests=200)
    result = search.check_access(cid, fetcher=source)
    assert result["access_status"] == "unknown" and calls == [BASE + "/robots.txt"]
    assert source.requests == 1
    # No temporary transport, EAN context or permission is installed on the source.
    with pytest.raises(WebSearchError):
        search.locate(pilot_scope(store)["eans"][0], cid, fetcher=source)
    assert calls == [BASE + "/robots.txt"]


@pytest.mark.parametrize("url", [
    "https://evil.example/product.html", "https://www.visiofactory.es.evil.example/",
    "http://www.visiofactory.es/", "https://user:pass@www.visiofactory.es/",
    "https://www.visiofactory.es:8443/", "https://127.0.0.1/",
    "https://localhost/", "https://host.local/",
])
def test_invalid_configured_url_fails_before_http(pilot, url):
    service, store, search, site, cid, _ = pilot
    with store._connect() as c, c:
        c.execute("UPDATE competitors SET url=? WHERE id=?", (url, cid))
    with pytest.raises(WebSearchError):
        search.check_access(cid)
    assert site.calls == []


def test_nonexistent_or_url_as_competitor_id_is_not_configurable(pilot):
    service, store, search, site, cid, _ = pilot
    for invalid in ["nonexistent", "https://evil.example/robots.txt", "../outside"]:
        with pytest.raises(WebSearchError, match="no registrado"):
            search.check_access(invalid)
    assert site.calls == []


def test_configured_path_and_query_never_requested(pilot):
    service, store, search, site, cid, _ = pilot
    with store._connect() as c, c:
        c.execute("UPDATE competitors SET url=? WHERE id=?", (BASE + "/product.html?query=EAN#fragment", cid))
    search.check_access(cid)
    assert site.calls == [BASE + "/robots.txt"]


def test_lock_prevents_configuration_check_during_competitor_request(pilot):
    service, store, search, site, cid, _ = pilot
    with FileLock(f"{store.database_path}.competitor-{cid}.lock", timeout=0):
        with pytest.raises(WebSearchError, match="Otra ejecución"):
            search.check_access(cid)
    assert site.calls == []


def test_access_check_does_not_change_pilot_budgets_or_manual_restrictions(pilot):
    service, store, search, site, cid, _ = pilot
    known(pilot)
    scope = pilot_scope(store)
    search.check_access(cid)
    assert pilot_scope(store) == scope
    service.enable()
    service.run(search=search)
    results, metrics = service.results()
    assert len(results) == 120 and set(results.ean) == set(scope["eans"])
    assert set(results.competitor_id) == set(scope["competitors"])
    assert metrics["Peticiones HTTP"] == 2  # separate pilot budget: its robots + one known page
    assert len(site.calls) == 3
    assert len(pilot_scope(store)["eans"]) == 20 and len(pilot_scope(store)["competitors"]) == 6


def test_ui_robot_check_does_not_show_commercial_guard_error(pilot, monkeypatch):
    service, store, search, site, cid, _ = pilot
    import app_pages.price_comparator as page
    monkeypatch.setenv("PRICE_COMPARATOR_DB_PATH", str(store.database_path))
    monkeypatch.setattr(page, "WebPriceSearch", lambda store: search)
    domanci = next(row["id"] for row in service.competitors() if row["name"] == "Domanci")
    site.add(origin(search, domanci) + "/robots.txt", ROBOTS_OK)
    app = AppTest.from_string("from app_pages.price_comparator import render_price_comparator_page\n"
                              "render_price_comparator_page()").run(timeout=30)
    assert not app.exception
    app.button(key=f"web_{domanci}_robots").click().run(timeout=30)
    assert not app.exception and not app.error
    assert any("Comprobación de acceso realizada. Resultado: permitido" in item.value for item in app.info)
    assert any("Falta registrar la revisión" in item.value for item in app.info)
    assert search.config(domanci)["access_status"] == "robots_allowed"
    assert app.button(key="update_competitors").disabled
    assert site.calls == [origin(search, domanci) + "/robots.txt"]


def test_same_api_still_refuses_products_after_successful_check(pilot):
    service, store, search, site, cid, _ = pilot
    search.check_access(cid)
    service.enable()
    for action in [
        lambda: search.submit_url(pilot_scope(store)["eans"][0], cid, BASE + "/product.html"),
        lambda: search.run(pilot_scope(store)["eans"], [cid]),
    ]:
        with pytest.raises(WebSearchError, match="Hay un piloto configurado"):
            action()
    assert site.calls == [BASE + "/robots.txt"]


def test_future_registered_competitor_uses_same_configuration_operation(pilot):
    import pandas as pd
    service, store, search, site, _, _ = pilot
    scope = pilot_scope(store)
    report = store.import_competitors(pd.DataFrame([{"Competidor": "Nueva Tienda",
                                                    "Página web": "https://new-shop.example/es/"}]))
    assert not report.rejected
    cid = next(row["id"] for row in store.competitors().to_dict("records") if row["name"] == "Nueva Tienda")
    site.add("https://new-shop.example/robots.txt", ROBOTS_OK)
    assert search.check_access(cid)["access_status"] == "robots_allowed"
    assert site.calls == ["https://new-shop.example/robots.txt"]
    assert pilot_scope(store) == scope


def test_shared_fetcher_keeps_budget_and_has_no_extra_commercial_permission(pilot):
    service, store, search, site, cid, _ = pilot
    source = WebFetcher(transport=site.transport, max_requests=1, retries=3, sleep=lambda _: None)
    assert search.check_access(cid, fetcher=source)["requests"] == 1
    # The configuration operation never increases the fetcher's commercial HTTP allowance.
    result = search.check_access(cid, fetcher=source)
    assert result["access_status"] == "unknown" and result["requests"] == 0
    assert source.requests == 1 and source.retries == 3
    assert site.calls == [BASE + "/robots.txt"]


def test_public_host_validation_is_preserved_for_real_transport(pilot, monkeypatch):
    import socket
    from price_web_fetch import _requests_transport
    service, store, search, site, cid, _ = pilot
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))])
    monkeypatch.setattr("requests.get", lambda *args, **kwargs: pytest.fail("No request to private DNS target"))
    source = WebFetcher(transport=_requests_transport)
    result = search.check_access(cid, fetcher=source)
    assert result["access_status"] == "unknown"
    assert "interna" in search.config(cid)["last_error"]
    assert site.calls == []
