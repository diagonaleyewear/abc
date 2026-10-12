from datetime import date

import ast
from pathlib import Path

import pandas as pd

from abcd_snapshots import (
    build_abcd_report_excel_df,
    build_abcd_report_export_df,
    load_abcd_weekly_snapshot,
    save_abcd_weekly_snapshot,
)


class FakeResult:
    def __init__(self, row=None, rows=None) -> None:
        self.row = row
        self.rows = rows or []

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, engine) -> None:
        self.engine = engine

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, statement, parameters=None):
        query = str(statement)
        if query.lstrip().startswith("INSERT INTO"):
            self.engine.snapshot_data = parameters["data"]
            self.engine.snapshot_date = parameters["snapshot_date"]
            return FakeResult()
        if "SELECT data FROM" in query:
            return FakeResult((self.engine.snapshot_data,) if self.engine.snapshot_data else None)
        if "SELECT snapshot_date FROM" in query:
            return FakeResult(rows=[(self.engine.snapshot_date,)] if self.engine.snapshot_date else [])
        return FakeResult()


class FakeEngine:
    def __init__(self) -> None:
        self.snapshot_data = None
        self.snapshot_date = None

    def begin(self):
        return FakeConnection(self)

    def connect(self):
        return FakeConnection(self)


def test_report_export_preserves_canonical_order_and_extra_columns() -> None:
    products = pd.DataFrame(
        [{"extra": "kept", "EAN": "123", "Marca": "Brand", "Modelo": "Model", "Cód Barras": "SKU"}]
    )

    result = build_abcd_report_export_df(products)

    assert result.columns.tolist() == ["Marca", "Modelo", "Cód Barras", "EAN", "extra"]
    excel_result = build_abcd_report_excel_df(products)
    assert excel_result.columns[:4].tolist() == ["Marca", "NOMBRE", "SKU", "EAN"]


def test_snapshot_round_trip_keeps_existing_payload_and_date() -> None:
    engine = FakeEngine()
    products = pd.DataFrame(
        [{"Marca": "Brand", "Modelo": "Model", "Cód Barras": "00123", "Extra": "kept"}]
    )
    snapshot_day = date(2026, 10, 2)

    saved_day = save_abcd_weekly_snapshot(engine, products, snapshot_day)
    restored = load_abcd_weekly_snapshot(engine, snapshot_day)

    assert saved_day == snapshot_day
    assert restored.loc[0, "Cód Barras"] == "00123"
    assert restored.loc[0, "Extra"] == "kept"


def test_ui_and_cli_import_the_shared_snapshot_service() -> None:
    import abcd_snapshots
    import save_abcd_weekly_snapshot as cli

    app_tree = ast.parse(Path("app_enhanced.py").read_text(encoding="utf-8"))
    app_snapshot_imports = {
        alias.name
        for node in app_tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "abcd_snapshots"
        for alias in node.names
    }

    assert "build_abcd_report_export_df" in app_snapshot_imports
    assert "save_abcd_weekly_snapshot" in app_snapshot_imports
    assert cli.build_abcd_report_export_df is abcd_snapshots.build_abcd_report_export_df
    assert cli.save_abcd_weekly_snapshot is abcd_snapshots.save_abcd_weekly_snapshot