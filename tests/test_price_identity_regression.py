"""Production-title regression. Every observation and database is offline/temporary."""
import json
from pathlib import Path

import pandas as pd
import pytest

from price_pilot import REQUIRED_EANS, SAMPLE_EANS, pilot_scope
from price_references import EXACT, PROBABLE, REJECTED, classify, compare_calibers, parse_reference
from tests.test_price_pilot import known
from tests.web_fixtures import prada_page

pytest_plugins = ["tests.test_price_pilot"]

EAN = REQUIRED_EANS[0]
TITLE = "Prada Symbole PR 27ZS 16K08Z 54-21 Negro"


@pytest.mark.parametrize("title", [
    TITLE, "Prada Symbole PR27ZS 16K08Z 54-21 Negro",
    "Prada Symbole PR-27ZS 16K/08Z 54/21 Negro",
    "PR 27ZS 16K08Z 54-21",
])
def test_exact_title_parsing(title):
    reference = parse_reference(title, brands=["Prada"], models=["PR 27ZS"])
    assert reference.model == "PR 27ZS"
    assert reference.color_key == "16K08Z"
    assert reference.caliber == "54-21"
    assert not reference.ambiguous
    assert compare_calibers("54", reference.caliber) == "equal"


def test_parser_complete_model_tokens_and_ambiguous_variants():
    for text in ["Prada PR27ZS2 16K08Z 54-21", "Prada PR 26ZS 16K08Z 54-21"]:
        assert parse_reference(text, models=["PR 27ZS"]).model_key != "PR27ZS"
    reference = parse_reference("PR27ZS 16K08Z y PR27ZS ZVN09T", models=["PR 27ZS"])
    assert reference.ambiguous and not reference.model
    assert not parse_reference("Prada Symbole PR27ZS Negro 54-21", models=["PR 27ZS"]).color
    reference = parse_reference("Prada Symbole PR27ZS 16K08Z 54 Negro", models=["PR 27ZS"])
    assert reference.color_key == "16K08Z" and reference.caliber == "54"


def exact_production_page(pilot, *, title=TITLE, published=EAN, color="", caliber="54-21",
                          identifiers=None, **fields):
    """Use the already published production fields, not a network retrieval."""
    service, store, search, site, cid, _ = pilot
    url = known(pilot, name=title)
    html = prada_page(EAN, name=title)
    from price_web_extract import extract_product
    page = extract_product(html, url)
    page.title, page.ean_published, page.color, page.caliber = title, published, color, caliber
    page.identifiers = identifiers if identifiers is not None else ["PR27ZS", "PR 27ZS 16K08Z 54-21"]
    page.brand, page.price, page.currency = "Prada", 239, "EUR"
    page.includes_tax, page.vat_rate, page.availability, page.market = True, 21, "available", "ES"
    for name, value in fields.items():
        setattr(page, name, value)
    return page


def check(pilot, page):
    from price_web import Outcome
    service, store, search, site, cid, _ = pilot
    validation = {}
    outcome = Outcome(EAN, cid, "Visio Factory", "exact", url=page.url, page=page,
                      method="web:json-ld", match_status="exact_match")
    reasons = service._record(search, search.config(cid), outcome, validation=validation)
    return reasons, validation


def empty_caliber(pilot):
    with pilot[1]._connect() as connection, connection:
        connection.execute("UPDATE products SET caliber='' WHERE ean=?", (EAN,))


def test_production_values_missing_caliber_comparable_by_exact_ean(pilot):
    service, store, search, site, cid, _ = pilot
    empty_caliber(pilot)
    page = exact_production_page(pilot)
    reasons, validation = check(pilot, page)
    assert reasons == []
    assert validation["listing"]["models"] == ["PR27ZS"]
    assert validation["listing"]["colors"] == ["16K08Z"]
    assert validation["accepted"]
    assert validation["caliber_informational"]
    assert validation["observations"]
    assert store.dashboard()[1]["confirmed"] == 1
    before = pilot_scope(store)
    products_before = store.products().to_dict("records")
    reasons, validation = check(pilot, page)
    assert reasons == []
    assert validation["accepted"] and validation["caliber_comparison"] == "unknown"
    assert validation["catalogue"]["caliber"] == ""
    assert pilot_scope(store) == before
    assert store.products().to_dict("records") == products_before
    assert site.calls == []
    assert store.dashboard()[1]["confirmed"] == 1


