from contextlib import closing
import json
from pathlib import Path
import sqlite3

import pandas as pd
import pytest

import price_observations as po
from price_comparator import PriceComparatorStore

FIXTURE = Path(__file__).parent / "fixtures" / "observaciones_prada.json"
PRODUCTS = pd.DataFrame([
    {"MARCA": "Prada", "NOMBRE": "Prada PR 27ZS", "MODELO": "PR 27ZS", "COLOR": "16K08Z", "CALIBRE": "54-21",
     "EAN": "8056597875349"},
    {"MARCA": "Prada", "NOMBRE": "Prada PR 65ZS", "MODELO": "PR 65ZS", "COLOR": "ZVN09T", "CALIBRE": "55-20",
     "EAN": "8056597877329"},
    {"MARCA": "Prada", "NOMBRE": "Prada PR 26ZS", "MODELO": "PR 26ZS", "COLOR": "16K08Z", "CALIBRE": "55-16",
     "EAN": "8056597875226"},
    {"MARCA": "Bottega Veneta", "NOMBRE": "BV1274S", "MODELO": "BV1274S", "COLOR": "005", "CALIBRE": "63-13",
     "EAN": "889652637075"},
])
COMPETITORS = pd.DataFrame([
    {"Competidor": "Visio Factory", "Página web": "https://www.visiofactory.es/"},
    {"Competidor": "Edel-Optics", "Página web": "https://www.edel-optics.es/"},
])


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "prices.sqlite3"
    store = PriceComparatorStore(path)
    store.import_products(PRODUCTS)
    store.import_competitors(COMPETITORS)
    store.import_own_prices(pd.DataFrame([
        {"EAN": "8056597875349", "Precio": "312", "Moneda": "EUR", "Mercado": "ES"},
        {"EAN": "8056597877329", "Precio": "195", "Moneda": "EUR", "Mercado": "ES"},
        {"EAN": "8056597875226", "Precio": "273", "Moneda": "EUR", "Mercado": "ES"},
    ]))
    return path


def _count(path, table, where="1=1"):
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]


def _data(**changes):
    base = json.loads(FIXTURE.read_text(encoding="utf-8"))
    obs = dict(base["observaciones"][0], **changes)
    return {"schema": po.SCHEMA, "observaciones": [obs]}


def _write(tmp_path, data):
    path = tmp_path / "obs.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_dry_run_is_default_and_never_writes(db):
    result = po.import_observations(FIXTURE, db)
    assert result["applied"] is False and result["errors"] == []
    assert result["inserted"] == 4  # 3 offers + the blocked Edel-Optics entry (search_results)
    assert _count(db, "offers") == 0 and _count(db, "search_results") == 0


def test_apply_imports_compares_by_ean_and_handles_stock(db):
    result = po.import_observations(FIXTURE, db, apply=True)
    assert result["errors"] == []
    assert _count(db, "offers") == 3
    assert _count(db, "search_results", "match_status='error'") == 1
    summary, _ = PriceComparatorStore(db).dashboard(max_age_days=3650)
    rows = summary.set_index("ean")
    pr27 = rows.loc["8056597875349"]
    assert pr27["competitor_min_eur"] == pytest.approx(239)
    assert pr27["difference_eur"] == pytest.approx(73)
    assert pr27["difference_pct"] == pytest.approx(73 / 312 * 100)
    assert pr27["difference_vs_competitor_pct"] == pytest.approx(73 / 239 * 100)
    assert rows.loc["8056597875226"]["competitor_min_eur"] == pytest.approx(219)
    # Out of stock: stored and counted as confirmed? no: not comparable and not an available offer.
    assert rows.loc["8056597877329"]["competitor_count"] == 0
    assert pd.isna(rows.loc["8056597877329"]["difference_eur"])
    stored = PriceComparatorStore(db).offer_history("8056597875349").iloc[0]
    assert (stored["caliber"], stored["color_code"], stored["ean_published"]) == ("54-21", "16K08Z", "8056597875349")
    assert (stored["price_includes_tax"], stored["vat_rate"], stored["match_basis"]) == (1, 21, "ean")


def test_reimport_does_not_duplicate_and_records_checks(db, tmp_path):
    po.import_observations(FIXTURE, db, apply=True)
    again = po.import_observations(FIXTURE, db, apply=True)
    assert again["inserted"] == 0 and again["unchanged_checked"] == 3

    later = _data(fecha_observacion="2026-11-02T10:00:00+01:00")
    result = po.import_observations(_write(tmp_path, later), db, apply=True)
    assert (result["inserted"], result["unchanged_checked"]) == (0, 1)
    assert _count(db, "offers", "ean='8056597875349'") == 1
    with closing(sqlite3.connect(db)) as connection:
        last_ok, checks = connection.execute(
            "SELECT last_ok_at,checks FROM offer_checks WHERE ean='8056597875349'").fetchone()
    assert last_ok.startswith("2026-11-02") and checks >= 2


