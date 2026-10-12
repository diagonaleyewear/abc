#!/bin/sh
set -eu
test "$(id -u)" = 0 || { echo "Run with sudo"; exit 1; }
ROOT=/mnt/cloud-imagenes
HERE="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
python3 "$HERE/nfs_repository_guard.py" --root "$ROOT"
# Only an empty export may be provisioned automatically; never touch others' files.
test -z "$(find "$ROOT" -mindepth 1 -maxdepth 1 -print -quit)" || {
    echo "Export not empty; ACL/ownership review required, no recursive changes"
    exit 1
}
stat -c 'Before: %u:%g %a' "$ROOT"
test "${CLOUD_NAS_ACL_VERIFIED:-}" = "1037-owner-only" || {
    echo "Verify DSM ACL grants cloud rw/traversal and no general users access; activation blocked"
    exit 1
}
chown 1037:100 "$ROOT"
chmod 0700 "$ROOT"
stat -c 'After: %u:%g %a' "$ROOT"
test "$(stat -c '%u:%g %a' "$ROOT")" = "1037:100 700" || {
    echo "Synology ACL/mapping did not retain requested permissions; activation blocked"
    exit 1
}
