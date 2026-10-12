"""Durable, secret-free operational receipts; readers never create state."""
from contextlib import contextmanager, closing
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid
from threading import Event, Thread
from service_stop import request_timeout, StopRequested


def activity_path() -> Path:
    return Path(os.getenv("PROCESS_ACTIVITY_PATH", str(
        Path(__file__).parent / "masterdata_data" / "process_activity.sqlite3")))


@contextmanager
def read_state(path: Path):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        yield connection


@contextmanager
def record_process(process: str, *, enabled: bool | None = None, interval: int | None = None):
    """Called only by existing operations, never by the dashboard."""
    path = activity_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    busy_timeout = request_timeout(30)
    started = time.time()
    run_id = uuid.uuid4().hex
    with closing(sqlite3.connect(path, timeout=busy_timeout)) as connection, connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("""CREATE TABLE IF NOT EXISTS process_runs (
            id TEXT PRIMARY KEY, process TEXT NOT NULL, started REAL NOT NULL,
            ended REAL, result TEXT, counts TEXT NOT NULL DEFAULT '{}', error_code TEXT,
            enabled INTEGER, interval_seconds INTEGER, next_run REAL, heartbeat REAL)""")
        connection.execute("CREATE INDEX IF NOT EXISTS process_started_idx ON process_runs(process,started DESC)")
        connection.execute("INSERT INTO process_runs(id,process,started,enabled,interval_seconds,heartbeat) VALUES(?,?,?,?,?,?)",
                           (run_id, process, started, enabled, interval, started))
    stopped = Event()
    heartbeat_errors = []

    def heartbeat():
        while not stopped.wait(30):
            try:
                with closing(sqlite3.connect(path, timeout=busy_timeout)) as connection, connection:
                    connection.execute("UPDATE process_runs SET heartbeat=? WHERE id=?", (time.time(), run_id))
            except (OSError, sqlite3.Error) as error:
                heartbeat_errors.append(type(error).__name__)
                return

    keeper = Thread(target=heartbeat, daemon=True)
    keeper.start()
    receipt = {"result": "Correcto", "counts": {}}
    error_code = None
    try:
        yield receipt
    except StopRequested:
        receipt["result"] = "Parcial"
        error_code = "StopRequested"
        raise
    except BaseException as error:
        receipt["result"] = "Error"
        error_code = type(error).__name__
        raise
    finally:
        stopped.set()
        keeper.join()
        ended = time.time()
        if heartbeat_errors:
            receipt["result"] = "Error"
            error_code = heartbeat_errors[0]
        counts = receipt["counts"]
        if receipt["result"] not in {"Correcto", "Parcial", "Error"} or not isinstance(counts, dict) or any(
                not isinstance(value, int) or value < 0 for value in counts.values()):
            raise ValueError("Recibo de proceso invalido")
        due = ended + interval if enabled and interval else None
        with closing(sqlite3.connect(path, timeout=busy_timeout)) as connection, connection:
            connection.execute("UPDATE process_runs SET ended=?,result=?,counts=?,error_code=?,next_run=? WHERE id=?",
                               (ended, receipt["result"], json.dumps(counts), error_code, due, run_id))
        if heartbeat_errors:
            raise RuntimeError("No se pudo persistir el latido del proceso")
