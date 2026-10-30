from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from decimal import Decimal
from pathlib import Path
import sqlite3

import pandas as pd
import pytest
from filelock import FileLock
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import OperationalError
from streamlit.testing.v1 import AppTest

import vytria_price_sync
from price_comparator import PriceComparatorStore, normalize_gtin
from vytria_price_sync import (
    OdooProduct, PriceRow, SqlOdooPriceReader, SyncInProgress, VytriaPriceSync,
    validate_product,
)

# EAN, list price, tarifa 59 price, VAT %, map discount: the 12 audited pilot products.
AUDITED = [
    ("8056262398043", 360, 252, 21, 30),
    ("8056262600078", 320, 208, 10, 35),
    ("8056597669016", 320, 256, 10, 20),
    ("8056597669023", 320, 224, 10, 30),
    ("8056597669047", 320, 224, 10, 30),
    ("8056597981361", 320, 224, 10, 30),
    ("843023181451", 1100, 1100, 21, 0),
    ("889214029614", 320, 208, 21, 35),
    ("889214095985", 290, 189, 21, 35),
    ("889652543574", 310, 217, 21, 30),
    ("889652630922", 360, 252, 21, 30),
    ("889652637075", 450, 270, 21, 40),
]

SCHEMA = """
CREATE TABLE product_product(id INTEGER PRIMARY KEY, barcode TEXT, active BOOLEAN, product_tmpl_id INTEGER);
CREATE TABLE product_template(id INTEGER PRIMARY KEY, active BOOLEAN, sale_ok BOOLEAN, list_price NUMERIC);
CREATE TABLE prestashop_pricelist_item(id INTEGER PRIMARY KEY, product_id INTEGER, active BOOLEAN);
CREATE TABLE prestashop_pricelist_item_pricelist(id INTEGER PRIMARY KEY, pricelist_id INTEGER,
    prestashop_catalog_id INTEGER, currency_id INTEGER, price NUMERIC, active BOOLEAN,
    prestashop_pricelist_id TEXT, write_date TEXT);
CREATE TABLE res_currency(id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE product_taxes_rel(prod_id INTEGER, tax_id INTEGER);
CREATE TABLE account_tax(id INTEGER PRIMARY KEY, amount NUMERIC, amount_type TEXT, price_include BOOLEAN,
    type_tax_use TEXT, active BOOLEAN);
CREATE TABLE product_pricelist_map(id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE product_pricelist_map_product_template_rel(product_template_id INTEGER, product_pricelist_map_id INTEGER);
CREATE TABLE product_pricelist_item(id INTEGER PRIMARY KEY, pricelist_id INTEGER, applied_on TEXT,
    product_pricelist_map_id INTEGER, product_id INTEGER, product_tmpl_id INTEGER, compute_price TEXT, base TEXT,
    price_discount NUMERIC, price_surcharge NUMERIC, price_round NUMERIC, min_quantity NUMERIC,
    date_start TEXT, date_end TEXT, active BOOLEAN);
INSERT INTO res_currency VALUES (1,'EUR'),(2,'USD');
"""


