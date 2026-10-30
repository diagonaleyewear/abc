"""Initial configuration, competitor positioning, KPIs without data and the redesigned page."""
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import price_analytics as pa
from competitor_requirements import COMPETITORS, METHOD_STATUS, REQUIREMENTS
from price_comparator import PriceComparatorStore
from tests.test_price_analytics import EAN, _fresh, _offers, _own

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = "from app_pages.price_comparator import render_price_comparator_page\nrender_price_comparator_page()"


def test_seed_registers_14_competitors_with_safe_defaults(tmp_path):
    store = PriceComparatorStore(tmp_path / "new.sqlite3")
    added = store.seed_defaults()

    assert added["competitors"] == 14 and added["schedules"] == 14
    competitors = store.competitors()
    assert set(competitors["market"]) == {""} and set(competitors["currency"]) == {""}
    assert sorted(competitors["name"]) == sorted(name for name, _ in COMPETITORS)
    with store._connect() as connection:
        rows = connection.execute("SELECT enabled,interval_hours,max_requests_per_run,pause_seconds,max_attempts,"
                                  "connector FROM competitor_schedule").fetchall()
        settings = dict(connection.execute("SELECT key,value FROM comparator_settings").fetchall())
    assert {tuple(row) for row in rows} == {(0, 24.0, 20, 2.0, 3, "")}  # nothing enabled, nothing queried
    assert settings == {"retention_months": "12", "price_expiry_days": "7", "alert_threshold_pct": "5"}
    assert "operational" not in {status for _, status in METHOD_STATUS.values()}
    assert store.coverage_table()["integration_status"].ne("operational").all()


def test_seed_is_idempotent_and_never_overwrites_existing_values(tmp_path):
    path = tmp_path / "keep.sqlite3"
    store = PriceComparatorStore(path)
    store.seed_defaults()
    store.set_setting("retention_months", "24")
    with store._connect() as connection, connection:
        connection.execute("UPDATE competitors SET market='PT',name='Nombre propio' WHERE domain='optica-optima.com'")
        connection.execute("UPDATE competitor_schedule SET interval_hours=6,max_requests_per_run=5")

    again = PriceComparatorStore(path)
    assert again.seed_defaults() == {"competitors": 0, "settings": 0, "schedules": 0, "coverage": 0}

    assert again.get_setting("retention_months") == "24"
    row = again.competitors().set_index("domain").loc["optica-optima.com"]
    assert (row["market"], row["name"]) == ("PT", "Nombre propio")
    with again._connect() as connection:
        assert {tuple(r) for r in connection.execute(
            "SELECT interval_hours,max_requests_per_run FROM competitor_schedule")} == {(6.0, 5)}
        assert connection.execute("SELECT COUNT(*) FROM competitors").fetchone()[0] == 14


def test_seed_keeps_blank_market_and_existing_competitor_ids(tmp_path):
    store = PriceComparatorStore(tmp_path / "legacy.sqlite3")
    with store._connect() as connection, connection:
        connection.execute("INSERT INTO competitors(id,name,url,domain) VALUES('blickers-com','Blickers','https://www.blickers.com/','blickers.com')")
    store.seed_defaults()
    legacy = store.competitors().set_index("id").loc["blickers-com"]
    assert (legacy["market"], legacy["currency"], legacy["url"]) == ("", "", "https://www.blickers.com/")
    assert len(store.competitors()) == 14


def test_every_competitor_has_a_concrete_request_and_a_status():
    assert set(REQUIREMENTS) == set(METHOD_STATUS) == {name for name, _ in COMPETITORS}
    assert all(need.strip() and restriction.strip() for restriction, need in REQUIREMENTS.values())


def test_dockerfile_copies_every_comparator_module():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    for module in ("price_comparator", "price_analytics", "price_scheduler", "price_connectors", "price_references",
                   "price_observations", "vytria_price_sync", "competitor_requirements"):
        assert f"{module}.py" in dockerfile, module


def _frames(rows, own=100.0):
    summary = pd.DataFrame([{"ean": ean, "market": "ES", "own_price_eur": own} for ean in {r[0] for r in rows}])
    offers = pd.DataFrame([{
        "id": index, "ean": ean, "competitor_id": name, "competitor_name": name, "market": market,
        "price": price, "promotional_price": None, "fx_to_eur": 1.0, "availability": availability,
        "match_status": status, "price_includes_tax": tax, "captured_at": "2025-03-01T10:00:00+00:00",
        "checked_at": "2025-03-01T10:00:00+00:00",
    } for index, (ean, name, price, availability, status, market, tax) in enumerate(rows)])
    return summary, offers


def test_positioning_averages_same_references_and_flags_threshold():
    ok = ("available", "exact_match", "ES", 1)
    summary, offers = _frames([("A", "Barato", 80.0, *ok), ("B", "Barato", 90.0, *ok), ("A", "Caro", 120.0, *ok),
                               ("A", "Igual", 101.0, *ok)])
    result = pa.competitor_positioning(summary, offers, 5.0).set_index("competitor")

    assert result.loc["Barato", "difference_pct"] == pytest.approx(-15.0)  # (-20 and -10) / own 100
    assert result.loc["Barato", "references"] == 2 and result.loc["Barato", "cheaper_pct"] == 100
    assert result.loc["Barato", "signal"] == pa.SIGNAL_BELOW
    assert result.loc["Caro", "signal"] == pa.SIGNAL_ABOVE and result.loc["Igual", "signal"] == pa.SIGNAL_INLINE
    assert list(result.index) == ["Barato", "Igual", "Caro"]  # sorted by gap
    assert pa.mean_gap_pct(pa.comparable_pairs(summary, offers)) == pytest.approx((-20 - 10 + 20 + 1) / 4)


