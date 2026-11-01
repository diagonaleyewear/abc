"""Idempotent import of the known competitor pages: candidates only, no HTTP, no offers, nothing enabled."""
import hashlib
import socket

import pytest
import requests
from streamlit.testing.v1 import AppTest

import price_known_urls as known
import price_web_fetch
from price_comparator import PriceComparatorStore
from price_known_urls import KNOWN_URLS, KnownUrl
from price_web import LOCATION_TEXT, WebPriceSearch
from tests.test_price_observations import PRODUCTS
from tests.test_price_review_fixes import _freeze

PRADA_27 = "8056597875349"
PRADA_26 = "8056597875226"
SCRIPT = "from app_pages.price_comparator import render_price_comparator_page\nrender_price_comparator_page()"


@pytest.fixture
def store(tmp_path):
    store = PriceComparatorStore(tmp_path / "prod_copy.sqlite3")
    store.seed_defaults()
    store.import_products(PRODUCTS)
    return store


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("El importador no debe abrir conexiones de red")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


def _rows(store, sql="SELECT * FROM competitor_product_urls ORDER BY competitor_id"):
    with store._connect() as connection:
        return [dict(r) for r in connection.execute(sql)]


def _count(store, table):
    return _rows(store, f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]


def test_dataset_has_fourteen_pages_one_per_competitor_and_the_right_eans():
    assert len(KNOWN_URLS) == 14 and len({e.url for e in KNOWN_URLS}) == 14
    by_name = {e.competitor: e for e in KNOWN_URLS}
    assert len(by_name) == 14
    assert by_name["Mia Burton"].ean == PRADA_26 and "pr-26zs" in by_name["Mia Burton"].url
    assert {e.ean for e in KNOWN_URLS if e.competitor != "Mia Burton"} == {PRADA_27}
    assert all(e.url.startswith("https://") for e in KNOWN_URLS)


def test_import_registers_fourteen_candidates_without_inventing_anything(store, no_network):
    operations = known.plan(store.database_path)
    assert [o.action for o in operations] == ["add"] * 14
    assert known.apply(store.database_path, operations) == {"added": 14, "exists": 0, "skipped": 0}
    rows = _rows(store)
    assert len(rows) == 14 and len({r["competitor_id"] for r in rows}) == 14
    assert {r["status"] for r in rows} == {"pending_verification"}
    assert {r["match_status"] for r in rows} == {"manual_review"} and {r["source"] for r in rows} == {"known_urls"}
    assert all(r["last_price"] is None and r["last_availability"] == "" and r["last_ok_at"] is None for r in rows)
    assert _count(store, "offers") == 0 and _count(store, "search_results") == 0
    # nothing enabled, no queue, no authorisation
    assert not _rows(store, "SELECT 1 FROM competitor_schedule WHERE enabled=1")
    assert _count(store, "scheduler_jobs") == 0 and _count(store, "scheduler_runs") == 0
    assert not _rows(store, "SELECT 1 FROM competitor_web_config WHERE terms_reviewed=1 OR access_status<>'unknown'")


def test_mia_burton_page_belongs_to_the_26zs_ean_and_never_to_the_27zs(store):
    known.apply(store.database_path, known.plan(store.database_path))
    mia = [r for r in _rows(store) if r["competitor_id"].startswith("miaburton")]
    assert [(r["ean"], r["url"]) for r in mia] == [(PRADA_26, "https://miaburton.com/es/gafas-de-sol/prada/pr-26zs-16k08z")]
    assert not [r for r in _rows(store) if r["ean"] == PRADA_27 and "26zs" in r["url"]]


def test_second_run_is_a_noop_without_duplicates(store):
    known.apply(store.database_path, known.plan(store.database_path))
    before = _rows(store)
    again = known.plan(store.database_path)
    assert [o.action for o in again] == ["exists"] * 14
    assert known.apply(store.database_path, again) == {"added": 0, "exists": 14, "skipped": 0}
    assert _rows(store) == before