class OdooFixture:
    """SQLite stand-in for the Odoo tables read by the synchronizer."""

    def __init__(self, path: Path) -> None:
        self.url = f"sqlite:///{path}"
        self.statements: list[str] = []
        engine = self.engine_factory()
        with engine.begin() as connection:
            for statement in SCHEMA.strip().split(";\n"):
                if statement.strip():
                    connection.execute(text(statement))
        engine.dispose()
        self._next = 1
        self._taxes: dict[tuple, int] = {}
        self._maps: dict[str, int] = {}
        self._rules: dict[tuple, int] = {}

    def engine_factory(self):
        engine = create_engine(self.url)

        @event.listens_for(engine, "before_cursor_execute")
        def record(conn, cursor, statement, parameters, context, executemany):
            self.statements.append(statement)
        return engine

    def execute(self, sql: str, **params) -> None:
        engine = self.engine_factory()
        with engine.begin() as connection:
            connection.execute(text(sql), params)
        engine.dispose()

    def _id(self) -> int:
        self._next += 1
        return self._next

    def tax(self, rate: int, included: bool = True) -> int:
        key = (rate, included)
        if key not in self._taxes:
            self._taxes[key] = self._id()
            self.execute("INSERT INTO account_tax VALUES(:i,:a,'percent',:inc,'sale',1)",
                         i=self._taxes[key], a=rate, inc=included)
        return self._taxes[key]

    def map_rule(self, name: str, discount: float, **extra) -> int:
        if name not in self._maps:
            self._maps[name] = self._id()
            self.execute("INSERT INTO product_pricelist_map VALUES(:i,:n)", i=self._maps[name], n=name)
            self.execute("""INSERT INTO product_pricelist_item(id,pricelist_id,applied_on,product_pricelist_map_id,
                compute_price,base,price_discount,price_surcharge,price_round,min_quantity,active)
                VALUES(:i,59,'1_pricelist_map',:m,'formula','list_price',:d,:s,1.0,0,1)""",
                         i=self._id(), m=self._maps[name], d=discount, s=extra.get("surcharge", 0))
        return self._maps[name]

    def product(self, ean: str, list_price: float, price: float | None, *, vat: int | None = 21,
                vat_included: bool = True, maps: tuple = (), currency: int = 1, sale_ok: bool = True,
                active: bool = True, catalog: str = "TARIFA_ESPAÑA", price_active: bool = True) -> int:
        pid, tid = self._id(), self._id()
        self.execute("INSERT INTO product_product VALUES(:p,:b,:a,:t)", p=pid, b=ean, a=active, t=tid)
        self.execute("INSERT INTO product_template VALUES(:t,:a,:s,:l)", t=tid, a=True, s=sale_ok, l=list_price)
        if vat is not None:
            self.execute("INSERT INTO product_taxes_rel VALUES(:t,:x)", t=tid, x=self.tax(vat, vat_included))
        for map_id in maps:
            self.execute("INSERT INTO product_pricelist_map_product_template_rel VALUES(:t,:m)", t=tid, m=map_id)
        if price is not None:
            catalog_id = self._id()
            self.execute("INSERT INTO prestashop_pricelist_item VALUES(:c,:p,1)", c=catalog_id, p=pid)
            self.set_price(catalog_id, price, currency=currency, catalog=catalog, active=price_active)
        return pid

    def set_price(self, catalog_id: int, price: float | None, *, currency: int = 1,
                  catalog: str = "TARIFA_ESPAÑA", active: bool = True) -> None:
        self.execute("DELETE FROM prestashop_pricelist_item_pricelist WHERE prestashop_catalog_id=:c", c=catalog_id)
        self.execute("""INSERT INTO prestashop_pricelist_item_pricelist VALUES(:i,59,:c,:cur,:p,:a,:n,'2026-10-30 01:31:00')""",
                     i=self._id(), c=catalog_id, cur=currency, p=price, a=active, n=catalog)

    def catalog_id(self, ean: str) -> int:
        engine = self.engine_factory()
        with engine.connect() as connection:
            return connection.execute(text("""SELECT psi.id FROM prestashop_pricelist_item psi
                JOIN product_product pp ON pp.id=psi.product_id WHERE pp.barcode=:b"""), {"b": ean}).scalar_one()


def _store(tmp_path, eans=None) -> PriceComparatorStore:
    store = PriceComparatorStore(tmp_path / "comparator.sqlite3")
    rows = [{"MARCA": "Marca", "NOMBRE": f"Gafa {ean}", "MODELO": "M", "COLOR": "01", "CATEGORIA": "Sol", "EAN": ean}
            for ean in (eans or [item[0] for item in AUDITED])]
    report = store.import_products(pd.DataFrame(rows))
    assert not report.rejected
    return store


def _audited_odoo(tmp_path) -> OdooFixture:
    odoo = OdooFixture(tmp_path / "odoo.sqlite3")
    for ean, list_price, price, vat, discount in AUDITED:
        odoo.product(ean, list_price, price, vat=vat, maps=(odoo.map_rule(f"Vytria España ({discount}%)", discount),))
    return odoo


