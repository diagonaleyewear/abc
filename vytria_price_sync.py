"""Read-only synchronization of the Vytria España selling price from Odoo.

Source of truth: ``prestashop_pricelist_item_pricelist`` for pricelist 59 (TARIFA_ESPAÑA),
which already holds the final price with VAT included. The price is stored as received: it is
never recalculated, discounted again or increased with VAT. The pricelist rules are only used
to cross-check the value and any discrepancy is recorded without altering the source price.

Odoo is accessed with SELECT statements only, inside a READ ONLY transaction.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
import hashlib
import re
import sys
import time
from typing import Any, Callable, Protocol
import unicodedata

from filelock import FileLock, Timeout
from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.engine import URL, Engine
from sqlalchemy.exc import InterfaceError, OperationalError

from price_comparator import SYNCED_PRICE_MAX_AGE_DAYS, PriceComparatorStore, normalize_gtin


VYTRIA_PRICELIST_ID = 59
MARKET = "ES"
SOURCE = "Odoo tarifa 59"
EXPECTED_CATALOG_NAME = "TARIFA_ESPANA"
PRICE_TOLERANCE = Decimal("0.01")
SCHEDULE_INTERVAL = timedelta(hours=23)
FETCH_ATTEMPTS = 3
CHUNK_SIZE = 500

VALIDATED = "validated"
STATUS_LABELS = {
    "validated": "Validado con la tarifa",
    "price_mismatch": "Discrepancia con la tarifa",
    "no_map": "Sin mapa de descuento",
    "multiple_maps": "Varios mapas de descuento",
    "exceptional_rule": "Regla excepcional",
    "tax_issue": "IVA no verificable",
    "currency_issue": "Moneda no EUR",
    "discontinued": "Producto no vendible/descatalogado",
    "stale": "Desactualizado: incidencia en la última sincronización",
    "expired": "Desactualizado: sin sincronizar recientemente",
}
ISSUE_LABELS = {
    "not_in_odoo": "EAN no encontrado en Odoo",
    "no_price": "Sin precio en la tarifa 59",
    "multiple_prices": "Varios precios distintos para el EAN",
    "ambiguous_product": "Varios productos activos con el mismo EAN",
    "unexpected_catalog": "El precio no pertenece a TARIFA_ESPAÑA",
}


class SyncInProgress(RuntimeError):
    """Another synchronization already holds the lock."""


class OdooReadError(RuntimeError):
    """Odoo could not be read after the allowed retries."""


@dataclass(frozen=True)
class PriceRow:
    price_id: int
    catalog_id: int | None
    price: Decimal | None
    price_active: bool
    catalog_active: bool
    currency: str
    catalog_name: str
    write_date: str


@dataclass(frozen=True)
class MapRule:
    map_id: int
    map_name: str
    has_rule: bool
    discount: Decimal | None = None
    surcharge: Decimal | None = None
    compute_price: str = "formula"
    base: str = "list_price"
    rounding: Decimal | None = None
    min_quantity: Decimal | None = None
    has_dates: bool = False


@dataclass(frozen=True)
class OdooProduct:
    ean: str
    product_id: int
    template_id: int
    variant_active: bool
    template_active: bool
    sale_ok: bool
    list_price: Decimal | None
    prices: tuple[PriceRow, ...] = ()
    taxes: tuple[dict, ...] = ()
    maps: tuple[MapRule, ...] = ()
    product_rules: int = 0


@dataclass(frozen=True)
class Validation:
    status: str
    detail: str
    expected: Decimal | None
    comparable: bool


@dataclass
class SyncResult:
    run_id: int
    status: str
    monitored: int = 0
    inserted: int = 0
    changed: int = 0
    unchanged: int = 0
    issues: int = 0
    error: str = ""


class PriceReader(Protocol):
    def fetch(self, eans: list[str]) -> list[OdooProduct]: ...


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dec(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _barcode_forms(ean: str) -> list[str]:
    forms = {ean}
    forms.add(ean[1:] if ean.startswith("0") else ean)
    return sorted(forms)


def _ascii_upper(value: str) -> str:
    return unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().upper()


def build_readonly_engine() -> Engine:
    """Engine over the existing Cloud Odoo configuration (``load_db_config``)."""
    from db_config import load_db_config

    config = load_db_config()
    url = URL.create("postgresql+psycopg2", username=config.user, password=config.password,
                     host=config.host, port=config.port, database=config.database)
    return create_engine(url, pool_pre_ping=True, connect_args={
        "options": "-c default_transaction_read_only=on -c statement_timeout=30000",
        "connect_timeout": 10,
    })


PRODUCTS_SQL = text("""
    SELECT pp.id AS product_id, pp.barcode, pp.active AS variant_active,
           pt.id AS template_id, pt.active AS template_active, pt.sale_ok, pt.list_price
    FROM product_product pp JOIN product_template pt ON pt.id = pp.product_tmpl_id
    WHERE pp.barcode IN :barcodes ORDER BY pp.id
