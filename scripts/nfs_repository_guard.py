"""Fail closed for an exact NFS export; does not mount or create directories."""
import argparse
import os
from pathlib import Path
import re


EXPECTED_SOURCE = "192.168.1.32:/volume1/cloud-imagenes"


def _unescape(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)


def require_nfs(root: Path, source: str = EXPECTED_SOURCE,
                mountinfo: Path = Path("/proc/self/mountinfo")) -> dict:
    root = Path(os.path.abspath(root))
    if not root.is_dir() or root.resolve() != root:
        raise RuntimeError("NFS repository missing or symlinked; local fallback forbidden")
    if not mountinfo.is_file():
        raise RuntimeError("Cannot verify mountinfo; NFS writes forbidden")
    matches = []
    for line in mountinfo.read_text(encoding="utf-8").splitlines():
        before, separator, after = line.partition(" - ")
        if not separator:
            raise RuntimeError("Invalid mountinfo")
        fields, filesystem = before.split(), after.split()
        mountpoint = Path(_unescape(fields[4]))
        if root == mountpoint or mountpoint in root.parents:
            matches.append((len(mountpoint.parts), fields, filesystem))
    if not matches:
        raise RuntimeError("No mounted filesystem covers image root")
    _, fields, filesystem = max(reversed(matches), key=lambda match: match[0])
    if filesystem[0] not in {"nfs", "nfs4"} or _unescape(filesystem[1]) != source:
        raise RuntimeError("Wrong filesystem or NFS export; local fallback forbidden")
    # Do not permit a bind of an arbitrary subfolder to masquerade as the export root.
    if fields[3] != "/" or Path(_unescape(fields[4])) != root:
        raise RuntimeError("Image root must be the exact export mount, not a subdirectory")
    options = set(fields[5].split(",")) | set(filesystem[2].split(","))
    if "rw" not in options or "ro" in options or "soft" in options or "softerr" in options:
        raise RuntimeError("Writable hard NFS mount required")
    if "hard" not in options:
        raise RuntimeError("NFS hard option missing")
    return {"root": str(root), "source": source, "filesystem": filesystem[0]}


def require_nfs_directory(root: Path, mount: Path, source: str,
                          mountinfo: Path = Path("/proc/self/mountinfo")) -> dict:
    """Allow a canonical child, but never a symlink or nested bind/overmount."""
    root, mount = Path(os.path.abspath(root)), Path(os.path.abspath(mount))
    receipt = require_nfs(mount, source, mountinfo)
    if root.resolve() != root or not root.is_dir() or mount not in root.parents:
        raise RuntimeError("NFS child missing, aliased or outside the expected mount")
    for line in mountinfo.read_text(encoding="utf-8").splitlines():
        fields = line.partition(" - ")[0].split()
        point = Path(_unescape(fields[4]))
        if point != mount and mount in point.parents and (
                point == root or point in root.parents):
            raise RuntimeError("Nested mount or bind alias inside NFS staging")
    return {**receipt, "directory": str(root)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/mnt/cloud-imagenes"))
    parser.add_argument("--source", default=EXPECTED_SOURCE)
    arguments = parser.parse_args()
    print(require_nfs(arguments.root, arguments.source))
