import price_references as refs

CATALOG = [
    {"ean": "8056597875226", "brand": "Prada", "model": "PR 26ZS", "color": "16K08Z", "caliber": "55-16"},
    {"ean": "8056597875349", "brand": "Prada", "model": "PR 27ZS", "color": "16K08Z", "caliber": "54-21"},
    {"ean": "8056597877329", "brand": "Prada", "model": "PR 65ZS", "color": "ZVN09T", "caliber": "55-20"},
    {"ean": "0889652637075", "brand": "Bottega Veneta", "model": "BV1274S", "color": "005", "caliber": "63-13"},
    {"ean": "1000000000011", "brand": "Ray Ban", "model": "RB2140", "color": "901", "caliber": "50-22"},
    {"ean": "1000000000028", "brand": "Ray Ban", "model": "RB2140", "color": "901", "caliber": "54-22"},
]
BV = CATALOG[3]
PR27 = CATALOG[1]


def test_bottega_reference_variants_are_equivalent():
    index = refs.ReferenceIndex(CATALOG)
    for text in ("BOTTEGA VENETA BV1274S 005", "BV1274S 005", "bv1274s-005", "BV1274S/005", "bv1274s005"):
        level, found = index.lookup(text)
        assert [p["ean"] for p in found] == ["0889652637075"], text
        assert level
    assert index.lookup("BOTTEGA VENETA BV1274S 005")[0] == "brand_model_color"
    assert index.lookup("BV1274S 005")[0] == "model_color"
    assert index.lookup("bv1274s005")[0] == "normalized_variants"


def test_similar_models_and_colours_are_not_confused():
    index = refs.ReferenceIndex(CATALOG)
    assert index.lookup("PR 26ZS 16K08Z")[1][0]["ean"] == "8056597875226"
    assert index.lookup("PR27ZS-16K08Z")[1][0]["ean"] == "8056597875349"
    assert index.lookup("BV1274S 5")[1] == []
    assert index.lookup("PR 27ZS ZVN09T")[1] == []


def test_ean_lookup_first_and_caliber_variants_not_mixed():
    index = refs.ReferenceIndex(CATALOG)
    assert index.lookup("8056597877329")[0] == "ean"
    assert len(index.lookup("RB2140 901")[1]) == 2
    assert [p["ean"] for p in index.lookup("RB2140 901 54-22")[1]] == ["1000000000028"]


def test_search_plan_order():
    levels = [level for level, _ in refs.search_plan(BV)]
    assert levels[0] == "ean"
    assert levels[1:3] == ["brand_model_color", "model_color"]
    assert "normalized_variants" in levels
    assert ("normalized_variants", "BV1274S005") in refs.search_plan(BV)


def test_caliber_normalisation():
    assert refs.normalize_caliber("54\u25a121") == "54-21"
    assert refs.normalize_caliber("54/21") == "54-21"
    assert refs.normalize_caliber("63") == "63"
    assert refs.compare_calibers("54-21", "54-20") == "different"
    assert refs.compare_calibers("63-13", "63") == "equal"
    assert refs.compare_calibers("", "63") == "unknown"


def test_exact_by_published_ean():
    result = refs.classify(PR27, {"ean_published": "8056597875349", "title": "Prada PR 27ZS 16K08Z"})
    assert (result.status, result.basis) == (refs.EXACT, "ean")


def test_exact_by_reference_without_ean():
    result = refs.classify(PR27, {"brand": "Prada", "model": "PR 27ZS", "color": "16K08Z", "caliber": "54-21"})
    assert (result.status, result.basis) == (refs.EXACT, "reference")


def test_without_ean_and_without_caliber_is_probable():
    result = refs.classify(PR27, {"brand": "Prada", "model": "PR 27ZS", "color": "16K08Z"})
    assert result.status == refs.PROBABLE and not result.comparable


def test_different_colour_or_caliber_is_rejected():
    assert refs.classify(PR27, {"brand": "Prada", "model": "PR 27ZS", "color": "ZVN09T",
                                "caliber": "54-21"}).status == refs.REJECTED
    assert refs.classify(PR27, {"brand": "Prada", "model": "PR 27ZS", "color": "16K08Z",
                                "caliber": "55-21"}).status == refs.REJECTED


def test_other_ean_is_rejected_and_contradicting_ean_goes_to_review():
    assert refs.classify(PR27, {"ean_published": "8056597875226"}).status == refs.REJECTED
    result = refs.classify(PR27, {"ean_published": "8056597875349", "model": "PR 27ZS", "color": "ZVN09T"})
    assert result.status == refs.PROBABLE


def test_title_parsing_and_brand_evidence():
    ok = refs.classify(BV, {"title": "Gafas Bottega Veneta BV1274S 005 63-13", "caliber": "63-13"})
    assert ok.status == refs.EXACT
    no_brand = refs.classify(BV, {"title": "BV1274S 005", "caliber": "63-13"})
    assert no_brand.status == refs.PROBABLE
    assert refs.classify(BV, {"title": "Gafas BV1274S 006", "caliber": "63-13"}).status == refs.REJECTED
