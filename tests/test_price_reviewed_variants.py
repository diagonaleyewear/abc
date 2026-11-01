"""Synthetic regressions inspired by the dated 84-cell review; no live shop prices or HTTP."""
import json
from datetime import timedelta

import pandas as pd
import pytest
import requests

from price_discovery import PilotDiscovery
from price_pilot import SAMPLE_EANS, pilot_scope
from price_references import EXACT, REJECTED, ReferenceIndex, classify
from price_web import Outcome
from price_web_extract import extract_product
from tests.test_price_discovery import commerce, configure, only_pending
from tests.test_price_review_fixes import _freeze, _vytria
from tests.web_fixtures import BASE as SITE, NOW, FakeSite, competitor_id, index_url, make_search, prada_page

pytest_plugins = ["tests.test_price_pilot"]

PR26 = {"ean": "8056597875226", "brand": "Prada", "model": "PR 26ZS", "color": "16K08Z"}
FORD = {"ean": "0889214684097", "brand": "Tom Ford", "model": "FT1044", "color": "93E"}
DUNE = {"ean": "0889652429687", "brand": "Saint Laurent", "model": "SL 596 DUNE", "color": "003"}
BASE = "https://shop.example/products/glasses"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    _freeze(monkeypatch)

    def reject_http(*args, **kwargs):
        pytest.fail("Only synthetic fixtures and FakeSite transport are allowed")

    monkeypatch.setattr(requests.Session, "send", reject_http)


def product(*, name="Prada PR 26ZS 16K08Z", ean=PR26["ean"], url=BASE, price=150, color="16K08Z",
            availability="InStock", **offer_fields):
    node = {"@type": "Product", "name": name, "gtin13": ean, "url": url, "color": color,
            "offers": {"@type": "Offer", "price": price, "priceCurrency": "EUR",
                       "availability": availability, **offer_fields}}
    return node


def html(nodes, extra=""):
    return '<script type="application/ld+json">' + json.dumps(nodes) + "</script>" + extra


@pytest.mark.parametrize("group", [False, True])
def test_active_variant_uses_its_own_identity_price_and_stock(group):
    wrong = product(name="Prada PR 26ZS 01A01D", ean="8056597875349", color="01A01D",
                    url=BASE + "?variant=first", price=177)
    target = product(url=BASE + "?variant=target", price=153, availability="OutOfStock")
    nodes = {"@type": "ProductGroup", "hasVariant": [wrong, target]} if group else [wrong, target]
    page = extract_product(html(nodes), BASE + "?variant=target")
    assert (page.ean_published, page.color, page.price, page.availability) == (
        PR26["ean"], "16K08Z", 153, "unavailable")
    assert not page.ambiguous
    assert classify(PR26, page.candidate()).status == EXACT


def test_offer_variant_url_disambiguates_product_without_product_url():
    wrong = product(ean="8056597875349", color="01A01D", price=177)
    target = product(price=153)
    for node, variant in [(wrong, "first"), (target, "target")]:
        del node["url"]
        node["offers"]["url"] = BASE + "?variant=" + variant
    page = extract_product(html([wrong, target]), BASE + "?variant=target")
    assert page.price == 153 and not page.ambiguous


@pytest.mark.parametrize("target_query", ["?variant=missing", ""])
def test_unresolved_variant_never_chooses_first_product(target_query):
    nodes = [product(url=BASE + "?variant=one", price=177),
             product(url=BASE + "?variant=two", price=153, availability="OutOfStock")]
    page = extract_product(html(nodes), BASE + target_query)
    assert page.ambiguous and page.price is None


def test_transient_global_price_is_not_used_for_selected_variant():
    wrong = product(url=BASE + "?variant=first", price=177)
    target = product(url=BASE + "?variant=target", price=None)
    page = extract_product(html([wrong, target], '<span class="price">177 EUR</span>'),
                           BASE + "?variant=target", price_selector="span.price")
    assert page.ean_published == PR26["ean"] and page.price is None
    reopened = extract_product(html([wrong, product(url=BASE + "?variant=target", price=153)]),
                               BASE + "?variant=target")
    assert reopened.price == 153


