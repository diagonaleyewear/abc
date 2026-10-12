#!/usr/bin/env python3
"""Guarda una foto semanal del analisis ABCD en PostgreSQL."""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import URL

from abcd_snapshots import (
    ABCD_SNAPSHOT_TABLE,
    build_abcd_report_export_df,
    current_week_start,
    ensure_abcd_snapshot_table,
    load_abcd_weekly_snapshot,
    save_abcd_weekly_snapshot,
)
from db_config import load_db_config
from db_loader import load_odoo_dataframe
from engine import run_abcd_engine


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