def test_price_and_availability_changes_keep_history(db, tmp_path):
    po.import_observations(FIXTURE, db, apply=True)
    changed = _data(precio_venta=229.0, fecha_observacion="2026-11-03T10:00:00+01:00")
    po.import_observations(_write(tmp_path, changed), db, apply=True)
    out_of_stock = _data(precio_venta=229.0, disponibilidad="unavailable",
                         fecha_observacion="2026-11-04T10:00:00+01:00")
    po.import_observations(_write(tmp_path, out_of_stock), db, apply=True)
    history = PriceComparatorStore(db).offer_history("8056597875349")
    assert list(history["price"]) == [229.0, 229.0, 239.0]
    assert list(history["availability"]) == ["unavailable", "available", "available"]


def test_reference_only_observation_resolves_through_normalised_reference(db, tmp_path):
    obs = {"referencia": "BOTTEGA VENETA BV1274S 005", "competidor": "Visio Factory", "marca": "Bottega Veneta",
           "modelo": "BV1274S", "color": "005", "calibre": "63-13", "precio_venta": 300, "iva_incluido": True,
           "moneda": "EUR", "mercado": "ES",
           "url": "https://www.visiofactory.es/bv1274s-005.html", "disponibilidad": "available",
           "coincidencia": "exact_match", "evidencia": "Modelo, color y calibre", "metodo": "Ficha pública",
           "fecha_observacion": "2026-11-01T10:00:00+01:00"}
    result = po.import_observations(_write(tmp_path, {"schema": po.SCHEMA, "observaciones": [obs]}), db, apply=True)
    assert result["errors"] == []
    stored = PriceComparatorStore(db).offer_history("0889652637075").iloc[0]
    assert (stored["match_status"], stored["match_basis"], stored["ean_published"]) == ("exact_match", "reference", "")


@pytest.mark.parametrize("changes, expected", [
    ({"ean_publicado": ""}, "exact_match"),                               # no EAN: model+colour+calibre suffice
    ({"ean_publicado": "", "calibre": ""}, None),                         # no EAN and no calibre: not exact
    ({"ean_publicado": "", "color": "ZVN09T"}, None),                     # other colour
    ({"ean_publicado": "", "calibre": "55-21"}, None),                    # other calibre
    ({"ean_publicado": "8056597875226"}, None),                           # another product's EAN
])
def test_exact_claims_are_verified(db, tmp_path, changes, expected):
    result = po.import_observations(_write(tmp_path, _data(**changes)), db, apply=True)
    if expected:
        assert result["errors"] == [] and _count(db, "offers", "match_status='exact_match'") == 1
    else:
        assert result["errors"] and _count(db, "offers") == 0


def test_probable_and_rejected_never_enter_the_comparison(db, tmp_path):
    probable = _data(ean_publicado="", calibre="", coincidencia="probable_match")
    po.import_observations(_write(tmp_path, probable), db, apply=True)
    wrong = _data(ean_publicado="", color="ZVN09T", coincidencia="rejected", precio_venta=100.0,
                  fecha_observacion="2026-10-31T10:00:00+01:00")
    po.import_observations(_write(tmp_path, wrong), db, apply=True)
    assert _count(db, "offers", "match_status='probable_match'") == 1
    assert _count(db, "search_results", "match_status='rejected'") == 1
    summary, _ = PriceComparatorStore(db).dashboard(max_age_days=3650)
    assert summary.set_index("ean").loc["8056597875349"]["competitor_count"] == 0


def test_undetermined_vat_goes_to_review_and_vat_excluded_is_not_compared(db, tmp_path):
    po.import_observations(_write(tmp_path, _data(iva_incluido=None)), db, apply=True)
    assert _count(db, "offers", "match_status='manual_review'") == 1
    net = _data(iva_incluido=False, precio_venta=190.0, fecha_observacion="2026-11-05T10:00:00+01:00")
    po.import_observations(_write(tmp_path, net), db, apply=True)
    summary, _ = PriceComparatorStore(db).dashboard(max_age_days=3650)
    assert summary.set_index("ean").loc["8056597875349"]["competitor_count"] == 0