def test_selected_variant_does_not_inherit_related_ean_vat_or_market():
    target = product(url=BASE + "?variant=target", price=121)
    del target["gtin13"]
    wrong = product(url=BASE + "?variant=first", ean="8056597875349")
    page = extract_product(html([wrong, target], '<aside>EAN 8056597875349</aside>'
                                '<meta property="product:pretax_price:amount" content="100">'
                                '<meta property="product:market" content="ES">'), BASE + "?variant=target")
    assert page.ean_published == "" and page.includes_tax is None and page.market == ""


@pytest.mark.parametrize("color", ["93E", "52S", "01E"])
def test_ft1044_slash_s_alias_preserves_color_identity(color):
    candidate = {"title": f"Tom Ford Bronson FT1044/S {color}", "brand": "Tom Ford",
                 "model": "FT1044/S", "color": color}
    assert classify(FORD, candidate).status == (EXACT if color == "93E" else REJECTED)


@pytest.mark.parametrize("title", ["Saint Laurent SL 596 DUNE OPT 003",
                                 "Saint Laurent SL 596 DUNE OPT 003 58-18"])
def test_dune_opt_is_not_the_sunglasses_model_even_with_target_ean(title):
    assert classify(DUNE, {"title": title, "ean_published": DUNE["ean"]}).status != EXACT


@pytest.mark.parametrize("text", ["Añadir al carrito", "Disponible bajo pedido", "Entrega en 6-8 días"])
def test_cart_order_and_delivery_are_not_physical_stock(text):
    node = product(availability="")
    page = extract_product(html(node, f'<p class="stock">{text}</p>'), BASE, availability_selector="p.stock")
    assert page.price == 150 and page.availability == "unknown"


def test_conflicting_stock_selector_is_not_ignored_when_jsonld_says_instock():
    page = extract_product(html(product(), '<p class="stock">Agotado</p>'), BASE,
                           availability_selector="p.stock")
    assert page.availability == "unknown"


def test_stock_and_delivery_delay_preserve_both_signals_and_require_review():
    page = extract_product(html(product(), '<p class="stock">En Stock. Entrega en 40 días</p>'),
                           BASE, availability_selector="p.stock")
    assert page.availability == "unknown"
    assert any("40" in note and "Stock" in note for note in page.notes)


@pytest.mark.parametrize("spec", [
    {"priceType": "https://schema.org/ListPrice", "price": 300},
    {"priceType": "https://schema.org/SalePrice", "price": 120,
     "eligibleQuantity": {"minValue": 2}},
    {"priceType": "https://schema.org/SalePrice", "price": 120,
     "validForMemberTier": {"@type": "MemberProgramTier", "name": "Members"}},
])
def test_rrp_or_conditional_specification_never_fills_missing_sale_price(spec):
    node = product(price=None, priceSpecification=spec)
    page = extract_product(html(node), BASE)
    assert page.price is None


def test_multiple_gtins_in_same_product_do_not_hide_contradiction():
    node = product()
    node["gtin13"] = [PR26["ean"], "8056597875349"]
    page = extract_product(html(node), BASE)
    assert page.ambiguous and classify(PR26, page.candidate()).status != EXACT


def test_explicit_jsonld_model_cannot_hide_behind_matching_title_and_ean():
    node = product()
    node["model"] = "PR 99ZS"
    page = extract_product(html(node), BASE)
    assert page.model == "PR 99ZS" and classify(PR26, page.candidate()).status != EXACT


@pytest.mark.parametrize("status", ["OnlineOnly", "InStoreOnly"])
def test_sales_channel_is_not_proof_of_stock(status):
    page = extract_product(html(product(availability=status)), BASE)
    assert page.availability == "unknown"
    assert status in page.stock_evidence


