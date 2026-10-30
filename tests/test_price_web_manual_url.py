"""Manual collaboration: a person supplies the exact product page; it is validated, verified and learned."""
from __future__ import annotations

import pytest

from price_web import WebSearchError, validate_competitor_url
from tests.web_fixtures import BASE, PRADA, FakeSite, competitor_id, make_search, prada_page, url_of

EAN = "8056597875349"
OTHER = "8056597877329"
GOOD = url_of(PRADA[EAN]["slug"])


def _setup(tmp_path):
    site = FakeSite()
    store, search = make_search(tmp_path, site)
    return site, store, search, competitor_id(store)


def _offers(store, ean=EAN) -> int:
    with store._connect() as connection:
        return connection.execute("SELECT COUNT(*) FROM offers WHERE ean=?", (ean,)).fetchone()[0]


def _row(store, cid, ean, url) -> dict:
    with store._connect() as connection:
        return dict(connection.execute("SELECT * FROM competitor_product_urls WHERE competitor_id=? AND ean=? AND url=?",
                                       (cid, ean, url)).fetchone())


def test_correct_url_is_verified_saved_and_recorded(tmp_path):
    site, store, search, cid = _setup(tmp_path)
    site.add(GOOD, prada_page(EAN))
    assert search.needs_url(EAN, cid)
    sub = search.submit_url(EAN, cid, GOOD)
    assert sub.state == "verified", sub.message
    row = _row(store, cid, EAN, GOOD)
    assert (row["status"], row["source"]) == ("verified", "user")
    assert _offers(store) == 1
    assert search.url_state(EAN, cid)["text"] == "URL verificada y guardada"
    assert not search.needs_url(EAN, cid)


@pytest.mark.parametrize("url", [
    "http://www.visiofactory.es/x.html", "https://user:pw@www.visiofactory.es/x.html",
    "https://www.visiofactory.es:8443/x.html", "https://127.0.0.1/x.html", "https://[::1]/x.html",
    "https://localhost/x.html", "https://www.visiofactory.es.evil.com/x.html", "https://evilvisiofactory.es/x.html",
    "https://www.visiofactory.es@evil.com/x.html", "https://169.254.169.254/latest", "ftp://www.visiofactory.es/x",
    "javascript:alert(1)", "", "https://www.visiofactory.es/a b"])
def test_malicious_or_private_urls_are_rejected_without_network(tmp_path, url):
    site, store, search, cid = _setup(tmp_path)
    sub = search.submit_url(EAN, cid, url)
    assert sub.state == "invalid"
    assert site.calls == []
    assert search.needs_url(EAN, cid)
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM competitor_product_urls").fetchone()[0] == 0


def test_url_from_another_competitor_is_rejected(tmp_path):
    site, store, search, cid = _setup(tmp_path)
    sub = search.submit_url(EAN, cid, "https://www.edel-optics.es/prada-pr27zs.html")
    assert sub.state == "invalid" and "dominio" in sub.message
    assert site.calls == []
    with pytest.raises(WebSearchError):
        search.add_known_url(cid, EAN, "https://www.edel-optics.es/prada-pr27zs.html")


def test_validator_cleans_fragment_and_host_case():
    assert validate_competitor_url("https://WWW.Visiofactory.es/a.html#top", "visiofactory.es") == \
        "https://www.visiofactory.es/a.html"


def test_wrong_product_is_rejected_and_never_compared(tmp_path):
    site, store, search, cid = _setup(tmp_path)
    wrong = url_of(PRADA[OTHER]["slug"])
    site.add(wrong, prada_page(OTHER))
    sub = search.submit_url(EAN, cid, wrong)
    assert sub.state == "rejected"
    assert _row(store, cid, EAN, wrong)["status"] == "rejected"
    assert _offers(store) == 0
    assert search.url_state(EAN, cid)["text"] == "Coincidencia incorrecta"
    assert search.needs_url(EAN, cid)  # the person can try another page


def test_different_color_without_published_ean_is_not_accepted(tmp_path):
    site, store, search, cid = _setup(tmp_path)
    site.add(GOOD, prada_page(EAN, name="Prada PR 27ZS 1AB0A7 54-21", gtin=None))
    sub = search.submit_url(EAN, cid, GOOD)
    assert sub.state != "verified"
    assert _offers(store) == 0


def test_ambiguous_page_needs_confirmation_and_is_not_comparable(tmp_path):
    site, store, search, cid = _setup(tmp_path)
    extra = {"@type": "Offer", "priceCurrency": "EUR", "price": "199", "availability": "https://schema.org/InStock"}
    site.add(GOOD, prada_page(EAN, extra_offer=extra))
    sub = search.submit_url(EAN, cid, GOOD)
    assert sub.state == "needs_confirmation"
    assert _offers(store) == 0
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM competitor_product_urls").fetchone()[0] == 0
    saved = search.save_for_review(sub)
    assert saved.state == "saved_for_review"
    row = _row(store, cid, EAN, GOOD)
    assert row["status"] == "probable" and row["source"] == "user"
    assert _offers(store) == 0
    assert not search.needs_url(EAN, cid)
    assert search.url_state(EAN, cid)["key"] == "probable"
    with pytest.raises(WebSearchError):
        search.save_for_review(saved)


