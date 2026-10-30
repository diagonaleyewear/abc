"""Estructuras reales de fichas de competidores (observadas el 2026-10-30), reducidas y sin conexión."""
from pathlib import Path

import pytest

from price_references import caliber_from_code, classify, extract_lens
from price_web_extract import extract_product

FIXTURES = Path(__file__).parent / "fixtures" / "web_pages"
PR27 = {"ean": "8056597875349", "brand": "Prada", "model": "PR 27ZS", "color": "16K08Z", "caliber": "54-21"}
PR26 = {"ean": "8056597875226", "brand": "Prada", "model": "PR 26ZS", "color": "16K08Z", "caliber": "55-16"}

# nombre, URL, producto buscado, precio, IVA incluido, mercado, calibre extraído
CASES = [
    ("optica_optima", "https://optica-optima.com/gafas-de-sol/prada-pr-27zs-16k08z.html", PR27, 273.0, True, "ES", "54-21"),
    ("amevista", "https://www.amevista.com/es/prada-0pr-27zs-16k08z", PR27, 273.0, None, "", None),
    ("edel_optics", "https://www.edel-optics.es/PR-27ZS-16K08Z-de-Prada.html?selectedSize=1907958", PR27, 243.5, None, "", None),
    ("otticait", "https://www.otticait.com/en/prada-pr-27zs-16k08z", PR27, 241.8, None, "", None),
    ("mia_burton", "https://miaburton.com/es/gafas-de-sol/prada/pr-26zs-16k08z", PR26, 247.0, None, "", None),
    ("congafasdesol", "https://www.congafasdesol.com/prada-pr-27zs-16k08z.html", PR27, 253.5, None, "", None),
    ("blickers", "https://www.blickers.com/en-es/glasses/prada-pr-27zs-c54/5649966192263168/", PR27, 273.0, None, "", "54"),
    ("domanci", "https://domanci.com/es/gafas-de-sol/prada-pr-27zs-16k08z-54.html", PR27, 240.5, True, "ES", "54-21"),
]


def load(name, url):
    return extract_product((FIXTURES / f"{name}.html").read_text(encoding="utf-8"), url)


@pytest.mark.parametrize("name,url,product,price,tax,market,caliber", CASES)
def test_real_structures_are_extracted(name, url, product, price, tax, market, caliber):
    page = load(name, url)
    assert page is not None
    assert page.price == price
    assert page.currency == "EUR"
    assert page.availability == "available"
    assert page.ean_published == product["ean"]
    assert page.includes_tax is tax
    assert page.market == market
    if caliber:
        assert page.caliber == caliber
    assert classify(product, page.candidate()).status == "exact_match"


def test_url_slug_alone_proves_nothing():
    # Misma URL de PR 27ZS pero ficha de otro EAN: no puede ser coincidencia exacta.
    page = load("mia_burton", "https://miaburton.com/es/gafas-de-sol/prada/pr-27zs-16k08z")
    assert classify(PR27, page.candidate()).status == "rejected"


def test_mia_burton_url_is_the_pr26zs_reference():
    page = load("mia_burton", "https://miaburton.com/es/gafas-de-sol/prada/pr-26zs-16k08z")
    assert page.ean_published == "8056597875226"
    assert page.color == ""  # "black" no es un código de color


def test_edel_optics_uses_sale_price_not_recommended_price():
    page = load("edel_optics", "https://www.edel-optics.es/PR-27ZS-16K08Z-de-Prada.html")
    assert page.price == 243.5 and page.price != 390.0


def test_universitaria_without_static_price_is_not_an_offer():
    page = load("optica_universitaria", "https://www.opticauniversitaria.es/gafas-de-sol/prada/prada-pr-27zs-16k08z/312245.html")
    assert page is None or page.price is None
    if page is not None:
        assert page.ean_published == "8056597875349"


def test_several_variant_eans_are_not_guessed():
    html = ('<html lang="es"><body><h1>Prada PR 27ZS</h1><div>EAN 8056597875349</div>'
            '<div>EAN 8056597877329</div><span itemprop="price" content="200"></span></body></html>')
    page = extract_product(html, "https://tienda.example/prada")
    assert page is None or not page.ean_published


@pytest.mark.parametrize("text,lens", [("Buy Prada PR 27ZS C54 16K08Z", "54"), ("PR 27ZS 16K08Z 54", "54")])
def test_lens_only_is_not_a_full_caliber(text, lens):
    assert extract_lens(text) == lens


def test_caliber_from_manufacturer_code():
    assert caliber_from_code("0PR_27ZS16K08Z5421") == "54-21"
    assert caliber_from_code("0PR_27ZS16K08Z54") == ""


def test_comment_closed_with_bang_does_not_swallow_page():
    page = load("edel_optics", "https://www.edel-optics.es/PR-27ZS-16K08Z-de-Prada.html")
    assert page.price == 243.5


def test_json_ld_with_raw_newlines_is_parsed():
    page = load("amevista", "https://www.amevista.com/es/prada-0pr-27zs-16k08z")
    assert page.method == "json-ld"
