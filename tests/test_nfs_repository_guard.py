from pathlib import Path

import pytest
from scripts.nfs_repository_guard import require_nfs, EXPECTED_SOURCE


def mount_line(root, fs="nfs4", source=EXPECTED_SOURCE, options="rw,hard", mount_root="/"):
    path = str(root).replace("\\", "\\134").replace(" ", "\\040")
    return f"23 1 0:42 {mount_root} {path} rw - {fs} {source} {options}\n"


def test_exact_nfs_mount(tmp_path):
    root = tmp_path / "mount"
    root.mkdir()
    info = tmp_path / "mountinfo"
    info.write_text(mount_line(root))
    assert require_nfs(root, mountinfo=info)["source"] == EXPECTED_SOURCE


@pytest.mark.parametrize("fs,source,options,mount_root", [
    ("ext4", "/dev/sda", "rw", "/"),
    ("nfs4", "192.168.1.32:/volume1/other", "rw,hard", "/"),
    ("nfs4", EXPECTED_SOURCE, "ro,hard", "/"),
    ("nfs4", EXPECTED_SOURCE, "rw,soft", "/"),
    ("nfs4", EXPECTED_SOURCE, "rw", "/"),
    ("nfs4", EXPECTED_SOURCE, "rw,hard", "/other-folder"),
])
def test_wrong_or_unsafe_mount_is_rejected(tmp_path, fs, source, options, mount_root):
    root = tmp_path / "mount"
    root.mkdir()
    info = tmp_path / "mountinfo"
    info.write_text(mount_line(root, fs, source, options, mount_root))
    with pytest.raises(RuntimeError):
        require_nfs(root, mountinfo=info)
    assert list(root.iterdir()) == []


def test_unmount_or_overmount_does_not_fall_back(tmp_path):
    root = tmp_path / "mount"
    root.mkdir()
    info = tmp_path / "mountinfo"
    info.write_text(mount_line(root))
    require_nfs(root, mountinfo=info)
    info.write_text("")
    with pytest.raises(RuntimeError):
        require_nfs(root, mountinfo=info)
    info.write_text(mount_line(root) + mount_line(root, "ext4", "/dev/sda", "rw"))
    with pytest.raises(RuntimeError):
        require_nfs(root, mountinfo=info)


def test_missing_root_never_created(tmp_path):
    root = tmp_path / "missing"
    with pytest.raises(RuntimeError):
        require_nfs(root)
    assert not root.exists()
