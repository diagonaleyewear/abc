from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
from io import BytesIO
import math
import os
from pathlib import Path
import re
import sqlite3
from typing import Any
from urllib.parse import urlparse

import pandas as pd
from filelock import FileLock, Timeout


GTIN_ERROR = "EAN/UPC debe ser GTIN-12 o GTIN-13 válido"
MATCH_STATES = {"exact_match", "probable_match", "manual_review", "not_found", "error"}
AVAILABILITY_STATES = {"available", "unavailable", "unknown"}
DEFAULT_DB_PATH = Path(__file__).parent / "masterdata_data" / "price_comparator.sqlite3"
CLOUD_DATA_DIR = Path("/app/data")
CLOUD_DB_PATH = CLOUD_DATA_DIR / "price_comparator.sqlite3"


class PriceComparatorStorageError(RuntimeError):
    """The comparator database location is unavailable or not writable."""


def resolve_database_path() -> Path:
    """Explicit variable first; then the Cloud persistent volume; then local development.

    The production controller only swaps the image in a separately approved Compose
    snapshot, so the repository Compose variable cannot be relied upon there.
    """
    configured = os.getenv("PRICE_COMPARATOR_DB_PATH")
    if configured:
        return Path(configured)
    if CLOUD_DATA_DIR.is_dir():
        return CLOUD_DB_PATH
    return DEFAULT_DB_PATH


def normalize_gtin(value: Any) -> str:
    if value is None or pd.isna(value):
        raise ValueError(GTIN_ERROR)
    original = str(value).strip()
    if not original.isdigit() or len(original) not in (12, 13):
        raise ValueError(GTIN_ERROR)
    body = original[:-1]
    check = (10 - sum(int(digit) * (3 if (len(body) - i) % 2 else 1)
                      for i, digit in enumerate(body)) % 10) % 10
    if check != int(original[-1]):
        raise ValueError(GTIN_ERROR)
    return original.zfill(13)


def _text(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def _number(value: Any, label: str, *, optional: bool = False) -> float | None:
    text = _text(value).replace(",", ".")
    if not text:
        if optional:
            return None
        raise ValueError(f"{label} es obligatorio")
    try:
        result = float(text)
    except ValueError as exc:
        raise ValueError(f"{label} no es numérico") from exc
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{label} debe ser un número finito no negativo")
    return result


def _date(value: Any) -> str:
    text = _text(value)
    if not text:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
    parsed = pd.to_datetime(text, utc=True, errors="coerce")
    if pd.isna(parsed):
        raise ValueError("Fecha de consulta no válida")
    return parsed.isoformat()


def _frame(source: Any, sheet_name: str) -> pd.DataFrame:
    try:
        return pd.read_excel(source, sheet_name=sheet_name, dtype=str).fillna("")
    except ValueError as exc:
        raise ValueError(f"El Excel debe incluir la hoja «{sheet_name}»") from exc


def _required_columns(frame: pd.DataFrame, required: set[str], sheet: str) -> None:
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"Faltan columnas en «{sheet}»: {', '.join(missing)}")


@dataclass(frozen=True)
class ImportReport:
    imported: int
    updated: int
    rejected: tuple[str, ...]


