"""Isolated live NFS probe, run as the actual intended writer UID/GID."""
from hashlib import sha256
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import time

from nfs_repository_guard import require_nfs


def locked_writer(root, directory, index):
    import fcntl
    require_nfs(Path(root))
    with open(Path(directory) / "shared.lock", "a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with open(Path(directory) / "sequence.txt", "a", encoding="ascii") as stream:
            stream.write(f"start-{index}\n")
            stream.flush()
            os.fsync(stream.fileno())
            time.sleep(.3)
            stream.write(f"end-{index}\n")
            stream.flush()
            os.fsync(stream.fileno())
        fcntl.flock(lock, fcntl.LOCK_UN)


def probe(root):
    mount = require_nfs(root)
    capacity = os.statvfs(root)
    result = {**mount, "uid": os.geteuid(), "gid": os.getegid(),
              "available_bytes": capacity.f_bavail * capacity.f_frsize}
    # No access to existing images; only this unique, newly-created directory.
    with tempfile.TemporaryDirectory(prefix=".cloud-nfs-probe-", dir=root) as temporary:
        directory = Path(temporary)
        content = b"cloud-nfs-isolated-probe\n"
        original = directory / "write.tmp"
        require_nfs(root)
        with original.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        renamed = directory / "renamed.bin"
        require_nfs(root)
        original.rename(renamed)
        assert sha256(renamed.read_bytes()).digest() == sha256(content).digest()
        context = multiprocessing.get_context("spawn")
        workers = [context.Process(target=locked_writer, args=(str(root), temporary, index))
                   for index in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(30)
            if worker.is_alive():
                worker.terminate()
                worker.join()
                raise RuntimeError("NFS lock probe timed out")
            if worker.exitcode != 0:
                raise RuntimeError("NFS lock worker failed")
        sequence = (directory / "sequence.txt").read_text(encoding="ascii").splitlines()
        assert sequence in [
            ["start-0", "end-0", "start-1", "end-1"],
            ["start-1", "end-1", "start-0", "end-0"],
        ], "Cross-process locks did not serialize writes"
        require_nfs(root)
        result.update(write_read_rename_checksum=True, cross_process_lock=True, cleaned=True)
    return result


if __name__ == "__main__":
    print(json.dumps(probe(Path("/mnt/cloud-imagenes")), indent=2))
