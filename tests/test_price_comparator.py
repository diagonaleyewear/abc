from io import BytesIO

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from price_comparator import ImportReport, PriceComparatorStore, normalize_gtin


PRODUCTS = pd.DataFrame([
    {"MARCA": "Marca A", "NOMBRE": "Modelo A", "MODELO": "A1", "COLOR": "01",
     "CATEGORIA": "Sol", "EAN": "036000291452"},
])
COMPETITORS = pd.DataFrame([
    {"Competidor": "Tienda EUR", "Página web": "https://eur.example/"},
    {"Competidor": "Tienda USD", "Página web": "https://usd.example/"},
])


def _offer(competitor, *, price, currency="EUR", fx="", promo="", available="available",
           match="exact_match", market="ES", domain=None):
    domain = domain or f"{competitor.lower()}.example"
    return {
        "EAN": "036000291452",
        "Competidor": competitor,
        "Precio": str(price),
        "Precio promocional": str(promo),
        "Precio anterior": "",
        "Coste envío": "5",
        "Moneda": currency,
        "Tipo cambio EUR": str(fx),
        "Mercado": market,
        "URL": f"https://{domain}/producto/modelo-a",
        "Disponibilidad": available,
        "Estado coincidencia": match,
        "Evidencia": "EAN idéntico visible",
        "Método": "Importación manual",
    }


def _store(tmp_path):
    store = PriceComparatorStore(tmp_path / "prices.sqlite3")
    assert store.import_products(PRODUCTS).imported == 1
    store.import_competitors(COMPETITORS)
    return store


def test_normalize_gtin_preserves_upc_and_checks_digit() -> None:
    assert normalize_gtin("036000291452") == "0036000291452"
    assert normalize_gtin("4006381333931") == "4006381333931"
    with pytest.raises(ValueError, match="GTIN"):
        normalize_gtin("036000291453")
    with pytest.raises(ValueError, match="GTIN"):
        normalize_gtin("36000291452")


def test_catalog_import_is_idempotent_and_incomplete_rows_do_not_erase(tmp_path) -> None:
    store = _store(tmp_path)
    repeated = store.import_products(PRODUCTS)
    assert (repeated.imported, repeated.updated) == (0, 1)

    incomplete = pd.DataFrame([{"EAN": "036000291452", "MARCA": "", "MODELO": "A2"}])
    assert store.import_products(incomplete).updated == 1
    product = store.products().iloc[0]
    assert product["brand"] == "Marca A"
    assert product["model"] == "A2"

    duplicate = pd.concat([PRODUCTS, PRODUCTS], ignore_index=True)
    result = store.import_products(duplicate)
    assert len(result.rejected) == 1
    assert "duplicado" in result.rejected[0]


def test_workbook_import_reads_both_pilot_sheets(tmp_path) -> None:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        PRODUCTS.to_excel(writer, sheet_name="100 gafas", index=False)
        COMPETITORS.to_excel(writer, sheet_name="Competidores", index=False)
    store = PriceComparatorStore(tmp_path / "prices.sqlite3")

    report = store.import_workbook(output.getvalue())

    assert report["products"].imported == 1
    assert report["competitors"].imported == 2
    assert len(store.competitors()) == 2


def test_comparison_uses_exact_available_fresh_same_market_offers_and_eur_fx(tmp_path) -> None:
    store = _store(tmp_path)
    own = pd.DataFrame([{
        "EAN": "036000291452", "Precio": "100", "Moneda": "EUR", "Mercado": "ES",
        "Origen": "Excel Vytria", "Fecha consulta": "2026-10-28T10:00:00Z",
    }])
    assert store.import_own_prices(own).imported == 1
    assert store.import_own_prices(own).imported == 0
    offers = pd.DataFrame([
        _offer("Tienda EUR", price=100, promo=80, domain="eur.example"),
        _offer("Tienda USD", price=90, currency="USD", fx=0.9, domain="usd.example"),
        _offer("Tienda EUR", price=70, available="unavailable", domain="eur.example"),
        _offer("Tienda USD", price=60, match="probable_match", currency="USD",
               fx=0.9, domain="usd.example"),
        _offer("Tienda EUR", price=10, market="FR", domain="eur.example"),
        {**_offer("Tienda EUR", price=1, domain="eur.example"),
         "Fecha consulta": "2020-01-01T00:00:00Z"},
    ])
    imported = store.import_offers(offers)
    assert (imported.imported, len(imported.rejected)) == (6, 0)
    summary, counts = store.dashboard()
    row = summary.iloc[0]
    assert row["competitor_count"] == 2
    assert row["confirmed_offer_count"] == 2
    assert row["competitor_min_eur"] == pytest.approx(80)
    assert row["competitor_mean_eur"] == pytest.approx(80.5)
    assert row["competitor_max_eur"] == pytest.approx(81)
    assert row["difference_eur"] == pytest.approx(20)
    assert row["difference_pct"] == pytest.approx(25)
    assert row["position"] == 3
    assert counts["pending_review"] == 1