def test_unknown_schema_and_competitor_are_reported(db, tmp_path):
    assert po.import_observations(_write(tmp_path, {"schema": "x"}), db)["errors"]
    assert po.import_observations(_write(tmp_path, _data(competidor="Inventada")), db)["errors"]


def test_competitor_market_and_currency_are_not_assumed(tmp_path):
    store = PriceComparatorStore(tmp_path / "p.sqlite3")
    store.import_competitors(COMPETITORS)
    assert set(store.competitors()["market"]) == {""} and set(store.competitors()["currency"]) == {""}
    assert len(store.fill_competitor_defaults()) == 2
    assert set(store.competitors()["market"]) == {""}
    with pytest.raises(ValueError, match="verificados"):
        store.fill_competitor_defaults(apply=True)
    store.fill_competitor_defaults(apply=True, market="ES", currency="eur")
    assert set(store.competitors()["market"]) == {"ES"} and set(store.competitors()["currency"]) == {"EUR"}


def test_observation_without_verified_market_or_currency_is_not_imported(db, tmp_path):
    for missing in ("mercado", "moneda"):
        data = _data()
        data["observaciones"][0].pop(missing, None)
        report = po.import_observations(_write(tmp_path, data), db)
        assert report["errors"] and "no se asumen" in report["errors"][0]
        assert report["inserted"] == 0 and _count(db, "offers") == 0


def test_coverage_requires_proof_for_operational(db):
    store = PriceComparatorStore(db)
    store.save_coverage([{"competitor": "Edel-Optics", "integration_status": "blocked", "method": "web pública",
                          "restrictions": "HTTP 403", "last_checked_at": "2026-10-30"}])
    with pytest.raises(ValueError, match="operational"):
        store.save_coverage([{"competitor": "Visio Factory", "integration_status": "operational"}])
    po.import_observations(FIXTURE, db, apply=True)
    table = PriceComparatorStore(db).coverage_table().set_index("competitor")
    assert table.loc["Visio Factory", "confirmed_offers"] == 3
    assert table.loc["Visio Factory", "integration_status"] == "not_checked"
    assert table.loc["Edel-Optics", "integration_status"] == "blocked"
    assert table.loc["Edel-Optics", "references_located"] == 0


def test_catalog_sync_dry_run_then_apply(db):
    def reader(eans):
        return [{"ean": "889652637075", "model": "BV1274S", "color": "005", "caliber": "64-13"}]
    changes = po.sync_catalog(db, reader=reader)
    assert changes[0]["changes"]["caliber"] == ("63-13", "64-13")
    assert PriceComparatorStore(db).products().set_index("ean").loc["0889652637075", "caliber"] == "63-13"
    po.sync_catalog(db, apply=True, reader=reader)
    assert PriceComparatorStore(db).products().set_index("ean").loc["0889652637075", "caliber"] == "64-13"


def test_search_report_and_cli(db, capsys):
    report = po.search_report(PriceComparatorStore(db), "BOTTEGA VENETA BV1274S 005")
    assert report["level"] == "brand_model_color" and report["products"][0]["ean"] == "0889652637075"
    assert report["plan"][0] == {"level": "ean", "query": "0889652637075"}
    assert po.main(["--db", str(db), "search", "bv1274s005"]) == 0
    assert "0889652637075" in capsys.readouterr().out


def test_old_database_gets_new_columns(tmp_path):
    path = tmp_path / "old.sqlite3"
    PriceComparatorStore(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("ALTER TABLE offers DROP COLUMN match_basis")
        connection.execute("ALTER TABLE products DROP COLUMN caliber")
    store = PriceComparatorStore(path)
    assert "caliber" in store.products().columns


def test_coverage_file_lists_14_competitors_none_operational():
    from price_comparator import COVERAGE_STATUSES

    path = Path(__file__).parents[1] / "docs" / "Precios" / "cobertura_competidores.json"
    records = json.loads(path.read_text(encoding="utf-8"))["competidores"]
    assert len({r["competitor"] for r in records}) == 14
    assert {r["integration_status"] for r in records} <= COVERAGE_STATUSES - {"operational"}
    assert all(r["restrictions"] and r["allowed_frequency"] and r["last_checked_at"] for r in records)


def test_coverage_matches_accented_competitor_names(tmp_path):
    store = PriceComparatorStore(tmp_path / "p.sqlite3")
    store.import_competitors(pd.DataFrame([{"Competidor": "Óptica Óptima", "Página web": "https://optica-optima.com/"}]))
    store.save_coverage([{"competitor": "óptica óptima", "integration_status": "no_authorized_connector"}])
    assert store.coverage_table().iloc[0]["integration_status"] == "no_authorized_connector"