def test_blocked_page_keeps_only_the_url_pending_and_does_not_retry(tmp_path):
    site, store, search, cid = _setup(tmp_path)
    site.add(GOOD, "blocked", status=403)
    sub = search.submit_url(EAN, cid, GOOD)
    assert sub.state == "blocked"
    assert _row(store, cid, EAN, GOOD)["status"] == "pending_verification"
    assert search.config(cid)["access_status"] == "blocked"
    assert _offers(store) == 0
    assert search.url_state(EAN, cid)["text"] == "Acceso bloqueado"
    calls = len(site.calls)
    again = search.submit_url(EAN, cid, url_of("otra-ficha.html"))
    assert again.state == "blocked"
    assert len(site.calls) == calls  # no insistence once the site blocked us


def test_robots_restricted_site_is_not_queried(tmp_path):
    site, store, search, cid = _setup(tmp_path)
    site.add(f"{BASE}/robots.txt", "User-agent: *\nDisallow: /\n")
    sub = search.submit_url(EAN, cid, GOOD)
    assert sub.state == "not_allowed"
    assert GOOD not in site.calls
    assert _row(store, cid, EAN, GOOD)["status"] == "pending_verification"


def test_duplicate_url_is_not_registered_twice(tmp_path):
    site, store, search, cid = _setup(tmp_path)
    site.add(GOOD, prada_page(EAN))
    assert search.submit_url(EAN, cid, GOOD).state == "verified"
    calls = len(site.calls)
    assert search.submit_url(EAN, cid, GOOD).state == "duplicate"
    assert search.submit_url(EAN, cid, GOOD + "#reviews").state == "duplicate"
    assert len(site.calls) == calls
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM competitor_product_urls").fetchone()[0] == 1
    assert _offers(store) == 1


def test_second_url_needs_explicit_replacement(tmp_path):
    site, store, search, cid = _setup(tmp_path)
    site.add(GOOD, prada_page(EAN))
    search.submit_url(EAN, cid, GOOD)
    new = url_of("pr27zs-nueva.html")
    site.add(new, prada_page(EAN, price=229, pretax=round(229 / 1.21, 6)))
    assert search.submit_url(EAN, cid, new).state == "already_registered"
    assert search.submit_url(EAN, cid, new, replace=True).state == "verified"
    assert _row(store, cid, EAN, GOOD)["status"] == "replaced"
    assert _row(store, cid, EAN, new)["status"] == "verified"
    assert _offers(store) == 2  # the old observation stays in the history


def test_verified_url_is_reused_without_asking_or_searching(tmp_path):
    site, store, search, cid = _setup(tmp_path)
    site.add(GOOD, prada_page(EAN))
    search.submit_url(EAN, cid, GOOD)
    site.calls.clear()
    outcome = search.run([EAN], [cid])[0]
    assert outcome.state == "exact" and outcome.url == GOOD
    assert not any("sitemap" in call for call in site.calls)
    assert not search.needs_url(EAN, cid)
    _, frame = search.stored_results(EAN, competitor_ids=[cid])
    assert frame.iloc[0]["Ficha registrada"] == "URL verificada y guardada"


def test_dead_url_is_replaced_keeping_history(tmp_path):
    site, store, search, cid = _setup(tmp_path)
    site.add(GOOD, prada_page(EAN))
    search.submit_url(EAN, cid, GOOD)
    site.add(GOOD, "", status=404)
    search.run([EAN], [cid])
    assert _row(store, cid, EAN, GOOD)["status"] == "needs_relocation"
    assert search.needs_url(EAN, cid)
    assert search.url_state(EAN, cid)["key"] == "relocation"
    new = url_of("pr27zs-movida.html")
    site.add(new, prada_page(EAN, price=229, pretax=round(229 / 1.21, 6)))
    assert search.submit_url(EAN, cid, new).state == "verified"
    assert _row(store, cid, EAN, GOOD)["status"] == "replaced"
    assert _offers(store) == 2


def test_known_url_stays_pending_until_verified_by_a_search(tmp_path):
    site, store, search, cid = _setup(tmp_path)
    search.add_known_url(cid, EAN, GOOD)
    assert _row(store, cid, EAN, GOOD)["status"] == "pending_verification"
    assert search.url_state(EAN, cid)["text"] == "URL aportada por el usuario, pendiente de verificar"
    assert site.calls == []
    site.add(GOOD, prada_page(EAN))
    search.check_access(cid)
    assert search.run([EAN], [cid])[0].state == "exact"
    row = _row(store, cid, EAN, GOOD)
    assert (row["status"], row["source"]) == ("verified", "user")


def test_automatically_located_page_is_labelled_as_such(tmp_path):
    site, store, search, cid = _setup(tmp_path)
    site.add(f"{BASE}/sitemap.xml", f"<urlset><url><loc>{GOOD}</loc></url></urlset>")
    site.add(GOOD, prada_page(EAN))
    search.check_access(cid)
    assert search.run([EAN], [cid])[0].state == "exact"
    assert search.url_state(EAN, cid)["text"] == "Ficha localizada automáticamente"
