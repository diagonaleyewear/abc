"""Offline monitor lifecycle probes; synthetic state only, no provider access."""
import argparse
import base64
from contextlib import contextmanager, closing
from datetime import datetime, timezone
from io import BytesIO
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time

from PIL import Image


def wait_file(path: Path, timeout: float = 10):
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Synthetic probe deadline: {path.name}")
        time.sleep(0.01)


def install_synthetic(root: Path, scenario: str):
    import graph_mail_downloader as graph
    from image_repository import ImageRepository

    for key in ("M365_CLIENT_SECRET", "DB_PASSWORD", "KERING_ENCRYPTION_KEY"):
        if os.getenv(key):
            raise RuntimeError("Lifecycle smoke must not receive production secrets")
    for key, part in (("IMAGE_REPOSITORY_ROOT", "images"), ("IMAGE_REPOSITORY_STATE_ROOT", "state"),
                      ("IMAGE_REPOSITORY_WORK_ROOT", "work")):
        os.environ[key] = str(root / part)
    os.environ["PROCESS_ACTIVITY_PATH"] = str(root / "activity.sqlite3")
    os.environ["GRAPH_IMAGE_MAX_ARCHIVE_BYTES"] = "1048576"
    os.environ["IMAGE_REPOSITORY_WORK_RESERVE_BYTES"] = "1048576"
    for key in ("IMAGE_REPOSITORY_NFS_SOURCE", "IMAGE_REPOSITORY_WORK_NFS_SOURCE"):
        os.environ.pop(key, None)
    root.mkdir(parents=True, exist_ok=True)
    graph.load_m365_config = lambda: graph.M365Config("synthetic", "synthetic", "synthetic", "offline", root / "images")
    graph._get_access_token = lambda config: "synthetic"
    graph._refresh_pending_market_images = lambda root: (0, 0)
    graph._get_market_ids_by_ean = lambda eans: {}
    graph._load_eans_by_image_key = lambda: {("MODEL", "COLOR"): "0012345678901"}
    buffer = BytesIO()
    Image.new("RGB", (600, 600), (10, 20, 30)).save(buffer, "PNG")
    content = buffer.getvalue()
    original_repository = ImageRepository()
    restarting = (root / "restart").exists()
    children = []

    def inbox(*args):
        if scenario == "idle":
            (root / "ready").touch()
            return []
        return [{"id": "synthetic-message", "subject": "image", "hasAttachments": True,
                 "receivedDateTime": datetime.now(timezone.utc).isoformat(), "isRead": False,
                 "from": {"emailAddress": {"address": "luxottica@example.invalid"}}}]

    graph._list_inbox_messages = inbox
    graph._list_attachments = lambda *args: [
        {"id": "synthetic-attachment", "name": "MODEL__COLOR_000A.png",
         "@odata.type": "#microsoft.graph.fileAttachment",
         **({} if scenario == "download" else {"contentBytes": base64.b64encode(content).decode()})}]

    def mark_read(*args):
        with (root / "acknowledged").open("a") as stream:
            stream.write("synthetic-message\n")
        if scenario == "ack-failure" and not restarting:
            (root / "ready").touch()
            raise OSError("Synthetic acknowledgement failure after durable completion")

    graph._mark_message_read = mark_read

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            (root / "response-closed").touch()

        def raise_for_status(self):
            pass

        def iter_content(self, size):
            yield content[:16]
            if not restarting:
                (root / "ready").touch()
                wait_file(root / "release")
            yield content[16:]

    def offline_get(*args, **kwargs):
        if scenario != "download":
            raise AssertionError("Unexpected network call in offline probe")
        assert kwargs["stream"] and kwargs["timeout"] == 5
        return Response()

    graph.requests.get = offline_get
    graph.requests.post = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("External send forbidden"))

    original_connect = ImageRepository.connect

    @contextmanager
    def sqlite_connect(repository):
        with original_connect(repository) as connection:
            class Proxy:
                def execute(self, sql, *args):
                    if sql == "BEGIN IMMEDIATE" and not restarting and not children:
                        child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--sqlite-holder",
                                                  str(repository.database), "--directory", str(root)],
                                                 env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])})
                        children.append(child)
                        wait_file(root / "sqlite-locked")
                        (root / "ready").touch()
                    return connection.execute(sql, *args)

                def __getattr__(self, name):
                    return getattr(connection, name)
            yield Proxy()

    if scenario == "sqlite":
        ImageRepository.connect = sqlite_connect

    original_fsync = os.fsync
    blocked = False

    def nfs_fsync(descriptor):
        nonlocal blocked
        if not restarting and not blocked:
            target = os.readlink(f"/proc/self/fd/{descriptor}")
            if "/.image-" in target and "/images/" in target:
                blocked = True
                (root / "ready").touch()
                wait_file(root / "release", timeout=40)
        return original_fsync(descriptor)

    if scenario == "nfs":
        os.fsync = nfs_fsync

    def cleanup():
        os.fsync = original_fsync
        for child in children:
            code = child.wait(timeout=5)
            if code != 0:
                raise RuntimeError(f"Synthetic SQLite child failed: {code}")
        with closing(sqlite3.connect(original_repository.database)) as connection:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Synthetic catalog integrity failure")

    return cleanup


