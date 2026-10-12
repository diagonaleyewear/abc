from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
from pathlib import Path

from PIL import Image
import pytest

from image_repository import ImageRepository
import image_repository
import graph_mail_downloader as graph
import migrate_image_repository as migration
import repository_storage as storage


EAN = "0012345678901"


@pytest.fixture
def production_nfs(tmp_path, monkeypatch):
    mount = tmp_path / "nas"
    mount.mkdir()
    for role in ("images", "backups", "work", "staging"):
        (mount / role).mkdir()
    monkeypatch.setenv("IMAGE_REPOSITORY_MOUNT_ROOT", str(mount))
    monkeypatch.setenv("IMAGE_REPOSITORY_NFS_SOURCE", storage.EXPECTED_SOURCE)
    monkeypatch.delenv("IMAGE_REPOSITORY_STAGING_ROOT", raising=False)
    monkeypatch.setattr(storage, "require_nfs_directory", lambda *args: {})
    monkeypatch.setattr(storage, "filesystem_mount", lambda p: (mount, "nfs4", storage.EXPECTED_SOURCE))
    return mount


def test_production_sibling_storage_does_not_allow_self_replica(production_nfs):
    root = production_nfs
    storage.check_image_root(root / "images")
    storage.check_backup_root(root / "images", root / "backups")
    storage.require_distinct_storage(root / "images", root / "work")
    for role in ("images", "backups", "work"):
        storage.check_write_path(root / role / "new-file")
    with pytest.raises(ValueError, match="misma exportacion"):
        storage.require_distinct_replica(root / "images", root / "backups")


def test_production_root_staging_alias_and_nested_mount_rejected(production_nfs, monkeypatch):
    root = production_nfs
    for path in (root, root / "staging", root / "other"):
        with pytest.raises(ValueError):
            storage.check_write_path(path)
    with pytest.raises(ValueError):
        storage.check_image_root(root)
    with pytest.raises(ValueError):
        storage.check_image_root(root / "staging")
    monkeypatch.setattr(storage, "filesystem_mount", lambda p: (root / "images", "ext4", "local"))
    with pytest.raises(ValueError, match="otro montaje"):
        storage.check_write_path(root / "images" / "file")
    with pytest.raises(ValueError, match="aliases"):
        storage.check_production_directory(root / "images" / ".." / "images", "images")


def test_production_missing_mount_and_same_path_fail_closed(production_nfs, monkeypatch):
    root = production_nfs
    with pytest.raises(ValueError, match="distintos"):
        storage.require_distinct_storage(root / "images", root / "images")
    def absent(*args):
        raise RuntimeError("NFS absent")
    monkeypatch.setattr(storage, "require_nfs_directory", absent)
    with pytest.raises(RuntimeError, match="absent"):
        storage.check_image_root(root / "images")


def photo():
    buffer = BytesIO()
    Image.new("RGB", (600, 600), (10, 20, 30)).save(buffer, "PNG")
    return buffer.getvalue()


@pytest.fixture
def split(tmp_path, monkeypatch):
    images, state, backups = [tmp_path / name for name in ("images", "state", "backups")]
    monkeypatch.setenv("IMAGE_REPOSITORY_ROOT", str(images))
    monkeypatch.setenv("IMAGE_REPOSITORY_STATE_ROOT", str(state))
    monkeypatch.setenv("IMAGE_REPOSITORY_BACKUP_ROOT", str(backups))
    monkeypatch.delenv("IMAGE_REPOSITORY_NFS_SOURCE", raising=False)
    monkeypatch.setattr(graph, "_load_eans_by_image_key", lambda: {})
    monkeypatch.setattr(migration, "_load_eans_by_image_key", lambda: {})
    return images, state, backups


