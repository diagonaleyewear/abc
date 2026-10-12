#!/bin/sh
# Remove only a failed unit matching the previous staging template.
set -eu
test "$(id -u)" = 0 || { echo "Run with sudo"; exit 1; }
HERE="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
TMP="$(mktemp -d)"
trap 'rm -f "$TMP"/*.mount; rmdir "$TMP"' EXIT
for SHARE in cloud-migration-source cloud-imagenes-staging cloud-migration-backups cloud-migration-recovery cloud-image-work; do
    ROOT="/mnt/$SHARE"
    UNIT="$(systemd-escape --path --suffix=mount "$ROOT")"
    STATE="$(systemctl show "$UNIT" -p ActiveState --value)"
    if test "$STATE" != failed; then echo "Retained $UNIT ($STATE; not failed)"; continue; fi
    test "$(systemctl show "$UNIT" -p FragmentPath --value)" = "/etc/systemd/system/$UNIT" ||
        { echo "Unexpected fragment; retained $UNIT"; exit 1; }
    if mountpoint -q "$ROOT"; then echo "Mounted unit retained: $UNIT"; exit 1; fi
    test ! -L "/etc/systemd/system/$UNIT" || { echo "Unit symlink retained"; exit 1; }
    sed -e "s|192.168.1.32:/volume1/cloud-imagenes|192.168.1.32:/volume1/$SHARE|" \
        -e "s|/mnt/cloud-imagenes|$ROOT|" \
        "$HERE/cloud-images.mount.template" > "$TMP/$UNIT"
    cmp -s "$TMP/$UNIT" "/etc/systemd/system/$UNIT" ||
        { echo "Modified unit retained for review: $UNIT"; exit 1; }
    systemctl disable "$UNIT"
    rm "/etc/systemd/system/$UNIT"
    systemctl daemon-reload
    systemctl reset-failed "$UNIT"
    echo "Removed failed staging unit only: $UNIT"
done
