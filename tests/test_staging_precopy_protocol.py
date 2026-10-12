"""Synthetic rsync protocol checks; no NAS, production paths or services."""
from pathlib import Path
import os
import shutil
import subprocess

import pytest


RSYNC = shutil.which("rsync")
pytestmark = pytest.mark.skipif(os.name != "posix" or RSYNC is None,
                                reason="Requires native POSIX rsync, as on Cloud/CI")


def sync(source: Path, candidate: Path, retained: Path, *, closing=False):
    options = ["-rt", "--backup", f"--backup-dir={retained}", "--exclude=staging/",
               "--exclude=*.sqlite3", "--exclude=*.sqlite3-wal", "--exclude=*.sqlite3-shm",
               "--exclude=*.sqlite3-journal", "--exclude=*.lock"]
    if closing:
        options += ["--checksum", "--delete-delay"]
    subprocess.run([RSYNC, *options, f"{source}/", f"{candidate}/"],
                   check=True, capture_output=True, text=True)


def test_closure_recopies_same_size_time_and_retains_replaced_deleted_versions(tmp_path):
    live, candidate, retained = (tmp_path / name for name in ("live", "candidate", "retained"))
    live.mkdir()
    (live / "123").mkdir()
    changed = live / "123" / "original.png"
    changed.write_bytes(b"old-photo")
    removed = live / "123" / "removed.png"
    removed.write_bytes(b"retain-deleted")
    (live / "ambiguous.png").write_bytes(b"invalid-original")
    (live / "history.sqlite3").write_bytes(b"SQLite-local-only")
    (live / "staging").mkdir()
    (live / "staging" / "private.png").write_bytes(b"excluded")
    sync(live, candidate, retained / "precopy")
    timestamp = changed.stat().st_mtime_ns
    changed.write_bytes(b"new-photo")
    os.utime(changed, ns=(timestamp, timestamp))
    removed.unlink()
    (live / "123" / "new.png").write_bytes(b"added")
    sync(live, candidate, retained / "closing", closing=True)
    assert (candidate / "123" / "original.png").read_bytes() == b"new-photo"
    assert (candidate / "123" / "new.png").read_bytes() == b"added"
    assert not (candidate / "123" / "removed.png").exists()
    assert (retained / "closing" / "123" / "original.png").read_bytes() == b"old-photo"
    assert (retained / "closing" / "123" / "removed.png").read_bytes() == b"retain-deleted"
    assert (candidate / "ambiguous.png").read_bytes() == b"invalid-original"
    assert not (candidate / "history.sqlite3").exists()
    assert not (candidate / "staging").exists()
    assert changed.read_bytes() == b"new-photo"


def test_quick_check_alone_does_not_prove_coherence(tmp_path):
    live, candidate = tmp_path / "live", tmp_path / "candidate"
    live.mkdir()
    photo = live / "same.png"
    photo.write_bytes(b"AAAA")
    sync(live, candidate, tmp_path / "retained" / "first")
    timestamp = photo.stat().st_mtime_ns
    photo.write_bytes(b"BBBB")
    os.utime(photo, ns=(timestamp, timestamp))
    sync(live, candidate, tmp_path / "retained" / "quick")
    assert (candidate / "same.png").read_bytes() == b"AAAA"
    sync(live, candidate, tmp_path / "retained" / "verified", closing=True)
    assert (candidate / "same.png").read_bytes() == b"BBBB"