""").bindparams(bindparam("barcodes", expanding=True))
PRICES_SQL = text("""
    SELECT psi.product_id, r.id AS price_id, r.prestashop_catalog_id AS catalog_id, r.price,
           r.active AS price_active, psi.active AS catalog_active, cur.name AS currency,
           r.prestashop_pricelist_id AS catalog_name, r.write_date
    FROM prestashop_pricelist_item_pricelist r
    JOIN prestashop_pricelist_item psi ON psi.id = r.prestashop_catalog_id
    LEFT JOIN res_currency cur ON cur.id = r.currency_id
    WHERE r.pricelist_id = :pricelist_id AND psi.product_id IN :product_ids ORDER BY r.id
""").bindparams(bindparam("product_ids", expanding=True))
TAXES_SQL = text("""
    SELECT rel.prod_id AS template_id, tx.id, tx.amount, tx.amount_type, tx.price_include
    FROM product_taxes_rel rel JOIN account_tax tx ON tx.id = rel.tax_id
    WHERE rel.prod_id IN :template_ids AND tx.type_tax_use = 'sale' AND tx.active
""").bindparams(bindparam("template_ids", expanding=True))
MAPS_SQL = text("""
    SELECT rel.product_template_id AS template_id, m.id AS map_id, CAST(m.name AS TEXT) AS map_name,
           i.id AS item_id, i.price_discount, i.price_surcharge, i.compute_price, i.base,
           i.price_round, i.min_quantity, i.date_start, i.date_end
    FROM product_pricelist_map_product_template_rel rel
    JOIN product_pricelist_map m ON m.id = rel.product_pricelist_map_id
    LEFT JOIN product_pricelist_item i ON i.product_pricelist_map_id = m.id
         AND i.pricelist_id = :pricelist_id AND i.active AND i.applied_on = '1_pricelist_map'
    WHERE rel.product_template_id IN :template_ids
""").bindparams(bindparam("template_ids", expanding=True))
PRODUCT_RULES_SQL = text("""
    SELECT product_id, product_tmpl_id FROM product_pricelist_item
    WHERE pricelist_id = :pricelist_id AND active
      AND (product_id IN :product_ids OR product_tmpl_id IN :template_ids)