@pytest.mark.parametrize("fields,reason", [
    ({"title": "Prada Symbole PR 27ZS ZVN09T 54-21 Negro", "identifiers": ["PR27ZS"]}, "Color"),
    ({"title": "Prada Symbole PR26ZS 16K08Z 54-21 Negro", "identifiers": ["PR26ZS"]}, "Modelo"),
    ({"published": "8056597875226"}, "EAN"),
    ({"published": ""}, "EAN"),
    ({"published": "invalid"}, "EAN"),
    ({"color": "ZVN09T"}, "Color"),
    ({"title": "Prada Symbole PR27ZS Negro", "identifiers": ["PR27ZS"]}, "Color"),
    ({"ambiguous": True}, "Coincidencia"),
    ({"identifiers": ["PR27ZS", "PR27ZS ZVN09T 54-21"]}, "Color"),
    ({"identifiers": ["PR26ZS 16K08Z 54-21"]}, "Modelo"),
    ({"brand": "Gucci"}, "Marca"),
    ({"market": ""}, "Mercado"),
    ({"includes_tax": None}, "IVA"),
    ({"availability": "unknown"}, "Stock"),
    ({"availability": "unavailable"}, "stock"),
    ({"currency": "USD"}, "Moneda"),
])
def test_strict_identity_and_commercial_rejections(pilot, fields, reason):
    empty_caliber(pilot)
    reasons, validation = check(pilot, exact_production_page(pilot, **fields))
    assert any(reason.lower() in item.lower() for item in reasons), reasons
    assert not validation["accepted"]
    assert pilot[1].dashboard()[1]["confirmed"] == 0
    assert pilot[3].calls == []


def test_twenty_real_sample_references_still_parse_without_scope_change():
    sample = pd.read_csv(Path(__file__).parents[1] / "docs" / "price-pilot-20.csv", dtype=str).fillna("")
    assert len(sample) == 20 and set(sample["EAN"]) == set(SAMPLE_EANS)
    for row in sample.to_dict("records"):
        reference = parse_reference(f"{row['Marca']} Colección {row['Modelo']} {row['Color']} 54-21 Negro",
                                    brands=[row["Marca"]], models=[row["Modelo"]])
        assert reference.model_key == parse_reference(f"{row['Modelo']} {row['Color']}",
                                                     models=[row["Modelo"]]).model_key
        assert reference.color_key == row["Color"]


@pytest.mark.parametrize("catalogue,listing", [("", "54-21"), ("54", "54-21"), ("55-21", "54-21"),
                                             ("54-20", "54-21"), ("54", "")])
def test_exact_ean_caliber_informational_preserves_catalogue(pilot, catalogue, listing):
    with pilot[1]._connect() as c, c:
        c.execute("UPDATE products SET caliber=? WHERE ean=?", (catalogue, EAN))
    before = pilot[1].products().to_dict("records")
    reasons, validation = check(pilot, exact_production_page(pilot, caliber=listing))
    assert not reasons
    assert validation["accepted"] and validation["caliber_informational"]
    assert pilot[1].products().to_dict("records") == before
    if validation["caliber_comparison"] != "equal":
        assert validation["observations"]


def test_end_to_end_title_audit_persisted_in_results(pilot):
    service, store, search, site, cid, _ = pilot
    empty_caliber(pilot)
    known(pilot, name=TITLE)
    with store._connect() as c, c:
        c.execute("UPDATE vytria_prices SET price=312 WHERE ean=?", (EAN,))
    service.enable()
    service.run(search=search)
    results, metrics = service.results()
    row = results.loc[(results.ean == EAN) & (results.competitor_id == cid)].iloc[0]
    assert row.comparable == 1, row.detail
    assert row["Diferencia frente a Vytria €"] == -73
    assert row["Diferencia frente a Vytria %"] == pytest.approx(-23.3974358974)
    observation = json.loads(row.observation)
    assert observation["page"]["title"] == TITLE
    assert observation["validation"]["accepted"]
    assert observation["validation"]["listing"]["colors"] == ["16K08Z"]
    assert "EAN publicado idéntico" in row["Motivo de validación"]
    assert metrics["Ofertas comparables"] == 1


