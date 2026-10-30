from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import price_analytics as pa
from tests.test_price_comparator import _offer, _store

EAN = "0036000291452"
NOW = datetime(2025, 3, 1, 12, tzinfo=timezone.utc)


def _iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


def _own(store, history, status="validated", synced_days_ago=0.1):
    """history: [(days_ago, price)] oldest first; the last one is the current price."""
    with store._connect() as c, c:
        for days_ago, price in history:
            c.execute("""INSERT INTO vytria_price_history(ean,market,price,currency,price_includes_tax,vat_rate,
                source,pricelist_id,validation_status,recorded_at) VALUES(?,?,?,?,1,21,'odoo',59,?,?)""",
                      (EAN, "ES", price, "EUR", status, _iso(days_ago)))
        c.execute("""INSERT OR REPLACE INTO vytria_prices(ean,market,price,currency,price_includes_tax,vat_rate,source,
            pricelist_id,validation_status,comparable,fingerprint,first_seen_at,changed_at,synced_at)
            VALUES(?,?,?,?,1,21,'odoo',59,?,1,'f',?,?,?)""",
                  (EAN, "ES", history[-1][1], "EUR", status, _iso(30), _iso(1), _iso(synced_days_ago)))


def _domain(name):
    return f"{name.split()[-1].lower()}.example"


def _offers(store, rows):
    """rows: [(competitor, price, days_ago)] -> imported with explicit capture dates."""
    for name, price, days_ago in rows:
        frame = pd.DataFrame([{**_offer(name, price=price, domain=_domain(name)), "Fecha consulta": _iso(days_ago)}])
        assert not store.import_offers(frame).rejected


def _fresh(tmp_path, monkeypatch):
    store = _store(tmp_path)
    with store._connect() as c, c:
        c.execute("UPDATE competitors SET market='ES'")
    monkeypatch.setattr(pa, "_utc", lambda: NOW)
    monkeypatch.setattr("price_comparator.datetime", type("D", (datetime,), {"now": classmethod(lambda cls, tz=None: NOW)}))
    return store


def test_price_state_distinguishes_current_last_known_and_expired():
    assert pa.price_state(_iso(3), 7, NOW) == pa.STATE_CURRENT
    assert pa.price_state(_iso(10), 7, NOW) == pa.STATE_LAST_KNOWN
    assert pa.price_state(_iso(20), 7, NOW) == pa.STATE_EXPIRED
    assert pa.price_state(None, 7, NOW) == pa.STATE_EXPIRED


def test_table_and_kpis_use_only_validated_prices(tmp_path, monkeypatch):
    store = _fresh(tmp_path, monkeypatch)
    _own(store, [(20, 120.0), (5, 100.0)])
    _offers(store, [("Tienda EUR", 110, 2), ("Tienda USD", 95, 2)])
    summary, _ = store.dashboard(7)
    assert summary.iloc[0]["own_price_eur"] == pytest.approx(100)
    table = pa.commercial_table(summary)
    assert table.iloc[0]["position"] in pa.POSITIONS
    indicators = pa.kpis(summary, 0)
    assert indicators["vytria_confirmed"] == 1 and indicators["comparable"] >= 1


def test_unvalidated_vytria_price_is_excluded_from_comparison(tmp_path, monkeypatch):
    store = _fresh(tmp_path, monkeypatch)
    _own(store, [(5, 100.0)], status="discrepancy")
    _offers(store, [("Tienda EUR", 90, 1)])
    summary, _ = store.dashboard(7)
    assert pd.isna(summary.iloc[0]["own_price_eur"])
    assert pa.commercial_table(summary).iloc[0]["position"] == pa.POSITION_NONE
    assert pa.kpis(summary, 0)["best_price_pct"] is None


def test_filters_by_brand_competitor_availability_and_position(tmp_path, monkeypatch):
    store = _fresh(tmp_path, monkeypatch)
    _own(store, [(5, 100.0)])
    _offers(store, [("Tienda EUR", 90, 1)])
    summary, _ = store.dashboard(7)
    offers = store.current_offers(7)
    table = pa.commercial_table(summary)
    assert len(pa.filter_table(table, offers, competitors=["Tienda EUR"], availability=["available"])) == 1
    assert pa.filter_table(table, offers, competitors=["Tienda USD"]).empty
    assert pa.filter_table(table, offers, positions=[pa.POSITION_BEST]).empty
    assert len(pa.filter_table(table, offers, positions=[pa.POSITION_HIGHER], brands=["Marca A"])) == 1


