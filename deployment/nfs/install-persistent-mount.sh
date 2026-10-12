#!/bin/sh
set -eu
test "$(id -u)" = 0 || { echo "Run with sudo"; exit 1; }
ROOT=/mnt/cloud-imagenes
UNIT="$(systemd-escape --path --suffix=mount "$ROOT")"
HERE="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
python3 "$HERE/nfs_repository_guard.py" --root "$ROOT"
systemctl show "$UNIT" -p FragmentPath -p SourcePath -p UnitFileState
FRAGMENT="$(systemctl show "$UNIT" -p FragmentPath --value)"
test -z "$FRAGMENT" || { echo "Persistent unit already exists; review before replacing"; exit 1; }
if grep -Eq '^[^#]+[[:space:]]/mnt/cloud-imagenes[[:space:]]' /etc/fstab; then
    echo "fstab already manages this mount; review instead of adding a second definition"
    exit 1
fi
TMP="$(mktemp -d)"
trap 'rm -f "$TMP/$UNIT"; rmdir "$TMP"' EXIT
cp "$HERE/cloud-images.mount.template" "$TMP/$UNIT"
systemd-analyze verify "$TMP/$UNIT"
install -m 0644 "$TMP/$UNIT" "/etc/systemd/system/$UNIT"
systemctl daemon-reload
systemctl enable "$UNIT"
# No restart, stop, remount or --now: preserve the shared live mount.
python3 "$HERE/nfs_repository_guard.py" --root "$ROOT"
systemctl show "$UNIT" -p FragmentPath -p SourcePath -p UnitFileState
