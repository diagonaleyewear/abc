from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
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
MATCH_STATES = {"exact_match", "probable_match", "manual_review", "not_found", "error", "rejected"}
MATCH_BASES = {"", "ean", "reference"}
DEFAULT_MARKET, DEFAULT_CURRENCY = "ES", "EUR"  # target market of the comparison (Vytria), NOT competitor data
TARGET_MARKET, TARGET_CURRENCY = DEFAULT_MARKET, DEFAULT_CURRENCY
COVERAGE_STATUSES = {"not_checked", "no_authorized_connector", "pending_authorization", "blocked",
                     "manual_verified", "operational"}
# Columns added after the first release; CREATE TABLE IF NOT EXISTS cannot add them to existing databases.
_ADDED_COLUMNS = {
    "pilot_runs": {"operation": "TEXT NOT NULL DEFAULT 'prices'",
                   "selection": "TEXT NOT NULL DEFAULT '[]'",
                   "selection_request": "TEXT NOT NULL DEFAULT '[]'"},
    "competitor_web_config": {
        "sales_market": "TEXT NOT NULL DEFAULT ''",
        "market_verified": "INTEGER NOT NULL DEFAULT 0",
        "market_evidence_url": "TEXT NOT NULL DEFAULT ''",
        "market_note": "TEXT NOT NULL DEFAULT ''",
        "market_verified_at": "TEXT",
        "search_mode": "TEXT NOT NULL DEFAULT 'auto'",
        "search_preferred": "TEXT NOT NULL DEFAULT 'ean'",
        "search_alternatives": "TEXT NOT NULL DEFAULT ''",
        "search_verified": "INTEGER NOT NULL DEFAULT 0",
        "search_evidence": "TEXT NOT NULL DEFAULT ''",
    },
    "competitor_product_urls": {"source": "TEXT NOT NULL DEFAULT 'auto'", "note": "TEXT NOT NULL DEFAULT ''",
                                "submitted_at": "TEXT"},
    "products": {"caliber": "TEXT NOT NULL DEFAULT ''"},
    "pilot_budgets": {"last_http_at": "REAL"},
    "price_pilot": {"last_error": "TEXT NOT NULL DEFAULT ''"},
    "offers": {"ean_published": "TEXT NOT NULL DEFAULT ''", "model_code": "TEXT NOT NULL DEFAULT ''",
               "color_code": "TEXT NOT NULL DEFAULT ''", "caliber": "TEXT NOT NULL DEFAULT ''",
               "match_basis": "TEXT NOT NULL DEFAULT ''", "price_includes_tax": "INTEGER",
               "vat_rate": "REAL"},
    "search_results": {"ean_published": "TEXT NOT NULL DEFAULT ''", "model_code": "TEXT NOT NULL DEFAULT ''",
                       "color_code": "TEXT NOT NULL DEFAULT ''", "caliber": "TEXT NOT NULL DEFAULT ''",
                       "match_basis": "TEXT NOT NULL DEFAULT ''"},
}
AVAILABILITY_STATES = {"available", "unavailable", "unknown"}
DEFAULT_DB_PATH = Path(__file__).parent / "masterdata_data" / "price_comparator.sqlite3"
CLOUD_DATA_DIR = Path("/app/data")
CLOUD_DB_PATH = CLOUD_DATA_DIR / "price_comparator.sqlite3"
# Default validity of a price for commercial comparisons; configurable through comparator_settings.
SYNCED_PRICE_MAX_AGE_DAYS = 7
RETENTION_MONTHS_OPTIONS = (3, 6, 12, 24)
SETTING_DEFAULTS = {"retention_months": "12", "price_expiry_days": str(SYNCED_PRICE_MAX_AGE_DAYS),
                    "alert_threshold_pct": "5"}


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
                        stale_since TEXT,
                        stale_reason TEXT NOT NULL DEFAULT '',
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
                    CREATE TABLE IF NOT EXISTS comparator_settings (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS offer_checks (
                        ean TEXT NOT NULL REFERENCES products(ean),
                        competitor_id TEXT NOT NULL REFERENCES competitors(id),
                        market TEXT NOT NULL,
                        last_offer_id INTEGER REFERENCES offers(id),
                        last_ok_at TEXT NOT NULL,
                        checks INTEGER NOT NULL DEFAULT 1,
                        PRIMARY KEY (ean, competitor_id, market)
                    );
                    CREATE TABLE IF NOT EXISTS competitor_schedule (
                        competitor_id TEXT PRIMARY KEY REFERENCES competitors(id),
                        connector TEXT NOT NULL DEFAULT '',
                        source TEXT NOT NULL DEFAULT '',
                        authorization_note TEXT NOT NULL DEFAULT '',
                        verified_at TEXT,
                        enabled INTEGER NOT NULL DEFAULT 0,
                        paused INTEGER NOT NULL DEFAULT 0,
                        suspended_at TEXT,
                        suspended_reason TEXT NOT NULL DEFAULT '',
                        interval_hours REAL NOT NULL DEFAULT 24,
                        max_requests_per_run INTEGER NOT NULL DEFAULT 20,
                        pause_seconds REAL NOT NULL DEFAULT 2,
                        max_attempts INTEGER NOT NULL DEFAULT 3,
                        next_run_at TEXT,
                        cycle_started_at TEXT,
                        last_run_at TEXT,
                        last_ok_at TEXT
                    );
                    CREATE TABLE IF NOT EXISTS scheduler_runs (
                        id INTEGER PRIMARY KEY,
                        competitor_id TEXT NOT NULL REFERENCES competitors(id),
                        trigger TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        finished_at TEXT,
                        status TEXT NOT NULL,
                        requests INTEGER NOT NULL DEFAULT 0,
                        ok INTEGER NOT NULL DEFAULT 0,
                        changed INTEGER NOT NULL DEFAULT 0,
                        unchanged INTEGER NOT NULL DEFAULT 0,
                        not_listed INTEGER NOT NULL DEFAULT 0,
                        rejected INTEGER NOT NULL DEFAULT 0,
                        errors INTEGER NOT NULL DEFAULT 0,
                        message TEXT NOT NULL DEFAULT ''
                    );
                    CREATE TABLE IF NOT EXISTS scheduler_jobs (
                        id INTEGER PRIMARY KEY,
                        competitor_id TEXT NOT NULL REFERENCES competitors(id),
                        ean TEXT NOT NULL REFERENCES products(ean),
                        cycle_started_at TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending',
                        attempts INTEGER NOT NULL DEFAULT 0,
                        not_before TEXT NOT NULL,
                        lease_until TEXT,
                        outcome TEXT NOT NULL DEFAULT '',
                        detail TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        finished_at TEXT
                    );
                    CREATE TABLE IF NOT EXISTS price_pilot (
                        id INTEGER PRIMARY KEY CHECK(id=1),
                        eans TEXT NOT NULL,
                        competitors TEXT NOT NULL,
                        enabled INTEGER NOT NULL DEFAULT 0,
                        last_error TEXT NOT NULL DEFAULT '',
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS pilot_runs (
                        id INTEGER PRIMARY KEY,
                        started_at TEXT NOT NULL,
                        finished_at TEXT,
                        status TEXT NOT NULL,
                        scope TEXT NOT NULL,
                        competitors TEXT NOT NULL,
                        error TEXT NOT NULL DEFAULT ''
                    );
                    CREATE TABLE IF NOT EXISTS pilot_budgets (
                        run_id INTEGER NOT NULL REFERENCES pilot_runs(id),
                        competitor_id TEXT NOT NULL REFERENCES competitors(id),
                        requests INTEGER NOT NULL DEFAULT 0,
                        blocked INTEGER NOT NULL DEFAULT 0,
                        last_http_at REAL,
                        PRIMARY KEY(run_id,competitor_id)
                    );
                    CREATE TABLE IF NOT EXISTS pilot_jobs (
                        id INTEGER PRIMARY KEY,
                        run_id INTEGER NOT NULL REFERENCES pilot_runs(id),
                        competitor_id TEXT NOT NULL REFERENCES competitors(id),
                        ean TEXT NOT NULL REFERENCES products(ean),
                        status TEXT NOT NULL DEFAULT 'pending',
                        outcome TEXT NOT NULL DEFAULT '',
                        detail TEXT NOT NULL DEFAULT '',
                        observation TEXT NOT NULL DEFAULT '{}',
                        comparable INTEGER NOT NULL DEFAULT 0,
                        priced INTEGER NOT NULL DEFAULT 0,
                        checked_at TEXT,
                        UNIQUE(run_id,competitor_id,ean)
                    );
                    CREATE TABLE IF NOT EXISTS pilot_discovery (
                        competitor_id TEXT NOT NULL REFERENCES competitors(id),
                        ean TEXT NOT NULL REFERENCES products(ean),
                        run_id INTEGER REFERENCES pilot_runs(id),
                        state TEXT NOT NULL DEFAULT 'pending',
                        method TEXT NOT NULL DEFAULT '',
                        detail TEXT NOT NULL DEFAULT '',
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY(competitor_id,ean)
                    );
                    CREATE TABLE IF NOT EXISTS pilot_http_log (
                        id INTEGER PRIMARY KEY,
                        run_id INTEGER NOT NULL REFERENCES pilot_runs(id),
                        competitor_id TEXT NOT NULL REFERENCES competitors(id),
                        ean TEXT NOT NULL REFERENCES products(ean),
                        url TEXT NOT NULL,
                        result TEXT NOT NULL,
                        requested_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS pilot_url_candidates (
                        id INTEGER PRIMARY KEY,
                        competitor_id TEXT NOT NULL REFERENCES competitors(id),
                        ean TEXT NOT NULL REFERENCES products(ean),
                        url TEXT NOT NULL,
                        run_id INTEGER NOT NULL REFERENCES pilot_runs(id),
                        state TEXT NOT NULL DEFAULT 'pending_validation',
                        method TEXT NOT NULL,
                        evidence TEXT NOT NULL,
                        identity TEXT NOT NULL,
                        checked_at TEXT NOT NULL,
                        reviewed_at TEXT,
                        review_note TEXT NOT NULL DEFAULT '',
                        UNIQUE(run_id,competitor_id,ean,url)
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS scheduler_jobs_open_idx
                        ON scheduler_jobs(competitor_id, ean) WHERE status IN ('pending', 'running');
                    CREATE INDEX IF NOT EXISTS scheduler_jobs_due_idx ON scheduler_jobs(competitor_id, status, not_before);
                    CREATE INDEX IF NOT EXISTS vytria_history_ean_idx ON vytria_price_history(ean, recorded_at DESC);
                    CREATE INDEX IF NOT EXISTS offers_ean_date_idx ON offers(ean, captured_at DESC);
                    CREATE INDEX IF NOT EXISTS offers_competitor_date_idx ON offers(competitor_id, captured_at DESC);
                    CREATE INDEX IF NOT EXISTS search_results_ean_date_idx ON search_results(ean, captured_at DESC);
                    CREATE TABLE IF NOT EXISTS competitor_coverage (
                        competitor_id TEXT PRIMARY KEY REFERENCES competitors(id),
                        method TEXT NOT NULL DEFAULT '',
                        integration_status TEXT NOT NULL DEFAULT 'not_checked',
                        restrictions TEXT NOT NULL DEFAULT '',
                        allowed_frequency TEXT NOT NULL DEFAULT '',
                        notes TEXT NOT NULL DEFAULT '',
                        last_checked_at TEXT
                    );
                    CREATE TABLE IF NOT EXISTS competitor_web_config (
                        competitor_id TEXT PRIMARY KEY REFERENCES competitors(id),
                        locate_strategy TEXT NOT NULL DEFAULT 'index,sitemap,site_search',
                        search_url_template TEXT NOT NULL DEFAULT '',
                        price_selector TEXT NOT NULL DEFAULT '',
                        availability_selector TEXT NOT NULL DEFAULT '',
                        structured_type TEXT NOT NULL DEFAULT '',
                        tax_policy TEXT NOT NULL DEFAULT 'unknown',
                        pause_seconds REAL NOT NULL DEFAULT 3,
                        max_requests INTEGER NOT NULL DEFAULT 10,
                        concurrency INTEGER NOT NULL DEFAULT 1,
                        timeout_seconds REAL NOT NULL DEFAULT 15,
                        retries INTEGER NOT NULL DEFAULT 1,
                        access_status TEXT NOT NULL DEFAULT 'unknown',
                        terms_reviewed INTEGER NOT NULL DEFAULT 0,
                        terms_note TEXT NOT NULL DEFAULT '',
                        robots_checked_at TEXT,
                        robots_summary TEXT NOT NULL DEFAULT '',
                        last_query_at TEXT,
                        last_ok_at TEXT,
                        error_count INTEGER NOT NULL DEFAULT 0,
                        last_error TEXT NOT NULL DEFAULT '',
                        blocked_at TEXT
                    );
                    CREATE TABLE IF NOT EXISTS competitor_product_urls (
                        competitor_id TEXT NOT NULL REFERENCES competitors(id),
                        ean TEXT NOT NULL REFERENCES products(ean),
                        url TEXT NOT NULL,
                        model_code TEXT NOT NULL DEFAULT '',
                        color_code TEXT NOT NULL DEFAULT '',
                        caliber TEXT NOT NULL DEFAULT '',
                        ean_published TEXT NOT NULL DEFAULT '',
                        match_status TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'verified',
                        verified_at TEXT NOT NULL,
                        last_price REAL,
                        last_availability TEXT NOT NULL DEFAULT '',
                        last_ok_at TEXT,
                        failures INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (competitor_id, ean, url)
                    );
                """)
                for table, columns in _ADDED_COLUMNS.items():
                    existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
                    for name, definition in columns.items():
                        if name not in existing:
                            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def seed_defaults(self) -> dict[str, int]:
        """Persist the initial configuration. Idempotent: existing values are never overwritten.

        Registers the 14 reference competitors, the general settings and one disabled
        schedule row per competitor with the default limits. Nothing is enabled or queried.
        Competitor market/currency stay empty until verified: ES/EUR is only the comparison target.
        """
        from competitor_requirements import CHECKED_ON, COMPETITORS, METHOD_STATUS, requirement_for

        added = {"competitors": 0, "settings": 0, "schedules": 0, "coverage": 0}
        with FileLock(str(self.database_path) + ".schema.lock", timeout=10):
            with self._connect() as connection, connection:
                for name, url in COMPETITORS:
                    domain = (urlparse(url).hostname or "").lower().removeprefix("www.")
                    competitor_id = re.sub(r"[^a-z0-9]+", "-", domain).strip("-")
                    added["competitors"] += connection.execute(
                        """INSERT OR IGNORE INTO competitors(id,name,url,domain,market,currency)
                           VALUES(?,?,?,?,'','')""",
                        (competitor_id, name, url, domain)).rowcount
                    method, status = METHOD_STATUS[name]
                    added["coverage"] += connection.execute(
                        """INSERT OR IGNORE INTO competitor_coverage(competitor_id,method,integration_status,
                               restrictions,allowed_frequency,notes,last_checked_at) VALUES(?,?,?,?,?,?,?)""",
                        (competitor_id, method, status, requirement_for(name)[0],
                         "Ninguna hasta obtener autorización", "", CHECKED_ON)).rowcount
                for key, value in SETTING_DEFAULTS.items():
                    added["settings"] += connection.execute(
                        "INSERT OR IGNORE INTO comparator_settings(key,value) VALUES(?,?)", (key, value)).rowcount
                added["schedules"] = connection.execute(
                    """INSERT OR IGNORE INTO competitor_schedule(competitor_id)
                       SELECT id FROM competitors""").rowcount
        return added

    def get_setting(self, key: str) -> str:
        if key not in SETTING_DEFAULTS:
            raise ValueError(f"Ajuste desconocido: {key}")
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM comparator_settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else SETTING_DEFAULTS[key]

    def set_setting(self, key: str, value: Any) -> None:
        if key not in SETTING_DEFAULTS:
            raise ValueError(f"Ajuste desconocido: {key}")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("El valor del ajuste debe ser numérico") from exc
        if key == "retention_months" and number not in RETENTION_MONTHS_OPTIONS:
            raise ValueError("La conservación del histórico debe ser de 3, 6, 12 o 24 meses")
        if key == "price_expiry_days" and not (number == int(number) and 1 <= number <= 90):
            raise ValueError("La caducidad debe ser un número entero de días entre 1 y 90")
        if key == "alert_threshold_pct" and not (0.5 <= number <= 100):
            raise ValueError("El umbral de cambio importante debe estar entre 0,5 % y 100 %")
        with self._connect() as connection, connection:
            connection.execute("""INSERT INTO comparator_settings(key,value) VALUES(?,?)
                                  ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                               (key, str(int(number)) if number == int(number) else str(number)))

    def price_expiry_days(self) -> int:
        return int(float(self.get_setting("price_expiry_days")))

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
                    caliber_text = _text(row.get("CALIBRE"))
                    if caliber_text:
                        from price_references import normalize_caliber
                        caliber = normalize_caliber(caliber_text)
                        if not caliber:
                            raise ValueError(f"Calibre no válido: {caliber_text}")
                        connection.execute("UPDATE products SET caliber=? WHERE ean=?", (caliber, ean))
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
                        INSERT INTO competitors(id,name,url,domain,market,currency) VALUES(?,?,?,?,'','')
                        ON CONFLICT(id) DO UPDATE SET name=excluded.name,url=excluded.url,domain=excluded.domain
                    """, (competitor_id, name, url, domain))
                    if old:
                        updated += 1
                    else:
                        imported += 1
                except ValueError as exc:
                    rejected.append(f"Fila {index + 2}: {exc}")
        return ImportReport(imported, updated, tuple(rejected))

    def fill_competitor_defaults(self, *, apply: bool = False, market: str = "", currency: str = "") -> list[str]:
        """List competitors whose real market/currency is not verified yet.

        Nothing is assumed: with ``apply`` the empty fields are filled only with the ``market`` and ``currency``
        explicitly confirmed by the caller (ES/EUR is the comparison target, not a competitor fact).
        """
        market, currency = market.strip(), currency.strip().upper()
        if apply and not (market and re.fullmatch(r"[A-Z]{3}", currency)):
            raise ValueError("Indique mercado y moneda ISO verificados para aplicar")
        with self._connect() as connection, connection:
            pending = connection.execute(
                "SELECT id,market,currency FROM competitors WHERE market='' OR currency=''").fetchall()
            if apply:
                connection.execute("UPDATE competitors SET market=? WHERE market=''", (market,))
                connection.execute("UPDATE competitors SET currency=? WHERE currency=''", (currency,))
        return [row["id"] for row in pending]

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
        imported = updated = rejected = 0
        errors: list[str] = []
        with self._connect() as connection, connection:
            competitors = {row["name"].casefold(): row for row in connection.execute("SELECT * FROM competitors")}
            competitors.update({row["id"].casefold(): row for row in connection.execute("SELECT * FROM competitors")})
            for index, row in frame.iterrows():
                try:
                    ean = normalize_gtin(row["EAN"])
                    product = connection.execute("SELECT * FROM products WHERE ean=?", (ean,)).fetchone()
                    if not product:
                        raise ValueError("EAN no importado en el catálogo")
                    competitor = competitors.get(_text(row["Competidor"]).casefold())
                    if not competitor:
                        raise ValueError("Competidor no importado")
                    market = _text(row["Mercado"])
                    if not market:
                        raise ValueError("El mercado es obligatorio")
                    configured_market = competitor["market"]
                    web_record = _text(row["Método"]).startswith("web")
                    if web_record:
                        commercial = connection.execute("""SELECT * FROM competitor_web_config
                            WHERE competitor_id=?""", (competitor["id"],)).fetchone()
                        if commercial and commercial["market_verified"] and commercial["market_evidence_url"] \
                                and commercial["market_note"]:
                            configured_market = commercial["sales_market"]
                    if configured_market and configured_market != market \
                            and (not web_record or _text(row["Estado coincidencia"]) != "manual_review"):
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

                    validation: dict = {}
                    identity = self._offer_identity(row, ean, product, match_status, validation=validation)
                    if validation:
                        evidence += " | Validación identidad: " + json.dumps(validation, ensure_ascii=False)

                    if not _text(row["Precio"]) or match_status == "rejected":
                        fingerprint = _fingerprint((ean, competitor["id"], market, url, captured,
                                                    method, match_status, evidence, *identity[:5]))
                        cursor = connection.execute("""
                            INSERT OR IGNORE INTO search_results(
                                ean,competitor_id,market,url,captured_at,method,match_status,evidence,fingerprint,
                                ean_published,model_code,color_code,caliber,match_basis
                            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """, (ean, competitor["id"], market, url, captured, method, match_status,
                              evidence, fingerprint, *identity[:5]))
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
                    tax_included, vat_rate = identity[5], identity[6]
                    content = (price, promo, previous, shipping, currency, fx, availability, url, match_status,
                               *identity)
                    latest = connection.execute("""
                        SELECT id,captured_at,price,promotional_price,previous_price,shipping_cost,currency,
                               fx_to_eur,availability,url,match_status,ean_published,model_code,color_code,
                               caliber,match_basis,price_includes_tax,vat_rate
                        FROM offers WHERE ean=? AND competitor_id=? AND market=?
                        ORDER BY captured_at DESC,id DESC LIMIT 1
                    """, (ean, competitor["id"], market)).fetchone()
                    if latest and captured >= latest["captured_at"] and content == (
                            latest["price"], latest["promotional_price"], latest["previous_price"],
                            latest["shipping_cost"], latest["currency"], latest["fx_to_eur"],
                            latest["availability"], latest["url"], latest["match_status"],
                            latest["ean_published"], latest["model_code"], latest["color_code"],
                            latest["caliber"], latest["match_basis"], latest["price_includes_tax"],
                            latest["vat_rate"]):
                        # Unchanged: keep the single row and only record the latest successful check.
                        connection.execute("""
                            INSERT INTO offer_checks(ean,competitor_id,market,last_offer_id,last_ok_at,checks)
                            VALUES(?,?,?,?,?,1)
                            ON CONFLICT(ean,competitor_id,market) DO UPDATE SET
                                last_offer_id=excluded.last_offer_id,
                                last_ok_at=MAX(offer_checks.last_ok_at,excluded.last_ok_at),
                                checks=CASE WHEN offer_checks.last_offer_id=excluded.last_offer_id
                                       THEN offer_checks.checks+1 ELSE 1 END
                        """, (ean, competitor["id"], market, latest["id"], captured))
                        connection.execute(
                            "UPDATE competitors SET last_checked_at=?,connectivity='manual_import' WHERE id=?",
                            (captured, competitor["id"]))
                        updated += 1
                        continue
                    fingerprint = _fingerprint((ean, competitor["id"], market, price, promo, previous, shipping,
                                                currency, fx, availability, url, captured, method,
                                                match_status, evidence, *identity))
                    cursor = connection.execute("""
                        INSERT OR IGNORE INTO offers(
                            ean,competitor_id,market,price,promotional_price,previous_price,shipping_cost,
                            currency,fx_to_eur,availability,url,captured_at,method,match_status,evidence,fingerprint,
                            ean_published,model_code,color_code,caliber,match_basis,price_includes_tax,vat_rate
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (ean, competitor["id"], market, price, promo, previous, shipping, currency, fx,
                          availability, url, captured, method, match_status, evidence, fingerprint, *identity))
                    imported += cursor.rowcount
                    connection.execute(
                        "UPDATE competitors SET last_checked_at=?,connectivity='manual_import' WHERE id=?",
                        (captured, competitor["id"]),
                    )
                except ValueError as exc:
                    rejected += 1
                    errors.append(f"Fila {index + 2}: {exc}")
        return ImportReport(imported, updated, tuple(errors))

    @staticmethod
    def _offer_identity(row: Any, ean: str, product: Any, match_status: str, *, validation: dict | None = None) -> tuple:
        """Structured identity of an offer: (ean_published, model, colour, caliber, basis, tax_included, vat).

        An exact match is accepted only if the declared identity really verifies against the catalogue.
        """
        from price_references import EXACT, REJECTED, classify, normalize_caliber

        published = _text(row.get("EAN publicado"))
        if published:
            published = normalize_gtin(published)
        model, color = _text(row.get("Modelo")), _text(row.get("Color"))
        raw_caliber = _text(row.get("Calibre"))
        caliber = normalize_caliber(raw_caliber)
        if raw_caliber and not caliber:
            raise ValueError(f"Calibre no válido: {raw_caliber}")
        basis = _text(row.get("Base coincidencia")).lower()
        if basis not in MATCH_BASES:
            raise ValueError("Base de coincidencia no válida")
        tax_raw = _text(row.get("IVA incluido")).lower()
        if tax_raw in {"", "nan", "none", "indeterminado"}:
            tax_included = None
        elif tax_raw in {"1", "true", "si", "sí", "yes"}:
            tax_included = 1
        elif tax_raw in {"0", "false", "no"}:
            tax_included = 0
        else:
            raise ValueError("IVA incluido debe ser sí, no o vacío")
        vat_rate = _number(row.get("Tipo IVA"), "Tipo IVA", optional=True)
        if any((published, model, color, caliber, _text(row.get("Marca")), _text(row.get("Título")))):
            result = classify(dict(product), {
                "ean_published": published, "brand": _text(row.get("Marca")), "model": model,
                "color": color, "caliber": caliber, "title": _text(row.get("Título")),
                "identifiers": row.get("Identificadores") if isinstance(row.get("Identificadores"), list) else [],
                "ambiguous": row.get("Ambigua") is True})
            if validation is not None:
                validation.update(result.normalized)
                validation.update({"reasons": result.reasons, "observations": result.observations})
            if match_status == "exact_match" and result.status != EXACT:
                raise ValueError("Marcado exact_match, pero no se verifica: " + "; ".join(result.reasons))
            if result.status == REJECTED and match_status not in {"rejected", "not_found", "error"}:
                raise ValueError("Identidad descartada: " + "; ".join(result.reasons))
            if match_status == "exact_match" and not basis:
                basis = result.basis
            listing = result.normalized["listing"]
            if not model and len(listing["models"]) == 1:
                model = listing["models"][0]
            if not color and len(listing["colors"]) == 1:
                color = listing["colors"][0]
        elif match_status == "exact_match" and basis:
            raise ValueError("Base de coincidencia sin datos de identidad")
        return (published, model, color, caliber, basis, tax_included, vat_rate)

    def update_catalog_references(self, records: list[dict[str, str]], *, apply: bool = False) -> list[dict]:
        """Complete model, colour and calibre of catalogue products (e.g. from Odoo). Returns the changes.

        Only fills fields that are empty or differ; never touches EAN, brand or names. Nothing is written
        unless ``apply``.
        """
        from price_references import normalize_caliber

        changes: list[dict] = []
        with self._connect() as connection, connection:
            for record in records:
                ean = normalize_gtin(record["ean"])
                product = connection.execute("SELECT * FROM products WHERE ean=?", (ean,)).fetchone()
                if not product:
                    continue
                new = {"model": _text(record.get("model")), "color": _text(record.get("color")),
                       "caliber": normalize_caliber(record.get("caliber"))}
                diff = {name: (product[name], value) for name, value in new.items()
                        if value and product[name] != value}
                if not diff:
                    continue
                changes.append({"ean": ean, "changes": diff})
                if apply:
                    sets = ",".join(f"{name}=?" for name in diff)
                    connection.execute(f"UPDATE products SET {sets},updated_at=? WHERE ean=?",
                                       (*[value[1] for value in diff.values()],
                                        datetime.now(timezone.utc).isoformat(timespec="seconds"), ean))
        return changes

    def save_coverage(self, records: list[dict[str, Any]]) -> None:
        """Store the per-competitor query mechanism state. ``operational`` needs real proof."""
        with self._connect() as connection, connection:
            known = {}
            for row in connection.execute("SELECT id,name FROM competitors"):
                known[row["id"].casefold()] = row
                known[row["name"].casefold()] = row
            for record in records:
                competitor = known.get(_text(record.get("competitor")).casefold())
                if not competitor:
                    raise ValueError(f"Competidor desconocido: {record.get('competitor')}")
                status = _text(record.get("integration_status")) or "not_checked"
                if status not in COVERAGE_STATUSES:
                    raise ValueError(f"Estado de integración no válido: {status}")
                if status == "operational":
                    verified = connection.execute(
                        "SELECT 1 FROM competitor_schedule WHERE competitor_id=? AND connector!='' "
                        "AND verified_at IS NOT NULL", (competitor["id"],)).fetchone()
                    confirmed = connection.execute(
                        "SELECT 1 FROM offers WHERE competitor_id=? AND match_status='exact_match'",
                        (competitor["id"],)).fetchone()
                    if not (verified and confirmed):
                        raise ValueError(f"{competitor['id']}: 'operational' exige conector verificado "
                                         "y al menos una oferta confirmada")
                connection.execute("""
                    INSERT INTO competitor_coverage(competitor_id,method,integration_status,restrictions,
                                                    allowed_frequency,notes,last_checked_at)
                    VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(competitor_id) DO UPDATE SET method=excluded.method,
                        integration_status=excluded.integration_status,restrictions=excluded.restrictions,
                        allowed_frequency=excluded.allowed_frequency,notes=excluded.notes,
                        last_checked_at=excluded.last_checked_at
                """, (competitor["id"], _text(record.get("method")), status, _text(record.get("restrictions")),
                      _text(record.get("allowed_frequency")), _text(record.get("notes")),
                      _text(record.get("last_checked_at")) or None))

    def coverage_table(self) -> pd.DataFrame:
        """One row per competitor; reference and offer counts are computed from stored observations."""
        with self._connect() as connection:
            return pd.read_sql_query("""
                SELECT c.name AS competitor,c.domain,
                       COALESCE(v.method,'') AS method,
                       COALESCE(v.integration_status,'not_checked') AS integration_status,
                       COALESCE(v.restrictions,'') AS restrictions,
                       COALESCE(v.allowed_frequency,'') AS allowed_frequency,
                       COALESCE(v.notes,'') AS notes,
                       v.last_checked_at AS last_checked_at,
                       (SELECT COUNT(DISTINCT ean) FROM (
                            SELECT ean FROM offers WHERE competitor_id=c.id AND match_status IN ('exact_match','probable_match')
                            UNION ALL
                            SELECT ean FROM search_results WHERE competitor_id=c.id AND match_status IN ('exact_match','probable_match')
                       )) AS references_located,
                       (SELECT COUNT(DISTINCT ean) FROM offers
                         WHERE competitor_id=c.id AND match_status='exact_match') AS confirmed_offers
                FROM competitors c LEFT JOIN competitor_coverage v ON v.competitor_id=c.id
                ORDER BY c.name
            """, connection)

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
                SELECT o.*,c.name AS competitor_name,p.brand,p.commercial_name,
                       COALESCE(k.last_ok_at,o.captured_at) AS checked_at
                FROM offers o JOIN competitors c ON c.id=o.competitor_id
                JOIN products p ON p.ean=o.ean
                LEFT JOIN offer_checks k ON k.last_offer_id=o.id
                WHERE COALESCE(k.last_ok_at,o.captured_at)>=? AND c.active=1
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
                SELECT o.*,c.name AS competitor_name,COALESCE(k.last_ok_at,o.captured_at) AS checked_at
                FROM offers o JOIN competitors c ON c.id=o.competitor_id
                LEFT JOIN offer_checks k ON k.last_offer_id=o.id
                WHERE COALESCE(k.last_ok_at,o.captured_at)>=? AND c.active=1
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

        # A synchronized Odoo price supersedes the manual contingency import for its EAN. It takes part
        # in comparisons only when validated, not flagged stale by a later incident and recently synced.
        synced_cutoff = (datetime.now(timezone.utc) - timedelta(days=self.price_expiry_days())).isoformat()
        if not synced.empty:
            own = own[~own["ean"].isin(synced["ean"])]
            confirmed = synced[
                (synced["validation_status"] == "validated") & (synced["comparable"] == 1)
                & synced["stale_since"].isna() & (synced["synced_at"] >= synced_cutoff)
            ]
            own = pd.concat([own, pd.DataFrame({
                "ean": confirmed["ean"], "market": confirmed["market"], "price": confirmed["price"],
                "promotional_price": None, "currency": confirmed["currency"], "fx_to_eur": 1.0,
                "captured_at": confirmed["synced_at"], "source": confirmed["source"],
            })], ignore_index=True)
        synced_by_ean = {row["ean"]: row for row in synced.to_dict("records")}

        def synced_status(row: dict[str, Any]) -> str:
            if row["stale_since"] is not None and not pd.isna(row["stale_since"]):
                return "stale"
            if row["synced_at"] < synced_cutoff:
                return "expired"
            return row["validation_status"]

        rows: list[dict[str, Any]] = []
        for product in products.to_dict("records"):
            own_rows = own[own["ean"] == product["ean"]]
            own_record = own_rows.sort_values("captured_at").iloc[-1].to_dict() if not own_rows.empty else None
            valid_product_offers = product_offers = comparable_offers(
                offers[offers["ean"] == product["ean"]])
            comparable = []
            if own_record:
                matched = product_offers[product_offers["market"] == own_record["market"]]
                for competitor_id, group in matched.groupby("competitor_id"):
                    offer = group.iloc[-1].to_dict()
                    effective = offer["promotional_price"]
                    if effective is None or pd.isna(effective):
                        effective = offer["price"]
                    effective_eur = float(effective) * float(offer["fx_to_eur"])
                    comparable.append((effective_eur, offer))
            own_eur = None
            minimum = mean = maximum = diff = diff_pct = diff_vs_competitor_pct = None
            position = None
            best_competitor = best_url = best_checked_at = ""
            if own_record and own_record["fx_to_eur"] is not None and not pd.isna(own_record["fx_to_eur"]):
                own_price = own_record["promotional_price"]
                if own_price is None or pd.isna(own_price):
                    own_price = own_record["price"]
                own_eur = float(own_price) * float(own_record["fx_to_eur"])
            if comparable:
                prices = [item[0] for item in comparable]
                minimum, mean, maximum = min(prices), sum(prices) / len(prices), max(prices)
                best_price, best_offer = min(comparable, key=lambda item: item[0])
                best_competitor, best_url = best_offer["competitor_name"], best_offer["url"]
                best_checked_at = best_offer["checked_at"]
                if own_eur is not None:
                    diff = own_eur - minimum
                    diff_pct = (diff / own_eur * 100) if own_eur else None
                    diff_vs_competitor_pct = (diff / minimum * 100) if minimum else None
                    position = 1 + sum(price < own_eur for price in prices)
            rows.append({
                **product,
                "own_price_eur": own_eur,
                "competitor_min_eur": minimum,
                "best_competitor": best_competitor,
                "best_url": best_url,
                "last_checked_at": best_checked_at,
                "competitor_mean_eur": mean,
                "competitor_max_eur": maximum,
                "competitor_count": len(comparable),
                "confirmed_offer_count": int(valid_product_offers["competitor_id"].nunique()),
                "difference_eur": diff,
                "difference_pct": diff_pct,
                "difference_vs_competitor_pct": diff_vs_competitor_pct,
                "position": position,
                "market": own_record["market"] if own_record else "",
                "own_price_source": (own_record["source"] if own_record else
                                     ("Odoo (sin precio confirmado)" if product["ean"] in synced_by_ean else "")),
                "own_price_status": (synced_status(synced_by_ean[product["ean"]])
                                     if product["ean"] in synced_by_ean else ""),
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


def comparable_offers(offers: pd.DataFrame) -> pd.DataFrame:
    """Offers that may enter a price comparison, oldest to newest.

    Only the latest exact observation of each (EAN, competitor, market) counts, and it is then required to be
    available, with tax-inclusive price explicitly confirmed (``price_includes_tax = 1``) and an EUR rate.
    Filtering after taking the latest prevents an out-of-stock or unknown-VAT update from silently falling back
    to an older price. Older observations stay stored for consultation and history.
    """
    if offers.empty:
        return offers
    exact = offers[offers["match_status"] == "exact_match"]
    latest = exact.sort_values(["captured_at", "id"]).groupby(["ean", "competitor_id", "market"]).tail(1)
    return latest[(latest["availability"] == "available") & (latest["price_includes_tax"] == 1)
                  & latest["fx_to_eur"].notna()].sort_values(["captured_at", "id"])


def exclusion_reason(offer: dict[str, Any], latest: bool = True) -> str:
    """Why a stored offer is not used in comparisons ('' when it is)."""
    if offer["match_status"] != "exact_match":
        return "Coincidencia no exacta"
    if not latest:
        return "Sustituida por una observación posterior"
    if offer["availability"] != "available":
        return "Sin stock" if offer["availability"] == "unavailable" else "Disponibilidad desconocida"
    tax = offer.get("price_includes_tax")
    if tax is None or pd.isna(tax):
        return "IVA no confirmado"
    if tax != 1:
        return "Precio sin IVA"
    if offer.get("fx_to_eur") is None or pd.isna(offer.get("fx_to_eur")):
        return "Sin tipo de cambio a EUR"
    return ""
