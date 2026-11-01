from pathlib import Path
from urllib.parse import quote

from price_references import EXACT, PROBABLE, REJECTED, classify
from price_web import product_search_urls
from price_web_extract import extract_edel_search_cards, extract_product
from tests.web_fixtures import FakeSite, make_search

FIXTURE = Path(__file__).parent / "fixtures" / "edel_optics_search_results.html"
SEARCH_URL = "https://www.edel-optics.es/B%C3%BAsqueda.html?query=8056597877329"
EAN = "8056597877329"
PRODUCT = {"ean": EAN, "brand": "Prada", "model": "PR 65ZS", "color": "ZVN09T", "caliber": "55-20"}


def test_result_cards_keep_identity_price_and_availability_scoped_per_card():
    html = FIXTURE.read_text(encoding="utf-8")
    cards = extract_edel_search_cards(html, SEARCH_URL, PRODUCT, tax_policy="included")

    assert len(cards) == 3
    by_url = {page.url: page for page in cards}
    first = by_url["https://www.edel-optics.es/PR-65ZS-ZVN09T-de-Prada.html"]
    second = by_url["https://www.edel-optics.es/PR-27ZS-16K08Z-de-Prada.html"]
    third = by_url["https://www.edel-optics.es/PR-67ZS-1BC5W1-de-Prada.html"]
    assert (first.brand, first.model, first.color, first.price, first.currency) == (
        "Prada", "PR 65ZS", "ZVN09T", 187.99, "EUR")
    assert (second.model, second.color, second.price) == ("PR 27ZS", "16K08Z", 243.5)
    assert (third.model, third.color, third.price) == ("PR 67ZS", "1BC5W1", 243.5)
    assert all(page.availability == "unknown" for page in cards)
    assert all(page.ean_published == "" for page in cards)
    assert all(page.includes_tax is True and page.source_url == SEARCH_URL for page in cards)
    assert classify(PRODUCT, first.candidate()).status == EXACT
    assert classify(PRODUCT, second.candidate()).status == REJECTED
    assert classify(PRODUCT, third.candidate()).status == REJECTED


def test_search_results_page_is_never_extracted_as_one_product():
    html = FIXTURE.read_text(encoding="utf-8")
    assert extract_product(html, SEARCH_URL) is None
    one_card = html.split('<div class="eoViewsListviewItemClassic eoItemRight', 1)[0]
    assert extract_product(one_card, SEARCH_URL) is None


def test_ean_conflict_requires_review_and_caliber_does_not_change_identity():
    listing = {"brand": "Prada", "model": "PR 65ZS", "color": "ZVN09T",
               "caliber": "99-99", "ean_published": "8056597875349"}
    assert classify(PRODUCT, listing).status == PROBABLE
    assert classify(PRODUCT, {**listing, "ean_published": ""}).status == EXACT
    assert classify(PRODUCT, {**listing, "ean_published": "", "color": "16K08Z"}).status == REJECTED


def test_ean_search_uses_the_configured_query_and_never_fetches_each_card(tmp_path):
    site = FakeSite()
    store, search = make_search(tmp_path, site)
    cid = next(row["id"] for row in store.competitors().to_dict("records")
               if row["name"] == "Edel-Optics")
    search_url = "https://www.edel-optics.es/B%C3%BAsqueda.html?query=" + quote(EAN, safe="")
    site.add("https://www.edel-optics.es/robots.txt", "User-agent: *\nAllow: /\n")
    site.add(search_url, FIXTURE.read_text(encoding="utf-8"))
    search.confirm_market(cid, "ES", "EUR")
    search.update_config(
        cid, access_status="robots_allowed", locate_strategy="site_search", search_mode="ean",
        search_url_template="https://www.edel-optics.es/B%C3%BAsqueda.html?query={query}",
        search_verified=True, search_evidence="Offline fixture test; no live mechanism verification",
        tax_policy="included", sales_market="ES", market_verified=True,
        market_evidence_url="https://www.edel-optics.es/entrega", market_note="Revisión de test")

    assert product_search_urls(PRODUCT, search.config(cid), modes=["ean"]) == [("ean", search_url)]
    outcome = search.run([EAN], [cid])[0]

    assert (outcome.state, outcome.url, outcome.page.price, outcome.page.availability) == (
        "exact", "https://www.edel-optics.es/PR-65ZS-ZVN09T-de-Prada.html", 187.99, "unknown")
    assert outcome.method == "web:edel-optics-search-card"
    assert outcome.page.source_url == search_url
    assert search_url in outcome.detail
    assert site.calls.count(search_url) == 1
    assert outcome.url not in site.calls
    with store._connect() as connection:
        rows = connection.execute("SELECT price,availability,match_status FROM offers").fetchall()
    assert [(row["price"], row["availability"], row["match_status"]) for row in rows] == [
        (187.99, "unknown", "exact_match")]
    assert store.dashboard()[0].set_index("ean").loc[EAN, "competitor_count"] == 0