def test_same_price_conflicting_stock_keeps_identity_separate():
    node = product()
    node["offers"] = [node["offers"], {**node["offers"], "availability": "OutOfStock"}]
    page = extract_product(html(node), BASE)
    assert page.price == 150 and page.availability == "unknown" and page.stock_conflict
    assert not page.ambiguous and classify(PR26, page.candidate()).status == EXACT


def test_single_declared_variant_without_active_url_is_not_guessed():
    page = extract_product(html(product(url=BASE + "?variant=first")), BASE)
    assert page.price is None and page.ambiguous


def test_rrp_tax_does_not_prove_vat_for_the_sale_price():
    node = product(priceSpecification={"priceType": "ListPrice", "price": 300, "valueAddedTaxIncluded": True})
    page = extract_product(html(node), BASE)
    assert page.price == 150 and page.previous_price == 300 and page.includes_tax is None


def test_missing_sale_preserves_published_currency_stock_and_rrp():
    node = product(price=None, availability="OutOfStock",
                   priceSpecification={"priceType": "ListPrice", "price": 300})
    page = extract_product(html(node), BASE)
    assert page.price is None and page.currency == "EUR" and page.previous_price == 300
    assert page.availability == "unavailable" and not page.has_price


@pytest.mark.parametrize("model", ["FT1044", "FT1044/S"])
def test_catalogue_reference_lookup_uses_same_alias_as_classification(model):
    index = ReferenceIndex([{**FORD, "model": model}])
    for query in ("Tom Ford FT1044 93E", "Tom Ford FT1044/S 93E"):
        assert index.lookup(query)[1] == [{**FORD, "model": model}]
    assert index.lookup("Tom Ford FT1044/S 52S")[1] == []


@pytest.mark.parametrize("target", [
    {"ean": "8056597875349", "brand": "Prada", "model": "PR 27ZS", "color": "16K08Z"},
    {"ean": "8056597877329", "brand": "Prada", "model": "PR 65ZS", "color": "ZVN09T"},
    PR26, FORD,
    {"ean": "0889652494838", "brand": "Gucci", "model": "GG1660S", "color": "002"},
    DUNE,
])
def test_six_reviewed_identity_codes_remain_text_without_commercial_assumptions(target):
    title = f"{target['brand']} {target['model']} {target['color']}"
    node = product(name=title, ean=target["ean"], color=target["color"], availability="")
    node["brand"] = target["brand"]
    page = extract_product(html(node), BASE)
    assert page.ean_published == target["ean"] and page.color == target["color"]
    assert classify(target, page.candidate()).status == EXACT
    assert page.availability == "unknown" and page.includes_tax is None and page.market == ""


def test_tom_ford_published_upc_equivalence_with_slash_s_is_preserved():
    node = product(name="Tom Ford FT1044/S 93E 60-12", color="93E")
    del node["gtin13"]
    node["gtin12"] = "889214684097"
    page = extract_product(html(node), BASE)
    assert page.ean_published == FORD["ean"]
    assert classify(FORD, page.candidate()).status == EXACT


def test_active_offer_in_one_product_is_not_the_first_offer():
    node = product()
    node["offers"] = [
        {"@type": "Offer", "url": BASE + "?variant=one", "price": 177,
         "priceCurrency": "EUR", "availability": "InStock"},
        {"@type": "Offer", "url": BASE + "?variant=target", "price": 153,
         "priceCurrency": "EUR", "availability": "OutOfStock"},
    ]
    page = extract_product(html(node), BASE + "?variant=target")
    assert page.price == 153 and page.availability == "unavailable" and not page.ambiguous


def test_duplicate_variant_url_with_inconsistent_data_is_ambiguous():
    page = extract_product(html([product(price=177), product(price=153)]), BASE)
    assert page.ambiguous and page.price is None


