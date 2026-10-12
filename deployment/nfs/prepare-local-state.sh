#!/bin/sh
# New Cloud staging state only; no production chown/ACL/SQLite changes.
set -eu
test "$(id -u)" = 0 || { echo "Run with sudo"; exit 1; }
ROOT=/opt/cloud-image-staging
test ! -L "$ROOT" || { echo "Symlink not allowed"; exit 1; }
CHECK=/opt
if test -e "$ROOT"; then CHECK="$ROOT"; fi
FSTYPE="$(findmnt -T "$CHECK" -n -o FSTYPE)"
case "$FSTYPE" in nfs|nfs4|cifs|smb3) echo "Local state cannot live on NAS"; exit 1 ;; esac
test ! -e "$ROOT/state" || { echo "Existing state requires explicit review, no recursive changes"; exit 1; }
install -d -m 0755 "$ROOT"
install -d -o 1037 -g 100 -m 0700 "$ROOT/state"
install -d -o 1037 -g 100 -m 0700 "$ROOT/state/image-repository"
stat -c '%n %u:%g %a' "$ROOT/state" "$ROOT/state/image-repository"
