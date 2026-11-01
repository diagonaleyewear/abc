"""Review fixes of PR #90: unconfirmed VAT, unverified market/currency, legacy offers and the three Prada EANs."""
from datetime import datetime, timezone
import json
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import price_analytics as pa
import price_observations as po
from price_comparator import PriceComparatorStore, comparable_offers, exclusion_reason
from tests.test_price_comparator import _offer, _store
from tests.test_price_observations import FIXTURE, PRODUCTS, _write

NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
SCRIPT = "from app_pages.price_comparator import render_price_comparator_page\nrender_price_comparator_page()"
PRADA = {"8056597875349": 312.0, "8056597877329": 195.0, "8056597875226": 273.0}


def _freeze(monkeypatch):
    monkeypatch.setattr(pa, "_utc", lambda: NOW)
    monkeypatch.setattr("price_comparator.datetime",
                        type("D", (datetime,), {"now": classmethod(lambda cls, tz=None: NOW)}))


def _vytria(store, ean, price):
    with store._connect() as c, c:
        c.execute("""INSERT INTO vytria_price_history(ean,market,price,currency,price_includes_tax,vat_rate,source,
            pricelist_id,validation_status,recorded_at) VALUES(?,?,?,?,1,21,'odoo',59,'validated',?)""",
                  (ean, "ES", price, "EUR", "2026-10-29T08:00:00+00:00"))
        c.execute("""INSERT INTO vytria_prices(ean,market,price,currency,price_includes_tax,vat_rate,source,
            pricelist_id,validation_status,comparable,fingerprint,first_seen_at,changed_at,synced_at)
            VALUES(?,?,?,?,1,21,'odoo',59,'validated',1,?,?,?,?)""",
                  (ean, "ES", price, "EUR", ean, "2026-10-29T08:00:00+00:00", "2026-10-29T08:00:00+00:00",
                   "2026-10-31T08:00:00+00:00"))


@pytest.fixture
def prada(tmp_path, monkeypatch):
    """Temporary DB: 3 Prada EANs, Vytria 312/195/273, Visio Factory 239/187 (out of stock)/219."""
    _freeze(monkeypatch)
    store = PriceComparatorStore(tmp_path / "prada.sqlite3")
    store.import_products(PRODUCTS)
    store.seed_defaults()
    for ean, price in PRADA.items():
        _vytria(store, ean, price)
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    earlier = dict(data["observaciones"][1], disponibilidad="available",
                   fecha_observacion="2026-10-29T10:00:00+01:00")
    data["observaciones"].insert(0, earlier)  # PR 65ZS was in stock the day before: availability change
    report = po.import_observations(_write(tmp_path, data), store.database_path, apply=True)
    assert report["errors"] == []
    monkeypatch.setenv("PRICE_COMPARATOR_DB_PATH", str(store.database_path))
    return store


def test_prada_percentages_and_out_of_stock_exclusion(prada):
    summary, _ = prada.dashboard(30)
    rows = summary.set_index("ean")
    first, second, third = (rows.loc[e] for e in PRADA)
    assert first["competitor_min_eur"] == pytest.approx(239)
    assert first["difference_eur"] == pytest.approx(73)
    assert first["difference_pct"] == pytest.approx(23.397, abs=1e-3)  # vs Vytria 312
    assert third["competitor_min_eur"] == pytest.approx(219)
    assert third["difference_pct"] == pytest.approx(19.780, abs=1e-3)  # (273 - 219) / 273
    # The out-of-stock 187 EUR must neither compare nor fall back to the older in-stock price.
    assert pd.isna(second["competitor_min_eur"]) and second["competitor_count"] == 0

    pairs = pa.comparable_pairs(summary, prada.current_offers(30))
    assert set(pairs["ean"]) == {"8056597875349", "8056597875226"}
    by_ean = pairs.set_index("ean")["difference_pct"]
    assert by_ean["8056597875349"] == pytest.approx((239 - 312) / 312 * 100)
    assert by_ean["8056597875226"] == pytest.approx((219 - 273) / 273 * 100)
    assert pa.mean_gap_pct(pairs) == pytest.approx((by_ean.sum()) / 2)


def test_prada_history_keeps_availability_change_and_chart_plots_real_points(prada):
    history = prada.offer_history("8056597877329").sort_values("captured_at")
    assert list(history["availability"]) == ["available", "unavailable"]
    assert list(history["price"]) == [187.0, 187.0]
    series = pa.price_series(prada, "8056597877329", 30)
    assert set(series["series"]) == {"Vytria", "Visio Factory"}
    competitor = series[series["series"] == "Visio Factory"].sort_values("at")
    assert competitor.iloc[0]["eur"] == 187.0
    assert competitor.iloc[-1]["kind"] == "unavailable" and pd.isna(competitor.iloc[-1]["eur"])
    figure = pa.price_figure(series)
    assert {trace.name for trace in figure.data} >= {"Vytria", "Visio Factory"}
    # Competitiveness uses the latest observation per day: the out-of-stock EAN no longer counts.
    daily = pa.competitiveness_series(prada, 3, NOW).set_index("day")
    assert daily.loc[NOW.date(), "compared"] == 2
    assert daily.loc[datetime(2026, 10, 29).date(), "compared"] == 1