""").bindparams(bindparam("product_ids", expanding=True), bindparam("template_ids", expanding=True))


def _chunks(values: list, size: int = CHUNK_SIZE):
    for start in range(0, len(values), size):
        yield values[start:start + size]


class SqlOdooPriceReader:
    """SELECT-only reader. ``engine_factory`` allows tests and Cloud to share one code path."""

    def __init__(self, engine_factory: Callable[[], Engine] = build_readonly_engine,
                 pricelist_id: int = VYTRIA_PRICELIST_ID) -> None:
        self._engine_factory = engine_factory
        self.pricelist_id = pricelist_id

    def fetch(self, eans: list[str]) -> list[OdooProduct]:
        engine = self._engine_factory()
        try:
            with engine.connect() as connection:
                if connection.dialect.name == "postgresql":
                    connection.execute(text("SET TRANSACTION READ ONLY"))
                    connection.execute(text("SET LOCAL statement_timeout = 30000"))
                try:
                    return self._fetch(connection, eans)
                finally:
                    connection.rollback()
        finally:
            engine.dispose()

    def _fetch(self, connection, eans: list[str]) -> list[OdooProduct]:
        forms = sorted({form for ean in eans for form in _barcode_forms(ean)})
        base: dict[int, dict] = {}
        for chunk in _chunks(forms):
            for row in connection.execute(PRODUCTS_SQL, {"barcodes": chunk}).mappings():
                try:
                    base[row["product_id"]] = {**row, "ean": normalize_gtin(row["barcode"])}
                except ValueError:
                    continue
        product_ids = sorted(base)
        template_ids = sorted({row["template_id"] for row in base.values()})
        prices: dict[int, list[PriceRow]] = {}
        taxes: dict[int, list[dict]] = {}
        maps: dict[int, list[MapRule]] = {}
        rules: set[tuple] = set()
        for chunk in _chunks(product_ids):
            for row in connection.execute(PRICES_SQL, {"pricelist_id": self.pricelist_id,
                                                      "product_ids": chunk}).mappings():
                prices.setdefault(row["product_id"], []).append(PriceRow(
                    price_id=row["price_id"], catalog_id=row["catalog_id"], price=_dec(row["price"]),
                    price_active=bool(row["price_active"]), catalog_active=bool(row["catalog_active"]),
                    currency=(row["currency"] or "").upper(), catalog_name=str(row["catalog_name"] or ""),
                    write_date=str(row["write_date"] or "")))
        for chunk in _chunks(template_ids):
            for row in connection.execute(TAXES_SQL, {"template_ids": chunk}).mappings():
                taxes.setdefault(row["template_id"], []).append(dict(row))
            for row in connection.execute(MAPS_SQL, {"pricelist_id": self.pricelist_id,
                                                    "template_ids": chunk}).mappings():
                maps.setdefault(row["template_id"], []).append(MapRule(
                    map_id=row["map_id"], map_name=str(row["map_name"] or ""),
                    has_rule=row["item_id"] is not None, discount=_dec(row["price_discount"]),
                    surcharge=_dec(row["price_surcharge"]), compute_price=row["compute_price"] or "",
                    base=row["base"] or "", rounding=_dec(row["price_round"]),
                    min_quantity=_dec(row["min_quantity"]),
                    has_dates=bool(row["date_start"] or row["date_end"])))
        for pid_chunk in _chunks(product_ids):
            for tid_chunk in _chunks(template_ids):
                for row in connection.execute(PRODUCT_RULES_SQL, {
                        "pricelist_id": self.pricelist_id, "product_ids": pid_chunk,
                        "template_ids": tid_chunk}).mappings():
                    rules.add((row["product_id"], row["product_tmpl_id"]))
        result = []
        for product_id, row in base.items():
            result.append(OdooProduct(
                ean=row["ean"], product_id=product_id, template_id=row["template_id"],
                variant_active=bool(row["variant_active"]), template_active=bool(row["template_active"]),
                sale_ok=bool(row["sale_ok"]), list_price=_dec(row["list_price"]),
                prices=tuple(prices.get(product_id, ())), taxes=tuple(taxes.get(row["template_id"], ())),
                maps=tuple(maps.get(row["template_id"], ())),
                product_rules=sum(1 for p, t in rules if p == product_id or t == row["template_id"])))
        return result


def _rounded(value: Decimal, step: Decimal | None) -> Decimal:
    if not step or step <= 0:
        return value.quantize(Decimal("0.01"), ROUND_HALF_UP)
    return (value / step).quantize(Decimal(1), ROUND_HALF_UP) * step


def _tax_treatment(taxes: tuple[dict, ...]) -> tuple[bool | None, Decimal | None, str]:
    """(price_includes_tax, rate, problem). The rate is informative; it is never applied."""
    if len(taxes) != 1:
        return None, None, "se esperaba exactamente un IVA de venta, hay %d" % len(taxes)
    tax = taxes[0]
    if tax["amount_type"] != "percent":
        return None, None, "el impuesto de venta no es porcentual"
    return bool(tax["price_include"]), Decimal(str(tax["amount"])), ""


def _map_expected(product: OdooProduct) -> tuple[Decimal | None, str, str]:
    """(expected price, problem code, detail) from the pricelist map rules."""
    if not product.maps:
        return None, "no_map", "el producto no tiene mapa de tarifa"
    if product.list_price is None:
        return None, "exceptional_rule", "sin precio de lista"
    expectations: dict[Decimal, str] = {}
    exceptional: list[str] = []
    missing = 0
    for rule in product.maps:
        if not rule.has_rule:
            missing += 1
            continue
        discount = rule.discount or Decimal(0)
        named = re.search(r"\((\d+(?:[.,]\d+)?)\s*%\)", rule.map_name)
        if rule.compute_price != "formula" or rule.base != "list_price":
            exceptional.append(f"{rule.map_name}: cálculo {rule.compute_price}/{rule.base}")
            continue
        if rule.surcharge or rule.min_quantity not in (None, 0, 1) or rule.has_dates:
            exceptional.append(f"{rule.map_name}: recargo, cantidad mínima o fechas")
        if discount < 0:
            exceptional.append(f"{rule.map_name}: descuento negativo {discount}%")
        if named and Decimal(named.group(1).replace(",", ".")) != discount:
            exceptional.append(f"{rule.map_name}: la tarifa aplica {discount}%")
        value = product.list_price * (1 - discount / 100) + (rule.surcharge or 0)
        expectations[_rounded(value, rule.rounding)] = rule.map_name
    if not expectations:
        return None, "no_map", "los mapas del producto no tienen regla en la tarifa 59" if missing else "sin reglas"
    if len(expectations) > 1:
        return None, "multiple_maps", "mapas con resultados distintos: " + ", ".join(sorted(expectations.values()))
    expected = next(iter(expectations))
    if exceptional or product.product_rules:
        extra = list(exceptional)
        if product.product_rules:
            extra.append("reglas específicas por producto/plantilla")
        return expected, "exceptional_rule", "; ".join(extra)
    return expected, "", ""


def validate_product(product: OdooProduct, row: PriceRow) -> Validation:
    """Cross-check the source price. Never changes it; statuses other than VALIDATED are not comparable."""
    included, rate, tax_problem = _tax_treatment(product.taxes)
    expected, problem, detail = _map_expected(product)
    found = row.price
    if row.currency != "EUR":
        return Validation("currency_issue", f"moneda {row.currency or 'desconocida'}", expected, False)
    if not (product.variant_active and product.template_active and product.sale_ok):
        return Validation("discontinued", "variante/plantilla inactiva o no vendible", expected, False)
    if tax_problem:
        return Validation("tax_issue", tax_problem, expected, False)
    if not included:
        return Validation("tax_issue", "el impuesto de venta no está incluido en el precio", expected, False)
    if problem in ("no_map", "multiple_maps"):
        return Validation(problem, detail, expected, False)
    if expected is not None and abs(expected - found) > PRICE_TOLERANCE:
        return Validation("price_mismatch", f"tarifa {found} frente a {expected} recalculado ({detail or 'fórmula'})"
                          .strip(), expected, False)
    if problem == "exceptional_rule":
        return Validation("exceptional_rule", detail, expected, False)
    return Validation(VALIDATED, "", expected, True)


def _fingerprint(*values: Any) -> str:
    return hashlib.sha256(repr(values).encode("utf-8")).hexdigest()


class VytriaPriceSync:
    def __init__(self, store: PriceComparatorStore, reader: PriceReader | None = None, *,
                 sleep: Callable[[float], None] = time.sleep, retry_delay: float = 2.0) -> None:
        self.store = store
        self.reader = reader or SqlOdooPriceReader()
        self._sleep = sleep
        self._retry_delay = retry_delay
        self._lock_path = str(store.database_path) + ".vytria-sync.lock"

    # settings ---------------------------------------------------------------------------------
    def get_setting(self, key: str, default: str = "") -> str:
        with self.store._connect() as connection:
            row = connection.execute("SELECT value FROM vytria_sync_settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.store._connect() as connection, connection:
            connection.execute("""INSERT INTO vytria_sync_settings(key,value) VALUES(?,?)
                                  ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (key, value))

    @property
    def auto_enabled(self) -> bool:
        return self.get_setting("auto_enabled", "0") == "1"

    def set_auto_enabled(self, enabled: bool) -> None:
        self.set_setting("auto_enabled", "1" if enabled else "0")

    # execution --------------------------------------------------------------------------------
    def run_scheduled(self, now: datetime | None = None) -> SyncResult | None:
        """Daily entry point: does nothing while disabled or when a recent sync succeeded."""
        if not self.auto_enabled:
            return None
        now = now or datetime.now(timezone.utc)
        last = self.last_successful_run()
        if last and now - datetime.fromisoformat(last["finished_at"]) < SCHEDULE_INTERVAL:
            return None
        return self.run(trigger="scheduled")

    def run(self, trigger: str = "manual") -> SyncResult:
        lock = FileLock(self._lock_path, timeout=0)
        try:
            lock.acquire()
        except Timeout as exc:
            raise SyncInProgress("Ya hay una sincronización de precios en curso") from exc
        try:
            return self._run_locked(trigger)
        finally:
            lock.release()

    def is_running(self) -> bool:
        lock = FileLock(self._lock_path, timeout=0)
        try:
            lock.acquire()
        except Timeout:
            return True
        lock.release()
        return False

    def _run_locked(self, trigger: str) -> SyncResult:
        with self.store._connect() as connection, connection:
            # Holding the lock means any run still marked as running died without closing.
            connection.execute("""UPDATE vytria_sync_runs SET status='error',finished_at=?,
                error='Interrumpida antes de terminar' WHERE status='running'""", (_now(),))
            run_id = connection.execute(
                "INSERT INTO vytria_sync_runs(trigger,started_at,status) VALUES(?,?,'running')",
                (trigger, _now())).lastrowid
            eans = [row["ean"] for row in connection.execute("SELECT ean FROM products ORDER BY ean")]
        result = SyncResult(run_id, "running", monitored=len(eans))
        try:
            products = self._fetch_with_retries(eans) if eans else []
            self._apply(result, eans, products)
            result.status = "ok" if not result.issues else "partial"
        except OdooReadError as exc:
            result.status, result.error = "error", str(exc)
        except Exception as exc:  # noqa: BLE001 - the run must always be closed, details stay out of the log
            result.status, result.error = "error", f"Error interno: {type(exc).__name__}"
        self._finish(result)
        return result

    def _fetch_with_retries(self, eans: list[str]) -> list[OdooProduct]:
        last: Exception | None = None
        for attempt in range(FETCH_ATTEMPTS):
            try:
                return self.reader.fetch(eans)
            except (OperationalError, InterfaceError, OSError) as exc:
                last = exc
                if attempt + 1 < FETCH_ATTEMPTS:
                    self._sleep(self._retry_delay * (2 ** attempt))
        raise OdooReadError(
            f"No se pudo leer Odoo tras {FETCH_ATTEMPTS} intentos ({type(last).__name__}). "
            "Se conservan los últimos precios confirmados.")

    def _apply(self, result: SyncResult, eans: list[str], products: list[OdooProduct]) -> None:
        by_ean: dict[str, list[OdooProduct]] = {}
        for product in products:
            by_ean.setdefault(product.ean, []).append(product)
        issues: list[tuple[str, str, str]] = []
        now = _now()
        with self.store._connect() as connection, connection:
            for ean in eans:
                chosen, issue = self._choose(by_ean.get(ean, []))
                if issue:
                    issues.append((ean, *issue))
                    # The last confirmed price and its history stay, but it is flagged as not re-verified.
                    connection.execute("""UPDATE vytria_prices SET stale_reason=?,
                        stale_since=COALESCE(stale_since,?) WHERE ean=?""", (issue[0], now, ean))
                    continue
                product, row = chosen
                validation = validate_product(product, row)
                included, rate, _ = _tax_treatment(product.taxes)
                self._upsert(connection, result, ean, product, row, validation, included, rate, now)
            for ean, code, detail in issues:
                connection.execute("INSERT INTO vytria_sync_issues(run_id,ean,code,detail) VALUES(?,?,?,?)",
                                   (result.run_id, ean, code, detail))
        result.issues = len(issues)

    @staticmethod
    def _choose(candidates: list[OdooProduct]):
        """Pick the single product/price row for an EAN, or return an issue without touching data."""
        if not candidates:
            return None, ("not_in_odoo", "")
        active = [p for p in candidates if p.variant_active and p.template_active]
        pool = active or candidates
        if len({p.product_id for p in pool}) > 1:
            return None, ("ambiguous_product", "productos " + ", ".join(str(p.product_id) for p in pool))
        product = pool[0]
        rows = [r for r in product.prices if r.price_active and r.catalog_active
                and r.price is not None and r.price > 0]
        if not rows:
            return None, ("no_price", f"producto {product.product_id}")
        foreign = [r for r in rows if _ascii_upper(r.catalog_name) != EXPECTED_CATALOG_NAME]
        if foreign:
            return None, ("unexpected_catalog", foreign[0].catalog_name)
        if len({(r.price, r.currency) for r in rows}) > 1:
            return None, ("multiple_prices", ", ".join(str(r.price) for r in rows))
        return (product, rows[0]), None

    def _upsert(self, connection, result, ean, product, row, validation, included, rate, now) -> None:
        fingerprint = _fingerprint(str(row.price), row.currency, included, str(rate), validation.status,
                                   validation.detail, row.price_id, product.product_id)
        current = connection.execute("SELECT fingerprint, first_seen_at, changed_at FROM vytria_prices WHERE ean=?",
                                     (ean,)).fetchone()
        values = (MARKET, float(row.price), row.currency, int(bool(included)),
                  float(rate) if rate is not None else None, SOURCE, VYTRIA_PRICELIST_ID,
                  product.product_id, product.template_id, row.catalog_id, row.price_id, row.write_date,
                  float(validation.expected) if validation.expected is not None else None,
                  validation.status, validation.detail, int(validation.comparable), fingerprint)
        if current and current["fingerprint"] == fingerprint:
            connection.execute("""UPDATE vytria_prices SET synced_at=?,stale_since=NULL,stale_reason=''
                                  WHERE ean=?""", (now, ean))
            result.unchanged += 1
            return
        connection.execute("""
            INSERT INTO vytria_prices(market,price,currency,price_includes_tax,vat_rate,source,pricelist_id,
                source_product_id,source_template_id,source_catalog_id,source_price_id,source_updated_at,
                expected_price,validation_status,validation_detail,comparable,fingerprint,
                ean,first_seen_at,changed_at,synced_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(ean) DO UPDATE SET market=excluded.market,price=excluded.price,
                currency=excluded.currency,price_includes_tax=excluded.price_includes_tax,
                vat_rate=excluded.vat_rate,source=excluded.source,pricelist_id=excluded.pricelist_id,
                source_product_id=excluded.source_product_id,source_template_id=excluded.source_template_id,
                source_catalog_id=excluded.source_catalog_id,source_price_id=excluded.source_price_id,
                source_updated_at=excluded.source_updated_at,expected_price=excluded.expected_price,
                validation_status=excluded.validation_status,validation_detail=excluded.validation_detail,
                comparable=excluded.comparable,fingerprint=excluded.fingerprint,
                changed_at=excluded.changed_at,synced_at=excluded.synced_at,
                stale_since=NULL,stale_reason=''
        """, (*values, ean, current["first_seen_at"] if current else now, now, now))
        connection.execute("""
            INSERT INTO vytria_price_history(ean,market,price,currency,price_includes_tax,vat_rate,source,
                pricelist_id,source_product_id,source_price_id,validation_status,recorded_at,sync_run_id)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (ean, MARKET, float(row.price), row.currency, int(bool(included)),
              float(rate) if rate is not None else None, SOURCE, VYTRIA_PRICELIST_ID,
              product.product_id, row.price_id, validation.status, now, result.run_id))
        if current:
            result.changed += 1
        else:
            result.inserted += 1

    def _finish(self, result: SyncResult) -> None:
        with self.store._connect() as connection, connection:
            connection.execute("""UPDATE vytria_sync_runs SET finished_at=?,status=?,monitored=?,inserted=?,
                changed=?,unchanged=?,issues=?,error=? WHERE id=?""",
                               (_now(), result.status, result.monitored, result.inserted, result.changed,
                                result.unchanged, result.issues, result.error, result.run_id))

    # queries ----------------------------------------------------------------------------------
    def last_run(self) -> dict | None:
        with self.store._connect() as connection:
            row = connection.execute("SELECT * FROM vytria_sync_runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def last_successful_run(self) -> dict | None:
        with self.store._connect() as connection:
            row = connection.execute("""SELECT * FROM vytria_sync_runs WHERE status IN ('ok','partial')
                                        ORDER BY id DESC LIMIT 1""").fetchone()
        return dict(row) if row else None

    def last_issues(self) -> list[dict]:
        with self.store._connect() as connection:
            rows = connection.execute("""SELECT i.ean,i.code,i.detail FROM vytria_sync_issues i
                WHERE i.run_id=(SELECT MAX(id) FROM vytria_sync_runs WHERE status<>'running')
                ORDER BY i.ean""").fetchall()
        return [dict(row) for row in rows]

    def prices(self):
        import pandas as pd

        with self.store._connect() as connection:
            frame = pd.read_sql_query("""
                SELECT p.ean,p.brand,p.commercial_name,v.price,v.currency,v.price_includes_tax,v.vat_rate,
                       v.source,v.validation_status,v.validation_detail,v.comparable,v.expected_price,
                       v.source_updated_at,v.synced_at,v.stale_since,v.stale_reason
                FROM products p LEFT JOIN vytria_prices v ON v.ean=p.ean ORDER BY p.brand,p.commercial_name
            """, connection)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=SYNCED_PRICE_MAX_AGE_DAYS)).isoformat()
        stale = frame["stale_since"].notna()
        expired = frame["synced_at"].notna() & (frame["synced_at"] < cutoff)
        frame["current_status"] = frame["validation_status"].where(~expired, "expired").where(~stale, "stale")
        # Same rule as the comparator dashboard: only fresh, validated prices are commercially usable.
        frame["usable"] = (frame["validation_status"] == VALIDATED) & (frame["comparable"] == 1) & ~stale & ~expired
        return frame

    def price_history(self, ean: str):
        import pandas as pd

        with self.store._connect() as connection:
            return pd.read_sql_query("""SELECT * FROM vytria_price_history WHERE ean=?
                                        ORDER BY recorded_at DESC,id DESC""", connection,
                                     params=(normalize_gtin(ean),))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sincroniza el PVP Vytria España desde Odoo (solo lectura).")
    parser.add_argument("--scheduled", action="store_true",
                        help="ejecuta sólo si la sincronización diaria está activada y toca")
    args = parser.parse_args(argv)
    sync = VytriaPriceSync(PriceComparatorStore())
    try:
        result = sync.run_scheduled() if args.scheduled else sync.run(trigger="cli")
    except SyncInProgress as exc:
        print(exc)
        return 0
    if result is None:
        print("Sincronización automática desactivada o ya realizada hoy; no se ejecuta.")
        return 0
    print(f"Sincronización {result.status}: {result.inserted} nuevos, {result.changed} cambiados, "
          f"{result.unchanged} sin cambios, {result.issues} incidencias {result.error}".strip())
    return 0 if result.status != "error" else 1


if __name__ == "__main__":
    sys.exit(main())
