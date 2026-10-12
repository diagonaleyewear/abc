from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import threading
import time
import multiprocessing
import os

from filelock import FileLock, Timeout
import pytest

import order_alerts
from order_alerts import OrderAlertStore
from scripts.smoke_order_alerts import concurrent_alerts


def crashed_initializer(database, ready):
    with FileLock(database + ".init.lock"):
        ready.set()
        os._exit(17)


def test_independent_processes_initialize_and_send_once(tmp_path):
    outcomes = concurrent_alerts(tmp_path, actors=6)
    assert sum(sent for sent, _ in outcomes) == 1
    assert all(error is None for _, error in outcomes)
    assert (tmp_path / "email-ledger.txt").read_text().splitlines() == ["synthetic-order"]
    assert all(not sent and error is None for sent, error in concurrent_alerts(tmp_path))


def test_independent_process_recovers_failed_send_without_duplicate(tmp_path):
    assert concurrent_alerts(tmp_path, actors=1, fail_send=True) == [(False, "synthetic email failure")]
    assert not (tmp_path / "email-ledger.txt").exists()
    assert concurrent_alerts(tmp_path, actors=1) == [(True, None)]
    assert concurrent_alerts(tmp_path, actors=2) == [(False, None), (False, None)]
    assert (tmp_path / "email-ledger.txt").read_text().splitlines() == ["synthetic-order"]


def test_external_sqlite_lock_is_retried_then_recovers(tmp_path, caplog, monkeypatch):
    database = tmp_path / "alerts.sqlite3"
    retry_seen = threading.Event()
    warning = order_alerts.LOGGER.warning

    def observed_warning(*args):
        warning(*args)
        retry_seen.set()

    monkeypatch.setattr(order_alerts.LOGGER, "warning", observed_warning)
    with closing(sqlite3.connect(database)) as blocker:
        blocker.execute("CREATE TABLE external(value TEXT)")
        blocker.execute("BEGIN EXCLUSIVE")
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(OrderAlertStore, database)
            assert retry_seen.wait(5)
            blocker.rollback()
            store = future.result(timeout=5)
    assert "bounded retry" in caplog.text
    with store._connect() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


def test_external_lock_timeout_propagates_and_next_attempt_recovers(tmp_path, monkeypatch):
    monkeypatch.setattr(order_alerts, "SQLITE_INITIALIZATION_TIMEOUT", 0.2)
    database = tmp_path / "alerts.sqlite3"
    with closing(sqlite3.connect(database)) as blocker:
        blocker.execute("CREATE TABLE external(value TEXT)")
        blocker.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            OrderAlertStore(database)
        assert time.monotonic() - started < 1
        blocker.rollback()
    assert OrderAlertStore(database).get_status("auto") is None


def test_initialization_lock_timeout_and_release(tmp_path, monkeypatch):
    monkeypatch.setattr(order_alerts, "SQLITE_INITIALIZATION_TIMEOUT", 0.1)
    database = tmp_path / "alerts.sqlite3"
    with FileLock(str(database) + ".init.lock"):
        with pytest.raises(Timeout):
            OrderAlertStore(database)
    assert OrderAlertStore(database).claim_orders([1]).order_ids == (1,)


def test_retry_deadline_keeps_original_sqlite_error(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(order_alerts.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(order_alerts.time, "sleep", lambda _delay: clock.__setitem__(0, 11.0))
    busy = sqlite3.OperationalError("database is locked")
    busy.sqlite_errorcode = sqlite3.SQLITE_BUSY

    def blocked_schema(_connection):
        raise busy

    monkeypatch.setattr(OrderAlertStore, "_initialize_schema", staticmethod(blocked_schema))
    with pytest.raises(sqlite3.OperationalError) as caught:
        OrderAlertStore(tmp_path / "alerts.sqlite3")
    assert caught.value is busy


def test_process_death_does_not_leave_initialization_locked(tmp_path):
    database = tmp_path / "alerts.sqlite3"
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    worker = context.Process(target=crashed_initializer, args=(str(database), ready))
    worker.start()
    try:
        assert ready.wait(5)
        worker.join(5)
        assert worker.exitcode == 17
        assert OrderAlertStore(database).claim_orders([7]).order_ids == (7,)
    finally:
        if worker.is_alive():
            worker.terminate()
            worker.join(5)


def test_non_busy_failure_is_not_retried_and_lock_released(tmp_path, monkeypatch):
    database = tmp_path / "alerts.sqlite3"
    database.write_bytes(b"not a SQLite database")
    with pytest.raises(sqlite3.DatabaseError):
        OrderAlertStore(database)
    # Only the synthetic corrupt fixture is replaced; production is never repaired automatically.
    database.unlink()
    assert OrderAlertStore(database).get_status("auto") is None


def test_paths_resolve_to_one_initialization_lock(tmp_path):
    store = OrderAlertStore(tmp_path / "nested" / ".." / "alerts.sqlite3")
    assert store.database_path == Path(tmp_path / "alerts.sqlite3").resolve()