def _sync(store, odoo, **kwargs) -> VytriaPriceSync:
    kwargs.setdefault("sleep", lambda _: None)
    return VytriaPriceSync(store, SqlOdooPriceReader(odoo.engine_factory), **kwargs)


def _current(store, ean: str) -> dict:
    with store._connect() as connection:
        return dict(connection.execute("SELECT * FROM vytria_prices WHERE ean=?", (normalize_gtin(ean),)).fetchone())


def _count(store, table: str) -> int:
    with store._connect() as connection:
        return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_audited_eans_are_stored_as_received_with_vat_included(tmp_path) -> None:
    store, odoo = _store(tmp_path), _audited_odoo(tmp_path)

    result = _sync(store, odoo).run()

    assert (result.status, result.inserted, result.issues) == ("ok", 12, 0)
    for ean, _, price, vat, _ in AUDITED:
        row = _current(store, ean)
        assert row["price"] == price  # no discount re-applied, no VAT added
        assert (row["currency"], row["price_includes_tax"], row["vat_rate"]) == ("EUR", 1, vat)
        assert (row["validation_status"], row["comparable"], row["expected_price"]) == ("validated", 1, price)
        assert row["source"] == "Odoo tarifa 59" and row["pricelist_id"] == 59
        assert row["source_product_id"] and row["source_price_id"] and row["synced_at"]
    assert _count(store, "vytria_price_history") == 12


def test_upc_with_odoo_barcode_without_leading_zero_is_matched(tmp_path) -> None:
    store, odoo = _store(tmp_path, ["843023181451"]), _audited_odoo(tmp_path)
    assert normalize_gtin("843023181451") == "0843023181451"

    _sync(store, odoo).run()

    assert _current(store, "843023181451")["price"] == 1100
    with store._connect() as connection:
        assert connection.execute("SELECT original_ean FROM products").fetchone()[0] == "843023181451"


def test_repeated_sync_is_idempotent_and_keeps_history(tmp_path) -> None:
    store, odoo = _store(tmp_path), _audited_odoo(tmp_path)
    sync = _sync(store, odoo)
    sync.run()

    second = sync.run()

    assert (second.inserted, second.changed, second.unchanged) == (0, 0, 12)
    assert _count(store, "vytria_price_history") == 12
    assert _count(store, "vytria_prices") == 12


def test_price_change_keeps_previous_price_in_history(tmp_path) -> None:
    store, odoo = _store(tmp_path, ["8056262398043"]), _audited_odoo(tmp_path)
    sync = _sync(store, odoo)
    sync.run()
    odoo.set_price(odoo.catalog_id("8056262398043"), 240)

    result = sync.run()

    assert result.changed == 1
    assert _current(store, "8056262398043")["price"] == 240
    history = sync.price_history("8056262398043")
    assert history["price"].tolist() == [240, 252]
    assert _current(store, "8056262398043")["validation_status"] == "price_mismatch"


def test_discrepancy_is_recorded_without_altering_source_price(tmp_path) -> None:
    store, odoo = _store(tmp_path, ["8056262398043"]), OdooFixture(tmp_path / "odoo.sqlite3")
    odoo.product("8056262398043", 360, 300, maps=(odoo.map_rule("Vytria España (30%)", 30),))

    _sync(store, odoo).run()

    row = _current(store, "8056262398043")
    assert (row["price"], row["expected_price"]) == (300, 252)
    assert (row["validation_status"], row["comparable"]) == ("price_mismatch", 0)
    assert "252" in row["validation_detail"]


def test_special_map_65_percent_is_flagged_for_review(tmp_path) -> None:
    store, odoo = _store(tmp_path, ["8056262398043"]), OdooFixture(tmp_path / "odoo.sqlite3")
    # The 59 pricelist applies 45 % to the map named 65 %: price is consistent but exceptional.
    odoo.product("8056262398043", 400, 220, maps=(odoo.map_rule("Vytria España (65%)", 45),))

    _sync(store, odoo).run()

    row = _current(store, "8056262398043")
    assert (row["price"], row["validation_status"], row["comparable"]) == (220, "exceptional_rule", 0)
    assert "aplica 45" in row["validation_detail"]


