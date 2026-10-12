"""Snapshot a quiesced SQLite DB through local scratch; never open the original."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile

from repository_storage import require_local


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def snapshot(source: Path, target: Path) -> dict:
    require_local(source)
    require_local(target.parent)
    if source.resolve() == target.resolve() or target.exists() or target.is_symlink():
        raise ValueError("Snapshot requires a new independent local file")
    if not target.parent.is_dir() or target.parent.resolve() != target.parent:
        raise ValueError("Local snapshot parent missing or aliased")
    journal = Path(str(source) + "-journal")
    if journal.exists() and journal.stat().st_size:
        raise ValueError("Rollback journal present; quiescence/recovery must be reviewed")
    originals = [source]
    wal = Path(str(source) + "-wal")
    if wal.exists():
        originals.append(wal)
    before = {path: digest(path) for path in originals}
    with tempfile.TemporaryDirectory(prefix=".sqlite-scratch-", dir=target.parent) as folder:
        scratch = Path(folder) / "input"
        scratch.mkdir(mode=0o700)
        local = scratch / source.name
        for path in originals:
            shutil.copyfile(path, scratch / path.name)
        if {path: digest(path) for path in originals} != before or wal.exists() != (wal in originals):
            raise ValueError("SQLite changed during copy; stop all writers before capture")
        if any(digest(scratch / path.name) != before[path] for path in originals):
            raise ValueError("Scratch copy checksum mismatch")
        temporary = Path(folder) / "snapshot.sqlite3"
        with closing(sqlite3.connect(local)) as copied, closing(sqlite3.connect(temporary)) as output:
            copied.backup(output)
            if [row[0] for row in output.execute("PRAGMA integrity_check")] != ["ok"]:
                raise ValueError("SQLite snapshot integrity check failed")
            if output.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("SQLite snapshot foreign key check failed")
            tables = [row[0] for row in output.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            counts = {name: output.execute('SELECT COUNT(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
                      for name in tables}
        if {path: digest(path) for path in originals} != before or wal.exists() != (wal in originals):
            raise ValueError("SQLite changed before snapshot publication")
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        # A hard link publishes without replacing a preexisting destination.
        os.link(temporary, target)
        if os.name == "posix":
            directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    return {"checksum": digest(target), "tables": counts, "integrity": "ok",
            "original_db_and_wal_unchanged": True, "quiescence_still_required": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        print(json.dumps(snapshot(args.source, args.target), indent=2))
    except (OSError, ValueError, sqlite3.Error) as error:
        parser.exit(1, f"Snapshot blocked: {error}\n")
