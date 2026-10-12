"""Prepare/check private staging siblings on the existing image export only."""
import argparse
import json
import os
from pathlib import Path

from scripts.nfs_repository_guard import require_nfs, require_nfs_directory, EXPECTED_SOURCE
from repository_storage import require_local, STAGING_ROLES


def read_layout(manifest: Path) -> dict[str, int]:
    roles = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        name, value = line.split()
        if name not in STAGING_ROLES or name in roles or int(value) <= 0:
            raise ValueError("Invalid or duplicate staging role/reserve")
        roles[name] = int(value)
    if set(roles) != STAGING_ROLES:
        raise ValueError("Exactly five staging roles required")
    return roles


def verify_layout(manifest: Path, mount: Path, state: Path | None = None, *, create=False) -> dict:
    if os.getuid() != 1037 or os.getgid() != 100 or set(os.getgroups()) - {100}:
        raise RuntimeError("Run as 1037:100 without supplementary groups")
    require_nfs(mount, EXPECTED_SOURCE)
    roles = read_layout(manifest)
    available = os.statvfs(mount).f_bavail * os.statvfs(mount).f_frsize
    required = sum(roles.values()) + 2 * 1024**3
    if available < required:
        raise RuntimeError(f"Insufficient aggregate NAS capacity: {available} < {required}")
    root = mount / "staging"
    paths = [root, *(root / role for role in sorted(roles))]
    # Validate all existing paths before creating any directory; never chmod/chown existing data.
    for path in paths:
        if path.is_symlink():
            raise RuntimeError(f"Staging alias forbidden: {path}")
        if path.exists():
            require_nfs_directory(path, mount, EXPECTED_SOURCE)
            info = path.stat()
            if (info.st_uid, info.st_gid, info.st_mode & 0o7777) != (1037, 100, 0o700):
                raise RuntimeError(f"Existing staging path requires private1037:100/0700: {path}")
        elif not create:
            raise RuntimeError(f"Staging directory missing: {path}")
    for path in paths:
        require_nfs(mount, EXPECTED_SOURCE)
        if create and not path.exists():
            path.mkdir(mode=0o700)
            # Synology inherited ACLs may override mkdir's mode; only our new directory is adjusted.
            path.chmod(0o700)
        require_nfs_directory(path, mount, EXPECTED_SOURCE)
        info = path.stat()
        if (info.st_uid, info.st_gid, info.st_mode & 0o7777) != (1037, 100, 0o700):
            raise RuntimeError(f"Private staging ownership/mode not retained: {path}")
        if not all(os.access(path, mode) for mode in (os.R_OK, os.W_OK, os.X_OK)):
            raise RuntimeError(f"Missing cloud rw/traverse: {path}")
    if state is not None:
        if not state.is_dir() or state.resolve() != state:
            raise RuntimeError("New local state missing or aliased")
        require_local(state)
        info = state.stat()
        if (info.st_uid, info.st_gid, info.st_mode & 0o7777) != (1037, 100, 0o700):
            raise RuntimeError("Local state requires1037:100/0700")
        if os.statvfs(state).f_bavail * os.statvfs(state).f_frsize < 2 * 1024**3:
            raise RuntimeError("Local reserve below2 GiB")
    return {"mount": str(mount), "staging": str(root), "available_once": available,
            "aggregate_required": required, "roles": roles, "created_if_missing": create,
            "independent_nas_backup": False, "writes_locks_and_acl_not_verified": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--mount", type=Path, default=Path("/mnt/cloud-imagenes"))
    parser.add_argument("--state", type=Path)
    parser.add_argument("--create", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        print(json.dumps(verify_layout(args.manifest, args.mount, args.state, create=args.create), indent=2))
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(1, f"Staging blocked: {error}\n")
