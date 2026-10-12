from pathlib import Path
from types import SimpleNamespace
from io import BytesIO
from zipfile import ZipFile
from PIL import Image

import pytest

from scripts import verify_staging_exports as preparation
from scripts.nfs_repository_guard import require_nfs_directory, EXPECTED_SOURCE
import repository_storage as storage
from image_repository import ImageRepository
import migrate_image_repository as migration


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "deployment/nfs/staging-layout.tsv"


def mount_line(root, mount_root="/", fs="nfs4"):
    escaped = str(root).replace("\\", "\\134")
    return f"23 1 0:42 {mount_root} {escaped} rw - {fs} {EXPECTED_SOURCE} rw,hard\n"


@pytest.fixture
def staged(tmp_path, monkeypatch):
    mount = tmp_path / "nas"
    root = mount / "staging"
    for role in storage.STAGING_ROLES:
        (root / role).mkdir(parents=True)
    info = tmp_path / "mountinfo"
    info.write_text(mount_line(mount))
    monkeypatch.setenv("IMAGE_REPOSITORY_STAGING_ROOT", str(root))
    monkeypatch.setenv("IMAGE_REPOSITORY_STAGING_MOUNT_ROOT", str(mount))
    monkeypatch.setenv("IMAGE_REPOSITORY_ROOT", str(root / "images"))
    monkeypatch.setenv("IMAGE_REPOSITORY_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("IMAGE_REPOSITORY_BACKUP_ROOT", str(root / "backups"))
    monkeypatch.setattr(storage, "require_nfs_directory",
                        lambda path, base, source: require_nfs_directory(path, base, source, info))
    monkeypatch.setattr(storage, "filesystem_mount", lambda path: (
        (mount, "nfs4", EXPECTED_SOURCE) if mount == path or mount in path.parents else None))
    return mount, root, info


def test_siblings_allowed_but_same_destination_and_parent_rejected(staged):
    mount, root, _ = staged
    storage.check_backup_root(root / "images", root / "backups")
    storage.require_distinct_replica(root / "images", root / "backups")
    with pytest.raises(ValueError):
        storage.require_distinct_replica(root / "images", root / "images")
    for path in (mount, root, root / "source", mount / "production"):
        with pytest.raises(ValueError):
            storage.check_migration_target(path)
    storage.check_migration_target(root / "recovery", "recovery")


def test_subdirectory_overmount_and_alias_rejected(staged):
    mount, root, info = staged
    info.write_text(mount_line(mount) + mount_line(root / "images", "/staging/images"))
    with pytest.raises(RuntimeError, match="Nested mount"):
        storage.check_image_root(root / "images")
    info.write_text(mount_line(mount) + mount_line(root / "work", fs="ext4"))
    with pytest.raises(RuntimeError):
        storage.check_staging_role(root / "work", "work")
    with pytest.raises(ValueError):
        storage.check_staging_role(root / "images" / ".." / "backups", "backups")


def test_missing_mount_and_writes_outside_staging_fail_closed(staged):
    mount, root, info = staged
    for path in (mount / "product.png", root / "source" / "file.png", root / "file.png"):
        with pytest.raises(ValueError):
            storage.check_write_path(path)
    storage.check_write_path(root / "images" / "123" / "file.png")
    info.write_text("")
    with pytest.raises(RuntimeError):
        storage.check_write_path(root / "images" / "123" / "file.png")


def test_nested_asset_mount_is_not_a_valid_write_destination(staged, monkeypatch):
    _, root, _ = staged
    monkeypatch.setattr(storage, "filesystem_mount", lambda path: (
        root / "images" / "123", "nfs4", EXPECTED_SOURCE))
    with pytest.raises(ValueError, match="otro montaje"):
        storage.check_write_path(root / "images" / "123" / "file.png")


def test_production_inventory_prunes_staging_and_blocks_parent_target(tmp_path, monkeypatch):
    monkeypatch.delenv("IMAGE_REPOSITORY_STAGING_ROOT", raising=False)
    monkeypatch.delenv("IMAGE_REPOSITORY_STAGING_MOUNT_ROOT", raising=False)
    source = tmp_path / "legacy"
    (source / "staging" / "source").mkdir(parents=True)
    (source / "staging" / "source" / "private.txt").write_text("private")
    (source / "public.txt").write_text("public")
    target = tmp_path / "target"
    monkeypatch.setattr(migration, "_load_eans_by_image_key", lambda: {})
    plan = migration.inventory([("Other", source)], target)
    assert [entry["name"] for entry in plan["entries"]] == ["public.txt"]
    with pytest.raises(ValueError, match="staging"):
        migration.inventory([("Other", source)], source)
    assert (source / "staging" / "source" / "private.txt").read_text() == "private"


def test_catalog_search_gallery_and_zip_exclude_staging_rows(tmp_path, monkeypatch):
    monkeypatch.delenv("IMAGE_REPOSITORY_STAGING_ROOT", raising=False)
    repository = ImageRepository(tmp_path / "images")
    with repository.connect() as connection:
        connection.execute("INSERT INTO assets VALUES(1,'123','digest','pixels','staging/source/123/private.png')")
        connection.execute("INSERT INTO representations VALUES(1,1,'Other','test','unknown','','private.png',"
                           "'2026-01-01T00:00:00+00:00','{}')")
    assert repository.records() == []
    assert repository.catalog_rows() == []
    assert repository.order_records("Other", "1") == []
    with ZipFile(BytesIO(repository.zip_eans(["123"]))) as archive:
        assert not any(name.endswith("private.png") for name in archive.namelist())


def test_sibling_migration_repeat_recover_and_ean_zip(staged, monkeypatch):
    _, root, _ = staged
    source = root / "source" / "Other"
    source.mkdir()
    content = BytesIO()
    Image.new("RGB", (600, 600), (10, 20, 30)).save(content, "PNG")
    photo = source / "same.png"
    photo.write_bytes(content.getvalue())
    monkeypatch.setattr(migration, "_load_eans_by_image_key", lambda: {})
    plan = migration.inventory([("Other", source)], root / "images", {
        "files": {str(photo): {"ean": "123", "view": "unknown", "orders": ["order-A"]}},
    })
    assert not plan["blockers"]
    assert migration.apply(plan)["verified"] == 1
    assert migration.apply(plan)["verified"] == 1
    repository = ImageRepository()
    assert len(repository.records()) == 1
    assert len(repository.order_records("Other", "order-A")) == 1
    assert len(repository.catalog_rows()) == 1
    with ZipFile(BytesIO(repository.zip_eans(["123"]))) as archive:
        assert archive.read("123/same.png") == content.getvalue()
    assert migration.recover(plan, root / "recovery")["recovered"] == 1
    entry = plan["entries"][0]
    assert (root / "recovery" / entry["backup"]).read_bytes() == photo.read_bytes()


def test_manifest_reserves_one_volume_not_five_exports():
    roles = preparation.read_layout(MANIFEST)
    assert roles["work"] == 64 * 1024**3
    assert sum(roles.values()) == 184 * 1024**3


def test_capacity_is_aggregate_and_checked_before_mkdir(tmp_path, monkeypatch):
    monkeypatch.setattr(preparation.os, "getuid", lambda: 1037, raising=False)
    monkeypatch.setattr(preparation.os, "getgid", lambda: 100, raising=False)
    monkeypatch.setattr(preparation.os, "getgroups", lambda: [100], raising=False)
    monkeypatch.setattr(preparation, "require_nfs", lambda *args: None)
    minimum = 186 * 1024**3
    monkeypatch.setattr(preparation.os, "statvfs",
                        lambda path: SimpleNamespace(f_bavail=minimum - 1, f_frsize=1), raising=False)
    with pytest.raises(RuntimeError, match="aggregate"):
        preparation.verify_layout(MANIFEST, tmp_path, create=True)
    assert not (tmp_path / "staging").exists()


def test_cleanup_script_removes_only_failed_matching_units():
    script = (ROOT / "deployment/nfs/remove-failed-staging-units.sh").read_text()
    assert 'test "$STATE" != failed' in script
    assert 'cmp -s "$TMP/$UNIT" "/etc/systemd/system/$UNIT"' in script
    assert "systemctl stop" not in script and "umount " not in script
    assert "cloud-staging " not in script
