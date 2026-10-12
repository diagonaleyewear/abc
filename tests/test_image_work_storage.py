from datetime import datetime, timezone
import shutil
from zipfile import ZipFile

import pytest

import graph_mail_downloader as graph
import image_work_storage as work


@pytest.fixture
def staging(tmp_path, monkeypatch):
    images, state, incoming = [tmp_path / part for part in ("images", "state", "incoming")]
    monkeypatch.setenv("IMAGE_REPOSITORY_ROOT", str(images))
    monkeypatch.setenv("IMAGE_REPOSITORY_STATE_ROOT", str(state))
    monkeypatch.setenv("IMAGE_REPOSITORY_WORK_ROOT", str(incoming))
    monkeypatch.delenv("IMAGE_REPOSITORY_NFS_SOURCE", raising=False)
    monkeypatch.delenv("IMAGE_REPOSITORY_WORK_NFS_SOURCE", raising=False)
    monkeypatch.setenv("GRAPH_IMAGE_MAX_ARCHIVE_BYTES", "1024")
    monkeypatch.setenv("IMAGE_REPOSITORY_WORK_RESERVE_BYTES", "2048")
    images.mkdir()
    incoming.mkdir()
    return images, state, incoming


def test_archive_goes_to_independent_storage(staging):
    images, state, incoming = staging
    saved = graph._save_attachment_bytes(b"synthetic", images, datetime.now(timezone.utc), "test", "archive.zip")
    assert incoming in saved.parents
    assert not state.exists()
    again = graph._save_attachment_bytes(b"synthetic", images, datetime.now(timezone.utc), "test", "archive.zip")
    assert again == saved


def test_reserve_blocks_before_provider_access(staging, monkeypatch):
    monkeypatch.setattr(work.shutil, "disk_usage", lambda path: shutil._ntuple_diskusage(10000, 9000, 1000))
    monkeypatch.setattr(graph, "_download_luxoptica_mail_attachments",
                        lambda *args: pytest.fail("Provider must not be consulted"))
    with pytest.raises(OSError, match="reserva"):
        graph.download_luxoptica_mail_attachments()


def test_stream_limit_cleanup_no_overwrite(staging):
    _, _, incoming = staging
    previous = incoming / "archive.zip"
    previous.write_bytes(b"valid original")
    with pytest.raises(ValueError, match="MAX_ARCHIVE"):
        graph._save_streamed_attachment([b"a" * 1024, b"x"], incoming, "archive.zip")
    assert previous.read_bytes() == b"valid original"
    assert not list(incoming.glob("*.part"))
    alternative = graph._save_streamed_attachment([b"different"], incoming, "archive.zip")
    assert alternative != previous
    assert alternative.suffix == ".zip"
    assert previous.read_bytes() == b"valid original"


def test_missing_work_mount_has_no_fallback(staging, monkeypatch):
    _, _, incoming = staging
    root = incoming / "not-mounted"
    monkeypatch.setenv("IMAGE_REPOSITORY_WORK_ROOT", str(root / ".work"))
    monkeypatch.setenv("IMAGE_REPOSITORY_WORK_MOUNT_ROOT", str(root))
    monkeypatch.setenv("IMAGE_REPOSITORY_WORK_NFS_SOURCE", "synthetic:/work")
    with pytest.raises(RuntimeError):
        with work.incoming_lock(staging[0]):
            pytest.fail("Missing mount accepted")
    assert not root.exists()


def test_entry_size_bounded_before_read(staging, monkeypatch):
    images, _, incoming = staging
    archive = incoming / "archive.zip"
    with ZipFile(archive, "w") as stream:
        stream.writestr("image.png", b"a" * 33)
    monkeypatch.setenv("GRAPH_IMAGE_MAX_ENTRY_BYTES", "32")
    monkeypatch.setattr(graph, "_load_eans_by_image_key", lambda: {})
    monkeypatch.setattr(graph, "_get_market_ids_by_ean", lambda eans: {})
    with pytest.raises(ValueError, match="MAX_ENTRY"):
        graph._extract_zip(archive, images)
    assert archive.exists()


def test_staging_lock_serializes_independent_instances(staging):
    from filelock import FileLock, Timeout
    images, _, incoming = staging
    with work.incoming_lock(images):
        with pytest.raises(Timeout):
            with FileLock(incoming / ".incoming.lock", timeout=0):
                pytest.fail("Second importer acquired lock")


def test_retained_archives_consume_reserve(staging, monkeypatch):
    _, _, incoming = staging
    free = iter([10000, 10000, 2048])
    monkeypatch.setattr(work.shutil, "disk_usage", lambda path: shutil._ntuple_diskusage(10000, 0, next(free)))
    with pytest.raises(OSError, match="reserva"):
        graph._save_streamed_attachment([b"one", b"two"], incoming, "archive.zip")
    assert not list(incoming.iterdir())


def test_nested_local_overmount_is_rejected(staging, monkeypatch):
    _, _, incoming = staging
    monkeypatch.setenv("IMAGE_REPOSITORY_WORK_NFS_SOURCE", "synthetic:/work")
    monkeypatch.setenv("IMAGE_REPOSITORY_WORK_MOUNT_ROOT", str(incoming))
    monkeypatch.setattr(work, "require_nfs", lambda *args: {})
    monkeypatch.setattr(work, "filesystem_mount", lambda root: (root, "ext4", "local"))
    with pytest.raises(ValueError, match="montaje distinto"):
        work.check_work_root(incoming / ".work")


def test_no_implicit_local_incoming_for_nfs_images(staging, monkeypatch):
    images, _, _ = staging
    monkeypatch.delenv("IMAGE_REPOSITORY_WORK_ROOT")
    monkeypatch.setenv("IMAGE_REPOSITORY_NFS_SOURCE", "synthetic:/images")
    with pytest.raises(ValueError, match="independientes"):
        work.work_root(images)
