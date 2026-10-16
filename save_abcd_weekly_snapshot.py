#!/usr/bin/env python3
"""Guarda una foto semanal del analisis ABCD en PostgreSQL."""

from __future__ import annotations

import sys

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.engine import URL

from abcd_snapshots import (
    build_snapshot_parameters,
    current_week_start,
    engine_source_version,
    load_abcd_official_week,
    load_abcd_snapshot_by_id,
    madrid_now,
    save_abcd_official_snapshot,
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
    try:
        with record_process("abc-snapshot-auto") as receipt:
            return _run_snapshot(receipt)
    except Exception as error:
        print(f"Falló la captura semanal ABCD. Código: {type(error).__name__}.", file=sys.stderr)
        return 1


def _run_snapshot(receipt) -> int:
    engine = get_db_engine()
    try:
        captured_at = madrid_now()
        week_start = current_week_start(captured_at)
        iso_year, iso_week, _ = week_start.isocalendar()
        existing = load_abcd_official_week(engine, captured_at)
        if existing is not None:
            snapshot_df, verified = load_abcd_snapshot_by_id(engine, existing.snapshot_id)
            if verified is None or len(snapshot_df) != verified.product_count:
                raise RuntimeError("La captura oficial existente no supera la verificación de lectura")
            print(
                f"Semana ISO {iso_year}-W{iso_week:02}: ya capturada "
                f"| id={verified.snapshot_id} | productos={len(snapshot_df)}"
            )
            receipt["counts"] = {
                "iso_year": iso_year,
                "iso_week": iso_week,
                "snapshot_id": verified.snapshot_id,
                "products": len(snapshot_df),
                "already_captured": 1,
            }
            return 0

        df = load_odoo_dataframe()
        reference_date = pd.Timestamp(captured_at.date())
        snapshot_parameters = build_snapshot_parameters(captured_at)
        df_classified = run_abcd_engine(df.copy(), reference_date=reference_date)
        if ("EAN" not in df_classified.columns or df_classified["EAN"].isna().all()) and "Cód Barras" in df_classified.columns:
            df_classified["EAN"] = df_classified["Cód Barras"]

        snapshot_reference, created = save_abcd_official_snapshot(
            engine,
            df_classified,
            captured_at=captured_at,
            engine_version=engine_source_version(),
            parameters=snapshot_parameters,
        )
        snapshot_df, verified = load_abcd_snapshot_by_id(engine, snapshot_reference.snapshot_id)
        if (
            verified is None
            or verified.data_sha256 != snapshot_reference.data_sha256
            or verified.product_count != len(snapshot_df)
            or (created and len(snapshot_df) != len(df_classified))
        ):
            raise RuntimeError("Snapshot inconsistente: la lectura posterior no coincide con lo calculado")

        outcome = "guardado" if created else "ya capturado por otra ejecución"
        print(
            f"Snapshot ABCD {outcome}: {snapshot_reference.snapshot_date} "
            f"| semana ISO {iso_year}-W{iso_week:02} "
            f"| id={snapshot_reference.snapshot_id} | productos={len(snapshot_df)}"
        )
        receipt["counts"] = {
            "iso_year": iso_year,
            "iso_week": iso_week,
            "snapshot_id": snapshot_reference.snapshot_id,
            "products": len(snapshot_df),
            "already_captured": int(not created),
        }
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
