from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import text

from engine import (
    DEFAULT_DAYS_WITHOUT_SALES_FOR_C,
    DEFAULT_DAYS_WITHOUT_SALES_FOR_D,
    DEFAULT_MARGIN_DAYS,
    DEFAULT_PERCENTILE_A,
)


ABCD_SNAPSHOT_TABLE = "abcd_weekly_snapshots"
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


def current_week_start() -> date:
    today = pd.Timestamp.today().normalize()
    return (today - pd.Timedelta(days=today.weekday())).date()


def engine_source_version() -> str:
    source_path = Path(__file__).with_name("engine.py")
    digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    return f"sha256:{digest}"


def build_snapshot_parameters(reference_date: date | pd.Timestamp | None = None) -> dict[str, Any]:
    reference = pd.Timestamp(reference_date or pd.Timestamp.today()).normalize()
    return {
        "reference_date": reference.date().isoformat(),
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
        }
        if required_columns.issubset(columns):
            connection.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS {ABCD_SNAPSHOT_TABLE}_week_idx "
                    f"ON {ABCD_SNAPSHOT_TABLE} (snapshot_date, run_at DESC)"
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
                    ADD COLUMN IF NOT EXISTS data_sha256 text NOT NULL DEFAULT ''
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


def save_abcd_weekly_snapshot(
    engine,
    df: pd.DataFrame,
    snapshot_date: date | None = None,
    *,
    run_at: datetime | None = None,
    engine_version: str | None = None,
    parameters: dict[str, Any] | None = None,
) -> date:
    reference = save_abcd_snapshot(
        engine,
        df,
        snapshot_date,
        run_at=run_at,
        engine_version=engine_version,
        parameters=parameters,
    )
    return reference.snapshot_date


def save_abcd_snapshot(
    engine,
    df: pd.DataFrame,
    snapshot_date: date | None = None,
    *,
    run_at: datetime | None = None,
    engine_version: str | None = None,
    parameters: dict[str, Any] | None = None,
) -> SnapshotReference:
    snapshot_date = snapshot_date or current_week_start()
    iso_year, iso_week, _ = snapshot_date.isocalendar()
    run_at = run_at or datetime.now(timezone.utc)
    engine_version = engine_version or engine_source_version()
    parameters = parameters or build_snapshot_parameters(run_at.date())
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
                    run_at, engine_version, parameters, data_sha256, created_at, updated_at
                ) VALUES (
                    :snapshot_date, :iso_year, :iso_week, :product_count, CAST(:data AS jsonb),
                    :run_at, :engine_version, CAST(:parameters AS jsonb), :data_sha256, now(), now()
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
    )


def list_abcd_snapshot_dates(engine) -> list[date]:
    return [snapshot.snapshot_date for snapshot in list_abcd_snapshot_refs(engine)]


def list_abcd_snapshot_refs(engine) -> list[SnapshotReference]:
    ensure_abcd_snapshot_table(engine)
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                f"""
                SELECT snapshot_id, snapshot_date, run_at, engine_version, parameters,
                       data_sha256, product_count
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
                       data_sha256, product_count, data
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

    reference = SnapshotReference(
        snapshot_id=row[0],
        snapshot_date=row[1],
        run_at=row[2],
        engine_version=row[3],
        parameters=_json_object(row[4]),
        data_sha256=data_sha256,
        product_count=row[6],
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