def test_series_use_stored_data_only_and_mark_changes_and_checks(tmp_path, monkeypatch):
    store = _fresh(tmp_path, monkeypatch)
    _own(store, [(60, 120.0), (5, 100.0)])
    _offers(store, [("Tienda EUR", 120, 100), ("Tienda EUR", 110, 40), ("Tienda EUR", 105, 3)])
    with store._connect() as c, c:
        offer_id = c.execute("SELECT MAX(id) FROM offers").fetchone()[0]
        c.execute("INSERT INTO offer_checks(ean,competitor_id,market,last_offer_id,last_ok_at,checks) "
                  "VALUES(?,?,?,?,?,3)", (EAN, "eur-example", "ES", offer_id, _iso(1)))
    week = pa.price_series(store, EAN, 7, now=NOW)
    # Inside 7 days: Vytria's price in force from before the window plus the later one, and the check.
    assert set(week["series"]) == {"Vytria", "Tienda EUR"}
    assert (week[(week["series"] == "Tienda EUR") & (week["kind"] == "check")]["at"] == _iso(1)).all()
    assert not (week["eur"].isna()).any()
    year = pa.price_series(store, EAN, 365, now=NOW)
    assert len(year) > len(week)
    only = pa.price_series(store, EAN, 365, ["Tienda USD"], now=NOW)
    assert "Tienda EUR" not in set(only["series"])
    figure = pa.price_figure(year)
    assert {trace.line.shape for trace in figure.data if trace.mode == "lines"} == {"hv"}


def test_recent_changes_respect_threshold(tmp_path, monkeypatch):
    store = _fresh(tmp_path, monkeypatch)
    _own(store, [(20, 100.0), (2, 101.0)])
    _offers(store, [("Tienda EUR", 100, 10), ("Tienda EUR", 80, 2)])
    changes = pa.recent_changes(store, 7, 5)
    assert list(changes["source"]) == ["Tienda EUR"] and changes.iloc[0]["change_pct"] == pytest.approx(-20)
    assert len(pa.recent_changes(store, 7, 0.5)) == 2


def test_competitiveness_series_is_computed_as_of_each_day(tmp_path, monkeypatch):
    store = _fresh(tmp_path, monkeypatch)
    _own(store, [(30, 100.0)])
    _offers(store, [("Tienda EUR", 120, 20), ("Tienda EUR", 90, 3)])
    series = pa.competitiveness_series(store, 30, now=NOW)
    by_day = dict(zip(series["day"], series["best_price_pct"]))
    assert by_day[(NOW - timedelta(days=10)).date()] == 100.0
    assert by_day[NOW.date()] == 0.0


def test_page_shows_insufficient_data_message_and_renders_with_data(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setenv("PRICE_COMPARATOR_DB_PATH", str(store.database_path))
    script = ("from app_pages.price_comparator import render_price_comparator_page\n"
              "render_price_comparator_page()")
    app = AppTest.from_string(script).run(timeout=30)
    assert not app.exception
    assert any("No hay datos de competencia suficientes" in w.value for w in app.warning)
    assert [m.label for m in app.metric][:3] == ["Referencias con precio Vytria", "Con competencia comparable",
                                                  "Competidores con datos"]
    assert app.metric[3].value == "Sin datos" and app.metric[4].value == "Sin datos"
    assert app.button(key="update_competitors").disabled  # nothing enabled yet: Pendiente de configurar
    now = datetime.now(timezone.utc)
    with store._connect() as c, c:
        c.execute("UPDATE competitors SET market='ES'")
        c.execute("""INSERT INTO vytria_prices(ean,market,price,currency,price_includes_tax,vat_rate,source,pricelist_id,
            validation_status,comparable,fingerprint,first_seen_at,changed_at,synced_at)
            VALUES(?,?,100,'EUR',1,21,'odoo',59,'validated',1,'f',?,?,?)""",
                  (EAN, "ES", now.isoformat(), now.isoformat(), now.isoformat()))
        c.execute("""INSERT INTO vytria_price_history(ean,market,price,currency,price_includes_tax,vat_rate,source,
            pricelist_id,validation_status,recorded_at) VALUES(?,?,100,'EUR',1,21,'odoo',59,'validated',?)""",
                  (EAN, "ES", now.isoformat()))
    store.import_offers(pd.DataFrame([_offer("Tienda EUR", price=90, domain="eur.example")]))
    app = AppTest.from_string(script).run(timeout=30)
    assert not app.exception
    assert not any("No hay datos de competencia suficientes" in w.value for w in app.warning)
    assert app.selectbox(key="price_detail_ean").value == EAN