def test_sqlite_local_bytes_shared_and_lock_shared(split):
    images, state, _ = split
    repository = ImageRepository()
    content = photo()
    record, _ = repository.save(EAN, "same.png", content, provider="Kering", origin="synthetic")
    repository.save(EAN, "same.png", content, provider="Luxoptica", origin="synthetic")
    repository.associate_order("Kering", "1", [EAN])
    assert repository.database == state / ".catalog.sqlite3"
    assert not (images / ".catalog.sqlite3").exists()
    assert record.path == images / EAN / "same.png"
    assert len(repository.order_records("Kering", "1")) == 2
    assert repository.lock(EAN).lock_file == str(images / ".locks" / f"{EAN}.lock")


def test_incoming_and_pending_local(split):
    images, state, _ = split
    images.mkdir()
    archive = graph._save_attachment_bytes(b"archive", images, datetime.now(timezone.utc), "lote-test", "test.zip")
    assert state in archive.parents
    assert graph._market_pending_file(images) == state / ".market_pending.json"
    assert not (images / ".incoming").exists()


def test_copy_only_migration_split_backup_history_verify_recover(split, tmp_path):
    images, state, backups = split
    source = tmp_path / "legacy"
    (source / EAN).mkdir(parents=True)
    (source / EAN / "same.png").write_bytes(photo())
    (source / "photos.zip").write_bytes(b"synthetic archived source bytes")
    (source / ".mail_download_state.json").write_text(json.dumps({"processed_message_ids": ["synthetic"]}))
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in source.rglob("*") if path.is_file()}
    plan = migration.inventory([("Other", source)], images)
    assert not images.exists() and not state.exists() and not backups.exists()
    assert plan["storage"] == {"state_root": str(state), "backup_root": str(backups)}
    assert migration.apply(plan)["verified"] == 3
    assert migration.apply(plan)["verified"] == 3
    assert migration.verify(plan)["verified"] == 3
    for entry in plan["entries"]:
        base = backups if migration.external_backup(entry, plan) else state
        assert (base / ".migration" / plan["id"] / "originals" / entry["backup"]).is_file()
    assert (state / ".migration" / plan["id"] / "plan.json").exists()
    assert (state / ".mail_download_state.json").exists()
    assert not list(images.rglob("*.sqlite3"))
    assert not (images / ".migration").exists()
    assert migration.recover(plan, tmp_path / "recovery")["recovered"] == 3
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in before} == before
    with pytest.raises(ValueError, match="separado"):
        migration.recover(plan, backups / "restore")


def test_local_state_and_no_unmounted_fallback(split, monkeypatch):
    images, state, _ = split
    monkeypatch.setattr(storage, "filesystem_mount", lambda path: (Path("/mnt"), "nfs4", "nas:/volume"))
    with pytest.raises(ValueError, match="local"):
        storage.state_root(images)
    assert not state.exists()
    monkeypatch.setattr(storage, "filesystem_mount", lambda path: None)
    monkeypatch.setenv("IMAGE_REPOSITORY_NFS_SOURCE", "192.168.1.32:/volume1/cloud-imagenes")
    with pytest.raises(RuntimeError):
        ImageRepository()
    assert not images.exists()


def test_mount_lost_blocks_cached_repository(split, monkeypatch):
    repository = ImageRepository()
    monkeypatch.setenv("IMAGE_REPOSITORY_NFS_SOURCE", "192.168.1.32:/volume1/cloud-imagenes")
    with pytest.raises(RuntimeError):
        repository.save(EAN, "x.png", photo(), provider="Other", origin="synthetic")
    assert not (repository.root / EAN).exists()


def test_self_replica_alias_export_is_rejected(split, monkeypatch, tmp_path):
    images, _, _ = split
    repository = ImageRepository()
    alias = tmp_path / "other-mount"
    monkeypatch.setattr(storage, "filesystem_mount", lambda path: (
        path, "nfs4", "192.168.1.32:/volume1/cloud-imagenes"))
    with pytest.raises(ValueError, match="misma exportacion"):
        repository._sync_nas(alias)
    assert not alias.exists()


def test_legacy_replica_rejected_for_split_state(split, tmp_path):
    repository = ImageRepository()
    with pytest.raises(ValueError, match="independiente"):
        repository._sync_nas(tmp_path / "replica")