class PriceComparatorStore:
    """Persistent, isolated data store for manual price-comparison imports."""

    def __init__(self, database_path: str | Path | None = None) -> None:
        self.database_path = Path(database_path or resolve_database_path()).resolve()
        try:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            if not os.access(self.database_path.parent, os.W_OK | os.X_OK):
                raise PermissionError(f"Sin permiso de escritura en {self.database_path.parent}")
            self._initialize_schema()
        except (OSError, sqlite3.Error, Timeout) as exc:
            raise PriceComparatorStorageError(
                f"No se puede abrir la base del comparador en {self.database_path}: "
                f"{type(exc).__name__}. Verifique que PRICE_COMPARATOR_DB_PATH apunta a un "
                "directorio persistente con escritura para el usuario de la aplicación."
            ) from exc

    def _initialize_schema(self) -> None:
        with FileLock(str(self.database_path) + ".schema.lock", timeout=10):
            with self._connect() as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.executescript("""
                    CREATE TABLE IF NOT EXISTS products (
                        ean TEXT PRIMARY KEY,
                        original_ean TEXT NOT NULL,
                        brand TEXT NOT NULL DEFAULT '',
                        commercial_name TEXT NOT NULL DEFAULT '',
                        model TEXT NOT NULL DEFAULT '',
                        color TEXT NOT NULL DEFAULT '',
                        category TEXT NOT NULL DEFAULT '',
                        internal_reference TEXT NOT NULL DEFAULT '',
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS competitors (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        url TEXT NOT NULL,
                        domain TEXT NOT NULL,
                        market TEXT NOT NULL DEFAULT '',
                        currency TEXT NOT NULL DEFAULT '',
                        active INTEGER NOT NULL DEFAULT 1,
                        query_method TEXT NOT NULL DEFAULT 'manual',
                        last_checked_at TEXT,
                        connectivity TEXT NOT NULL DEFAULT 'not_checked'
                    );
                    CREATE TABLE IF NOT EXISTS own_prices (
                        id INTEGER PRIMARY KEY,
                        ean TEXT NOT NULL REFERENCES products(ean),
                        market TEXT NOT NULL,
                        price REAL NOT NULL,
                        promotional_price REAL,
                        currency TEXT NOT NULL,
                        fx_to_eur REAL,
                        captured_at TEXT NOT NULL,
                        source TEXT NOT NULL,
                        fingerprint TEXT NOT NULL UNIQUE
                    );
                    CREATE TABLE IF NOT EXISTS offers (
                        id INTEGER PRIMARY KEY,
                        ean TEXT NOT NULL REFERENCES products(ean),
                        competitor_id TEXT NOT NULL REFERENCES competitors(id),
                        market TEXT NOT NULL,
                        price REAL NOT NULL,
                        promotional_price REAL,
                        previous_price REAL,
                        shipping_cost REAL,
                        currency TEXT NOT NULL,
                        fx_to_eur REAL,
                        availability TEXT NOT NULL,
                        url TEXT NOT NULL,
                        captured_at TEXT NOT NULL,
                        method TEXT NOT NULL,
                        match_status TEXT NOT NULL,
                        evidence TEXT NOT NULL,
                        fingerprint TEXT NOT NULL UNIQUE
                    );
                    CREATE TABLE IF NOT EXISTS search_results (
                        id INTEGER PRIMARY KEY,
                        ean TEXT NOT NULL REFERENCES products(ean),
                        competitor_id TEXT NOT NULL REFERENCES competitors(id),
                        market TEXT NOT NULL,
                        url TEXT NOT NULL DEFAULT '',
                        captured_at TEXT NOT NULL,
                        method TEXT NOT NULL,
                        match_status TEXT NOT NULL,
                        evidence TEXT NOT NULL,
                        fingerprint TEXT NOT NULL UNIQUE
                    );
                    CREATE TABLE IF NOT EXISTS vytria_prices (
                        ean TEXT PRIMARY KEY REFERENCES products(ean),
                        market TEXT NOT NULL,
                        price REAL NOT NULL,
                        currency TEXT NOT NULL,
                        price_includes_tax INTEGER NOT NULL,
                        vat_rate REAL,
                        source TEXT NOT NULL,
                        pricelist_id INTEGER NOT NULL,
                        source_product_id INTEGER,
                        source_template_id INTEGER,
                        source_catalog_id INTEGER,
                        source_price_id INTEGER,
                        source_updated_at TEXT,
                        expected_price REAL,
                        validation_status TEXT NOT NULL,
                        validation_detail TEXT NOT NULL DEFAULT '',
                        comparable INTEGER NOT NULL,
                        fingerprint TEXT NOT NULL,
                        first_seen_at TEXT NOT NULL,
                        changed_at TEXT NOT NULL,
                        synced_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS vytria_price_history (
                        id INTEGER PRIMARY KEY,
                        ean TEXT NOT NULL REFERENCES products(ean),
                        market TEXT NOT NULL,
                        price REAL NOT NULL,
                        currency TEXT NOT NULL,
                        price_includes_tax INTEGER NOT NULL,
                        vat_rate REAL,
                        source TEXT NOT NULL,
                        pricelist_id INTEGER NOT NULL,
                        source_product_id INTEGER,
                        source_price_id INTEGER,
                        validation_status TEXT NOT NULL,
                        recorded_at TEXT NOT NULL,
                        sync_run_id INTEGER
                    );
                    CREATE TABLE IF NOT EXISTS vytria_sync_runs (
                        id INTEGER PRIMARY KEY,
                        trigger TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        finished_at TEXT,
                        status TEXT NOT NULL,
                        monitored INTEGER NOT NULL DEFAULT 0,
                        inserted INTEGER NOT NULL DEFAULT 0,
                        changed INTEGER NOT NULL DEFAULT 0,
                        unchanged INTEGER NOT NULL DEFAULT 0,
                        issues INTEGER NOT NULL DEFAULT 0,
                        error TEXT NOT NULL DEFAULT ''
                    );
                    CREATE TABLE IF NOT EXISTS vytria_sync_issues (
                        id INTEGER PRIMARY KEY,
                        run_id INTEGER NOT NULL REFERENCES vytria_sync_runs(id),
                        ean TEXT NOT NULL,
                        code TEXT NOT NULL,
                        detail TEXT NOT NULL DEFAULT ''
                    );
                    CREATE TABLE IF NOT EXISTS vytria_sync_settings (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS vytria_history_ean_idx ON vytria_price_history(ean, recorded_at DESC);
                    CREATE INDEX IF NOT EXISTS offers_ean_date_idx ON offers(ean, captured_at DESC);
                    CREATE INDEX IF NOT EXISTS offers_competitor_date_idx ON offers(competitor_id, captured_at DESC);
                    CREATE INDEX IF NOT EXISTS search_results_ean_date_idx ON search_results(ean, captured_at DESC);
                """)

    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA synchronous=FULL")
        return closing(connection)

    def import_workbook(self, source: Any) -> dict[str, ImportReport]:
        if isinstance(source, bytes):
            source = BytesIO(source)
        products = _frame(source, "100 gafas")
        competitors = _frame(source, "Competidores")
        _required_columns(products, {"MARCA", "NOMBRE", "MODELO", "COLOR", "CATEGORIA", "EAN"}, "100 gafas")
        _required_columns(competitors, {"Competidor", "Página web"}, "Competidores")
        return {
            "products": self.import_products(products),
            "competitors": self.import_competitors(competitors),
        }

    def import_products(self, frame: pd.DataFrame) -> ImportReport:
        _required_columns(frame, {"EAN"}, "productos")
        imported = updated = 0
        rejected: list[str] = []
        seen: set[str] = set()
        with self._connect() as connection, connection:
            for index, row in frame.iterrows():
                try:
                    original = _text(row.get("EAN"))
                    ean = normalize_gtin(original)
                    if ean in seen:
                        raise ValueError("EAN duplicado en el fichero")
                    seen.add(ean)
                    old = connection.execute("SELECT 1 FROM products WHERE ean=?", (ean,)).fetchone()
                    values = {
                        "brand": _text(row.get("MARCA")),
                        "commercial_name": _text(row.get("NOMBRE")),
                        "model": _text(row.get("MODELO")),
                        "color": _text(row.get("COLOR")),
                        "category": _text(row.get("CATEGORIA")),
                        "internal_reference": _text(row.get("REFERENCIA INTERNA")),
                    }
                    connection.execute("""
                        INSERT INTO products(ean,original_ean,brand,commercial_name,model,color,category,internal_reference,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(ean) DO UPDATE SET
                            original_ean=excluded.original_ean,
                            brand=CASE WHEN excluded.brand='' THEN products.brand ELSE excluded.brand END,
                            commercial_name=CASE WHEN excluded.commercial_name='' THEN products.commercial_name ELSE excluded.commercial_name END,
                            model=CASE WHEN excluded.model='' THEN products.model ELSE excluded.model END,
                            color=CASE WHEN excluded.color='' THEN products.color ELSE excluded.color END,
                            category=CASE WHEN excluded.category='' THEN products.category ELSE excluded.category END,
                            internal_reference=CASE WHEN excluded.internal_reference='' THEN products.internal_reference ELSE excluded.internal_reference END,
                            updated_at=excluded.updated_at
                    """, (ean, original, *values.values(), datetime.now(timezone.utc).isoformat(timespec="seconds")))
                    if old:
                        updated += 1
                    else:
                        imported += 1
                except ValueError as exc:
                    rejected.append(f"Fila {index + 2}: {exc}")
        return ImportReport(imported, updated, tuple(rejected))

    def import_competitors(self, frame: pd.DataFrame) -> ImportReport:
        _required_columns(frame, {"Competidor", "Página web"}, "competidores")
        imported = updated = 0
        rejected: list[str] = []
        with self._connect() as connection, connection:
            for index, row in frame.iterrows():
                try:
                    name, url = _text(row["Competidor"]), _text(row["Página web"])
                    parsed = urlparse(url)
                    if not name or parsed.scheme != "https" or not parsed.hostname:
                        raise ValueError("Nombre o URL HTTPS no válido")
                    domain = parsed.hostname.lower().removeprefix("www.")
                    competitor_id = re.sub(r"[^a-z0-9]+", "-", domain).strip("-")
                    old = connection.execute("SELECT 1 FROM competitors WHERE id=?", (competitor_id,)).fetchone()
                    connection.execute("""
                        INSERT INTO competitors(id,name,url,domain) VALUES(?,?,?,?)
                        ON CONFLICT(id) DO UPDATE SET name=excluded.name,url=excluded.url,domain=excluded.domain
                    """, (competitor_id, name, url, domain))
                    if old:
                        updated += 1
                    else:
                        imported += 1
                except ValueError as exc:
                    rejected.append(f"Fila {index + 2}: {exc}")
        return ImportReport(imported, updated, tuple(rejected))

    def save_competitors(self, frame: pd.DataFrame) -> None:
        _required_columns(frame, {"id", "market", "currency", "active", "query_method"}, "competidores")
        with self._connect() as connection, connection:
            for _, row in frame.iterrows():
                currency = _text(row["currency"]).upper()
                if currency and not re.fullmatch(r"[A-Z]{3}", currency):
                    raise ValueError(f"Moneda ISO no válida para {_text(row['id'])}")
                if _text(row["query_method"]) != "manual":
                    raise ValueError("El método de consulta permanece manual hasta validar el dominio")
                connection.execute("""
                    UPDATE competitors SET market=?,currency=?,active=?,query_method=? WHERE id=?
                """, (_text(row["market"]), currency, int(bool(row["active"])),
                      _text(row["query_method"]) or "manual", _text(row["id"])))

    def import_own_prices(self, frame: pd.DataFrame) -> ImportReport:
        _required_columns(frame, {"EAN", "Precio", "Moneda", "Mercado"}, "Precios Vytria")
        imported = rejected = 0
        errors: list[str] = []
        with self._connect() as connection, connection:
            for index, row in frame.iterrows():
                try:
                    ean = normalize_gtin(row["EAN"])
                    if not connection.execute("SELECT 1 FROM products WHERE ean=?", (ean,)).fetchone():
                        raise ValueError("EAN no importado en el catálogo")
                    currency = _text(row["Moneda"]).upper()
                    market = _text(row["Mercado"])
                    source = _text(row.get("Origen")) or "Excel"
                    if not re.fullmatch(r"[A-Z]{3}", currency) or not market:
                        raise ValueError("Mercado y moneda ISO son obligatorios")
                    price = _number(row["Precio"], "Precio")
                    promo = _number(row.get("Precio promocional"), "Precio promocional", optional=True)
                    if promo is not None and promo > price:
                        raise ValueError("El precio promocional no puede superar el precio habitual")
                    fx = _number(row.get("Tipo cambio EUR"), "Tipo cambio EUR", optional=True)
                    if currency == "EUR":
                        fx = 1.0
                    elif fx is not None and fx <= 0:
                        raise ValueError("Tipo cambio EUR debe ser mayor que cero")
                    captured = _date(row.get("Fecha consulta"))
                    fingerprint = _fingerprint((ean, market, price, promo, currency, fx, captured, source))
                    cursor = connection.execute("""
                        INSERT OR IGNORE INTO own_prices(ean,market,price,promotional_price,currency,fx_to_eur,captured_at,source,fingerprint)
                        VALUES(?,?,?,?,?,?,?,?,?)
                    """, (ean, market, price, promo, currency, fx, captured, source, fingerprint))
                    imported += cursor.rowcount
                except ValueError as exc:
                    rejected += 1
                    errors.append(f"Fila {index + 2}: {exc}")
        return ImportReport(imported, 0, tuple(errors))

    def import_offers(self, frame: pd.DataFrame) -> ImportReport:
        required = {"EAN", "Competidor", "Precio", "Moneda", "Mercado", "URL", "Disponibilidad",
                    "Estado coincidencia", "Evidencia", "Método"}
        _required_columns(frame, required, "Ofertas")
        imported = rejected = 0
        errors: list[str] = []
        with self._connect() as connection, connection:
            competitors = {row["name"].casefold(): row for row in connection.execute("SELECT * FROM competitors")}
            competitors.update({row["id"].casefold(): row for row in connection.execute("SELECT * FROM competitors")})
            for index, row in frame.iterrows():
                try:
                    ean = normalize_gtin(row["EAN"])
                    if not connection.execute("SELECT 1 FROM products WHERE ean=?", (ean,)).fetchone():
                        raise ValueError("EAN no importado en el catálogo")
                    competitor = competitors.get(_text(row["Competidor"]).casefold())
                    if not competitor:
                        raise ValueError("Competidor no importado")
                    market = _text(row["Mercado"])
                    if not market:
                        raise ValueError("El mercado es obligatorio")
                    if competitor["market"] and competitor["market"] != market:
                        raise ValueError("El mercado no coincide con la configuración del competidor")
                    match_status = _text(row["Estado coincidencia"]).lower()
                    if match_status not in MATCH_STATES:
                        raise ValueError("Estado de coincidencia no válido")
                    method, evidence = _text(row["Método"]), _text(row["Evidencia"])
                    if not method or not evidence:
                        raise ValueError("Método y evidencia son obligatorios")
                    captured = _date(row.get("Fecha consulta"))
                    url = _text(row["URL"])
                    if url:
                        parsed = urlparse(url)
                        if parsed.scheme != "https" or not parsed.hostname:
                            raise ValueError("URL de oferta HTTPS no válida")
                        offer_domain = parsed.hostname.lower().removeprefix("www.").rstrip(".")
                        if offer_domain != competitor["domain"] and not offer_domain.endswith("." + competitor["domain"]):
                            raise ValueError("El dominio de la oferta no coincide con el competidor")

                    if not _text(row["Precio"]):
                        fingerprint = _fingerprint((ean, competitor["id"], market, url, captured,
                                                    method, match_status, evidence))
                        cursor = connection.execute("""
                            INSERT OR IGNORE INTO search_results(
                                ean,competitor_id,market,url,captured_at,method,match_status,evidence,fingerprint
                            ) VALUES(?,?,?,?,?,?,?,?,?)
                        """, (ean, competitor["id"], market, url, captured, method, match_status,
                              evidence, fingerprint))
                        imported += cursor.rowcount
                        connection.execute(
                            "UPDATE competitors SET last_checked_at=?,connectivity='manual_import' WHERE id=?",
                            (captured, competitor["id"]),
                        )
                        continue
                    if match_status in {"not_found", "error"}:
                        raise ValueError(f"{match_status} no puede tener un precio")
                    if not url:
                        raise ValueError("La oferta con precio requiere URL directa")
                    currency = _text(row["Moneda"]).upper()
                    if not re.fullmatch(r"[A-Z]{3}", currency):
                        raise ValueError("Moneda ISO no válida")
                    if competitor["currency"] and competitor["currency"] != currency:
                        raise ValueError("La moneda no coincide con la configuración del competidor")
                    availability = _text(row["Disponibilidad"]).lower()
                    if availability not in AVAILABILITY_STATES:
                        raise ValueError("Disponibilidad no válida")
                    price = _number(row["Precio"], "Precio")
                    promo = _number(row.get("Precio promocional"), "Precio promocional", optional=True)
                    if promo is not None and promo > price:
                        raise ValueError("El precio promocional no puede superar el precio habitual")
                    previous = _number(row.get("Precio anterior"), "Precio anterior", optional=True)
                    shipping = _number(row.get("Coste envío"), "Coste envío", optional=True)
                    fx = _number(row.get("Tipo cambio EUR"), "Tipo cambio EUR", optional=True)
                    if currency == "EUR":
                        fx = 1.0
                    elif fx is not None and fx <= 0:
                        raise ValueError("Tipo cambio EUR debe ser mayor que cero")
                    fingerprint = _fingerprint((ean, competitor["id"], market, price, promo, previous, shipping,
                                                currency, fx, availability, url, captured, method,
                                                match_status, evidence))
                    cursor = connection.execute("""
                        INSERT OR IGNORE INTO offers(
                            ean,competitor_id,market,price,promotional_price,previous_price,shipping_cost,
                            currency,fx_to_eur,availability,url,captured_at,method,match_status,evidence,fingerprint
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (ean, competitor["id"], market, price, promo, previous, shipping, currency, fx,
                          availability, url, captured, method, match_status, evidence, fingerprint))
                    imported += cursor.rowcount
                    connection.execute(
                        "UPDATE competitors SET last_checked_at=?,connectivity='manual_import' WHERE id=?",
                        (captured, competitor["id"]),
                    )
                except ValueError as exc:
                    rejected += 1
                    errors.append(f"Fila {index + 2}: {exc}")
        return ImportReport(imported, 0, tuple(errors))

    def competitors(self) -> pd.DataFrame:
        with self._connect() as connection:
            return pd.read_sql_query("SELECT * FROM competitors ORDER BY name", connection)

    def products(self) -> pd.DataFrame:
        with self._connect() as connection:
            return pd.read_sql_query("SELECT * FROM products ORDER BY brand,commercial_name", connection)

    def offer_history(self, ean: str) -> pd.DataFrame:
        normalized = normalize_gtin(ean)
        with self._connect() as connection:
            return pd.read_sql_query("""
                SELECT o.*,c.name AS competitor_name FROM offers o
                JOIN competitors c ON c.id=o.competitor_id WHERE o.ean=?
                ORDER BY o.captured_at DESC,o.id DESC
            """, connection, params=(normalized,))

    def search_history(self, ean: str) -> pd.DataFrame:
        normalized = normalize_gtin(ean)
        with self._connect() as connection:
            return pd.read_sql_query("""
                SELECT s.*,c.name AS competitor_name FROM search_results s
                JOIN competitors c ON c.id=s.competitor_id WHERE s.ean=?
                ORDER BY s.captured_at DESC,s.id DESC
            """, connection, params=(normalized,))

    def search_result_summary(self, max_age_days: int = 30) -> dict[str, int]:
        if max_age_days < 1:
            raise ValueError("La antigüedad debe ser al menos un día")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
        with self._connect() as connection:
            rows = connection.execute("""
                SELECT s.match_status,COUNT(DISTINCT s.ean) AS products
                FROM search_results s JOIN competitors c ON c.id=s.competitor_id
                WHERE s.captured_at>=? AND c.active=1 GROUP BY s.match_status
            """, (cutoff,)).fetchall()
        return {row["match_status"]: row["products"] for row in rows}

    def own_price_history(self, ean: str) -> pd.DataFrame:
        normalized = normalize_gtin(ean)
        with self._connect() as connection:
            return pd.read_sql_query("""
                SELECT * FROM own_prices WHERE ean=? ORDER BY captured_at DESC,id DESC
            """, connection, params=(normalized,))

    def current_offers(self, max_age_days: int = 30) -> pd.DataFrame:
        if max_age_days < 1:
            raise ValueError("La antigüedad debe ser al menos un día")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
        with self._connect() as connection:
            return pd.read_sql_query("""
                SELECT o.*,c.name AS competitor_name,p.brand,p.commercial_name
                FROM offers o JOIN competitors c ON c.id=o.competitor_id
                JOIN products p ON p.ean=o.ean
                WHERE o.captured_at>=? AND c.active=1
                ORDER BY o.captured_at DESC,o.id DESC
            """, connection, params=(cutoff,))

    def current_search_results(self, max_age_days: int = 30) -> pd.DataFrame:
        if max_age_days < 1:
            raise ValueError("La antigüedad debe ser al menos un día")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
        with self._connect() as connection:
            return pd.read_sql_query("""
                SELECT s.*,c.name AS competitor_name,p.brand,p.commercial_name
                FROM search_results s JOIN competitors c ON c.id=s.competitor_id
                JOIN products p ON p.ean=s.ean
                WHERE s.captured_at>=? AND c.active=1
                ORDER BY s.captured_at DESC,s.id DESC
            """, connection, params=(cutoff,))

    def dashboard(self, max_age_days: int = 30) -> tuple[pd.DataFrame, dict[str, int]]:
        if max_age_days < 1:
            raise ValueError("La antigüedad debe ser al menos un día")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
        with self._connect() as connection:
            products = pd.read_sql_query("SELECT * FROM products ORDER BY brand,commercial_name", connection)
            offers = pd.read_sql_query("""
                SELECT o.*,c.name AS competitor_name FROM offers o
                JOIN competitors c ON c.id=o.competitor_id
                WHERE o.captured_at>=? AND c.active=1
                ORDER BY o.captured_at DESC,o.id DESC
            """, connection, params=(cutoff,))
            search_results = pd.read_sql_query("""
                SELECT s.* FROM search_results s
                JOIN competitors c ON c.id=s.competitor_id
                WHERE s.captured_at>=? AND c.active=1
            """, connection, params=(cutoff,))
            own = pd.read_sql_query("""
                SELECT p.* FROM own_prices p JOIN (
                    SELECT ean,market,MAX(captured_at) AS captured_at FROM own_prices
                    WHERE captured_at>=? GROUP BY ean,market
                ) latest ON latest.ean=p.ean AND latest.market=p.market AND latest.captured_at=p.captured_at
            """, connection, params=(cutoff,))
            competitor_count = connection.execute("SELECT COUNT(*) FROM competitors WHERE active=1").fetchone()[0]
            synced = pd.read_sql_query("SELECT * FROM vytria_prices", connection)

        # A synchronized Odoo price supersedes the manual contingency import for its EAN. It is
        # used only when confirmed (comparable); otherwise the product stays without own price.
        if not synced.empty:
            own = own[~own["ean"].isin(synced["ean"])]
            confirmed = synced[synced["comparable"] == 1]
            own = pd.concat([own, pd.DataFrame({
                "ean": confirmed["ean"], "market": confirmed["market"], "price": confirmed["price"],
                "promotional_price": None, "currency": confirmed["currency"], "fx_to_eur": 1.0,
                "captured_at": confirmed["synced_at"], "source": confirmed["source"],
            })], ignore_index=True)
        synced_by_ean = {row["ean"]: row for row in synced.to_dict("records")}

        rows: list[dict[str, Any]] = []
        for product in products.to_dict("records"):
            own_rows = own[own["ean"] == product["ean"]]
            own_record = own_rows.sort_values("captured_at").iloc[-1].to_dict() if not own_rows.empty else None
            valid_product_offers = offers[
                (offers["ean"] == product["ean"])
                & (offers["match_status"] == "exact_match")
                & (offers["availability"] == "available")
            ]
            comparable = []
            if own_record:
                matched = offers[
                    (offers["ean"] == product["ean"])
                    & (offers["market"] == own_record["market"])
                    & (offers["match_status"] == "exact_match")
                    & (offers["availability"] == "available")
                ]
                for competitor_id, group in matched.groupby("competitor_id"):
                    offer = group.sort_values("captured_at").iloc[-1].to_dict()
                    rate = offer["fx_to_eur"]
                    if rate is None or pd.isna(rate):
                        continue
                    effective = offer["promotional_price"]
                    if effective is None or pd.isna(effective):
                        effective = offer["price"]
                    effective_eur = float(effective) * float(rate)
                    comparable.append((effective_eur, offer))
            own_eur = None
            minimum = mean = maximum = diff = diff_pct = None
            position = None
            if own_record and own_record["fx_to_eur"] is not None and not pd.isna(own_record["fx_to_eur"]):
                own_price = own_record["promotional_price"]
                if own_price is None or pd.isna(own_price):
                    own_price = own_record["price"]
                own_eur = float(own_price) * float(own_record["fx_to_eur"])
            if comparable:
                prices = [item[0] for item in comparable]
                minimum, mean, maximum = min(prices), sum(prices) / len(prices), max(prices)
                if own_eur is not None:
                    diff = own_eur - minimum
                    diff_pct = (diff / minimum * 100) if minimum else None
                    position = 1 + sum(price < own_eur for price in prices)
            rows.append({
                **product,
                "own_price_eur": own_eur,
                "competitor_min_eur": minimum,
                "competitor_mean_eur": mean,
                "competitor_max_eur": maximum,
                "competitor_count": len(comparable),
                "confirmed_offer_count": int(valid_product_offers["competitor_id"].nunique()),
                "difference_eur": diff,
                "difference_pct": diff_pct,
                "position": position,
                "market": own_record["market"] if own_record else "",
                "own_price_source": (own_record["source"] if own_record else
                                     ("Odoo (sin precio confirmado)" if product["ean"] in synced_by_ean else "")),
                "own_price_status": synced_by_ean.get(product["ean"], {}).get("validation_status", ""),
            })
        result = pd.DataFrame(rows)
        statuses = {
            "products": len(products),
            "confirmed": int((result["confirmed_offer_count"] > 0).sum()) if not result.empty else 0,
            "no_results": int((result["confirmed_offer_count"] == 0).sum()) if not result.empty else 0,
            "pending_review": len(set(offers.loc[
                offers["match_status"].isin(["probable_match", "manual_review"]), "ean"
            ].unique()) | set(search_results.loc[
                search_results["match_status"].isin(["probable_match", "manual_review"]), "ean"
            ].unique())),
            "search_errors": int(search_results.loc[search_results["match_status"] == "error", "ean"].nunique()),
            "competitors": int(competitor_count),
            "vytria_more_expensive": int((result["difference_eur"] > 0).sum()) if not result.empty else 0,
            "vytria_less_expensive": int((result["difference_eur"] < 0).sum()) if not result.empty else 0,
        }
        return result, statuses


def _fingerprint(values: tuple[Any, ...]) -> str:
    return hashlib.sha256(repr(values).encode("utf-8")).hexdigest()
