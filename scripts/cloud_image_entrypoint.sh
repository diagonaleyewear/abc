#!/bin/sh
set -eu
if test "$(id -u)" = 1037; then
    test "$(id -g)" = 100 && test "$(id -G)" = 100 || {
        echo "Cloud image identity requires 1037:100 without supplementary groups" >&2
        exit 1
    }
    umask 077
fi
if test -n "${IMAGE_REPOSITORY_NFS_SOURCE:-}"; then
    python -c 'from pathlib import Path; from image_repository import repository_root; from repository_storage import check_image_root, state_root, backup_root, check_backup_root; from image_work_storage import work_root, require_capacity; r=repository_root(); check_image_root(r); state_root(r); check_backup_root(r, backup_root(r)); require_capacity(work_root(r))'
fi
exec "$@"
