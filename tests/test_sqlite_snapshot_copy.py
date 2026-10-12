import sqlite3

import pytest

from scripts.snapshot_sqlite_copy import snapshot, digest


def test_snapshot_captures_committed_wal_without_changing_originals(tmp_path):
    source, target = tmp_path / "active.sqlite3", tmp_path / "frozen.sqlite3"
    connection = sqlite3.connect(source)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO items VALUES(1,'synthetic')")
        connection.commit()
        wal = tmp_path / "active.sqlite3-wal"
        before = (digest(source), digest(wal))
        result = snapshot(source, target)
        assert result["tables"] == {"items": 1}
        assert result["original_db_and_wal_unchanged"]
        assert (digest(source), digest(wal)) == before
        restored = sqlite3.connect(target)
        try:
            assert restored.execute("SELECT value FROM items").fetchone()[0] == "synthetic"
        finally:
            restored.close()
        assert not list(tmp_path.glob(".sqlite-scratch-*"))
    finally:
        connection.close()


def test_snapshot_never_replaces_target_or_ignores_journal(tmp_path):
    source, target = tmp_path / "source.sqlite3", tmp_path / "target.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE data(value TEXT)")
    target.write_bytes(b"retained")
    with pytest.raises(ValueError, match="new independent"):
        snapshot(source, target)
    assert target.read_bytes() == b"retained"
    (tmp_path / "source.sqlite3-journal").write_bytes(b"review")
    with pytest.raises(ValueError, match="journal"):
        snapshot(source, tmp_path / "other.sqlite3")


def test_snapshot_rejects_nonlocal_destination(tmp_path, monkeypatch):
    from scripts import snapshot_sqlite_copy as module
    def reject(path):
        raise ValueError("local required")
    monkeypatch.setattr(module, "require_local", reject)
    with pytest.raises(ValueError, match="local"):
        snapshot(tmp_path / "source", tmp_path / "target")


def test_snapshot_name_cannot_collide_with_scratch_output(tmp_path):
    source = tmp_path / "snapshot.sqlite3"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE history(value TEXT)")
    result = snapshot(source, tmp_path / "restored.sqlite3")
    assert result["tables"] == {"history": 0}


def test_changed_source_blocks_publication_and_cleans_scratch(tmp_path, monkeypatch):
    from scripts import snapshot_sqlite_copy as module
    source, target = tmp_path / "original.sqlite3", tmp_path / "frozen.sqlite3"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE history(value TEXT)")
    copy = module.shutil.copyfile

    def copy_then_change(original, destination):
        copy(original, destination)
        with sqlite3.connect(source) as db:
            db.execute("INSERT INTO history VALUES('changed')")

    monkeypatch.setattr(module.shutil, "copyfile", copy_then_change)
    with pytest.raises(ValueError, match="changed during copy"):
        snapshot(source, target)
    assert not target.exists()
    assert not list(tmp_path.glob(".sqlite-scratch-*"))