def test_offer_url_cannot_override_another_variants_explicit_product_identity():
    node = product(url=BASE + "?variant=first", color="01A01D")
    node["offers"]["url"] = BASE + "?variant=target"
    page = extract_product(html(node), BASE + "?variant=target")
    assert page.ambiguous and page.price is None


def test_variant_url_query_order_and_relative_links_preserve_binding():
    target = product(url="?lang=es&variant=target", price=153)
    wrong = product(url="?lang=es&variant=first", price=177)
    page = extract_product(html([wrong, target]), BASE + "?variant=target&lang=es")
    assert page.price == 153 and page.variant_id == "target" and not page.ambiguous
    assert BASE + "?lang=es&variant=target" in page.variant_urls


@pytest.mark.parametrize("text", ["PVP recomendado 300 EUR", "Cupón HLLW8 -8%: 120 EUR",
                                 "Precio histórico 130 EUR"])
def test_selector_cannot_turn_rrp_coupon_or_history_into_sale_price(text):
    page = extract_product(html(product(price=None), f'<p class="price">{text}</p>'), BASE,
                           price_selector="p.price")
    assert page.price is None and page.notes


def test_visible_sale_price_is_not_modified_by_rrp_or_unvalidated_coupon():
    node = product(price=153, priceSpecification=[
        {"priceType": "ListPrice", "price": 300},
        {"priceType": "SalePrice", "price": 120, "eligibleQuantity": {"minValue": 2}},
    ])
    page = extract_product(html(node, "<p>HLLW8: -8%, ends 2026-11-01; not applied</p>"), BASE)
    assert page.price == 153 and page.previous_price == 300
    assert any("condicionado" in note for note in page.notes)


def test_shipping_transit_is_preserved_separately_without_inventing_stock_or_market():
    node = product(availability="", shippingDetails={"deliveryTime": {
        "transitTime": {"@type": "QuantitativeValue", "minValue": 6, "maxValue": 8, "unitCode": "DAY"}}})
    page = extract_product(html(node), BASE)
    assert page.availability == "unknown" and page.market == ""
    assert len(page.delivery_evidence) == 1 and "transitTime" in page.delivery_evidence[0]


def test_search_itemlist_and_nested_related_product_are_not_a_product_page():
    node = {"@type": "ItemList", "itemListElement": [product(price=153)]}
    assert extract_product(html(node), BASE + "?q=PR26ZS") is None


@pytest.mark.parametrize("fields", [
    {"includes_tax": None}, {"availability": "unknown"}, {"availability": "unavailable"},
    {"market": ""}, {"market": "AT"},
])
def test_exact_identity_does_not_bypass_commercial_validation(pilot, fields):
    service, store, search, site, cid, _ = pilot
    node = product(url=SITE + "/product.html", price=153,
                   valueAddedTaxIncluded=True, eligibleRegion="ES")
    page = extract_product(html(node), node["url"])
    for name, value in fields.items():
        setattr(page, name, value)
    assert classify(PR26, page.candidate()).status == EXACT
    outcome = Outcome(PR26["ean"], cid, "Visio Factory", "exact", url=page.url, page=page)
    validation = {}
    assert service._record(search, search.config(cid), outcome, validation=validation)
    assert not validation["accepted"]
    assert store.dashboard()[1]["confirmed"] == 0 and site.calls == []
    if fields.get("availability") == "unavailable":
        assert store.offer_history(PR26["ean"]).iloc[0]["price"] == 153


def test_stock_delivery_conflict_is_saved_but_not_compared(pilot):
    service, store, search, site, cid, _ = pilot
    page = extract_product(html(product(valueAddedTaxIncluded=True, eligibleRegion="ES"),
                                '<p class="stock">En Stock. Entrega en 40 días</p>'),
                           SITE + "/product.html", availability_selector="p.stock")
    outcome = Outcome(PR26["ean"], cid, "Visio Factory", "exact", url=page.url, page=page)
    assert service._record(search, search.config(cid), outcome)
    saved = store.offer_history(PR26["ean"]).iloc[0]
    assert saved["availability"] == "unknown" and saved["price"] == 150
    assert "40 días" in saved["evidence"] and "Stock" in saved["evidence"]
    assert store.dashboard()[1]["confirmed"] == 0 and site.calls == []


