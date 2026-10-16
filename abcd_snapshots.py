from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import text

from engine import (
    DEFAULT_DAYS_WITHOUT_SALES_FOR_C,
    DEFAULT_DAYS_WITHOUT_SALES_FOR_D,
    DEFAULT_MARGIN_DAYS,
    DEFAULT_PERCENTILE_A,
)


ABCD_SNAPSHOT_TABLE = "abcd_weekly_snapshots"
MADRID = ZoneInfo("Europe/Madrid")
ABCD_REPORT_PREFERRED_COLUMNS = [
    "Marca",
    "Modelo",
    "Cód Barras",
    "EAN",
    "categoria",
    "Stock",
    "ABCD",
    "Motivo",
    "Alerta",
    "Accion_Recomendada",
    "PVO",
    "PVO sin descuento",
    "Primera Compra",
    "Última Compra",
    "Última Venta",
    "Última Reposicion",
    "Num_Ventas_180D",
    "Ventas_180_Dias",
    "Ventas_7_Dias",
    "Dias_desde_Primera_Compra",
    "Capital_Bloqueado (€)",
    "Fecha_Revision",
    "Dias_para_D",
    "product_id",
]


@dataclass(frozen=True)
class SnapshotReference:
    snapshot_id: int
    snapshot_date: date
    run_at: datetime
    engine_version: str
    parameters: dict[str, Any]
    data_sha256: str
    product_count: int
    capture_kind: str = "manual"


def build_abcd_report_export_df(df: pd.DataFrame) -> pd.DataFrame:
    """Order the canonical snapshot/export fields while retaining extra columns."""
    ordered_columns = [column for column in ABCD_REPORT_PREFERRED_COLUMNS if column in df.columns]
    remaining_columns = [column for column in df.columns if column not in ordered_columns]
    return df[ordered_columns + remaining_columns]


def build_abcd_report_excel_df(df: pd.DataFrame) -> pd.DataFrame:
    """Apply the legacy Excel headers used by the ABCD report export."""
    return build_abcd_report_export_df(df).rename(
        columns={"Modelo": "NOMBRE", "Cód Barras": "SKU"}
    )


def madrid_now() -> datetime:
    return datetime.now(MADRID)


def _madrid_date(value: date | datetime | pd.Timestamp | None = None) -> date:
    if value is None:
        return madrid_now().date()
    if isinstance(value, pd.Timestamp):
        if value.tzinfo is not None:
            return value.to_pydatetime().astimezone(MADRID).date()
        return value.date()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("La fecha de captura debe incluir zona horaria")
        return value.astimezone(MADRID).date()
    return value


def current_week_start(reference: date | datetime | pd.Timestamp | None = None) -> date:
    today = _madrid_date(reference)
    return today - pd.Timedelta(days=today.weekday())


def engine_source_version() -> str:
    source_path = Path(__file__).with_name("engine.py")
    digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    return f"sha256:{digest}"


def build_snapshot_parameters(reference_date: date | pd.Timestamp | None = None) -> dict[str, Any]:
    reference = _madrid_date(reference_date)
    return {
        "reference_date": reference.isoformat(),
        "margin_days": DEFAULT_MARGIN_DAYS,
        "days_without_sales_for_c": DEFAULT_DAYS_WITHOUT_SALES_FOR_C,
        "days_without_sales_for_d": DEFAULT_DAYS_WITHOUT_SALES_FOR_D,
        "price_percentile_a": DEFAULT_PERCENTILE_A,
    }


def _snapshot_data_sha256(payload: Any) -> str:
    if isinstance(payload, str):
        payload = json.loads(payload)
    canonical_json = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    return value if isinstance(value, dict) else {}


def _snapshot_reference(row) -> SnapshotReference:
    return SnapshotReference(
        snapshot_id=row[0],
        snapshot_date=row[1],
        run_at=row[2],
        engine_version=row[3],
        parameters=_json_object(row[4]),
        data_sha256=row[5] or "",
        product_count=row[6],
        capture_kind=row[7],
    )