def test_offer_import_is_idempotent_and_history_keeps_price_changes(tmp_path) -> None:
    store = _store(tmp_path)
    first = pd.DataFrame([{
        **_offer("Tienda EUR", price=100, domain="eur.example"),
        "Fecha consulta": "2026-10-28T10:00:00Z",
    }])
    second = pd.DataFrame([{
        **_offer("Tienda EUR", price=90, domain="eur.example"),
        "Fecha consulta": "2026-10-29T10:00:00Z",
    }])

    assert store.import_offers(first).imported == 1
    assert store.import_offers(first).imported == 0
    assert store.import_offers(second).imported == 1
    history = store.offer_history("036000291452")
    assert len(history) == 2
    assert set(history["price"]) == {90, 100}


def test_search_results_without_prices_are_kept_and_idempotent(tmp_path) -> None:
    store = _store(tmp_path)
    row = _offer("Tienda EUR", price="", match="not_found", domain="eur.example")
    row.update({
        "URL": "",
        "Disponibilidad": "unknown",
        "Evidencia": "EAN consultado; sin producto",
        "Fecha consulta": "2026-10-29T10:00:00Z",
    })
    search = pd.DataFrame([row])

    assert store.import_offers(search).imported == 1
    assert store.import_offers(search).imported == 0
    history = store.search_history("036000291452")
    assert history.iloc[0]["match_status"] == "not_found"
    assert store.search_result_summary()["not_found"] == 1


def test_unconfirmed_own_currency_does_not_create_comparison(tmp_path) -> None:
    store = _store(tmp_path)
    own = pd.DataFrame([{
        "EAN": "036000291452", "Precio": "100", "Moneda": "USD", "Mercado": "ES",
    }])
    store.import_own_prices(own)
    store.import_offers(pd.DataFrame([
        _offer("Tienda EUR", price=80, domain="eur.example"),
    ]))

    summary, _ = store.dashboard()

    assert summary.iloc[0]["own_price_eur"] is None
    assert summary.iloc[0]["competitor_count"] == 1
    assert pd.isna(summary.iloc[0]["difference_pct"])


def test_own_price_and_competitor_fx_are_normalized_to_eur(tmp_path) -> None:
    store = _store(tmp_path)
    store.import_own_prices(pd.DataFrame([{
        "EAN": "036000291452", "Precio": "100", "Moneda": "USD", "Mercado": "US",
        "Tipo cambio EUR": "0.9",
    }]))
    store.import_offers(pd.DataFrame([
        _offer("Tienda EUR", price=100, currency="USD", fx=0.8, market="US",
               domain="eur.example"),
    ]))

    summary, _ = store.dashboard()

    assert summary.iloc[0]["own_price_eur"] == pytest.approx(90)
    assert summary.iloc[0]["competitor_min_eur"] == pytest.approx(80)
    assert summary.iloc[0]["difference_pct"] == pytest.approx(12.5)
    assert len(store.own_price_history("036000291452")) == 1


def test_comparator_page_renders_without_external_services(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PRICE_COMPARATOR_DB_PATH", str(tmp_path / "page.sqlite3"))

    app = AppTest.from_string(
        "from app_pages.price_comparator import render_price_comparator_page\n"
        "render_price_comparator_page()"
    ).run()

    assert not app.exception
    assert [tab.label for tab in app.tabs] == [
        "Catálogo y precio Vytria", "Ofertas e histórico", "Competidores",
    ]


def test_comparator_import_report_survives_page_rerun(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PRICE_COMPARATOR_DB_PATH", str(tmp_path / "report.sqlite3"))
    app = AppTest.from_string(
        "from app_pages.price_comparator import render_price_comparator_page\n"
        "render_price_comparator_page()"
    ).run()
    app.session_state["price_comparator_import_reports"] = [
        ("Productos", ImportReport(imported=100, updated=2, rejected=("Fila 7: error",))),
    ]

    app.run()

    assert any("Productos: 100 importados, 2 actualizados, 1 rechazados." in message.value
               for message in app.success)
    assert any("Fila 7: error" in element.value for element in app.get("markdown"))