def test_catalog_does_not_silently_start_empty_after_split(split):
    images, state, _ = split
    images.mkdir()
    (images / ".catalog.sqlite3").write_bytes(b"legacy")
    with pytest.raises(ValueError, match="legacy"):
        ImageRepository()
    assert not (state / ".catalog.sqlite3").exists()


def test_backup_root_is_pinned_in_plan_and_not_retargeted_by_environment(split, tmp_path, monkeypatch):
    images, state, backups = split
    source = tmp_path / "old"
    (source / EAN).mkdir(parents=True)
    (source / EAN / "x.png").write_bytes(photo())
    plan = migration.inventory([("Other", source)], images)
    different = tmp_path / "different"
    monkeypatch.setenv("IMAGE_REPOSITORY_BACKUP_ROOT", str(different))
    migration.apply(plan)
    assert (backups / ".migration" / plan["id"]).exists()
    assert not different.exists()


def test_old_plan_cannot_silently_change_storage_roots(split):
    images, _, _ = split
    plan = {"target": str(images)}
    with pytest.raises(ValueError, match="antiguo"):
        migration.plan_storage(plan)


def test_absent_backup_mount_never_creates_fallback(split, tmp_path, monkeypatch):
    images, _, backups = split
    source = tmp_path / "source"
    (source / EAN).mkdir(parents=True)
    (source / EAN / "x.png").write_bytes(photo())
    plan = migration.inventory([("Other", source)], images)
    monkeypatch.setenv("IMAGE_REPOSITORY_BACKUP_NFS_SOURCE", "synthetic:/separate-backup")
    with pytest.raises(RuntimeError):
        migration.apply(plan)
    assert not images.exists() and not backups.exists()


def test_state_cannot_be_nested_even_via_explicit_argument(split):
    images, _, _ = split
    with pytest.raises(ValueError, match="solapadas"):
        ImageRepository(images, state=images / "state")
    assert not images.exists()


def test_existing_local_version_one_plan_still_verifies(tmp_path, monkeypatch):
    for key in ("IMAGE_REPOSITORY_STATE_ROOT", "IMAGE_REPOSITORY_BACKUP_ROOT",
                "IMAGE_REPOSITORY_NFS_SOURCE", "IMAGE_REPOSITORY_BACKUP_NFS_SOURCE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(migration, "_load_eans_by_image_key", lambda: {})
    source, images = tmp_path / "old", tmp_path / "images"
    (source / EAN).mkdir(parents=True)
    (source / EAN / "x.png").write_bytes(photo())
    plan = migration.inventory([("Other", source)], images)
    plan["version"] = 1
    plan.pop("storage")
    body = {key: value for key, value in plan.items() if key != "id"}
    plan["id"] = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    assert migration.apply(plan)["verified"] == 1
    assert migration.verify(plan)["verified"] == 1


def test_guard_rechecks_before_atomic_publication(tmp_path):
    target = tmp_path / "new" / "x.png"
    calls = []

    def guard():
        calls.append(True)
        if len(calls) == 2:
            raise RuntimeError("synthetic lost mount")

    with pytest.raises(RuntimeError, match="lost mount"):
        image_repository.atomic_write(target, photo(), guard=guard)
    assert len(calls) == 2
    assert not target.exists()
    assert not list(target.parent.iterdir())


def test_guard_rechecks_before_copy_publication(tmp_path):
    source = tmp_path / "old.zip"
    source.write_bytes(b"synthetic")
    target = tmp_path / "new" / "x.zip"
    calls = []

    def guard():
        calls.append(True)
        if len(calls) == 2:
            raise RuntimeError("synthetic lost mount")

    with pytest.raises(RuntimeError, match="lost mount"):
        image_repository.copy_verified(source, target, migration.checksum(source), guard=guard)
    assert len(calls) == 2
    assert not target.exists()
    assert source.read_bytes() == b"synthetic"
    assert not list(target.parent.iterdir())