def test_existing_rows_and_settings_are_never_overwritten(store):
    search = WebPriceSearch(store)
    visio = next(c["id"] for c in store.competitors().to_dict("records") if c["domain"] == "visiofactory.es")
    optima = next(c["id"] for c in store.competitors().to_dict("records") if c["domain"] == "optica-optima.com")
    search.update_config(visio, pause_seconds=9, max_requests=4)
    visio_url = next(e.url for e in KNOWN_URLS if "visiofactory" in e.url)
    with store._connect() as connection, connection:
        connection.execute("""INSERT INTO competitor_product_urls(competitor_id,ean,url,match_status,status,verified_at,
            last_price,last_availability,last_ok_at,source) VALUES(?,?,?,'exact_match','verified','2026-10-30T10:00:00+00:00',
            239,'available','2026-10-30T10:00:00+00:00','auto')""", (visio, PRADA_27, visio_url))
        connection.execute("""INSERT INTO competitor_product_urls(competitor_id,ean,url,match_status,status,verified_at,source)
            VALUES(?,?,?,'probable_match','probable','2026-10-30T10:00:00+00:00','user')""",
                           (optima, PRADA_27, "https://optica-optima.com/otra-ficha.html"))
    snapshot = {(r["competitor_id"], r["url"]): r for r in _rows(store)}
    operations = {o.competitor_id: o for o in known.plan(store.database_path)}
    assert operations[visio].action == "exists"
    assert operations[optima].action == "skip" and "no se sobrescribe" in operations[optima].reason
    assert known.apply(store.database_path, list(operations.values()))["added"] == 12
    after = {(r["competitor_id"], r["url"]): r for r in _rows(store)}
    for key_, row in snapshot.items():
        assert after[key_] == row  # verified evidence and the other user's page untouched
    assert after[(visio, visio_url)]["status"] == "verified" and after[(visio, visio_url)]["last_price"] == 239
    config = search.config(visio)
    assert config["pause_seconds"] == 9 and config["max_requests"] == 4


def test_refuses_unknown_ean_wrong_catalogue_product_and_unknown_competitor(store):
    entries = [
        KnownUrl("Visio Factory", "8056597877329", "PR 27ZS", "16K08Z", KNOWN_URLS[0].url),  # catalogue says PR 65ZS
        KnownUrl("Visio Factory", "5901234123457", "PR 27ZS", "16K08Z", KNOWN_URLS[0].url),  # not in the catalogue
        KnownUrl("Otra", PRADA_27, "PR 27ZS", "16K08Z", "https://tienda-desconocida.example/prada-pr-27zs"),
        KnownUrl("Visio Factory", PRADA_26, "PR 27ZS", "16K08Z", KNOWN_URLS[0].url),  # EAN of the 26ZS for a 27ZS page
        KnownUrl("Mia Burton", PRADA_27, "PR 27ZS", "16K08Z", KNOWN_URLS[-1].url),  # the 26ZS page for the 27ZS EAN
    ]
    operations = known.plan(store.database_path, entries)
    assert [o.action for o in operations] == ["skip"] * 5
    reasons = [o.reason for o in operations]
    assert "no coincide" in reasons[0] and "catálogo" in reasons[1] and "no registrado" in reasons[2]
    assert "no coincide" in reasons[3] and "no corresponde al modelo" in reasons[4]
    assert known.apply(store.database_path, operations)["added"] == 0 and _count(store, "competitor_product_urls") == 0