def test_special_map_ct0649s_negative_discount_is_flagged(tmp_path) -> None:
    store, odoo = _store(tmp_path, ["8056262398043"]), OdooFixture(tmp_path / "odoo.sqlite3")
    odoo.product("8056262398043", 100, 400, maps=(odoo.map_rule("Vytria CT0649S", -300),))

    _sync(store, odoo).run()

    row = _current(store, "8056262398043")
    assert (row["price"], row["expected_price"]) == (400, 400)
    assert (row["validation_status"], row["comparable"]) == ("exceptional_rule", 0)
    assert "negativo" in row["validation_detail"]


def test_product_specific_rule_is_exceptional(tmp_path) -> None:
    store, odoo = _store(tmp_path, ["8056262398043"]), OdooFixture(tmp_path / "odoo.sqlite3")
    pid = odoo.product("8056262398043", 360, 252, maps=(odoo.map_rule("Vytria España (30%)", 30),))
    odoo.execute("""INSERT INTO product_pricelist_item(id,pricelist_id,applied_on,product_id,compute_price,base,active)
                    VALUES(900,59,'0_product_variant',:p,'fixed','list_price',1)""", p=pid)

    _sync(store, odoo).run()

    assert _current(store, "8056262398043")["validation_status"] == "exceptional_rule"


def test_product_without_map_is_not_comparable(tmp_path) -> None:
    store, odoo = _store(tmp_path, ["8056262398043"]), OdooFixture(tmp_path / "odoo.sqlite3")
    odoo.product("8056262398043", 360, 252)

    _sync(store, odoo).run()

    row = _current(store, "8056262398043")
    assert (row["validation_status"], row["comparable"], row["price"]) == ("no_map", 0, 252)


def test_multiple_maps_conflict_and_agreement(tmp_path) -> None:
    store = _store(tmp_path, ["8056262398043", "8056262600078"])
    odoo = OdooFixture(tmp_path / "odoo.sqlite3")
    odoo.product("8056262398043", 360, 252, maps=(odoo.map_rule("Vytria España (30%)", 30),
                                                  odoo.map_rule("Vytria España (35%)", 35)))
    odoo.product("8056262600078", 320, 224, maps=(odoo.map_rule("A (30%)", 30), odoo.map_rule("B (30%)", 30)))

    _sync(store, odoo).run()

    assert _current(store, "8056262398043")["validation_status"] == "multiple_maps"
    assert _current(store, "8056262600078")["validation_status"] == "validated"


@pytest.mark.parametrize("kwargs,status", [
    ({"vat": None}, "tax_issue"),
    ({"vat": 21, "vat_included": False}, "tax_issue"),
    ({"currency": 2}, "currency_issue"),
    ({"sale_ok": False}, "discontinued"),
])
def test_data_quality_statuses_are_not_comparable(tmp_path, kwargs, status) -> None:
    store, odoo = _store(tmp_path, ["8056262398043"]), OdooFixture(tmp_path / "odoo.sqlite3")
    odoo.product("8056262398043", 360, 252, maps=(odoo.map_rule("Vytria España (30%)", 30),), **kwargs)

    _sync(store, odoo).run()

    row = _current(store, "8056262398043")
    assert (row["validation_status"], row["comparable"]) == (status, 0)


def test_two_sale_taxes_are_not_guessed(tmp_path) -> None:
    store, odoo = _store(tmp_path, ["8056262398043"]), OdooFixture(tmp_path / "odoo.sqlite3")
    pid = odoo.product("8056262398043", 360, 252, maps=(odoo.map_rule("Vytria España (30%)", 30),))
    engine = odoo.engine_factory()
    with engine.begin() as connection:
        tid = connection.execute(text("SELECT product_tmpl_id FROM product_product WHERE id=:p"), {"p": pid}).scalar_one()
    odoo.execute("INSERT INTO product_taxes_rel VALUES(:t,:x)", t=tid, x=odoo.tax(10))

    _sync(store, odoo).run()

    assert _current(store, "8056262398043")["validation_status"] == "tax_issue"


