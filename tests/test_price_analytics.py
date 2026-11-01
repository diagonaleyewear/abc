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


def test_headline_percentages_use_compared_products_and_equal_prices_are_competitive():
    summary = pd.DataFrame([
        {"own_price_eur": 100.0, "competitor_count": 1, "difference_eur": 0.0,
         "difference_vs_competitor_pct": 0.0},
        {"own_price_eur": 90.0, "competitor_count": 1, "difference_eur": -10.0,
         "difference_vs_competitor_pct": -10.0},
        {"own_price_eur": 120.0, "competitor_count": 2, "difference_eur": 20.0,
         "difference_vs_competitor_pct": 20.0},
        {"own_price_eur": 80.0, "competitor_count": 0, "difference_eur": None,
         "difference_vs_competitor_pct": None},
    ])

    metrics = pa.kpis(summary, 0)

    assert metrics["compared"] == 3
    assert metrics["best_price_pct"] == pytest.approx(200 / 3)
    assert metrics["vytria_more_expensive_pct"] == pytest.approx(100 / 3)
    assert metrics["no_information"] == 1
    assert metrics["mean_gap_vs_best_pct"] == pytest.approx(10 / 3)


def test_attention_table_only_shows_expensive_products_sorted_by_percentage():
    summary = pd.DataFrame([
        {"brand": "Marca", "commercial_name": "A", "ean": "a", "own_price_eur": 150,
         "competitor_min_eur": 100, "difference_eur": 50, "difference_pct": 33.3,
         "difference_vs_competitor_pct": 50, "position": 2, "best_competitor": "Tienda A"},
        {"brand": "Marca", "commercial_name": "B", "ean": "b", "own_price_eur": 110,
         "competitor_min_eur": 100, "difference_eur": 10, "difference_pct": 9.1,
         "difference_vs_competitor_pct": 10, "position": 2, "best_competitor": "Tienda B"},
        {"brand": "Marca", "commercial_name": "C", "ean": "c", "own_price_eur": 90,
         "competitor_min_eur": 100, "difference_eur": -10, "difference_pct": -11.1,
         "difference_vs_competitor_pct": -10, "position": 1, "best_competitor": "Tienda C"},
    ])

    table = pa.attention_table(summary)

    assert list(table["ean"]) == ["a", "b"]
    assert list(table["best_competitor"]) == ["Tienda A", "Tienda B"]


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


def test_product_price_series_stops_when_the_latest_offer_is_unavailable(tmp_path, monkeypatch):
    store = _fresh(tmp_path, monkeypatch)
    _own(store, [(2, 100.0)])
    _offers(store, [("Tienda EUR", 90, 3)])
    unavailable = pd.DataFrame([{
        **_offer("Tienda EUR", price=90, available="unavailable", domain="eur.example"),
        "Fecha consulta": _iso(1),
    }])
    assert not store.import_offers(unavailable).rejected

    series = pa.price_series(store, EAN, 7, now=NOW, market="ES")
    competitor = series[series["series"] == "Tienda EUR"].sort_values("at")

    assert competitor.iloc[-1]["kind"] == "unavailable"
    assert pd.isna(competitor.iloc[-1]["eur"])


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
    series = pa.competitiveness_series(store, 30, now=NOW, expiry_days=30)
    by_day = dict(zip(series["day"], series["best_price_pct"]))
    assert by_day[(NOW - timedelta(days=10)).date()] == 100.0
    assert by_day[NOW.date()] == 0.0


def test_competitiveness_history_excludes_expired_data_and_stops_on_latest_stock_state(tmp_path, monkeypatch):
    store = _fresh(tmp_path, monkeypatch)
    _own(store, [(20, 100.0), (2, 100.0)])
    _offers(store, [("Tienda EUR", 120, 20), ("Tienda EUR", 90, 3)])

    series = pa.competitiveness_series(store, 30, now=NOW, expiry_days=7)

    by_day = dict(zip(series["day"], series["best_price_pct"]))
    assert (NOW - timedelta(days=10)).date() not in by_day
    assert by_day[NOW.date()] == 0.0
    assert series.loc[series["day"] == NOW.date(), "compared"].iloc[0] == 1