@pytest.mark.parametrize("row", [
    ("A", "X", 50.0, "unavailable", "exact_match", "ES", 1),
    ("A", "X", 50.0, "available", "probable_match", "ES", 1),
    ("A", "X", 50.0, "available", "rejected", "ES", 1),
    ("A", "X", 50.0, "available", "exact_match", "FR", 1),
    ("A", "X", 50.0, "available", "exact_match", "ES", 0),
    ("A", "X", 50.0, "available", "exact_match", "ES", None),
])
def test_positioning_excludes_offers_that_are_not_comparable(row):
    summary, offers = _frames([row])
    assert pa.competitor_positioning(summary, offers).empty
    assert pa.comparable_pairs(summary, offers).empty


def test_positioning_uses_latest_offer_per_reference_and_skips_missing_vytria_price():
    ok = ("available", "exact_match", "ES", 1)
    summary, offers = _frames([("A", "X", 70.0, *ok), ("A", "X", 90.0, *ok)])
    offers.loc[1, "captured_at"] = "2025-03-02T10:00:00+00:00"
    assert pa.competitor_positioning(summary, offers).iloc[0]["difference_pct"] == pytest.approx(-10.0)
    summary["own_price_eur"] = None
    assert pa.competitor_positioning(summary, offers).empty


def test_kpis_report_no_data_instead_of_zero_percent():
    empty = pd.DataFrame(columns=["own_price_eur", "competitor_count", "difference_eur"])
    indicators = pa.kpis(empty, 0)
    assert indicators["best_price_pct"] is None and indicators["vytria_more_expensive"] == 0
    assert pa.mean_gap_pct(pa.comparable_pairs(empty, pd.DataFrame())) is None


def test_price_figure_shows_isolated_points_and_vytria_green():
    series = pd.DataFrame([
        {"series": "Vytria", "at": "2025-03-01T10:00:00+00:00", "eur": 100.0, "kind": "change"},
        {"series": "Tienda", "at": "2025-03-01T11:00:00+00:00", "eur": 90.0, "kind": "change"},
    ])
    figure = pa.price_figure(series)
    markers = [trace for trace in figure.data if trace.mode == "markers"]
    assert len(markers) == 2 and all(len(trace.x) == 1 for trace in markers)
    colours = {trace.name: trace.line.color for trace in figure.data if trace.mode == "lines"}
    assert colours["Vytria"] == "#417B7B" and colours["Tienda"] != "#417B7B"
    assert figure.layout.xaxis.tickformat == "%d/%m/%Y"


def test_positioning_figure_orders_bars_and_marks_negative_gaps():
    ok = ("available", "exact_match", "ES", 1)
    summary, offers = _frames([("A", "Barato", 80.0, *ok), ("A", "Caro", 120.0, *ok)])
    figure = pa.positioning_figure(pa.competitor_positioning(summary, offers))
    assert figure.data[0].orientation == "h"
    assert list(figure.data[0].y) == ["Caro", "Barato"]
    assert list(figure.data[0].marker.color) == ["#417B7B", "#C2410C"]


def test_page_with_real_stored_offer_shows_gap_chart_and_competitor_requests(tmp_path, monkeypatch):
    store = _fresh(tmp_path, monkeypatch)
    _own(store, [(5, 100.0)])
    _offers(store, [("Tienda EUR", 90, 1)])
    monkeypatch.setenv("PRICE_COMPARATOR_DB_PATH", str(store.database_path))

    app = AppTest.from_string(SCRIPT).run(timeout=60)

    assert not app.exception
    assert [tab.label for tab in app.tabs] == ["Resumen", "Comparativa", "Histórico", "Competidores", "Configuración"]
    values = {metric.label: metric.value for metric in app.metric}
    assert values["Diferencia media del mercado vs Vytria"].startswith("-")
    assert values["Referencias con precio Vytria"] == "1/1"
    labels = [expander.label for expander in app.expander]
    assert any(label.startswith("Edel-Optics") and "Bloqueado" in label for label in labels)
    assert any("**Necesitamos:**" in markdown.value for markdown in app.markdown)
    assert EAN in app.selectbox(key="price_detail_ean").value


def test_page_creates_initial_configuration_on_first_access(tmp_path, monkeypatch):
    path = tmp_path / "first.sqlite3"
    monkeypatch.setenv("PRICE_COMPARATOR_DB_PATH", str(path))

    app = AppTest.from_string(SCRIPT).run(timeout=60)

    assert not app.exception
    reopened = PriceComparatorStore(path)
    assert len(reopened.competitors()) == 14
    with reopened._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM competitor_schedule WHERE enabled=0").fetchone()[0] == 14
        assert connection.execute("SELECT COUNT(*) FROM comparator_settings").fetchone()[0] == 3
    assert {metric.label: metric.value for metric in app.metric}["Diferencia media del mercado vs Vytria"] == "Sin datos"