def test_missing_price_and_unknown_ean_are_issues_not_rows(tmp_path) -> None:
    store = _store(tmp_path, ["8056262398043", "8056262600078", "8056597669016"])
    odoo = OdooFixture(tmp_path / "odoo.sqlite3")
    odoo.product("8056262398043", 360, None, maps=(odoo.map_rule("Vytria España (30%)", 30),))
    odoo.product("8056262600078", 320, 0, maps=(odoo.map_rule("Vytria España (30%)", 30),))
    sync = _sync(store, odoo)

    result = sync.run()

    assert (result.status, result.issues, _count(store, "vytria_prices")) == ("partial", 3, 0)
    assert {item["ean"]: item["code"] for item in sync.last_issues()} == {
        "0008056262398043"[-13:]: "no_price", "8056262600078": "no_price", "8056597669016": "not_in_odoo"}


def test_incomplete_data_never_overwrites_confirmed_price(tmp_path) -> None:
    store, odoo = _store(tmp_path, ["8056262398043"]), _audited_odoo(tmp_path)
    sync = _sync(store, odoo)
    sync.run()
    catalog = odoo.catalog_id("8056262398043")

    for price, active in ((None, True), (0, True), (252, False)):
        odoo.set_price(catalog, price, active=active)
        assert sync.run().issues == 1
        row = _current(store, "8056262398043")
        assert (row["price"], row["comparable"]) == (252, 1)
    assert _count(store, "vytria_price_history") == 1


def test_conflicting_prices_and_foreign_catalog_are_issues(tmp_path) -> None:
    store = _store(tmp_path, ["8056262398043", "8056262600078"])
    odoo = OdooFixture(tmp_path / "odoo.sqlite3")
    pid = odoo.product("8056262398043", 360, 252, maps=(odoo.map_rule("Vytria España (30%)", 30),))
    odoo.execute("INSERT INTO prestashop_pricelist_item VALUES(800,:p,1)", p=pid)
    odoo.set_price(800, 260)
    odoo.product("8056262600078", 320, 208, catalog="TARIFA_EUROPA_EU")
    sync = _sync(store, odoo)

    sync.run()

    assert {item["code"] for item in sync.last_issues()} == {"multiple_prices", "unexpected_catalog"}
    assert _count(store, "vytria_prices") == 0


def test_duplicate_active_products_with_same_barcode_are_ambiguous(tmp_path) -> None:
    store, odoo = _store(tmp_path, ["8056262398043"]), OdooFixture(tmp_path / "odoo.sqlite3")
    odoo.product("8056262398043", 360, 252, maps=(odoo.map_rule("Vytria España (30%)", 30),))
    odoo.product("8056262398043", 360, 252, maps=(odoo.map_rule("Vytria España (30%)", 30),))
    sync = _sync(store, odoo)

    sync.run()

    assert [item["code"] for item in sync.last_issues()] == ["ambiguous_product"]
    assert _count(store, "vytria_prices") == 0


class FlakyReader:
    def __init__(self, inner, failures: int) -> None:
        self.inner, self.failures, self.calls = inner, failures, 0

    def fetch(self, eans):
        self.calls += 1
        if self.calls <= self.failures:
            raise OperationalError("SELECT 1", {}, Exception("password=secret host=10.3.0.13"))
        return self.inner.fetch(eans)


def test_connection_failures_are_retried_with_backoff(tmp_path) -> None:
    store, odoo = _store(tmp_path, ["8056262398043"]), _audited_odoo(tmp_path)
    delays: list[float] = []
    reader = FlakyReader(SqlOdooPriceReader(odoo.engine_factory), failures=2)

    result = VytriaPriceSync(store, reader, sleep=delays.append, retry_delay=1).run()

    assert (result.status, reader.calls, delays) == ("ok", 3, [1, 2])


def test_persistent_failure_keeps_prices_and_hides_credentials(tmp_path) -> None:
    store, odoo = _store(tmp_path, ["8056262398043"]), _audited_odoo(tmp_path)
    _sync(store, odoo).run()
    reader = FlakyReader(SqlOdooPriceReader(odoo.engine_factory), failures=99)
    sync = VytriaPriceSync(store, reader, sleep=lambda _: None)

    result = sync.run()

    assert (result.status, reader.calls) == ("error", 3)
    assert "secret" not in result.error and "10.3.0.13" not in result.error
    assert _current(store, "8056262398043")["price"] == 252
    assert sync.last_run()["status"] == "error"
    assert sync.last_successful_run()["status"] == "ok"


