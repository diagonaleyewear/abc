from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3
import threading
import time

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import price_comparator
from price_comparator import PriceComparatorStore
from tests.test_vytria_price_sync import (
    FlakyReader, OdooFixture, _audited_odoo, _count, _current, _page, _store, _sync,
)
from vytria_price_sync import SqlOdooPriceReader, VytriaPriceSync

APP = ("from app_pages.price_comparator import render_price_comparator_page\n"
       "render_price_comparator_page()")


def _wait_until(condition, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


def test_page_button_runs_sync_in_background_without_blocking(tmp_path, monkeypatch) -> None:
    import app_pages.price_comparator as page

    odoo = OdooFixture(tmp_path / "odoo.sqlite3")
    odoo.product("8056262398043", 360, 252, maps=(odoo.map_rule("Vytria España (30%)", 30),))
    gate = threading.Event()

    class SlowReader(SqlOdooPriceReader):
        def fetch(self, eans):
            gate.wait(10)
            return super().fetch(eans)

    _page(tmp_path, monkeypatch)  # creates the store and the catalogue
    monkeypatch.setattr(page, "_make_sync", lambda s: VytriaPriceSync(
        s, SlowReader(odoo.engine_factory), sleep=lambda _: None))
    app = AppTest.from_string(APP, default_timeout=30).run()

    started = time.monotonic()
    app.button(key="sync_vytria_prices").click().run()
    elapsed = time.monotonic() - started
    try:
        assert elapsed < 5  # the click returned while Odoo is still being read
        assert not app.exception
        _wait_until(lambda: VytriaPriceSync(PriceComparatorStore()).is_running())
        app.run()
        assert app.button(key="sync_vytria_prices").disabled
        assert any("en curso" in info.value for info in app.info)
    finally:
        gate.set()
    _wait_until(lambda: not VytriaPriceSync(PriceComparatorStore()).is_running())
    app.run()
    assert not app.button(key="sync_vytria_prices").disabled
    app.selectbox(key="price_admin_section").set_value("Importar datos").run()
    assert any(metric.label == "Con precio confirmado" and metric.value == "1/1" for metric in app.metric)


def test_page_worker_swallows_failures_and_logs_them(tmp_path) -> None:
    import app_pages.price_comparator as page

    store = _store(tmp_path, ["8056262398043"])
    reader = FlakyReader(SqlOdooPriceReader(_audited_odoo(tmp_path).engine_factory), failures=99)
    sync = VytriaPriceSync(store, reader, sleep=lambda _: None)

    page._run_sync(sync)

    assert sync.last_run()["status"] == "error"


def test_issue_marks_previous_price_stale_but_keeps_it_and_its_history(tmp_path) -> None:
    store, odoo = _store(tmp_path, ["8056262398043"]), _audited_odoo(tmp_path)
    sync = _sync(store, odoo)
    sync.run()
    before = _current(store, "8056262398043")
    odoo.set_price(odoo.catalog_id("8056262398043"), None)

    sync.run()

    after = _current(store, "8056262398043")
    assert (after["price"], after["synced_at"]) == (before["price"], before["synced_at"])
    assert after["stale_since"] and after["stale_reason"] == "no_price"
    assert _count(store, "vytria_price_history") == 1
    row = sync.prices().iloc[0]
    assert (row["current_status"], bool(row["usable"])) == ("stale", False)
    summary, _ = store.dashboard()
    assert pd.isna(summary.iloc[0]["own_price_eur"]) and summary.iloc[0]["own_price_status"] == "stale"

    odoo.set_price(odoo.catalog_id("8056262398043"), 252)
    sync.run()
    recovered = _current(store, "8056262398043")
    assert recovered["stale_since"] is None and recovered["stale_reason"] == ""
    assert bool(sync.prices().iloc[0]["usable"])
    # the fixture recreates the source row (new source_price_id), so one extra history entry is expected
    assert _count(store, "vytria_price_history") == 2


def test_old_synced_price_expires_from_comparisons(tmp_path) -> None:
    store, odoo = _store(tmp_path, ["8056262398043"]), _audited_odoo(tmp_path)
    sync = _sync(store, odoo)
    sync.run()
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    with store._connect() as connection, connection:
        connection.execute("UPDATE vytria_prices SET synced_at=?", (old,))

    summary, _ = store.dashboard()

    assert pd.isna(summary.iloc[0]["own_price_eur"])
    assert summary.iloc[0]["own_price_status"] == "expired"
    assert sync.prices().iloc[0]["current_status"] == "expired"


@pytest.mark.parametrize("status", ["price_mismatch", "no_map", "multiple_maps", "exceptional_rule",
                                    "tax_issue", "currency_issue", "discontinued"])
def test_only_validated_status_reaches_comparisons(tmp_path, status) -> None:
    store, odoo = _store(tmp_path, ["8056262398043"]), _audited_odoo(tmp_path)
    _sync(store, odoo).run()
    with store._connect() as connection, connection:
        connection.execute("UPDATE vytria_prices SET validation_status=?,comparable=1", (status,))

    summary, _ = store.dashboard()

    assert pd.isna(summary.iloc[0]["own_price_eur"])


def test_failed_run_does_not_refresh_last_successful_sync(tmp_path) -> None:
    store, odoo = _store(tmp_path, ["8056262398043"]), _audited_odoo(tmp_path)
    _sync(store, odoo).run()
    failing = VytriaPriceSync(store, FlakyReader(SqlOdooPriceReader(odoo.engine_factory), 99),
                              sleep=lambda _: None)
    first_success = failing.last_successful_run()["finished_at"]

    failing.run()

    assert failing.last_run()["status"] == "error"
    assert failing.last_successful_run()["finished_at"] == first_success
    assert _current(store, "8056262398043")["synced_at"] <= first_success


def test_run_left_running_by_dead_process_is_closed_on_next_run(tmp_path) -> None:
    store, odoo = _store(tmp_path), _audited_odoo(tmp_path)
    with store._connect() as connection, connection:
        connection.execute("INSERT INTO vytria_sync_runs(trigger,started_at,status) "
                           "VALUES('manual','2026-10-01','running')")
    sync = _sync(store, odoo)

    sync.run()

    with store._connect() as connection:
        rows = connection.execute("SELECT status FROM vytria_sync_runs ORDER BY id").fetchall()
    assert [row[0] for row in rows] == ["error", "ok"]
    assert sync.is_running() is False


def test_existing_pre_sync_database_is_migrated_without_data_loss(tmp_path) -> None:
    store = _store(tmp_path, ["8056262398043"])
    store.import_competitors(pd.DataFrame([{"Competidor": "Tienda", "Página web": "https://tienda.example/"}]))
    store.import_own_prices(pd.DataFrame([{"EAN": "8056262398043", "Precio": "300", "Moneda": "EUR",
                                           "Mercado": "ES", "Origen": "Excel"}]))
    with sqlite3.connect(store.database_path) as connection:  # emulate the database deployed by PR #87
        for table in ("vytria_prices", "vytria_price_history", "vytria_sync_runs", "vytria_sync_issues",
                      "vytria_sync_settings"):
            connection.execute(f"DROP TABLE {table}")
        before = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in ("products", "competitors", "own_prices", "offers")}

    reopened = PriceComparatorStore(store.database_path)
    reopened_again = PriceComparatorStore(store.database_path)

    for table, count in before.items():
        assert _count(reopened_again, table) == count
    assert _count(reopened, "vytria_prices") == 0
    summary, _ = reopened.dashboard()
    assert summary.iloc[0]["own_price_eur"] == 300  # manual contingency price used until the first sync


def test_comparator_path_resolution_is_unchanged(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("PRICE_COMPARATOR_DB_PATH", raising=False)
    monkeypatch.setattr(price_comparator, "CLOUD_DATA_DIR", tmp_path)
    monkeypatch.setattr(price_comparator, "CLOUD_DB_PATH", tmp_path / "price_comparator.sqlite3")

    store = PriceComparatorStore()
    sync = VytriaPriceSync(store, SqlOdooPriceReader(lambda: pytest.fail("not used")))

    assert store.database_path == (tmp_path / "price_comparator.sqlite3").resolve()
    assert sync._lock_path.startswith(str(tmp_path.resolve()))  # the lock lives on the persistent volume
