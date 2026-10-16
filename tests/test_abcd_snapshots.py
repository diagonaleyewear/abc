import ast
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

import pandas as pd
import pytest

from abcd_snapshots import (
    SnapshotReference,
    build_abcd_report_excel_df,
    build_abcd_report_export_df,
    ensure_abcd_snapshot_table,
    current_week_start,
    list_abcd_snapshot_refs,
    load_abcd_snapshot_by_id,
    load_abcd_weekly_snapshot,
    save_abcd_official_snapshot,
    save_abcd_snapshot,
    save_abcd_weekly_snapshot,
    snapshot_week_inventory,
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
            iso_year, iso_week, _ = parameters["snapshot_date"].isocalendar()
            record = {
                    "snapshot_id": len(self.engine.records) + 1,
                    "snapshot_date": parameters["snapshot_date"],
                    "iso_year": iso_year,
                    "iso_week": iso_week,
                    "run_at": parameters["run_at"],
                    "engine_version": parameters["engine_version"],
                    "parameters": parameters["parameters"],
                    "data_sha256": parameters["data_sha256"],
                    "product_count": parameters["product_count"],
                    "data": parameters["data"],
                    "capture_kind": "official" if "'official'" in query else "manual",
                }
            self.engine.records.append(record)
            return FakeResult((record["snapshot_id"],))
        if query.lstrip().startswith("SELECT snapshot_id") and "WHERE iso_year" in query:
            record = next(
                (
                    item for item in self.engine.records
                    if item["iso_year"] == parameters["iso_year"]
                    and item["iso_week"] == parameters["iso_week"]
                    and item["capture_kind"] == "official"
                ),
                None,
            )
            return FakeResult(
                (
                    record["snapshot_id"],
                    record["snapshot_date"],
                    record["run_at"],
                    record["engine_version"],
                    record["parameters"],
                    record["data_sha256"],
                    record["product_count"],
                    record["capture_kind"],
                ) if record else None
            )
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
                    record["capture_kind"],
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
                        item["capture_kind"],
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
            "capture_kind",
            "created_at",
            "updated_at",
        }

    def begin(self):
        return FakeConnection(self)

    def connect(self):
        return FakeConnection(self)

    def dispose(self):
        self.disposed = True


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
    second_reference = save_abcd_snapshot(
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
    assert second_reference.snapshot_date == snapshot_day
    assert first_reference.capture_kind == second_reference.capture_kind == "manual"
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
    save_abcd_snapshot(
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


def test_official_capture_is_idempotent_and_preserves_manual_captures(monkeypatch) -> None:
    engine = FakeEngine()
    captured_at = datetime.now(ZoneInfo("Europe/Madrid"))
    monkeypatch.setattr("abcd_snapshots.madrid_now", lambda: captured_at)
    manual = save_abcd_snapshot(
        engine,
        pd.DataFrame([{"EAN": "manual", "Stock": 1}]),
        run_at=captured_at,
    )

    first, created = save_abcd_official_snapshot(
        engine,
        pd.DataFrame([{"EAN": "official", "Stock": 2}]),
        captured_at=captured_at,
        engine_version="official-engine",
    )
    retry, retry_created = save_abcd_official_snapshot(
        engine,
        pd.DataFrame([{"EAN": "changed-current-data", "Stock": 99}]),
        captured_at=captured_at,
        engine_version="different-engine",
    )
    saved_week = save_abcd_weekly_snapshot(
        engine,
        pd.DataFrame([{"EAN": "ignored", "Stock": 100}]),
        current_week_start(captured_at),
        run_at=captured_at,
    )

    assert created is True
    assert retry_created is False
    assert first == retry
    assert saved_week == first.snapshot_date
    assert first.capture_kind == "official"
    assert manual.capture_kind == "manual"
    assert len(engine.records) == 2


def test_iso_week_start_uses_madrid_calendar_across_iso_year_boundary() -> None:
    madrid = ZoneInfo("Europe/Madrid")
    late_2020 = datetime(2021, 1, 1, 0, 30, tzinfo=madrid)
    first_week_2021 = datetime(2021, 1, 4, 0, 30, tzinfo=madrid)

    assert current_week_start(late_2020) == date(2020, 12, 28)
    assert late_2020.isocalendar()[:2] == (2020, 53)
    assert current_week_start(first_week_2021) == date(2021, 1, 4)
    assert first_week_2021.isocalendar()[:2] == (2021, 1)


def test_week_inventory_uses_iso_year_and_reports_gaps_without_backfilling() -> None:
    refs = [
        SnapshotReference(
            1, date(2020, 12, 28), datetime(2021, 1, 1, tzinfo=timezone.utc),
            "engine", {}, "sha1", 1, "official",
        ),
        SnapshotReference(
            2, date(2021, 1, 11), datetime(2021, 1, 11, tzinfo=timezone.utc),
            "engine", {}, "sha2", 1, "manual",
        ),
    ]

    inventory = snapshot_week_inventory(refs, date(2021, 1, 20))

    assert [(row["Semana ISO"], row["Estado"]) for row in inventory] == [
        ("2020-W53", "Guardada"),
        ("2021-W01", "Ausente"),
        ("2021-W02", "Guardada"),
        ("2021-W03", "Ausente"),
    ]
    assert inventory[0]["Oficiales"] == "1"
    assert inventory[2]["Manuales/legacy"] == "1"


def test_official_api_rejects_backdated_capture(monkeypatch) -> None:
    current = datetime.now(ZoneInfo("Europe/Madrid"))
    monkeypatch.setattr("abcd_snapshots.madrid_now", lambda: current)

    with pytest.raises(ValueError, match="semana pasada o futura"):
        save_abcd_official_snapshot(
            FakeEngine(),
            pd.DataFrame([{"EAN": "current", "Stock": 1}]),
            captured_at=current - pd.Timedelta(days=7),
        )


@pytest.mark.parametrize("failure_stage", ["postgres_read", "postgres_write", "odoo"])
def test_scheduled_cli_records_failures_and_never_writes_partial_history(
    failure_stage, monkeypatch, tmp_path,
) -> None:
    import save_abcd_weekly_snapshot as cli

    engine = FakeEngine()
    captured_at = datetime.now(ZoneInfo("Europe/Madrid"))
    activity_db = tmp_path / "activity.sqlite3"
    monkeypatch.setenv("PROCESS_ACTIVITY_PATH", str(activity_db))
    monkeypatch.setattr(cli, "get_db_engine", lambda: engine)
    monkeypatch.setattr(cli, "madrid_now", lambda: captured_at)
    if failure_stage == "postgres_read":
        monkeypatch.setattr(
            cli,
            "load_abcd_official_week",
            lambda *_: (_ for _ in ()).throw(ConnectionError("postgres unavailable")),
        )
    else:
        monkeypatch.setattr(cli, "load_abcd_official_week", lambda *_: None)
    if failure_stage == "odoo":
        monkeypatch.setattr(
            cli,
            "load_odoo_dataframe",
            lambda: (_ for _ in ()).throw(ConnectionError("odoo unavailable")),
        )
    elif failure_stage == "postgres_write":
        monkeypatch.setattr(cli, "load_odoo_dataframe", lambda: pd.DataFrame([{"Stock": 1}]))
        monkeypatch.setattr(cli, "run_abcd_engine", lambda df, reference_date: df)
        monkeypatch.setattr(
            cli,
            "save_abcd_official_snapshot",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(ConnectionError("postgres unavailable")),
        )

    assert cli.main() == 1
    assert engine.records == []
    assert engine.disposed is True
    from process_activity import read_state
    with read_state(activity_db) as connection:
        receipt = connection.execute(
            "SELECT result, error_code, enabled, interval_seconds, ended FROM process_runs "
            "WHERE process='abc-snapshot-auto'"
        ).fetchone()
    assert receipt["result"] == "Error"
    assert receipt["error_code"] == "ConnectionError"
    assert receipt["enabled"] is None
    assert receipt["interval_seconds"] is None
    assert receipt["ended"] is not None


def test_scheduled_cli_reuses_verified_official_without_reading_odoo(monkeypatch, tmp_path) -> None:
    import save_abcd_weekly_snapshot as cli

    engine = FakeEngine()
    captured_at = datetime.now(ZoneInfo("Europe/Madrid"))
    saved, _ = save_abcd_official_snapshot(
        engine,
        pd.DataFrame([{"EAN": "official", "Stock": 1}]),
        captured_at=captured_at,
    )
    monkeypatch.setenv("PROCESS_ACTIVITY_PATH", str(tmp_path / "activity.sqlite3"))
    monkeypatch.setattr(cli, "get_db_engine", lambda: engine)
    monkeypatch.setattr(cli, "madrid_now", lambda: captured_at)
    monkeypatch.setattr(
        cli,
        "load_odoo_dataframe",
        lambda: (_ for _ in ()).throw(AssertionError("Odoo must not be queried")),
    )

    assert cli.main() == 0
    assert engine.records[0]["snapshot_id"] == saved.snapshot_id


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
    assert "capture_kind" in migration_sql


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
    assert "save_abcd_snapshot" in app_snapshot_imports
    assert cli.save_abcd_official_snapshot is abcd_snapshots.save_abcd_official_snapshot
    assert cli.load_abcd_official_week is abcd_snapshots.load_abcd_official_week