def test_concurrent_sync_is_rejected(tmp_path) -> None:
    store, odoo = _store(tmp_path), _audited_odoo(tmp_path)
    sync = _sync(store, odoo)

    with FileLock(str(store.database_path) + ".vytria-sync.lock"):
        with pytest.raises(SyncInProgress):
            sync.run()

    assert sync.run().status == "ok"


def test_odoo_access_is_select_only(tmp_path) -> None:
    store, odoo = _store(tmp_path), _audited_odoo(tmp_path)
    odoo.statements.clear()

    _sync(store, odoo).run()

    assert odoo.statements
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in odoo.statements)


def test_postgres_session_is_read_only_with_timeout() -> None:
    executed: list[str] = []

    class Dialect:
        name = "postgresql"

    class Connection:
        dialect = Dialect()

        def execute(self, statement, params=None):
            executed.append(str(statement))

            class Empty:
                def mappings(self):
                    return []
            return Empty()

        def rollback(self):
            executed.append("ROLLBACK")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class Engine:
        def connect(self):
            return Connection()

        def dispose(self):
            pass

    assert SqlOdooPriceReader(lambda: Engine()).fetch(["8056262398043"]) == []
    assert executed[:2] == ["SET TRANSACTION READ ONLY", "SET LOCAL statement_timeout = 30000"]
    assert all(item.lstrip().upper().startswith(("SET", "SELECT", "ROLLBACK")) for item in executed)


def test_sync_does_not_touch_other_databases(tmp_path) -> None:
    other = tmp_path / "other.sqlite3"
    with sqlite3.connect(other) as connection:
        connection.execute("CREATE TABLE keep(v TEXT)")
        connection.execute("INSERT INTO keep VALUES('x')")
    before = hashlib.sha256(other.read_bytes()).hexdigest()
    store, odoo = _store(tmp_path), _audited_odoo(tmp_path)
    odoo_before = hashlib.sha256((tmp_path / "odoo.sqlite3").read_bytes()).hexdigest()

    _sync(store, odoo).run()

    assert hashlib.sha256(other.read_bytes()).hexdigest() == before
    assert hashlib.sha256((tmp_path / "odoo.sqlite3").read_bytes()).hexdigest() == odoo_before


def test_synced_prices_persist_across_store_instances_and_wal(tmp_path) -> None:
    store, odoo = _store(tmp_path), _audited_odoo(tmp_path)
    _sync(store, odoo).run()

    reopened = PriceComparatorStore(store.database_path)

    assert _count(reopened, "vytria_prices") == 12
    with reopened._connect() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_auto_sync_is_disabled_by_default_and_runs_at_most_daily(tmp_path) -> None:
    store, odoo = _store(tmp_path), _audited_odoo(tmp_path)
    sync = _sync(store, odoo)
    assert sync.auto_enabled is False
    assert sync.run_scheduled() is None
    assert _count(store, "vytria_sync_runs") == 0

    sync.set_auto_enabled(True)
    first = sync.run_scheduled()
    assert first is not None and first.status == "ok"
    assert sync.run_scheduled() is None
    tomorrow = datetime.now(timezone.utc) + timedelta(hours=24)
    assert sync.run_scheduled(now=tomorrow).status == "ok"