def test_verified_url_avoids_search_and_missing_static_price_does_not_fill_from_history(tmp_path):
    site = FakeSite()
    store, search = make_search(tmp_path, site)
    cid = competitor_id(store)
    ean = PR26["ean"]
    url = SITE + "/product.html"
    index_url(store, cid, ean, url)
    search.update_config(cid, locate_strategy="index")
    site.add(url, prada_page(ean, price=153, stock=True))
    search.check_access(cid)
    first = search.run([ean], [cid])[0]
    assert first.state == "exact"
    history_before = store.offer_history(ean).to_dict("records")
    index_before = search._indexed(cid, ean)
    assert index_before[0]["status"] == "verified"
    site.calls.clear()
    search._now = lambda: NOW + timedelta(hours=1)
    site.add(url, html(product(name="Prada PR 26ZS 16K08Z", price=None),
                       "<script>window.priceLoadedLater = true;</script>"))
    current = search.run([ean], [cid])[0]
    assert site.calls == [SITE + "/robots.txt", url]
    assert current.state == "exact" and current.page.price is None and not current.priced
    assert current.reason == "no_price"
    assert store.offer_history(ean).to_dict("records") == history_before
    assert search._indexed(cid, ean)[0]["url"] == url


def test_discovery_rejects_52s_then_evaluates_93e_and_keeps_both_evidences(pilot):
    service, store, search, site, cid, _ = pilot
    store.import_products(pd.DataFrame([{
        "EAN": FORD["ean"], "MARCA": FORD["brand"], "MODELO": FORD["model"], "COLOR": FORD["color"],
    }]))
    _vytria(store, FORD["ean"], 300)
    scope = pilot_scope(store)
    service.save([*SAMPLE_EANS[:-1], FORD["ean"]], scope["competitors"])
    configure(search, cid)
    only_pending(store, cid, [FORD["ean"]])
    wrong, right = SITE + "/ft1044-52s.html", SITE + "/ft1044-93e.html"
    site.add(SITE + "/search?q=" + FORD["ean"], f'<a href="{wrong}">FT1044 52S</a>'
             f'<a href="{right}">FT1044 93E</a>')
    for url, color in [(wrong, "52S"), (right, "93E")]:
        node = product(name=f"Tom Ford FT1044/S {color}", ean="", color=color, url=url, price=153)
        node["brand"] = "Tom Ford"
        site.add(url, html(node))
    service.enable()
    before = commerce(store)
    discovery = PilotDiscovery(store)
    run = discovery.run(selection=[(cid, FORD["ean"])], search=search)
    rows = discovery.candidates().set_index("url")
    assert rows.loc[wrong, "state"] == "rejected" and rows.loc[right, "state"] == "candidate_found"
    assert "52S" in rows.loc[wrong, "identity"] and "93E" in rows.loc[right, "identity"]
    assert site.calls == [SITE + "/robots.txt", SITE + "/search?q=" + FORD["ean"], wrong, right]
    assert commerce(store) == before and search._indexed(cid, FORD["ean"]) == []
    with store._connect() as connection:
        assert connection.execute("SELECT requests FROM pilot_budgets WHERE run_id=?", (run,)).fetchone()[0] == 4
    preview = discovery.preview([(cid, FORD["ean"])])
    assert preview["effective"] == [] and preview["candidate_excluded"] == [[cid, FORD["ean"]]]
    discovery.review(int(rows.loc[right, "id"]), accept=True, note="Offline exact identity review")
    assert search._indexed(cid, FORD["ean"])[0]["url"] == right
    assert discovery.preview([(cid, FORD["ean"])])["known_excluded"] == [[cid, FORD["ean"]]]
    assert len(site.calls) == 4 and commerce(store) == before
