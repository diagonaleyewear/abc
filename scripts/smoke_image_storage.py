"""Artifact smoke: synthetic data only, no providers, production mounts or secrets."""
from io import BytesIO
import os
from pathlib import Path
import tempfile
import sqlite3
from zipfile import ZipFile

from PIL import Image
from playwright.sync_api import sync_playwright

import graph_mail_downloader as graph
from image_repository import ImageRepository
import migrate_image_repository as migration
from scripts.snapshot_sqlite_copy import snapshot, digest
from scripts.smoke_order_alerts import concurrent_alerts


def smoke():
    assert os.getuid() == 1037 and os.getgid() == 100
    assert os.getgroups() == [100]
    assert os.umask(0o077) == 0o077
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content("<title>Cloud synthetic smoke</title>")
        assert page.title() == "Cloud synthetic smoke"
        browser.close()
    with tempfile.TemporaryDirectory(prefix="cloud-storage-smoke-",
                                     dir=os.getenv("CLOUD_SMOKE_DIRECTORY")) as folder:
        root = Path(folder)
        for key, value in {
            "IMAGE_REPOSITORY_ROOT": root / "images",
            "IMAGE_REPOSITORY_STATE_ROOT": root / "state",
            "IMAGE_REPOSITORY_BACKUP_ROOT": root / "backups",
            "IMAGE_REPOSITORY_WORK_ROOT": root / "work",
        }.items():
            os.environ[key] = str(value)
        os.environ["GRAPH_IMAGE_MAX_ARCHIVE_BYTES"] = "1048576"
        os.environ["IMAGE_REPOSITORY_WORK_RESERVE_BYTES"] = "1048576"
        graph._load_eans_by_image_key = lambda: {"imagekey": "0012345678901"}
        migration._load_eans_by_image_key = lambda: {}
        repository = ImageRepository()
        alerts = root / "alerts"
        alerts.mkdir()
        outcomes = concurrent_alerts(alerts)
        assert sum(sent for sent, _ in outcomes) == 1
        assert all(error is None for _, error in outcomes)
        assert (alerts / "email-ledger.txt").read_text().splitlines() == ["synthetic-order"]
        assert all(not sent and error is None for sent, error in concurrent_alerts(alerts))
        buffer = BytesIO()
        Image.new("RGB", (600, 600), (10, 20, 30)).save(buffer, "PNG")
        source = root / "source"
        (source / "0012345678901").mkdir(parents=True)
        (source / "0012345678901" / "original.png").write_bytes(buffer.getvalue())
        with ZipFile(source / "originals.zip", "w") as archive:
            archive.writestr("original.png", buffer.getvalue())
        plan = migration.inventory([("Other", source)], repository.root)
        assert migration.apply(plan)["verified"] == 2
        assert migration.apply(plan)["verified"] == 2
        assert migration.recover(plan, root / "restore")["recovered"] == 2
        assert repository.database.parent == root / "state"
        assert not list(repository.root.glob("*.sqlite3"))
        with sqlite3.connect(root / "state" / "wal-probe.sqlite3") as connection:
            assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
            connection.execute("CREATE TABLE probe(value TEXT)")
            connection.execute("INSERT INTO probe VALUES('synthetic')")
            connection.commit()
            original = root / "state" / "wal-probe.sqlite3"
            wal = root / "state" / "wal-probe.sqlite3-wal"
            before = (digest(original), digest(wal))
            recovered = root / "state" / "restored.sqlite3"
            assert snapshot(original, recovered)["tables"] == {"probe": 1}
            assert (digest(original), digest(wal)) == before
            assert recovered.stat().st_uid == 1037 and recovered.stat().st_gid == 100
            assert recovered.stat().st_mode & 0o077 == 0
            restored = sqlite3.connect(recovered)
            try:
                assert restored.execute("SELECT value FROM probe").fetchone()[0] == "synthetic"
            finally:
                restored.close()
            for suffix in ("", "-wal", "-shm"):
                path = root / "state" / ("wal-probe.sqlite3" + suffix)
                assert path.is_file()
                assert path.stat().st_uid == 1037 and path.stat().st_gid == 100
                assert path.stat().st_mode & 0o077 == 0
        with graph.incoming_lock(repository.root) as incoming:
            saved = graph._save_streamed_attachment([b"synthetic archive"], incoming, "received.zip")
            assert saved == root / "work" / "received.zip"
            assert saved.stat().st_uid == 1037 and saved.stat().st_gid == 100
            assert saved.stat().st_mode & 0o077 == 0
        os.environ["IMAGE_REPOSITORY_NFS_SOURCE"] = "synthetic:/not-mounted"
        absent = root / "absent"
        try:
            ImageRepository(absent)
        except RuntimeError:
            pass
        else:
            raise AssertionError("Missing NFS gate accepted")
        assert not absent.exists()
    print("Nonroot 1037:100 artifact storage smoke passed: isolated state, SQLite/WAL/SHM, backups, streamed archives, "
          "migration/recovery, concurrent alert processes, consistent WAL snapshot/local recovery "
          "and missing-mount gate; synthetic data only")


if __name__ == "__main__":
    smoke()
