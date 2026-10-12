"""Authorized live precopy only; never stops services or promotes a migration source."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import subprocess

from repository_storage import require_local
from scripts.nfs_repository_guard import require_nfs_directory, EXPECTED_SOURCE
from scripts.verify_staging_exports import verify_layout


ROLES = ("Luxoptica", "Kering", "Common", "Luxoptica-manifests", "Luxoptica-docs")
EXCLUDES = ("staging/", "*.sqlite3", "*.sqlite3-wal", "*.sqlite3-shm", "*.sqlite3-journal",
            "*.db", "*.db-wal", "*.db-shm", "*.db-journal", "*.lock",
            "config.enc", "*.key", ".env")


def excluded(name: str) -> bool:
    import fnmatch
    return any(fnmatch.fnmatch(name, pattern) for pattern in EXCLUDES if pattern != "staging/")


def audit_source(root: Path) -> dict:
    if root.resolve() != root or not root.is_dir():
        raise ValueError(f"Missing or aliased source: {root}")
    summary = {"files": 0, "bytes": 0, "excluded_local_state": []}
    device = root.stat().st_dev

    def walk_error(error):
        raise error

    for folder, directories, files in os.walk(root, onerror=walk_error):
        directories[:] = [name for name in directories if name != "staging"]
        for name in directories + files:
            path = Path(folder) / name
            info = path.lstat()
            if path.is_symlink() or info.st_dev != device:
                raise ValueError(f"Source alias/nested filesystem: {path}")
            if stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"Source special file: {path}")
            if excluded(name):
                summary["excluded_local_state"].append(str(path.relative_to(root)))
                continue
            with path.open("rb") as stream:
                if stream.read(16) == b"SQLite format 3\0":
                    raise ValueError(f"Unlisted SQLite extension requires local snapshot rule: {path}")
            summary["files"] += 1
            summary["bytes"] += info.st_size
    return summary


def private_directory(path: Path):
    if path.exists() or path.is_symlink():
        raise ValueError(f"Precopy requires new directory: {path}")
    path.mkdir(mode=0o700)
    # Adjust only the newly created own directory if inherited NAS ACL changed mkdir mode.
    path.chmod(0o700)
    info = path.stat()
    if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (1037, 100, 0o700):
        raise ValueError(f"Precopy directory not private1037:100/0700: {path}")


def write_status(path: Path, status: dict):
    temporary = path.with_suffix(".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(status, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o600)
    temporary.replace(path)


def precopy(run: str, mount: Path, state: Path, sources: Path, manifest: Path) -> dict:
    if not re.fullmatch(r"capture-[0-9]{8}T[0-9]{6}Z", run):
        raise ValueError("Invalid capture run identifier")
    report = verify_layout(manifest, mount, state)
    audits = {role: audit_source(sources / role) for role in ROLES}
    if sum(row["bytes"] for row in audits.values()) > 24 * 1024**3:
        raise ValueError("Candidate exceeds reserved24GiB; recalculate before precopy")
    parent = mount / "staging" / "work" / "captures"
    if not parent.exists():
        private_directory(parent)
    require_nfs_directory(parent, mount, EXPECTED_SOURCE)
    info = parent.stat()
    if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (1037, 100, 0o700):
        raise ValueError("Existing capture parent is not private1037:100/0700")
    base = parent / run
    receipt = state / run
    private_directory(receipt)
    status = {"run": run, "state": "precopy-running", "migratable": False,
              "consistent_source": False, "live_sources": True,
              "blockers": ["consistent closure pending", "fresh integrity/mapping review pending",
                           "NAS backups not independent", "Kering canonical views unaccredited"],
              "preflight": report, "source_metadata_before": audits, "passes": []}
    status_path = receipt / "status.json"
    write_status(status_path, status)
    try:
        private_directory(base)
        private_directory(base / "candidate")
        private_directory(base / "retained")
        for role in ROLES:
            verify_layout(manifest, mount, state)
            require_nfs_directory(base, mount, EXPECTED_SOURCE)
            destination = base / "candidate" / role
            private_directory(destination)
            backup = base / "retained" / role
            command = ["rsync", "-rt", "--backup", f"--backup-dir={backup}", "--bwlimit=10240",
                       "--stats", "--itemize-changes", *(f"--exclude={value}" for value in EXCLUDES),
                       str(sources / role) + "/", str(destination) + "/"]
            with (receipt / f"{role}.log").open("x", encoding="utf-8") as log:
                result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=False)
                log.flush()
                os.fsync(log.fileno())
            status["passes"].append({"role": role, "exit_code": result.returncode})
            write_status(status_path, status)
            if result.returncode:
                raise RuntimeError(f"Live precopy {role} failed, rsync exit={result.returncode}; see private log")
            for folder, directories, files in os.walk(destination):
                for path in [Path(folder), *(Path(folder) / name for name in directories + files)]:
                    info = path.lstat()
                    expected_mode = 0o700 if path.is_dir() else 0o600
                    if path.is_symlink() or (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (
                            1037, 100, expected_mode):
                        raise ValueError(f"New candidate path lacks private permissions: {path}")
        status["state"] = "candidate-not-migratable"
        status["finished_at"] = datetime.now(timezone.utc).isoformat()
        status["candidate"] = str(base / "candidate")
        write_status(status_path, status)
        return status
    except (OSError, ValueError, RuntimeError) as error:
        status["state"] = "precopy-incomplete"
        status["error"] = str(error)
        write_status(status_path, status)
        raise


def main():
    import fcntl

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    state = Path("/local")
    require_local(state)
    # Serialize all precopies, not provider jobs or production writers.
    with (state / ".precopy.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = precopy(args.run, Path("/nas"), state, Path("/original"),
                             Path("/app/deployment/nfs/staging-layout.tsv"))
            print(json.dumps({key: result[key] for key in ("run", "state", "migratable", "passes")}, indent=2))
        except (OSError, ValueError, RuntimeError) as error:
            parser.exit(1, f"Precopy blocked/incomplete: {error}\n")


if __name__ == "__main__":
    main()