def exercise(root: Path, scenario: str) -> dict:
    root.mkdir()
    command = [sys.executable, str(Path(__file__).resolve()), "--worker", scenario, "--directory", str(root)]
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    with (root / "worker.log").open("w") as log:
        worker = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=environment)
        try:
            deadline = time.monotonic() + 20
            while not (root / "ready").exists():
                if worker.poll() is not None:
                    raise AssertionError(f"Probe startup exit={worker.returncode}: {(root / 'worker.log').read_text()}")
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Probe ready deadline: {(root / 'worker.log').read_text()}")
                time.sleep(0.01)
            time.sleep(0.15)
            started = time.monotonic()
            worker.send_signal(signal.SIGTERM)
            time.sleep(0.25)
            (root / "release").touch()
            code = worker.wait(timeout=10)
            elapsed = time.monotonic() - started
            if code != 0:
                raise AssertionError(f"{scenario} stop exit={code}: {(root / 'worker.log').read_text()}")
            if elapsed >= 8:
                raise AssertionError(f"{scenario} stop exceeded8s")
        finally:
            (root / "release").touch(exist_ok=True)
            if worker.poll() is None:
                worker.kill()
                worker.wait(timeout=5)
    with closing(sqlite3.connect(root / "activity.sqlite3")) as connection:
        assert connection.execute("SELECT COUNT(*) FROM process_runs WHERE ended IS NULL").fetchone()[0] == 0
    assert not list((root / "work").rglob("*.part"))
    (root / "restart").touch()
    restarted = subprocess.run([*command, "--once"], capture_output=True, text=True, timeout=20, env=environment)
    assert restarted.returncode == 0, restarted.stdout + restarted.stderr
    if scenario != "idle":
        from image_repository import ImageRepository
        repository = ImageRepository(root / "images", state=root / "state", backups=root / "backups")
        records = repository.records()
        assert len(records) == 1, restarted.stdout + restarted.stderr
        assert records[0].name == "MODEL__COLOR_000A.png"
        assert repository.verified_bytes(records[0])
        state = json.loads((root / "state" / ".mail_download_state.json").read_text())
        assert state["processed_message_ids"] == ["synthetic-message"]
        assert state["pending_read_ids"] == []
        assert (root / "acknowledged").read_text().splitlines() == ["synthetic-message"]
        if scenario == "download":
            assert (root / "response-closed").exists()
    return {"scenario": scenario, "exit": code, "stop_seconds": round(elapsed, 3),
            "restart": "ok", "unfinished_receipts": 0, "duplicates": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", choices=["idle", "download", "sqlite", "nfs", "ack-failure"])
    parser.add_argument("--sqlite-holder")
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--wait-ready", action="store_true")
    parser.add_argument("--release", action="store_true")
    args = parser.parse_args()
    if args.wait_ready:
        wait_file(args.directory / "ready", timeout=20)
        time.sleep(0.15)
    elif args.release:
        time.sleep(0.25)
        (args.directory / "release").touch()
    elif args.sqlite_holder:
        with closing(sqlite3.connect(args.sqlite_holder)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            (args.directory / "sqlite-locked").touch()
            wait_file(args.directory / "release")
            connection.rollback()
    elif args.worker:
        cleanup = install_synthetic(args.directory, args.worker)
        import poll_luxoptica_mail
        sys.argv = ["poll_luxoptica_mail.py", "--interval-minutes", "10",
                    "--sender-hint", "luxottica", "--subject-hint", "image",
                    "--lookback-days", "30", "--top-messages", "100", *(["--once"] if args.once else [])]
        try:
            raise SystemExit(poll_luxoptica_mail.main())
        finally:
            cleanup()
    else:
        args.directory.mkdir(parents=True, exist_ok=True)
        for scenario in ("idle", "download", "sqlite", "nfs"):
            print(json.dumps(exercise(args.directory / scenario, scenario)), flush=True)
