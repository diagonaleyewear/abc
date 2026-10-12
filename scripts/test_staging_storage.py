"""Live private-sibling probe; touches only its own temporary files."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from repository_storage import staging_root, check_staging_role


def main() -> None:
    if (os.getuid(), os.getgid()) != (1037, 100) or set(os.getgroups()) - {100}:
        raise RuntimeError("Probe requires1037:100 only")
    root = staging_root()
    if root is None:
        raise RuntimeError("Explicit staging configuration required")
    for role in ("source", "images", "backups", "recovery", "work"):
        base = root / role
        check_staging_role(base, role)
        info = base.stat()
        if (info.st_uid, info.st_gid, info.st_mode & 0o7777) != (1037, 100, 0o700):
            raise RuntimeError("Staging role is not private")
        directory = Path(tempfile.mkdtemp(prefix=".cloud-probe-", dir=base))
        try:
            original, renamed = directory / "test.tmp", directory / "test.bin"
            content = b"isolated-staging-probe\n" * 1024
            with original.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            original.rename(renamed)
            if hashlib.sha256(renamed.read_bytes()).digest() != hashlib.sha256(content).digest():
                raise RuntimeError("Checksum mismatch")
            code = """
import fcntl,os,sys,time
from pathlib import Path
p=Path(sys.argv[1]); i=sys.argv[2]
with (p/'shared.lock').open('a+b') as lock:
    fcntl.flock(lock,fcntl.LOCK_EX)
    with (p/'sequence.txt').open('a') as out:
        out.write('start-'+i+'\\n'); out.flush(); os.fsync(out.fileno())
        time.sleep(.15)
        out.write('end-'+i+'\\n'); out.flush(); os.fsync(out.fileno())
"""
            workers = [subprocess.Popen([sys.executable, "-c", code, str(directory), str(i)]) for i in range(2)]
            results = [worker.wait() for worker in workers]
            if any(result != 0 for result in results):
                raise RuntimeError("Lock worker failed")
            lines = (directory / "sequence.txt").read_text().splitlines()
            if lines not in (["start-0", "end-0", "start-1", "end-1"],
                             ["start-1", "end-1", "start-0", "end-0"]):
                raise RuntimeError("Shared lock interleaved")
            print(f"{role}: private1037 write/read/fsync/rename/checksum/two-process flock OK")
        finally:
            check_staging_role(base, role)
            for name in ("test.tmp", "test.bin", "shared.lock", "sequence.txt"):
                path = directory / name
                if path.exists():
                    path.unlink()
            directory.rmdir()


if __name__ == "__main__":
    main()