def test_dashboard_filters_recompute_the_selected_competitor_minimum_and_deduplicate(tmp_path, monkeypatch):
    store = _fresh(tmp_path, monkeypatch)
    _own(store, [(2, 100.0)])
    _offers(store, [("Tienda EUR", 80, 2), ("Tienda USD", 90, 2), ("Tienda USD", 95, 1)])
    summary, _ = store.dashboard(7)
    offers = store.current_offers(7)

    all_competitors = pa.dashboard_scope(summary, offers)
    one_competitor = pa.dashboard_scope(summary, offers, competitors=["Tienda USD"], query="A1",
                                        states=[pa.POSITION_HIGHER])
    pairs = pa.comparable_pairs(summary, offers)

    assert all_competitors.iloc[0]["competitor_min_eur"] == 80
    assert one_competitor.iloc[0]["competitor_min_eur"] == 95
    assert one_competitor.iloc[0]["competitor_count"] == 1
    assert len(pairs[pairs["competitor"] == "Tienda USD"]) == 1
    assert pairs[pairs["competitor"] == "Tienda USD"].iloc[0]["competitor_eur"] == 95


def test_distribution_and_competitiveness_figures_match_their_data():
    summary = pd.DataFrame([
        {"position": pa.POSITION_BEST}, {"position": pa.POSITION_BEST},
        {"position": pa.POSITION_HIGHER}, {"position": pa.POSITION_NONE},
    ])
    distribution = pa.positioning_counts(summary)
    distribution_figure = pa.positioning_distribution_figure(summary)
    series = pd.DataFrame([{"day": NOW.date(), "best_price_pct": 50.0, "compared": 2}])
    evolution_figure = pa.competitiveness_figure(series)

    assert distribution["count"].tolist() == [2, 1, 1]
    assert list(distribution_figure.data[0].x) == [2, 1, 1]
    assert list(evolution_figure.data[0].y) == [50.0]
    assert list(evolution_figure.data[0].customdata[:, 0]) == [2]


def test_page_shows_insufficient_data_message_and_renders_with_data(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setenv("PRICE_COMPARATOR_DB_PATH", str(store.database_path))
    script = ("from app_pages.price_comparator import render_price_comparator_page\n"
              "render_price_comparator_page()")
    app = AppTest.from_string(script).run(timeout=30)
    assert not app.exception
    assert any("Datos insuficientes para medir competitividad" in item.value for item in app.info)
    assert [m.label for m in app.metric[:4]] == [
        "Productos comparados", "Igual o más barato", "Más caro", "Diferencia media vs. mejor"]
    assert app.metric[0].value == "0 / 1"
    assert app.metric[1].value == "Sin datos" and app.metric[2].value == "Sin datos"
    assert app.button(key="update_competitors").disabled  # nothing enabled yet: Pendiente de configurar
    now = datetime.now(timezone.utc)
    store.set_setting("price_expiry_days", 30)
    with store._connect() as c, c:
        c.execute("UPDATE competitors SET market='ES'")
        c.execute("""INSERT INTO vytria_prices(ean,market,price,currency,price_includes_tax,vat_rate,source,pricelist_id,
            validation_status,comparable,fingerprint,first_seen_at,changed_at,synced_at)
            VALUES(?,?,100,'EUR',1,21,'odoo',59,'validated',1,'f',?,?,?)""",
                  (EAN, "ES", now.isoformat(), now.isoformat(), now.isoformat()))
        c.execute("""INSERT INTO vytria_price_history(ean,market,price,currency,price_includes_tax,vat_rate,source,
            pricelist_id,validation_status,recorded_at) VALUES(?,?,100,'EUR',1,21,'odoo',59,'validated',?)""",
                  (EAN, "ES", now.isoformat()))
    store.import_offers(pd.DataFrame([{
        **_offer("Tienda EUR", price=90, domain="eur.example"),
        "Fecha consulta": (now - timedelta(days=10)).isoformat(),
    }]))
    history_windows = []
    competitiveness_series = pa.competitiveness_series

    def track_history_window(*args, **kwargs):
        history_windows.append((args[1], kwargs["expiry_days"]))
        return competitiveness_series(*args, **kwargs)

    monkeypatch.setattr(pa, "competitiveness_series", track_history_window)
    app = AppTest.from_string(script).run(timeout=30)
    assert not app.exception
    assert not any("No hay datos de competencia suficientes" in w.value for w in app.warning)
    assert app.metric[0].value == "1 / 1"  # the 30-day view includes this valid observation
    assert history_windows[-1] == (30, 30)
    app.selectbox(key="dashboard_period").set_value("7 días").run(timeout=30)
    assert not app.exception
    assert app.metric[0].value == "0 / 1"  # the shared period filter excludes the 10-day-old offer
    assert history_windows[-1] == (7, 30)  # chart window shrinks; per-observation freshness stays configured
    assert any("Datos insuficientes para medir competitividad" in item.value for item in app.info)
    assert app.selectbox(key="products_selected_ean").value == EAN
    comparison = next(frame.value for frame in app.dataframe if "Uso en comparación" in frame.value.columns)
    assert comparison.iloc[0]["Uso en comparación"] == "Comparable"