def test_prada_five_tabs_render_with_real_values(prada):
    app = AppTest.from_string(SCRIPT).run(timeout=90)
    assert not app.exception
    assert [tab.label for tab in app.tabs] == ["Resumen", "Productos", "Pendientes", "Administración"]
    values = {metric.label: metric.value for metric in app.metric}
    assert values["Productos comparados"] == "2 / 4"  # only fresh, comparable competitor offers count
    assert values["Diferencia media vs. mejor"] == "+27,6 %"

    app.selectbox(key="products_selected_ean").set_value("8056597877329").run(timeout=90)
    assert not app.exception
    assert any(len(frame.value) == 2 for frame in app.dataframe)  # full history keeps both observations
    assert len(app.expander) >= 14  # competitors tab lists the 14 reference competitors


def test_unconfirmed_vat_is_stored_but_never_compared(prada):
    legacy = pd.DataFrame([{**_offer("Edel-Optics", price=150, domain="edel-optics.es"),
                            "EAN": "8056597875349", "Fecha consulta": "2026-10-30T14:00:00Z", "IVA incluido": ""}])
    assert not prada.import_offers(legacy).rejected
    stored = prada.offer_history("8056597875349")
    assert stored["price_includes_tax"].isna().sum() == 1 and len(stored) == 2  # kept for consultation
    summary, _ = prada.dashboard(30)
    assert summary.set_index("ean").loc["8056597875349", "competitor_min_eur"] == pytest.approx(239)
    offers = prada.current_offers(30)
    assert "Edel-Optics" not in set(pa.comparable_pairs(summary, offers)["competitor"])
    assert "Edel-Optics" not in set(pa.price_series(prada, "8056597875349", 30)["series"])
    row = offers[offers["competitor_name"] == "Edel-Optics"].iloc[0].to_dict()
    assert exclusion_reason(row) == "IVA no confirmado"


def test_pre_existing_null_vat_offers_survive_and_are_excluded(tmp_path):
    store = _store(tmp_path)
    with store._connect() as c, c:
        c.execute("UPDATE competitors SET market='ES',currency='EUR'")
    legacy = pd.DataFrame([{**_offer("Tienda EUR", price=50, domain="eur.example"), "IVA incluido": ""}])
    assert store.import_offers(legacy).imported == 1
    frame = store.current_offers(3650)
    assert len(frame) == 1 and comparable_offers(frame).empty
    assert len(store.offer_history("036000291452")) == 1


def test_a_newer_unknown_vat_observation_does_not_revive_the_older_price(tmp_path):
    store = _store(tmp_path)
    old = {**_offer("Tienda EUR", price=100, domain="eur.example"), "Fecha consulta": "2026-10-01T10:00:00Z"}
    new = {**_offer("Tienda EUR", price=90, domain="eur.example"), "Fecha consulta": "2026-10-02T10:00:00Z",
           "IVA incluido": ""}
    assert not store.import_offers(pd.DataFrame([old, new])).rejected
    assert comparable_offers(store.offer_history("036000291452")).empty


def test_incompatible_market_and_currency_are_excluded_and_not_assumed(tmp_path):
    store = _store(tmp_path)
    with store._connect() as c, c:
        c.execute("""INSERT INTO vytria_prices(ean,market,price,currency,price_includes_tax,vat_rate,source,
            pricelist_id,validation_status,comparable,fingerprint,first_seen_at,changed_at,synced_at)
            VALUES('0036000291452','ES',100,'EUR',1,21,'odoo',59,'validated',1,'f','2026-10-01','2026-10-01',
            ?)""", (datetime.now(timezone.utc).isoformat(),))
    rows = [
        {**_offer("Tienda EUR", price=10, market="FR", domain="eur.example")},
        {**_offer("Tienda USD", price=10, currency="USD", fx=0.9, market="US", domain="usd.example")},
    ]
    assert not store.import_offers(pd.DataFrame(rows)).rejected
    summary, _ = store.dashboard(3650)
    assert summary.iloc[0]["competitor_count"] == 0  # target market is ES: FR and US offers are not comparable
    assert not pa.comparable_pairs(summary, store.current_offers(3650)).shape[0]
    # Importing without market is rejected instead of defaulting to ES/EUR.
    missing = pd.DataFrame([{**_offer("Tienda EUR", price=10, domain="eur.example"), "Mercado": ""}])
    assert "obligatorio" in store.import_offers(missing).rejected[0]


def test_verified_competitor_market_is_enforced_after_explicit_confirmation(tmp_path):
    store = _store(tmp_path)
    store.fill_competitor_defaults(apply=True, market="ES", currency="EUR")
    report = store.import_offers(pd.DataFrame([_offer("Tienda EUR", price=10, market="FR", domain="eur.example")]))
    assert report.imported == 0 and "mercado no coincide" in report.rejected[0]


def test_dockerfile_copies_every_local_module_used_by_the_comparator():
    import re
    root = Path(__file__).resolve().parent.parent
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    copied = set(re.findall(r"([\w]+\.py)", " ".join(
        line for line in dockerfile.splitlines() if line.startswith("COPY ") and "scripts/" not in line)))
    modules = ["price_comparator", "price_analytics", "price_scheduler", "price_connectors", "price_references",
               "price_observations", "competitor_requirements", "vytria_price_sync"]
    for module in modules:
        source = (root / f"{module}.py").read_text(encoding="utf-8")
        local = {m for m in re.findall(r"^\s*(?:from|import)\s+(\w+)", source, re.M) if (root / f"{m}.py").exists()}
        assert {f"{m}.py" for m in local | {module}} <= copied, module