def ensure_abcd_snapshot_table(engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:lock_key)::bigint)"),
            {"lock_key": ABCD_SNAPSHOT_TABLE},
        )
        connection.execute(
            text(
                f"""
                CREATE TABLE IF NOT EXISTS {ABCD_SNAPSHOT_TABLE} (
                    snapshot_id bigserial PRIMARY KEY,
                    snapshot_date date NOT NULL,
                    iso_year integer NOT NULL,
                    iso_week integer NOT NULL,
                    product_count integer NOT NULL,
                    data jsonb NOT NULL,
                    run_at timestamp with time zone NOT NULL,
                    engine_version text NOT NULL,
                    parameters jsonb NOT NULL,
                    data_sha256 text NOT NULL,
                    capture_kind text NOT NULL DEFAULT 'manual',
                    created_at timestamp with time zone NOT NULL DEFAULT now(),
                    updated_at timestamp with time zone NOT NULL DEFAULT now()
                )
                """
            )
        )
        columns = {
            row[0]
            for row in connection.execute(
                text(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = current_schema() AND table_name = :table_name
                    """
                ),
                {"table_name": ABCD_SNAPSHOT_TABLE},
            ).fetchall()
        }
        required_columns = {
            "snapshot_id",
            "run_at",
            "engine_version",
            "parameters",
            "data_sha256",
            "capture_kind",
        }
        if "capture_kind" not in columns:
            connection.execute(
                text(
                    f"ALTER TABLE {ABCD_SNAPSHOT_TABLE} "
                    "ADD COLUMN IF NOT EXISTS capture_kind text NOT NULL DEFAULT 'manual'"
                )
            )
            columns.add("capture_kind")
        if required_columns.issubset(columns):
            connection.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS {ABCD_SNAPSHOT_TABLE}_week_idx "
                    f"ON {ABCD_SNAPSHOT_TABLE} (snapshot_date, run_at DESC)"
                )
            )
            connection.execute(
                text(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS {ABCD_SNAPSHOT_TABLE}_official_week_idx "
                    f"ON {ABCD_SNAPSHOT_TABLE} (iso_year, iso_week) "
                    "WHERE capture_kind = 'official'"
                )
            )
            return
        connection.execute(
            text(
                f"""
                ALTER TABLE {ABCD_SNAPSHOT_TABLE}
                    ADD COLUMN IF NOT EXISTS snapshot_id bigserial,
                    ADD COLUMN IF NOT EXISTS run_at timestamp with time zone,
                    ADD COLUMN IF NOT EXISTS engine_version text NOT NULL DEFAULT 'legacy',
                    ADD COLUMN IF NOT EXISTS parameters jsonb NOT NULL DEFAULT '{{"legacy": true}}'::jsonb,
                    ADD COLUMN IF NOT EXISTS data_sha256 text NOT NULL DEFAULT '',
                    ADD COLUMN IF NOT EXISTS capture_kind text NOT NULL DEFAULT 'manual'
                """
            )
        )
        connection.execute(
            text(
                f"""
                UPDATE {ABCD_SNAPSHOT_TABLE}
                SET run_at = created_at,
                    parameters = '{{"legacy": true}}'::jsonb
                WHERE run_at IS NULL
                """
            )
        )
        connection.execute(
            text(f"ALTER TABLE {ABCD_SNAPSHOT_TABLE} ALTER COLUMN run_at SET NOT NULL")
        )
        connection.execute(
            text(
                f"""
                DO $$
                DECLARE
                    primary_key_name text;
                    primary_key_definition text;
                BEGIN
                    SELECT conname, pg_get_constraintdef(oid)
                    INTO primary_key_name, primary_key_definition
                    FROM pg_constraint
                    WHERE conrelid = '{ABCD_SNAPSHOT_TABLE}'::regclass AND contype = 'p';

                    IF primary_key_name IS NOT NULL
                       AND primary_key_definition <> 'PRIMARY KEY (snapshot_id)' THEN
                        EXECUTE format('ALTER TABLE {ABCD_SNAPSHOT_TABLE} DROP CONSTRAINT %I', primary_key_name);
                        primary_key_name := NULL;
                    END IF;

                    IF primary_key_name IS NULL THEN
                        ALTER TABLE {ABCD_SNAPSHOT_TABLE}
                            ADD CONSTRAINT {ABCD_SNAPSHOT_TABLE}_pkey PRIMARY KEY (snapshot_id);
                    END IF;
                END $$
                """
            )
        )
        connection.execute(
            text(
                f"CREATE INDEX IF NOT EXISTS {ABCD_SNAPSHOT_TABLE}_week_idx "
                f"ON {ABCD_SNAPSHOT_TABLE} (snapshot_date, run_at DESC)"
            )
        )
        connection.execute(
            text(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {ABCD_SNAPSHOT_TABLE}_official_week_idx "
                f"ON {ABCD_SNAPSHOT_TABLE} (iso_year, iso_week) "
                "WHERE capture_kind = 'official'"
            )
        )


def save_abcd_weekly_snapshot(
    engine,
    df: pd.DataFrame,
    snapshot_date: date | None = None,
    *,
    run_at: datetime | None = None,
    engine_version: str | None = None,
    parameters: dict[str, Any] | None = None,
) -> date:
    captured_at = run_at or madrid_now()
    if snapshot_date is not None and snapshot_date != current_week_start(captured_at):
        raise ValueError("La captura oficial solo puede corresponder a la semana ISO actual")
    reference, _ = save_abcd_official_snapshot(
        engine,
        df,
        captured_at=captured_at,
        engine_version=engine_version,
        parameters=parameters,
    )
    return reference.snapshot_date


def save_abcd_official_snapshot(
    engine,
    df: pd.DataFrame,
    *,
    captured_at: datetime | None = None,
    engine_version: str | None = None,
    parameters: dict[str, Any] | None = None,
) -> tuple[SnapshotReference, bool]:
    instant = captured_at or madrid_now()
    if instant.tzinfo is None:
        raise ValueError("La fecha de captura debe incluir zona horaria")
    instant = instant.astimezone(MADRID)
    now = madrid_now()
    if instant > now or instant.isocalendar()[:2] != now.isocalendar()[:2]:
        raise ValueError("No se puede crear una captura oficial de una semana pasada o futura")
    snapshot_date = current_week_start(instant)
    iso_year, iso_week, _ = snapshot_date.isocalendar()
    run_at = instant.astimezone(timezone.utc)
    engine_version = engine_version or engine_source_version()
    parameters = parameters or build_snapshot_parameters(instant)
    snapshot_df = build_abcd_report_export_df(df.copy())
    snapshot_json = snapshot_df.to_json(orient="records", date_format="iso", force_ascii=False)
    data_sha256 = _snapshot_data_sha256(json.loads(snapshot_json))

    ensure_abcd_snapshot_table(engine)
    with engine.begin() as connection:
        connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:lock_key)::bigint)"),
            {"lock_key": f"{ABCD_SNAPSHOT_TABLE}:official:{iso_year}:{iso_week}"},
        )
        existing = connection.execute(
            text(
                f"""
                SELECT snapshot_id, snapshot_date, run_at, engine_version, parameters,
                       data_sha256, product_count, capture_kind
                FROM {ABCD_SNAPSHOT_TABLE}
                WHERE iso_year = :iso_year AND iso_week = :iso_week
                  AND capture_kind = 'official'
                """
            ),
            {"iso_year": iso_year, "iso_week": iso_week},
        ).fetchone()
        if existing is not None:
            return _snapshot_reference(existing), False
        row = connection.execute(
            text(
                f"""
                INSERT INTO {ABCD_SNAPSHOT_TABLE} (
                    snapshot_date, iso_year, iso_week, product_count, data,
                    run_at, engine_version, parameters, data_sha256, capture_kind,
                    created_at, updated_at
                ) VALUES (
                    :snapshot_date, :iso_year, :iso_week, :product_count, CAST(:data AS jsonb),
                    :run_at, :engine_version, CAST(:parameters AS jsonb), :data_sha256, 'official',
                    now(), now()
                )
                RETURNING snapshot_id
                """
            ),
            {
                "snapshot_date": snapshot_date,
                "iso_year": iso_year,
                "iso_week": iso_week,
                "product_count": len(snapshot_df),
                "data": snapshot_json,
                "run_at": run_at,
                "engine_version": engine_version,
                "parameters": json.dumps(parameters, ensure_ascii=False, default=str),
                "data_sha256": data_sha256,
            },
        ).fetchone()
        if row is None:
            raise RuntimeError("PostgreSQL no devolvió el ID del snapshot insertado")
        reference = SnapshotReference(
            snapshot_id=row[0],
            snapshot_date=snapshot_date,
            run_at=run_at,
            engine_version=engine_version,
            parameters=parameters,
            data_sha256=data_sha256,
            product_count=len(snapshot_df),
            capture_kind="official",
        )
    return reference, True


def save_abcd_snapshot(
    engine,
    df: pd.DataFrame,
    snapshot_date: date | None = None,
    *,
    run_at: datetime | None = None,
    engine_version: str | None = None,
    parameters: dict[str, Any] | None = None,
) -> SnapshotReference:
    run_at = run_at or datetime.now(timezone.utc)
    if run_at.tzinfo is None:
        raise ValueError("La fecha de captura debe incluir zona horaria")
    run_at = run_at.astimezone(timezone.utc)
    snapshot_date = snapshot_date or current_week_start(run_at)
    iso_year, iso_week, _ = snapshot_date.isocalendar()
    engine_version = engine_version or engine_source_version()
    parameters = parameters or build_snapshot_parameters(run_at)
    snapshot_df = build_abcd_report_export_df(df.copy())
    snapshot_json = snapshot_df.to_json(orient="records", date_format="iso", force_ascii=False)
    data_sha256 = _snapshot_data_sha256(json.loads(snapshot_json))

    ensure_abcd_snapshot_table(engine)
    with engine.begin() as connection:
        row = connection.execute(
            text(
                f"""
                INSERT INTO {ABCD_SNAPSHOT_TABLE} (
                    snapshot_date, iso_year, iso_week, product_count, data,
                    run_at, engine_version, parameters, data_sha256, capture_kind, created_at, updated_at
                ) VALUES (
                    :snapshot_date, :iso_year, :iso_week, :product_count, CAST(:data AS jsonb),
                    :run_at, :engine_version, CAST(:parameters AS jsonb), :data_sha256, 'manual', now(), now()
                )
                RETURNING snapshot_id
                """
            ),
            {
                "snapshot_date": snapshot_date,
                "iso_year": iso_year,
                "iso_week": iso_week,
                "product_count": len(snapshot_df),
                "data": snapshot_json,
                "run_at": run_at,
                "engine_version": engine_version,
                "parameters": json.dumps(parameters, ensure_ascii=False, default=str),
                "data_sha256": data_sha256,
            },
        ).fetchone()
    if row is None:
        raise RuntimeError("PostgreSQL no devolvió el ID del snapshot insertado")
    return SnapshotReference(
        snapshot_id=row[0],
        snapshot_date=snapshot_date,
        run_at=run_at,
        engine_version=engine_version,
        parameters=parameters,
        data_sha256=data_sha256,
        product_count=len(snapshot_df),
        capture_kind="manual",
    )


def list_abcd_snapshot_dates(engine) -> list[date]:
    return [snapshot.snapshot_date for snapshot in list_abcd_snapshot_refs(engine)]


def load_abcd_official_week(engine, reference: date | datetime | pd.Timestamp | None = None):
    iso_year, iso_week, _ = current_week_start(reference).isocalendar()
    ensure_abcd_snapshot_table(engine)
    with engine.connect() as connection:
        row = connection.execute(
            text(
                f"""
                SELECT snapshot_id, snapshot_date, run_at, engine_version, parameters,
                       data_sha256, product_count, capture_kind
                FROM {ABCD_SNAPSHOT_TABLE}
                WHERE iso_year = :iso_year AND iso_week = :iso_week
                  AND capture_kind = 'official'
                """
            ),
            {"iso_year": iso_year, "iso_week": iso_week},
        ).fetchone()
    return _snapshot_reference(row) if row is not None else None


def snapshot_week_inventory(snapshot_refs: list[SnapshotReference], reference: date) -> list[dict[str, str]]:
    if not snapshot_refs:
        return []
    week = current_week_start(min(item.snapshot_date for item in snapshot_refs))
    final_week = current_week_start(reference)
    by_week: dict[tuple[int, int], list[SnapshotReference]] = {}
    for item in snapshot_refs:
        iso = item.snapshot_date.isocalendar()
        by_week.setdefault((iso.year, iso.week), []).append(item)

    inventory = []
    while week <= final_week:
        iso = week.isocalendar()
        captures = by_week.get((iso.year, iso.week), [])
        inventory.append(
            {
                "Semana ISO": f"{iso.year}-W{iso.week:02}",
                "Estado": "Guardada" if captures else "Ausente",
                "Capturas": str(len(captures)),
                "Oficiales": str(sum(item.capture_kind == "official" for item in captures)),
                "Manuales/legacy": str(sum(item.capture_kind != "official" for item in captures)),
            }
        )
        week += pd.Timedelta(days=7)
    return inventory


def list_abcd_snapshot_refs(engine) -> list[SnapshotReference]:
    ensure_abcd_snapshot_table(engine)
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                f"""
                SELECT snapshot_id, snapshot_date, run_at, engine_version, parameters,
                       data_sha256, product_count, capture_kind
                FROM {ABCD_SNAPSHOT_TABLE}
                ORDER BY run_at DESC, snapshot_id DESC
                """
            )
        ).fetchall()
    return [
        SnapshotReference(
            snapshot_id=row[0],
            snapshot_date=row[1],
            run_at=row[2],
            engine_version=row[3],
            parameters=_json_object(row[4]),
            data_sha256=row[5],
            product_count=row[6],
            capture_kind=row[7],
        )
        for row in rows
    ]


def load_abcd_snapshot_by_id(
    engine,
    snapshot_id: int,
) -> tuple[pd.DataFrame, SnapshotReference | None]:
    ensure_abcd_snapshot_table(engine)
    with engine.connect() as connection:
        row = connection.execute(
            text(
                f"""
                SELECT snapshot_id, snapshot_date, run_at, engine_version, parameters,
                       data_sha256, product_count, data, capture_kind
                FROM {ABCD_SNAPSHOT_TABLE}
                WHERE snapshot_id = :snapshot_id
                """
            ),
            {"snapshot_id": snapshot_id},
        ).fetchone()
    if row is None:
        return pd.DataFrame(), None

    payload = row[7]
    if isinstance(payload, str):
        payload = json.loads(payload)
    data_sha256 = row[5] or ""
    if data_sha256 and _snapshot_data_sha256(payload) != data_sha256:
        raise RuntimeError(f"El snapshot {snapshot_id} no supera la comprobación SHA-256")
    if not isinstance(payload, list) or len(payload) != row[6]:
        raise RuntimeError(f"El snapshot {snapshot_id} no coincide con su recuento de productos")

    reference = SnapshotReference(
        snapshot_id=row[0],
        snapshot_date=row[1],
        run_at=row[2],
        engine_version=row[3],
        parameters=_json_object(row[4]),
        data_sha256=data_sha256,
        product_count=row[6],
        capture_kind=row[8],
    )
    return build_abcd_report_export_df(pd.DataFrame(payload)), reference


def load_abcd_weekly_snapshot(engine, snapshot_date: date) -> pd.DataFrame:
    ensure_abcd_snapshot_table(engine)
    with engine.connect() as connection:
        row = connection.execute(
            text(
                f"""
                SELECT snapshot_id FROM {ABCD_SNAPSHOT_TABLE}
                WHERE snapshot_date = :snapshot_date
                ORDER BY run_at DESC, snapshot_id DESC
                LIMIT 1
                """
            ),
            {"snapshot_date": snapshot_date},
        ).fetchone()
    if row is None:
        return pd.DataFrame()
    snapshot_df, _ = load_abcd_snapshot_by_id(engine, row[0])
    return snapshot_df