@pytest.mark.parametrize("published,caliber,status", [
    (EAN, "", EXACT), (EAN, "55-21", EXACT), ("8056597875226", "54-21", REJECTED),
    ("", "", PROBABLE), ("", "55-21", REJECTED), ("", "54-21", EXACT),
    ("invalid", "54-21", PROBABLE),
])
def test_shared_rule_without_pilot(published, caliber, status):
    product = {"ean": EAN, "brand": "Prada", "model": "PR 27ZS", "color": "16K08Z", "caliber": "54-21"}
    match = classify(product, {"ean_published": published, "brand": "Prada", "model": "PR27ZS",
                               "color": "16K08Z", "caliber": caliber})
    assert match.status == status
    assert match.normalized["caliber_informational"] is (published == EAN)
    if published == EAN and caliber != "54-21":
        assert match.observations


def test_import_observations_and_scheduler_revalidate_before_dedup(pilot):
    from price_connectors import Observation
    from price_observations import SCHEMA, observations_to_frame, catalog_index
    from price_scheduler import CompetitorScheduler
    service, store, search, site, cid, _ = pilot
    empty_caliber(pilot)
    page = exact_production_page(pilot)
    data = {"schema": SCHEMA, "observaciones": [{
        "ean_consultado": EAN, "ean_publicado": EAN, "competidor": "Visio Factory", "marca": "Prada",
        "titulo": TITLE, "identificadores": page.identifiers, "color": "", "calibre": "55-21",
        "precio_venta": 239, "moneda": "EUR", "mercado": "ES", "iva_incluido": True,
        "disponibilidad": "available", "url": page.url, "coincidencia": "exact_match",
        "metodo": "JSON verificado", "fecha_observacion": "2026-11-01T00:00:00+00:00",
    }]}
    products = store.products().to_dict("records")
    frame, errors = observations_to_frame(data, catalog_index(store))
    assert not errors
    assert not store.import_offers(frame).rejected
    assert not store.import_offers(frame).rejected
    with store._connect() as c:
        offers = c.execute("SELECT * FROM offers").fetchall()
        assert len(offers) == 1
        assert offers[0]["match_basis"] == "ean"
        assert offers[0]["ean_published"] == EAN and offers[0]["caliber"] == "55-21"
        assert "Calibre informativo" in offers[0]["evidence"]
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not c.execute("PRAGMA foreign_key_check").fetchall()
    assert store.products().to_dict("records") == products
    scheduler = CompetitorScheduler(store)
    competitor = scheduler._competitor(cid)
    connector = scheduler.connectors["web_ficha"]
    item = Observation(ean=EAN, price="239", currency="EUR", availability="available", url=page.url,
                       market="ES", brand="Prada", title=TITLE, ean_published="8056597875226",
                       color="16K08Z", caliber="55-21", tax_included=True)
    # Price unchanged must not bypass validation of a new, incorrect published EAN.
    state, reason = scheduler._record(competitor, connector, item)
    assert state == "rejected" and "EAN publicado distinto" in reason
    assert len(store.offer_history(EAN)) == 1
    assert site.calls == []


def test_all_twenty_products_and_six_competitors_use_same_rule(pilot):
    service, store, search, site, cid, _ = pilot
    real = pd.read_csv(Path(__file__).parents[1] / "docs" / "price-pilot-20.csv", dtype=str).fillna("")
    scope = pilot_scope(store)
    for row in real.to_dict("records"):
        product = {"ean": row["EAN"], "brand": row["Marca"], "model": row["Modelo"], "color": row["Color"],
                   "caliber": ""}
        candidate = {"ean_published": row["EAN"], "brand": row["Marca"],
                     "title": f"{row['Marca']} Collection {row['Modelo']} {row['Color']} 54-21 Negro",
                     "caliber": "54-21"}
        for competitor in service.competitors():
            match = classify(product, candidate)
            assert match.status == EXACT, (row["EAN"], competitor["name"], match.reasons)
            assert match.normalized["verified_ean"]
            assert match.observations
    assert pilot_scope(store) == scope
    assert site.calls == []
