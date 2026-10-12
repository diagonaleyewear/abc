from datetime import datetime, timezone
import json
import os
from threading import Event
from concurrent.futures import ThreadPoolExecutor
import time

from filelock import FileLock
import pytest

import graph_mail_downloader as graph
from scripts.smoke_luxoptica_stop import exercise
from service_stop import StopRequested, stopping_with, checkpoint, acquire, request_timeout, database_options
import poll_luxoptica_mail as monitor


@pytest.mark.parametrize("failure", [False, True])
def test_main_handles_stop_and_restores_handlers_without_hiding_errors(monkeypatch, failure):
    handlers = {}
    changes = []
    previous = object()

    def signal_handler(sig, handler):
        changes.append(handler)
        handlers[sig] = handler
        return previous

    @contextmanager
    def receipt(*args, **kwargs):
        yield {"counts": {}}

    def run_once(**kwargs):
        handlers[monitor.signal.SIGTERM](monitor.signal.SIGTERM, None)
        if failure:
            raise OSError("Synthetic storage failure during drain")
        return {"messages_scanned": 0, "messages_with_attachments": 0, "attachments_downloaded": 0,
                "saved_paths": [], "processed_message_ids": []}

    monkeypatch.setattr(monitor.signal, "signal", signal_handler)
    monkeypatch.setattr(monitor, "record_process", receipt)
    monkeypatch.setattr(monitor, "run_once", run_once)
    monkeypatch.setattr("sys.argv", ["poll_luxoptica_mail.py"])
    assert monitor.main() == (1 if failure else 0)
    assert changes[-2:] == [previous, previous]


def test_exceeded_drain_budget_never_reports_clean_stop(monkeypatch):
    handlers = {}
    monkeypatch.setattr(monitor.signal, "signal", lambda sig, handler: handlers.setdefault(sig, handler))
    clock = iter([0, 31])
    monkeypatch.setattr(monitor.time, "monotonic", lambda: next(clock))

    def cycle(args, interval, stopping):
        handlers[monitor.signal.SIGTERM](monitor.signal.SIGTERM, None)
        return 0

    monkeypatch.setattr(monitor, "monitor", cycle)
    monkeypatch.setattr("sys.argv", ["poll_luxoptica_mail.py"])
    assert monitor.main() == 1


@pytest.mark.skipif(os.name != "posix", reason="Real SIGTERM subprocess lifecycle requires POSIX")
@pytest.mark.parametrize("scenario", ["idle", "download", "sqlite", "nfs"])
def test_real_sigterm_drain_and_restart(tmp_path, scenario):
    result = exercise(tmp_path / scenario, scenario)
    assert result["exit"] == 0
    assert result["stop_seconds"] < 8
    assert result["duplicates"] == 0


def test_stop_context_is_scoped_and_lock_is_released(tmp_path):
    stopping = Event()
    assert request_timeout(120) == 120 and database_options() == {}
    with stopping_with(stopping):
        assert request_timeout(120) == 5
        with acquire(FileLock(tmp_path / "probe.lock", timeout=5)):
            stopping.set()
        with pytest.raises(StopRequested):
            checkpoint()
    checkpoint()
    with FileLock(tmp_path / "probe.lock", timeout=0):
        pass


def test_stop_interrupts_contended_lock_without_stealing_it(tmp_path):
    stopping = Event()
    path = tmp_path / "locked"

    def wait_for_lock():
        with stopping_with(stopping), acquire(FileLock(path, timeout=120)):
            pytest.fail("Acquired another process's active lock")

    with FileLock(path, timeout=0), ThreadPoolExecutor() as executor:
        future = executor.submit(wait_for_lock)
        time.sleep(0.05)
        started = time.monotonic()
        stopping.set()
        with pytest.raises(StopRequested):
            future.result(timeout=1)
        assert time.monotonic() - started < 1


def test_cancelled_stream_cleans_partial_and_preserves_previous(tmp_path, monkeypatch):
    root = tmp_path / "images"
    work = tmp_path / "work"
    root.mkdir()
    work.mkdir()
    monkeypatch.setenv("IMAGE_REPOSITORY_ROOT", str(root))
    monkeypatch.setenv("IMAGE_REPOSITORY_WORK_ROOT", str(work))
    monkeypatch.setenv("IMAGE_REPOSITORY_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("GRAPH_IMAGE_MAX_ARCHIVE_BYTES", "1024")
    monkeypatch.setenv("IMAGE_REPOSITORY_WORK_RESERVE_BYTES", "1024")
    existing = work / "received.zip"
    existing.write_bytes(b"previous")
    stopping = Event()

    def chunks():
        yield b"partial"
        stopping.set()
        yield b"remaining"

    with stopping_with(stopping), pytest.raises(StopRequested):
        graph._save_streamed_attachment(chunks(), work, existing.name)
    assert existing.read_bytes() == b"previous"
    assert not list(work.glob("*.part"))


def test_completed_mail_saved_before_ack_failure_and_not_processed_twice(tmp_path, monkeypatch):
    root = tmp_path / "images"
    state = tmp_path / "state"
    root.mkdir()
    monkeypatch.setenv("IMAGE_REPOSITORY_STATE_ROOT", str(state))
    monkeypatch.setenv("IMAGE_REPOSITORY_ROOT", str(root))
    monkeypatch.setattr(graph, "load_m365_config", lambda: graph.M365Config("test", "test", "test", "test", root))
    monkeypatch.setattr(graph, "_get_access_token", lambda config: "test")
    monkeypatch.setattr(graph, "_refresh_pending_market_images", lambda root: (0, 0))
    monkeypatch.setattr(graph, "_list_inbox_messages", lambda *args: [{
        "id": "test", "subject": "image", "hasAttachments": True, "isRead": False,
        "receivedDateTime": datetime.now(timezone.utc).isoformat()}])
    listed = []
    monkeypatch.setattr(graph, "_list_attachments", lambda *args: listed.append("test") or [
        {"@odata.type": "#microsoft.graph.fileAttachment"}])
    def fail(*args):
        raise OSError("ack failed")
    monkeypatch.setattr(graph, "_mark_message_read", fail)
    with pytest.raises(OSError, match="ack failed"):
        graph._download_luxoptica_mail_attachments("", "image", 7, 100)
    saved = json.loads((state / ".mail_download_state.json").read_text())
    assert saved == {"processed_message_ids": ["test"], "pending_read_ids": ["test"]}
    acknowledged = []
    monkeypatch.setattr(graph, "_mark_message_read", lambda *args: acknowledged.append("test"))
    graph._download_luxoptica_mail_attachments("", "image", 7, 100)
    assert listed == ["test"]
    assert acknowledged == ["test"]
    assert json.loads((state / ".mail_download_state.json").read_text())["pending_read_ids"] == []
from contextlib import contextmanager
