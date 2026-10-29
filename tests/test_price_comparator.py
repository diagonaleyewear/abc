from contextlib import closing
import hashlib
from io import BytesIO
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import price_comparator
from price_comparator import (
    ImportReport, PriceComparatorStorageError, PriceComparatorStore, normalize_gtin,
    resolve_database_path,
)


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


def test_database_path_prefers_explicit_variable(tmp_path, monkeypatch) -> None:
    target = tmp_path / "configured.sqlite3"
    monkeypatch.setattr(price_comparator, "CLOUD_DATA_DIR", tmp_path)
    monkeypatch.setenv("PRICE_COMPARATOR_DB_PATH", str(target))

    assert resolve_database_path() == target
    assert PriceComparatorStore().database_path == target.resolve()


def test_database_path_defaults_to_cloud_volume_without_variable(tmp_path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.delenv("PRICE_COMPARATOR_DB_PATH", raising=False)
    monkeypatch.setattr(price_comparator, "CLOUD_DATA_DIR", data_dir)
    monkeypatch.setattr(price_comparator, "CLOUD_DB_PATH", data_dir / "price_comparator.sqlite3")

    store = PriceComparatorStore()

    assert store.database_path == (data_dir / "price_comparator.sqlite3").resolve()
    assert store.database_path.exists()
    assert not (tmp_path / "masterdata_data").exists()


def test_database_path_uses_local_default_outside_docker(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("PRICE_COMPARATOR_DB_PATH", raising=False)
    monkeypatch.setattr(price_comparator, "CLOUD_DATA_DIR", tmp_path / "absent")

    assert resolve_database_path() == price_comparator.DEFAULT_DB_PATH


def test_cloud_default_does_not_create_directories_under_app_root(tmp_path, monkeypatch) -> None:
    app_root = tmp_path / "app"
    (app_root / "data").mkdir(parents=True)
    monkeypatch.delenv("PRICE_COMPARATOR_DB_PATH", raising=False)
    monkeypatch.setattr(price_comparator, "CLOUD_DATA_DIR", app_root / "data")
    monkeypatch.setattr(price_comparator, "CLOUD_DB_PATH", app_root / "data" / "price_comparator.sqlite3")

    PriceComparatorStore()

    assert sorted(path.name for path in app_root.iterdir()) == ["data"]


def test_unwritable_directory_raises_controlled_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(price_comparator.os, "access", lambda *_args: False)

    with pytest.raises(PriceComparatorStorageError) as error:
        PriceComparatorStore(tmp_path / "prices.sqlite3")

    assert "PRICE_COMPARATOR_DB_PATH" in str(error.value)
    assert not (tmp_path / "prices.sqlite3").exists()


def test_unwritable_parent_path_raises_controlled_error(tmp_path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("file")

    with pytest.raises(PriceComparatorStorageError):
        PriceComparatorStore(blocker / "child" / "prices.sqlite3")


def test_page_shows_controlled_error_when_database_is_unwritable(tmp_path, monkeypatch) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("file")
    monkeypatch.setenv("PRICE_COMPARATOR_DB_PATH", str(blocker / "prices.sqlite3"))

    app = AppTest.from_string(
        "from app_pages.price_comparator import render_price_comparator_page\n"
        "render_price_comparator_page()"
    ).run()

    assert not app.exception
    assert any("No se puede abrir la base del comparador" in error.value for error in app.error)


def test_existing_database_is_reused_and_repeated_initialization_keeps_data(tmp_path) -> None:
    path = tmp_path / "prices.sqlite3"
    first = PriceComparatorStore(path)
    first.import_products(PRODUCTS)
    before = path.stat().st_size

    for _ in range(3):
        again = PriceComparatorStore(path)

    assert [row["ean"] for row in again.products().to_dict("records")] == ["0036000291452"]
    assert path.stat().st_size >= before


def test_wal_mode_and_concurrent_initialization_are_safe(tmp_path) -> None:
    path = tmp_path / "prices.sqlite3"
    stores = []

    def create() -> None:
        stores.append(PriceComparatorStore(path))

    threads = [threading.Thread(target=create) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(stores) == 4
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_initialization_does_not_touch_other_sqlite_databases(tmp_path) -> None:
    other = tmp_path / "auth_state.sqlite3"
    with closing(sqlite3.connect(other)) as connection:
        connection.execute("CREATE TABLE sessions(id TEXT)")
        connection.execute("INSERT INTO sessions VALUES ('keep')")
        connection.commit()
    digest = hashlib.sha256(other.read_bytes()).hexdigest()
    siblings = sorted(path.name for path in tmp_path.iterdir())

    PriceComparatorStore(tmp_path / "price_comparator.sqlite3")

    assert hashlib.sha256(other.read_bytes()).hexdigest() == digest
    created = set(path.name for path in tmp_path.iterdir()) - set(siblings)
    assert all(name.startswith("price_comparator.sqlite3") for name in created)


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0, reason="requires root to switch UID")
def test_initialization_as_cloud_uid_and_gid_on_owned_volume(tmp_path) -> None:
    volume = tmp_path / "data"
    volume.mkdir()
    os.chown(volume, 1037, 100)
    script = (
        "import os, sys\n"
        "os.setgid(100); os.setuid(1037)\n"
        "from price_comparator import PriceComparatorStore\n"
        "print(PriceComparatorStore(sys.argv[1]).database_path)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(volume / "price_comparator.sqlite3")],
        cwd=Path(price_comparator.__file__).parent, capture_output=True, text=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (volume / "price_comparator.sqlite3").stat().st_uid == 1037
