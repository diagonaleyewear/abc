import ast
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from abcd_snapshots import (
    build_abcd_report_excel_df,
    build_abcd_report_export_df,
    ensure_abcd_snapshot_table,
    list_abcd_snapshot_refs,
    load_abcd_snapshot_by_id,
    load_abcd_weekly_snapshot,
    save_abcd_snapshot,
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
        self.engine.statements.append(query)
        if "information_schema.columns" in query:
            return FakeResult(rows=[(column,) for column in self.engine.columns])
        if query.lstrip().startswith("INSERT INTO"):
            record = {
                    "snapshot_id": len(self.engine.records) + 1,
                    "snapshot_date": parameters["snapshot_date"],
                    "run_at": parameters["run_at"],
                    "engine_version": parameters["engine_version"],
                    "parameters": parameters["parameters"],
                    "data_sha256": parameters["data_sha256"],
                    "product_count": parameters["product_count"],
                    "data": parameters["data"],
                }
            self.engine.records.append(record)
            return FakeResult((record["snapshot_id"],))
        if query.lstrip().startswith("SELECT snapshot_id") and "WHERE snapshot_id" in query:
            record = next(
                (item for item in self.engine.records if item["snapshot_id"] == parameters["snapshot_id"]),
                None,
            )
            if record is None:
                return FakeResult()
            return FakeResult(
                (
                    record["snapshot_id"],
                    record["snapshot_date"],
                    record["run_at"],
                    record["engine_version"],
                    record["parameters"],
                    record["data_sha256"],
                    record["product_count"],
                    record["data"],
                )
            )
        if query.lstrip().startswith("SELECT snapshot_id") and "WHERE snapshot_date" in query:
            matches = [
                item for item in self.engine.records if item["snapshot_date"] == parameters["snapshot_date"]
            ]
            if not matches:
                return FakeResult()
            latest = max(matches, key=lambda item: (item["run_at"], item["snapshot_id"]))
            return FakeResult((latest["snapshot_id"],))
        if query.lstrip().startswith("SELECT snapshot_id"):
            records = sorted(
                self.engine.records,
                key=lambda item: (item["run_at"], item["snapshot_id"]),
                reverse=True,
            )
            return FakeResult(
                rows=[
                    (
                        item["snapshot_id"],
                        item["snapshot_date"],
                        item["run_at"],
                        item["engine_version"],
                        item["parameters"],
                        item["data_sha256"],
                        item["product_count"],
                    )
                    for item in records
                ]
            )
        return FakeResult()


class FakeEngine:
    def __init__(self) -> None:
        self.records = []
        self.statements = []
        self.columns = {
            "snapshot_id",
            "snapshot_date",
            "iso_year",
            "iso_week",
            "product_count",
            "data",
            "run_at",
            "engine_version",
            "parameters",
            "data_sha256",
            "created_at",
            "updated_at",
        }

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


def test_repeated_same_week_snapshots_are_append_only_and_reproducible() -> None:
    engine = FakeEngine()
    products = pd.DataFrame(
        [{"Marca": "Brand", "Modelo": "Model", "Cód Barras": "00123", "Extra": "kept"}]
    )
    snapshot_day = date(2026, 10, 2)

    first_run = datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)
    second_run = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)
    parameters = {"reference_date": "2026-10-02", "days_without_sales_for_d": 120}
    first_reference = save_abcd_snapshot(
        engine,
        products,
        snapshot_day,
        run_at=first_run,
        engine_version="engine-sha-1",
        parameters=parameters,
    )
    saved_day = save_abcd_weekly_snapshot(
        engine,
        products.assign(Stock=2),
        snapshot_day,
        run_at=second_run,
        engine_version="engine-sha-2",
        parameters=parameters,
    )
    references = list_abcd_snapshot_refs(engine)
    restored, metadata = load_abcd_snapshot_by_id(engine, references[-1].snapshot_id)
    latest_legacy_api = load_abcd_weekly_snapshot(engine, snapshot_day)

    assert first_reference.snapshot_id == 1
    assert first_reference.snapshot_date == snapshot_day
    assert saved_day == snapshot_day
    assert len(references) == 2
    assert references[0].engine_version == "engine-sha-2"
    assert metadata is not None
    assert metadata.data_sha256
    assert restored.loc[0, "Cód Barras"] == "00123"
    assert restored.loc[0, "Extra"] == "kept"
    assert latest_legacy_api.loc[0, "Stock"] == 2


def test_corrupted_snapshot_payload_fails_digest_verification() -> None:
    engine = FakeEngine()
    saved_day = date(2026, 10, 2)
    save_abcd_weekly_snapshot(
        engine,
        pd.DataFrame([{"Marca": "Brand", "Stock": 1}]),
        saved_day,
        run_at=datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc),
        engine_version="engine-sha",
        parameters={"reference_date": "2026-10-02"},
    )
    engine.records[0]["data"] = "[]"

    with pytest.raises(RuntimeError, match="SHA-256"):
        load_abcd_snapshot_by_id(engine, 1)


def test_legacy_schema_migration_backfills_capture_metadata_and_changes_primary_key() -> None:
    engine = FakeEngine()
    engine.columns = {
        "snapshot_date",
        "iso_year",
        "iso_week",
        "product_count",
        "data",
        "created_at",
        "updated_at",
    }

    ensure_abcd_snapshot_table(engine)

    migration_sql = "\n".join(engine.statements)
    assert "SET run_at = created_at" in migration_sql
    assert '"legacy": true' in migration_sql
    assert "DROP CONSTRAINT" in migration_sql
    assert "PRIMARY KEY (snapshot_id)" in migration_sql


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