def test_dry_run_is_the_default_and_writes_nothing(store, capsys, no_network):
    digest = hashlib.sha256(store.database_path.read_bytes()).hexdigest()
    assert known.main(["--db", str(store.database_path)]) == 0
    assert known.main(["--db", str(store.database_path), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert out.count("ADD") == 28 and "Simulación: no se ha escrito nada" in out
    assert _count(store, "competitor_product_urls") == 0
    assert hashlib.sha256(store.database_path.read_bytes()).hexdigest() == digest


def test_apply_requires_a_backup_directory(store, capsys):
    assert known.main(["--db", str(store.database_path), "--apply"]) == 2
    assert "--backup-dir" in capsys.readouterr().out and _count(store, "competitor_product_urls") == 0


def test_apply_makes_a_verified_backup_first_and_revert_restores_the_previous_state(store, tmp_path, capsys):
    backups = tmp_path / "backups"
    assert known.main(["--db", str(store.database_path), "--apply", "--backup-dir", str(backups)]) == 0
    assert "integrity_check posterior: ok" in capsys.readouterr().out
    (backup,) = list(backups.glob("price_comparator.*.sqlite3"))
    copy = PriceComparatorStore(backup)
    assert _count(copy, "competitor_product_urls") == 0 and _count(copy, "products") == len(PRODUCTS)  # pre-import state
    assert _count(store, "competitor_product_urls") == 14 and known.integrity(store.database_path) == "ok"
    # one page gets verified in the meantime: the logical revert must not touch it
    with store._connect() as connection, connection:
        connection.execute("UPDATE competitor_product_urls SET status='verified',last_price=239 WHERE url=?",
                           (KNOWN_URLS[0].url,))
    assert known.main(["--db", str(store.database_path), "--revert", "--backup-dir", str(backups)]) == 0
    remaining = _rows(store)
    assert [r["url"] for r in remaining] == [KNOWN_URLS[0].url] and remaining[0]["status"] == "verified"
    assert list(backups.glob("price_comparator.*.sqlite3"))


def test_audit_is_read_only_and_reports_an_empty_url_table(store, capsys):
    digest = hashlib.sha256(store.database_path.read_bytes()).hexdigest()
    assert known.main(["--db", str(store.database_path), "--audit"]) == 0
    out = capsys.readouterr().out
    assert "competitors=14" in out and "schedule_enabled=0" in out and "VACÍA" in out and "integrity_check: ok" in out
    assert hashlib.sha256(store.database_path.read_bytes()).hexdigest() == digest
    known.apply(store.database_path, known.plan(store.database_path))
    assert known.main(["--db", str(store.database_path), "--audit"]) == 0
    assert "status=pending_verification source=known_urls: 14" in capsys.readouterr().out


def test_missing_database_is_reported_without_creating_it(tmp_path, capsys):
    path = tmp_path / "nope.sqlite3"
    assert known.main(["--db", str(path)]) == 2 and not path.exists()
    assert "No existe la base de datos" in capsys.readouterr().out


def test_imported_pages_survive_a_restart_and_are_listed_as_candidates(store):
    known.apply(store.database_path, known.plan(store.database_path))
    reopened = WebPriceSearch(PriceComparatorStore(store.database_path))
    pages = reopened.registered_pages([PRADA_27])
    assert len(pages) == 13 and set(pages["Estado"]) == {"Ficha candidata conocida, pendiente de verificar"}
    assert set(pages["Comprobación de precio"]) == {"Pendiente"} and pages["Precio"].isna().all()
    states = reopened.competitor_states().set_index("name")
    assert set(states["location"]) == {"candidate"} and set(states["extraction"]) == {"untested"}
    assert states["offers"].sum() == 0 and states["comparable"].sum() == 0
    assert set(states["operability"]) == {"pending_review"}


# --- interface ---------------------------------------------------------------------------------------------------
@pytest.fixture
def no_requests(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("La interfaz no debe consultar webs al mostrar fichas registradas")

    monkeypatch.setattr(requests, "get", refuse)
    monkeypatch.setattr(requests, "request", refuse)
    monkeypatch.setattr(requests.Session, "request", refuse)
    monkeypatch.setattr(price_web_fetch, "_requests_transport", refuse)


@pytest.fixture
def ui(store, monkeypatch, no_requests):
    _freeze(monkeypatch)
    known.apply(store.database_path, known.plan(store.database_path))
    monkeypatch.setenv("PRICE_COMPARATOR_DB_PATH", str(store.database_path))
    return store


def _run():
    app = AppTest.from_string(SCRIPT).run(timeout=90)
    assert not app.exception
    return app


def _search(app, text):
    app.text_input(key="web_text").set_value(text).run(timeout=90)
    app.radio(key="web_mode").set_value("Almacenados")
    app.button(key="web_search_button").click()
    return app.run(timeout=90)


def test_stored_search_shows_candidate_pages_not_a_bare_no_results(ui):
    app = _search(_run(), PRADA_27)
    assert not app.exception and not app.error
    assert any("Producto encontrado" in s.value and PRADA_27 in s.value for s in app.success)
    metrics = {m.label: m.value for m in app.metric}
    assert metrics["URLs conocidas"] == "13" and metrics["Competidores con ficha"] == "13"
    assert metrics["Ofertas comparables"] == "0"
    pages = next(f.value for f in app.dataframe if "Comprobación de precio" in f.value.columns)
    assert len(pages) == 13 and set(pages["Comprobación de precio"]) == {"Pendiente"}
    assert not any("No hay resultados" in i.value for i in app.info)
    assert any("no es una oferta" in c.value for c in app.caption)
    stored = next(f.value for f in app.dataframe if "Diferencia %" in f.value.columns)
    assert stored["Estado"].value_counts().to_dict() == {"Ficha registrada: falta comprobar el precio": 13,
                                                         "Sin datos": 1}  # Mia Burton only has the 26ZS page
    assert stored["Precio"].isna().all()


def test_competitor_panels_have_separate_web_and_scheduler_sections_with_the_scheduler_off(ui):
    app = _run()
    headings = [m.value for m in app.markdown]
    assert headings.count("#### Configuración web") == 14 and headings.count("#### Actualización automática") == 14
    labels = [e.label for e in app.expander if LOCATION_TEXT["candidate"] in e.label]
    assert len(labels) == 14 and all("Pendiente de revisión de condiciones" in label for label in labels)
    assert any("URLs conocidas:** 1" in m for m in headings)
    assert not _rows(ui, "SELECT 1 FROM competitor_schedule WHERE enabled=1")
    assert {metric.label: metric.value for metric in app.metric}["Más caro"] == "Sin datos"
    # Registered URLs remain configuration only; they do not create comparable price observations.
    assert not app.error


def test_web_section_adds_replaces_and_verifies_without_a_feed(ui):
    app = _run()
    cid = next(r["competitor_id"] for r in _rows(ui, "SELECT competitor_id FROM competitor_product_urls LIMIT 1"))
    assert app.text_input(key=f"web_{cid}_verify_url") is not None and app.text_input(key=f"web_{cid}_url_value")
    assert [b for b in app.button if b.label == "Verificar y guardar"]
