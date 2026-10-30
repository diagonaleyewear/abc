import sqlite3

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from price_comparator import PriceComparatorStore
from tests.test_price_comparator import PRODUCTS, _store
from tests.test_price_scheduler import _setup


def test_settings_defaults_persist_and_are_validated(tmp_path):
    store = PriceComparatorStore(tmp_path / "s.sqlite3")
    assert (store.get_setting("retention_months"), store.price_expiry_days()) == ("12", 7)
    store.set_setting("retention_months", "24")
    store.set_setting("price_expiry_days", "10")
    reopened = PriceComparatorStore(tmp_path / "s.sqlite3")
    assert reopened.get_setting("retention_months") == "24" and reopened.price_expiry_days() == 10
    for key, value in (("retention_months", "5"), ("price_expiry_days", "0"), ("price_expiry_days", "91"),
                       ("alert_threshold_pct", "0.1"), ("unknown", "1")):
        with pytest.raises(ValueError):
            store.set_setting(key, value)


def test_migration_keeps_existing_data_and_adds_new_tables(tmp_path):
    path = tmp_path / "old.sqlite3"
    store = _store(tmp_path)
    store.database_path.rename(path)
    with sqlite3.connect(path) as connection:
        for table in ("comparator_settings", "offer_checks", "competitor_schedule", "scheduler_runs", "scheduler_jobs"):
            connection.execute(f"DROP TABLE {table}")
    migrated = PriceComparatorStore(path)
    assert len(migrated.products()) == len(PRODUCTS) and len(migrated.competitors()) == 2
    with migrated._connect() as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"comparator_settings", "offer_checks", "competitor_schedule", "scheduler_runs", "scheduler_jobs"} <= tables
    PriceComparatorStore(path)  # idempotent


def test_page_settings_tab_lists_competitors_as_pending_and_saves_general_config(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setenv("PRICE_COMPARATOR_DB_PATH", str(store.database_path))
    app = AppTest.from_string("from app_pages.price_comparator import render_price_comparator_page\n"
                              "render_price_comparator_page()").run(timeout=30)
    assert not app.exception
    labels = [e.label for e in app.expander]
    assert sum("Pendiente de configurar" in label for label in labels) == 2
    app.selectbox(key="cfg_retention").set_value(6)
    app.number_input(key="cfg_expiry").set_value(10)
    app.number_input(key="cfg_odoo_hours").set_value(6.0)
    app.button(key="cfg_save").click().run(timeout=30)
    assert not app.exception
    assert store.get_setting("retention_months") == "6" and store.price_expiry_days() == 10


def test_manual_competitor_update_runs_in_background_thread(tmp_path, monkeypatch):
    import app_pages.price_comparator as page

    store, scheduler, *_ = _setup(tmp_path)
    calls = []
    monkeypatch.setattr(page, "_launch", lambda target, *args: calls.append((target, args)))
    monkeypatch.setenv("PRICE_COMPARATOR_DB_PATH", str(store.database_path))
    app = AppTest.from_string("from app_pages.price_comparator import render_price_comparator_page\n"
                              "render_price_comparator_page()").run(timeout=30)
    assert not app.button(key="update_competitors").disabled
    app.button(key="update_competitors").click().run(timeout=30)
    assert [call[0] for call in calls] == [page._run_competitors]  # never executed inside the script thread


def test_existing_odoo_sync_module_is_unchanged_in_behaviour_for_dashboard(tmp_path):
    store = _store(tmp_path)
    summary, counts = store.dashboard()
    assert counts["products"] == 1 and "best_competitor" in summary.columns
