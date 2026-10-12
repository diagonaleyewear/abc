from __future__ import annotations

from datetime import date
import json

import pandas as pd
from sqlalchemy import text


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


def ensure_abcd_snapshot_table(engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                f"""
                CREATE TABLE IF NOT EXISTS {ABCD_SNAPSHOT_TABLE} (
                    snapshot_date date PRIMARY KEY,
                    iso_year integer NOT NULL,
                    iso_week integer NOT NULL,
                    product_count integer NOT NULL,
                    data jsonb NOT NULL,
                    created_at timestamp with time zone NOT NULL DEFAULT now(),
                    updated_at timestamp with time zone NOT NULL DEFAULT now()
                )
                """
            )
        )


def save_abcd_weekly_snapshot(
    engine,
    df: pd.DataFrame,
    snapshot_date: date | None = None,
) -> date:
    snapshot_date = snapshot_date or current_week_start()
    iso_year, iso_week, _ = snapshot_date.isocalendar()
    snapshot_df = build_abcd_report_export_df(df.copy())
    snapshot_json = snapshot_df.to_json(orient="records", date_format="iso", force_ascii=False)

    ensure_abcd_snapshot_table(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                f"""
                INSERT INTO {ABCD_SNAPSHOT_TABLE} (
                    snapshot_date, iso_year, iso_week, product_count, data, created_at, updated_at
                ) VALUES (
                    :snapshot_date, :iso_year, :iso_week, :product_count, CAST(:data AS jsonb), now(), now()
                )
                ON CONFLICT (snapshot_date) DO UPDATE SET
                    iso_year = EXCLUDED.iso_year,
                    iso_week = EXCLUDED.iso_week,
                    product_count = EXCLUDED.product_count,
                    data = EXCLUDED.data,
                    updated_at = now()
                """
            ),
            {
                "snapshot_date": snapshot_date,
                "iso_year": iso_year,
                "iso_week": iso_week,
                "product_count": len(snapshot_df),
                "data": snapshot_json,
            },
        )
    return snapshot_date


def list_abcd_snapshot_dates(engine) -> list[date]:
    ensure_abcd_snapshot_table(engine)
    with engine.connect() as connection:
        rows = connection.execute(
            text(f"SELECT snapshot_date FROM {ABCD_SNAPSHOT_TABLE} ORDER BY snapshot_date DESC")
        ).fetchall()
    return [row[0] for row in rows]


def load_abcd_weekly_snapshot(engine, snapshot_date: date) -> pd.DataFrame:
    ensure_abcd_snapshot_table(engine)
    with engine.connect() as connection:
        row = connection.execute(
            text(f"SELECT data FROM {ABCD_SNAPSHOT_TABLE} WHERE snapshot_date = :snapshot_date"),
            {"snapshot_date": snapshot_date},
        ).fetchone()
    if row is None:
        return pd.DataFrame()
    payload = row[0]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return build_abcd_report_export_df(pd.DataFrame(payload))