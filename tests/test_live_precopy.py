from pathlib import Path

import pytest

from scripts.precopy_staging import audit_source, precopy


def test_live_audit_retains_unknown_images_and_excludes_local_state(tmp_path):
    (tmp_path / "unknown.png").write_bytes(b"nonreusable-original")
    (tmp_path / "history.sqlite3").write_bytes(b"SQLite format 3\0")
    (tmp_path / "history.sqlite3-wal").write_bytes(b"wal")
    (tmp_path / "config.enc").write_bytes(b"private-config-local")
    (tmp_path / "staging").mkdir()
    (tmp_path / "staging" / "excluded.png").write_bytes(b"excluded")
    report = audit_source(tmp_path)
    assert report["files"] == 1
    assert report["bytes"] == len(b"nonreusable-original")
    assert sorted(report["excluded_local_state"]) == ["config.enc", "history.sqlite3", "history.sqlite3-wal"]


def test_live_audit_blocks_unlisted_sqlite_extension(tmp_path):
    (tmp_path / "metadata.data").write_bytes(b"SQLite format 3\0other")
    with pytest.raises(ValueError, match="Unlisted SQLite"):
        audit_source(tmp_path)


def test_live_precopy_rejects_parent_or_aliased_run_before_writing(tmp_path):
    with pytest.raises(ValueError, match="run identifier"):
        precopy("../source", tmp_path, tmp_path, tmp_path, Path("unused"))
    assert list(tmp_path.iterdir()) == []
