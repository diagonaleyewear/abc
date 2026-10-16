from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import json
import os
from uuid import uuid4

import pandas as pd
import pytest
from sqlalchemy import create_engine, text

from abcd_snapshots import (
    ensure_abcd_snapshot_table,
    current_week_start,
    list_abcd_snapshot_refs,
    load_abcd_snapshot_by_id,
    save_abcd_official_snapshot,
    save_abcd_snapshot,
)


@pytest.fixture
def isolated_postgres_engine():
    database_url = os.environ.get("ABCD_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("ABCD_TEST_DATABASE_URL no está configurada")

    admin_engine = create_engine(database_url)
    schema = f"abcd_snapshot_test_{uuid4().hex}"
    test_engine = None
    try:
        with admin_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        test_engine = create_engine(
            database_url,
            connect_args={"options": f"-csearch_path={schema}"},
            pool_size=10,
        )
        yield test_engine
    finally:
        if test_engine is not None:
            test_engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin_engine.dispose()


def test_legacy_migration_preserves_rows_and_is_idempotent(isolated_postgres_engine) -> None:
    legacy_date = date(2026, 10, 5)
    created_at = datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)
    legacy_payload = [{"Marca": "Brand", "Modelo": "Legacy", "Stock": 3}]

    with isolated_postgres_engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE abcd_weekly_snapshots (
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
        connection.execute(
            text(
                """
                INSERT INTO abcd_weekly_snapshots (
                    snapshot_date, iso_year, iso_week, product_count, data, created_at, updated_at
                ) VALUES (
                    :snapshot_date, :iso_year, :iso_week, :product_count,
                    CAST(:data AS jsonb), :created_at, :created_at
                )
                """
            ),
            {
                "snapshot_date": legacy_date,
                "iso_year": legacy_date.isocalendar().year,
                "iso_week": legacy_date.isocalendar().week,
                "product_count": len(legacy_payload),
                "data": json.dumps(legacy_payload),
                "created_at": created_at,
            },
        )

    ensure_abcd_snapshot_table(isolated_postgres_engine)
    ensure_abcd_snapshot_table(isolated_postgres_engine)

    with isolated_postgres_engine.connect() as connection:
        legacy = connection.execute(
            text(
                """
                SELECT snapshot_id, snapshot_date, run_at, engine_version, parameters,
                       data_sha256, product_count, data, capture_kind
                FROM abcd_weekly_snapshots
                """
            )
        ).one()
        primary_keys = connection.execute(
            text(
                """
                SELECT pg_get_constraintdef(oid)
                FROM pg_constraint
                WHERE conrelid = 'abcd_weekly_snapshots'::regclass AND contype = 'p'
                """
            )
        ).scalars().all()

    assert legacy.snapshot_id > 0
    assert legacy.snapshot_date == legacy_date
    assert legacy.run_at == created_at
    assert legacy.engine_version == "legacy"
    assert legacy.parameters == {"legacy": True}
    assert legacy.data_sha256 == ""
    assert legacy.product_count == len(legacy_payload)
    assert legacy.data == legacy_payload
    assert legacy.capture_kind == "manual"
    assert primary_keys == ["PRIMARY KEY (snapshot_id)"]

    added = save_abcd_snapshot(
        isolated_postgres_engine,
        pd.DataFrame([{"Marca": "Brand", "Modelo": "New", "Stock": 4}]),
        legacy_date,
        run_at=created_at + timedelta(hours=1),
        engine_version="test-engine",
        parameters={"reference_date": legacy_date.isoformat()},
    )
    assert added.snapshot_id != legacy.snapshot_id
    assert len(list_abcd_snapshot_refs(isolated_postgres_engine)) == 2


def test_concurrent_official_captures_are_idempotent_and_keep_manual_history(
    isolated_postgres_engine,
) -> None:
    captured_at = datetime.now(ZoneInfo("Europe/Madrid"))
    week_start = current_week_start(captured_at)
    manual = save_abcd_snapshot(
        isolated_postgres_engine,
        pd.DataFrame([{"EAN": "manual", "Stock": 1}]),
        week_start,
        run_at=captured_at,
        engine_version="manual",
    )

    def capture(index: int):
        return save_abcd_official_snapshot(
            isolated_postgres_engine,
            pd.DataFrame([{"EAN": f"official-{index}", "Stock": index}]),
            captured_at=captured_at,
            engine_version=f"official-{index}",
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        captures = list(executor.map(capture, range(8)))

    official_ids = {reference.snapshot_id for reference, _ in captures}
    official_created = sum(created for _, created in captures)
    references = list_abcd_snapshot_refs(isolated_postgres_engine)
    official = [reference for reference in references if reference.capture_kind == "official"]

    assert len(official_ids) == 1
    assert official_created == 1
    assert len(official) == 1
    assert manual.snapshot_id != official[0].snapshot_id
    assert len(references) == 2
    restored, metadata = load_abcd_snapshot_by_id(isolated_postgres_engine, official[0].snapshot_id)
    assert len(restored) == 1
    assert metadata is not None and metadata.capture_kind == "official"


def test_concurrent_captures_receive_unique_ids_and_retain_metadata(
    isolated_postgres_engine,
) -> None:
    snapshot_day = date(2026, 10, 5)
    base_run_at = datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc)

    def capture(index: int):
        return save_abcd_snapshot(
            isolated_postgres_engine,
            pd.DataFrame([{"EAN": f"{index:03}", "Stock": index}]),
            snapshot_day,
            run_at=base_run_at + timedelta(seconds=index),
            engine_version=f"test-engine-{index}",
            parameters={"capture": index},
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        captures = list(executor.map(capture, range(8)))

    snapshot_ids = {capture.snapshot_id for capture in captures}
    assert len(snapshot_ids) == 8
    assert len(list_abcd_snapshot_refs(isolated_postgres_engine)) == 8

    for capture in captures:
        restored, metadata = load_abcd_snapshot_by_id(
            isolated_postgres_engine,
            capture.snapshot_id,
        )
        assert len(restored) == 1
        assert metadata is not None
        assert metadata.engine_version == capture.engine_version
        assert metadata.parameters == capture.parameters
        assert metadata.data_sha256 == capture.data_sha256


def test_corrupt_latest_snapshot_can_recover_previous_verified_capture(
    isolated_postgres_engine,
) -> None:
    snapshot_day = date(2026, 10, 5)
    first = save_abcd_snapshot(
        isolated_postgres_engine,
        pd.DataFrame([{"EAN": "001", "Stock": 1}]),
        snapshot_day,
        run_at=datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc),
        engine_version="test-engine",
        parameters={"capture": "first"},
    )
    latest = save_abcd_snapshot(
        isolated_postgres_engine,
        pd.DataFrame([{"EAN": "002", "Stock": 2}]),
        snapshot_day,
        run_at=datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc),
        engine_version="test-engine",
        parameters={"capture": "latest"},
    )

    with isolated_postgres_engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE abcd_weekly_snapshots
                SET data = CAST(:corrupt_data AS jsonb)
                WHERE snapshot_id = :snapshot_id
                """
            ),
            {"corrupt_data": "[]", "snapshot_id": latest.snapshot_id},
        )

    with pytest.raises(RuntimeError, match="SHA-256"):
        load_abcd_snapshot_by_id(isolated_postgres_engine, latest.snapshot_id)

    recovered, metadata = load_abcd_snapshot_by_id(isolated_postgres_engine, first.snapshot_id)
    assert recovered.loc[0, "EAN"] == "001"
    assert metadata is not None
    assert metadata.snapshot_id == first.snapshot_id