def test_cli_does_nothing_while_auto_sync_is_disabled(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("PRICE_COMPARATOR_DB_PATH", str(tmp_path / "cli.sqlite3"))
    monkeypatch.setattr(vytria_price_sync, "build_readonly_engine",
                        lambda: pytest.fail("Odoo must not be contacted"))

    assert vytria_price_sync.main(["--scheduled"]) == 0
    assert "desactivada" in capsys.readouterr().out


def test_dashboard_uses_only_confirmed_synced_price(tmp_path) -> None:
    eans = ["8056262398043", "8056262600078", "843023181451"]
    store, odoo = _store(tmp_path, eans), OdooFixture(tmp_path / "odoo.sqlite3")
    odoo.product("8056262398043", 360, 252, maps=(odoo.map_rule("Vytria España (30%)", 30),))
    odoo.product("8056262600078", 320, 300, maps=(odoo.map_rule("Vytria España (35%)", 35),))  # mismatch
    manual = pd.DataFrame([
        {"EAN": ean, "Precio": "999", "Moneda": "EUR", "Mercado": "ES", "Origen": "Excel"} for ean in eans
    ])
    store.import_own_prices(manual)
    competitor = pd.DataFrame([{"Competidor": "Tienda", "Página web": "https://tienda.example/"}])
    store.import_competitors(competitor)
    store.save_competitors(pd.DataFrame([{"id": "tienda-example", "market": "ES", "currency": "EUR",
                                          "active": True, "query_method": "manual"}]))
    store.import_offers(pd.DataFrame([{
        "EAN": "8056262398043", "Competidor": "Tienda", "Precio": "200", "Moneda": "EUR", "Mercado": "ES",
        "URL": "https://tienda.example/p", "Disponibilidad": "available", "Estado coincidencia": "exact_match",
        "Evidencia": "EAN", "Método": "manual", "IVA incluido": "sí"}]))

    _sync(store, odoo).run()
    summary, _ = store.dashboard()
    by_ean = summary.set_index("ean")

    assert by_ean.loc["8056262398043", "own_price_eur"] == 252
    assert by_ean.loc["8056262398043", "difference_eur"] == 52
    assert by_ean.loc["8056262398043", "own_price_source"] == "Odoo tarifa 59"
    assert pd.isna(by_ean.loc["8056262600078", "own_price_eur"])  # mismatch: Excel does not take over
    assert by_ean.loc["8056262600078", "own_price_status"] == "price_mismatch"
    assert by_ean.loc["0843023181451", "own_price_eur"] == 999  # not synced: contingency Excel remains


def test_validation_with_rounding_and_tolerance() -> None:
    from vytria_price_sync import MapRule
    rule = MapRule(1, "Vytria España (30%)", True, Decimal(30), Decimal(0), "formula", "list_price",
                   Decimal("1.0"), Decimal(0))
    tax = ({"amount": 21, "amount_type": "percent", "price_include": True},)
    product = OdooProduct("0000000000000", 1, 1, True, True, True, Decimal("333"), taxes=tax, maps=(rule,))
    price = PriceRow(1, 1, Decimal("233"), True, True, "EUR", "TARIFA_ESPAÑA", "")  # 233.1 rounds to 233

    assert validate_product(product, price).status == "validated"


def _page(tmp_path, monkeypatch, odoo=None, eans=None):
    monkeypatch.setenv("PRICE_COMPARATOR_DB_PATH", str(tmp_path / "ui.sqlite3"))
    store = PriceComparatorStore()
    store.import_products(pd.DataFrame([
        {"MARCA": "Marca", "NOMBRE": "Gafa", "MODELO": "M", "COLOR": "01", "CATEGORIA": "Sol", "EAN": ean}
        for ean in (eans or ["8056262398043"])]))
    if odoo:
        import app_pages.price_comparator as page
        monkeypatch.setattr(page, "_make_sync", lambda s: VytriaPriceSync(
            s, SqlOdooPriceReader(odoo.engine_factory), sleep=lambda _: None))
    return AppTest.from_string(
        "from app_pages.price_comparator import render_price_comparator_page\n"
        "render_price_comparator_page()", default_timeout=30)


def test_page_shows_odoo_price_section_with_manual_import_as_contingency(tmp_path, monkeypatch) -> None:
    app = _page(tmp_path, monkeypatch).run()

    assert not app.exception
    assert "Precio de venta Vytria" in [item.value for item in app.subheader]
    assert [button.label for button in app.button if button.label == "Actualizar precios Vytria"]
    assert any("pendiente (no validado)" in caption.value for caption in app.caption)
    assert [toggle.value for toggle in app.toggle] == [False]
    assert [expander.label for expander in app.expander if "Contingencia" in expander.label]
