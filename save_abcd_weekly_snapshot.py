#!/usr/bin/env python3
"""Guarda una foto semanal del analisis ABCD en PostgreSQL."""

from __future__ import annotations

from datetime import date
import json

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

from db_config import load_db_config
from db_loader import load_odoo_dataframe
from engine import run_abcd_engine


ABCD_SNAPSHOT_TABLE = "abcd_weekly_snapshots"


def get_db_engine():
    config = load_db_config()
    return create_engine(
        URL.create(
            "postgresql+psycopg2",
            username=config.user,
            password=config.password,
            host=config.host,
            port=config.port,
            database=config.database,
        )
    )


def build_abcd_report_export_df(df: pd.DataFrame) -> pd.DataFrame:
    preferred_columns = [
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
    ordered_columns = [column for column in preferred_columns if column in df.columns]
    remaining_columns = [column for column in df.columns if column not in ordered_columns]
    return df[ordered_columns + remaining_columns]


def current_week_start() -> date:
    today = pd.Timestamp.today().normalize()
    return (today - pd.Timedelta(days=today.weekday())).date()


def ensure_abcd_snapshot_table(engine) -> None:
    with engine.begin() as conn:
        conn.execute(
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


def save_abcd_weekly_snapshot(engine, df: pd.DataFrame, snapshot_date: date | None = None) -> date:
    snapshot_date = snapshot_date or current_week_start()
    iso_year, iso_week, _ = snapshot_date.isocalendar()
    snapshot_df = build_abcd_report_export_df(df.copy())
    snapshot_json = snapshot_df.to_json(orient="records", date_format="iso", force_ascii=False)

    ensure_abcd_snapshot_table(engine)
    with engine.begin() as conn:
        conn.execute(
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


def load_abcd_weekly_snapshot(engine, snapshot_date: date) -> pd.DataFrame:
    ensure_abcd_snapshot_table(engine)
    with engine.connect() as conn:
        row = conn.execute(
            text(f"SELECT data FROM {ABCD_SNAPSHOT_TABLE} WHERE snapshot_date = :snapshot_date"),
            {"snapshot_date": snapshot_date},
        ).fetchone()
    if row is None:
        return pd.DataFrame()
    payload = row[0]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return build_abcd_report_export_df(pd.DataFrame(payload))


def main() -> int:
    from process_activity import record_process
    with record_process("abc-snapshot") as receipt:
        return _run_snapshot(receipt)


def _run_snapshot(receipt) -> int:
    engine = get_db_engine()
    df = load_odoo_dataframe()
    df_classified = run_abcd_engine(df.copy())
    if ("EAN" not in df_classified.columns or df_classified["EAN"].isna().all()) and "Cód Barras" in df_classified.columns:
        df_classified["EAN"] = df_classified["Cód Barras"]

    snapshot_date = save_abcd_weekly_snapshot(engine, df_classified)
    snapshot_df = load_abcd_weekly_snapshot(engine, snapshot_date)
    if len(snapshot_df) != len(df_classified):
        raise RuntimeError(
            f"Snapshot inconsistente: {len(snapshot_df)} guardados vs {len(df_classified)} calculados"
        )

    print(f"Snapshot ABCD guardado: {snapshot_date} | productos={len(snapshot_df)}")
    receipt["counts"] = {"Productos": len(snapshot_df)}
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
