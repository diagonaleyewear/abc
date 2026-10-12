from concurrent.futures import ProcessPoolExecutor
from io import BytesIO
import json
import multiprocessing
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from zipfile import ZipFile

from PIL import Image
import pytest
from streamlit.testing.v1 import AppTest

from image_repository import ImageRepository
import image_thumbnails as thumbnails
import kering_images_ui as ui


EAN = "0012345678901"


def photo(color=(20, 30, 40), size=(1200, 600), *, orientation=None):
    stream = BytesIO()
    image = Image.new("RGB", size, color)
    if orientation:
        exif = Image.Exif()
        exif[274] = orientation
        image.save(stream, "JPEG", exif=exif)
    else:
        image.save(stream, "PNG")
    return stream.getvalue()


@pytest.fixture
def repository(tmp_path, monkeypatch):
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    for key in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.setenv(key, str(temporary))
    monkeypatch.setattr(thumbnails.tempfile, "gettempdir", lambda: str(temporary))
    monkeypatch.setenv("IMAGE_REPOSITORY_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("IMAGE_REPOSITORY_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("IMAGE_THUMBNAIL_CACHE_ROOT", str(tmp_path / "thumbnails"))
    monkeypatch.setenv("KERING_DATA_ROOT", str(tmp_path / "kering"))
    monkeypatch.setenv("PROCESS_ACTIVITY_PATH", str(tmp_path / "receipts" / "activity.sqlite3"))
    monkeypatch.setenv("IMAGE_THUMBNAIL_CACHE_RESERVE_BYTES", "1")
    ui.service.clear()
    yield ImageRepository()
    ui.service.clear()


def save(repository, name="original.png", color=(20, 30, 40), *, ean=EAN, provider="Kering", content=None):
    return repository.save(ean, name, content or photo(color), provider=provider,
                           origin="synthetic", view="frontal")[0]


def access(repository, record):
    return thumbnails.validate_visible(repository, [record])[record.id]


def test_cold_hot_and_identical_provider_content_no_original_reads(repository, monkeypatch):
    record = save(repository)
    other = save(repository, name="other.png", provider="Luxoptica")
    cache = thumbnails.ThumbnailCache(repository)
    proof = access(repository, record)
    reader = Mock(wraps=repository.verified_bytes)
    generator = Mock(wraps=thumbnails.generate)
    monkeypatch.setattr(repository, "verified_bytes", reader)
    monkeypatch.setattr(thumbnails, "generate", generator)
    first = cache.get(record, proof)
    assert cache.get(other, proof) == first
    assert reader.call_count == generator.call_count == 1
    monkeypatch.setattr(repository, "verified_bytes", Mock(side_effect=AssertionError("Warm original read")))
    monkeypatch.setattr(Path, "read_bytes", lambda path: first if path.parent == cache.root else
                        pytest.fail(f"Original read on warm cache: {path}"))
    assert cache.get(record, proof) == first
    assert len(list(cache.root.glob("*.jpg"))) == 1


def test_exif_aspect_no_crop_and_generator_invalidation(repository):
    record = save(repository, content=photo(orientation=6))
    cache = thumbnails.ThumbnailCache(repository)
    proof = access(repository, record)
    with Image.open(BytesIO(cache.get(record, proof))) as image:
        assert image.size == (120, 240)
    with Image.open(BytesIO(cache.get(record, proof, dimensions=(100, 100)))) as image:
        assert image.size == (50, 100)
    cache.get(record, proof, version="next-generator")
    assert len(list(cache.root.glob("*.jpg"))) == 3


def test_content_change_and_external_corruption_do_not_show_stale_cache(repository):
    record = save(repository)
    cache = thumbnails.ThumbnailCache(repository)
    cache.get(record, access(repository, record))
    record.path.write_bytes(photo((90, 0, 0)))
    with pytest.raises(ValueError, match="checksum"):
        cache.get(record, access(repository, record))
    changed = save(repository, name="changed.png", color=(90, 0, 0))
    assert changed.checksum != record.checksum
    cache.get(changed, access(repository, changed))
    assert len(list(cache.root.glob("*.jpg"))) == 2


def test_missing_permission_removed_catalog_and_absent_mount(repository, monkeypatch):
    record = save(repository)
    cache = thumbnails.ThumbnailCache(repository)
    proof = access(repository, record)
    cache.get(record, proof)
    original_access = thumbnails.os.access
    monkeypatch.setattr(thumbnails.os, "access", lambda *args: False)
    assert access(repository, record) is None
    with pytest.raises(OSError):
        cache.get(record, access(repository, record))
    monkeypatch.setattr(thumbnails.os, "access", original_access)
    record.path.unlink()
    assert access(repository, record) is None
    with repository.connect() as connection:
        connection.execute("DELETE FROM representations WHERE id=?", (record.id,))
    with pytest.raises(ValueError, match="catalogo"):
        cache.get(record, proof)
    monkeypatch.setattr(thumbnails, "check_image_root", Mock(side_effect=RuntimeError("NAS absent")))
    with pytest.raises(RuntimeError, match="NAS absent"):
        cache.get(record, proof)
    assert not record.path.exists()


def test_cached_bytes_corrupt_rebuilt_and_corrupt_original_rejected(repository):
    record = save(repository)
    cache = thumbnails.ThumbnailCache(repository)
    proof = access(repository, record)
    first = cache.get(record, proof)
    path = next(cache.root.glob("*.jpg"))
    path.write_bytes(b"broken")
    assert cache.get(record, proof) == first
    invalid, _ = repository.save("2", "corrupt.png", b"corrupt", provider="Other",
                                 origin="fixture", allow_invalid=True)
    with pytest.raises(OSError):
        cache.get(invalid, access(repository, invalid))


def test_capacity_lru_and_space_reserve(repository, monkeypatch):
    records = [save(repository, f"{index}.png", (index * 50, 20, 30)) for index in range(3)]
    cache = thumbnails.ThumbnailCache(repository)
    for record in records[:2]:
        cache.get(record, access(repository, record))
    cache.get(records[0], access(repository, records[0]))
    payloads = list(cache.root.glob("*.jpg"))
    limit = sum(path.stat().st_size for path in payloads) + 1400
    monkeypatch.setenv("IMAGE_THUMBNAIL_CACHE_MAX_BYTES", str(limit))
    cache = thumbnails.ThumbnailCache(repository)
    cache.get(records[2], access(repository, records[2]))
    index = json.loads((cache.root / "index.json").read_text())
    sources = {source for entry in index.values() for source in entry["sources"]}
    import hashlib
    assert hashlib.sha256(str(records[1].path).encode()).hexdigest() not in sources
    assert hashlib.sha256(str(records[0].path).encode()).hexdigest() in sources
    assert sum(path.stat().st_size for path in cache.root.iterdir()) <= limit
    monkeypatch.setattr(thumbnails.shutil, "disk_usage", lambda path: SimpleNamespace(free=0))
    with pytest.raises(OSError, match="Reserva"):
        cache.get(records[2], access(repository, records[2]))
    monkeypatch.setenv("IMAGE_THUMBNAIL_CACHE_MAX_BYTES", "1")
    monkeypatch.setattr(thumbnails.shutil, "disk_usage", lambda path: SimpleNamespace(free=10**10))
    with pytest.raises(OSError, match="capacidad"):
        thumbnails.ThumbnailCache(repository).get(records[0], access(repository, records[0]))
    assert sum(path.stat().st_size for path in cache.root.iterdir()) <= 1
    assert all(record.path.is_file() for record in records)


def worker(root):
    os.umask(0o077)
    repository = ImageRepository(Path(root))
    record = repository.catalog_records(EAN)[0]
    reader = repository.verified_bytes
    def counted(record):
        with (repository.state_root / "read-count.txt").open("ab") as output:
            output.write(b"read\n")
        return reader(record)
    repository.verified_bytes = counted
    return thumbnails.ThumbnailCache(repository).get(record, access(repository, record))


def test_multiprocess_atomic_single_generation(repository):
    record = save(repository)
    with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn")) as pool:
        results = list(pool.map(worker, [str(repository.root)] * 8))
    assert len(set(results)) == 1
    cache = thumbnails.ThumbnailCache(repository)
    assert len(list(cache.root.glob("*.jpg"))) == 1
    assert not list(cache.root.glob("*.tmp"))
    index = json.loads((cache.root / "index.json").read_text())
    assert len(index) == 1
    assert (repository.state_root / "read-count.txt").read_bytes() == b"read\n"
    assert cache.get(record, access(repository, record)) == results[0]
    if os.name == "posix":
        assert cache.root.stat().st_mode & 0o777 == 0o700
        assert all(path.stat().st_mode & 0o777 == 0o600 for path in cache.root.iterdir())


def test_crash_between_index_and_payload_recovers(repository, monkeypatch):
    record = save(repository)
    cache = thumbnails.ThumbnailCache(repository)
    atomic = cache._atomic
    def interrupted(path, content):
        if path.suffix == ".jpg":
            raise OSError("simulated crash")
        atomic(path, content)
    monkeypatch.setattr(cache, "_atomic", interrupted)
    with pytest.raises(OSError, match="simulated"):
        cache.get(record, access(repository, record))
    assert len(json.loads((cache.root / "index.json").read_text())) == 1
    assert not list(cache.root.glob("*.jpg"))
    monkeypatch.setattr(cache, "_atomic", atomic)
    assert cache.get(record, access(repository, record))


def test_roots_separate_local_persistent_and_bad_config(repository, monkeypatch, tmp_path):
    for root in (repository.root, repository.state_root / "cache", repository.backup_root,
                 tmp_path / "temporary" / "cache"):
        monkeypatch.setenv("IMAGE_THUMBNAIL_CACHE_ROOT", str(root))
        with pytest.raises(ValueError):
            thumbnails.ThumbnailCache(repository)
    monkeypatch.setenv("AUTH_STATE_PATH", str(tmp_path / "sqlite" / "auth.sqlite3"))
    monkeypatch.setenv("IMAGE_THUMBNAIL_CACHE_ROOT", str(tmp_path / "sqlite" / "thumbnails"))
    with pytest.raises(ValueError, match="independiente"):
        thumbnails.ThumbnailCache(repository)
    monkeypatch.setenv("IMAGE_THUMBNAIL_CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.setattr(thumbnails, "filesystem_mount", lambda _: (tmp_path, "tmpfs", "tmpfs"))
    with pytest.raises(ValueError, match="persistente"):
        thumbnails.ThumbnailCache(repository)
    monkeypatch.setattr(thumbnails, "filesystem_mount", lambda _: None)
    monkeypatch.setattr(thumbnails, "require_local", Mock(side_effect=ValueError("NFS rejected")))
    with pytest.raises(ValueError, match="NFS"):
        thumbnails.ThumbnailCache(repository)


def test_catalog_listing_no_per_original_metadata_or_bytes(repository, monkeypatch):
    for index in range(15):
        save(repository, ean=str(index + 1))
    monkeypatch.setattr(repository, "resolve", Mock(side_effect=AssertionError("NAS resolve")))
    monkeypatch.setattr(repository, "verified_bytes", Mock(side_effect=AssertionError("NAS read")))
    assert len(repository.catalog_records()) == len(repository.catalog_rows()) == 15


def test_ui_only_visible_manual_refresh_hot_no_nfs_original_reads(repository, monkeypatch):
    for index in range(15):
        save(repository, f"{index}.png", (index * 10, 20, 30))
    reader = Mock(wraps=ImageRepository.verified_bytes)
    monkeypatch.setattr(ImageRepository, "verified_bytes", lambda self, record: reader(self, record))
    validator = Mock(wraps=ui.validate_visible)
    monkeypatch.setattr(ui, "validate_visible", validator)
    app = AppTest.from_string(
        f"from image_repository import ImageRepository\nfrom kering_images_ui import render_gallery\n"
        f"render_gallery(ImageRepository(), {EAN!r}, 'test')")
    app.run()
    assert not app.exception
    assert reader.call_count == 12 and validator.call_count == 1
    original_stat = Path.stat
    def no_original_stat(path, *args, **kwargs):
        if repository.root in path.parents and path.suffix == ".png":
            raise AssertionError(f"Original stat on rerender: {path}")
        return original_stat(path, *args, **kwargs)
    monkeypatch.setattr(Path, "stat", no_original_stat)
    app.run()
    assert reader.call_count == 12 and validator.call_count == 1
    monkeypatch.setattr(Path, "stat", original_stat)
    app.number_input(key=f"test-{EAN}-page-2").set_value(2).run()
    assert reader.call_count == 15 and validator.call_count == 2
    app.button(key=f"test-{EAN}-refresh").click().run()
    assert reader.call_count == 15 and validator.call_count == 3
    record = repository.catalog_records(EAN)[12]
    record.path.unlink()
    app.button(key=f"test-{EAN}-refresh").click().run()
    assert any("no validado" in item.value for item in app.warning)
    assert not app.exception and reader.call_count == 15


def test_original_zip_names_and_associations_unchanged(repository):
    record = save(repository)
    repository.associate_order("Kering", "7", [EAN])
    thumbnails.ThumbnailCache(repository).get(record, access(repository, record))
    with ZipFile(BytesIO(repository.zip_eans([EAN]))) as archive:
        assert archive.namelist() == [f"{EAN}/original.png"]
        assert archive.read(f"{EAN}/original.png") == photo()
    assert repository.order_records("Kering", "7") == [record]


def test_cache_failure_is_explicit_placeholder_not_original_fallback(repository, monkeypatch):
    save(repository)
    monkeypatch.setattr(thumbnails.ThumbnailCache, "get", Mock(side_effect=OSError("cache read-only")))
    monkeypatch.setattr(ImageRepository, "verified_bytes", Mock(side_effect=AssertionError("Fallback read")))
    app = AppTest.from_string(
        f"from image_repository import ImageRepository\nfrom kering_images_ui import render_gallery\n"
        f"render_gallery(ImageRepository(), {EAN!r}, 'test')")
    app.run()
    assert not app.exception
    assert any("cache read-only" in warning.value for warning in app.warning)


def test_general_gallery_uses_same_cache_no_zip_or_original_on_hot_render(repository, monkeypatch):
    for index in range(27):
        save(repository, f"{index}.png", (index * 8, 20, 30))
    from app_pages.luxoptica import render_images_page

    reader = Mock(wraps=ImageRepository.verified_bytes)
    zipper = Mock(return_value=b"synthetic zip")
    monkeypatch.setattr(ImageRepository, "verified_bytes", lambda self, record: reader(self, record))
    monkeypatch.setattr(ImageRepository, "zip_eans", zipper)
    app = AppTest.from_string(
        "import streamlit as st\n"
        "from app_pages.luxoptica import render_images_page\n"
        "st.session_state['auth_role']='admin'\n"
        "def render_sidebar_shell(*args): pass\n"
        "def require_admin_access(): assert st.session_state['auth_role']=='admin'\n"
        "def render_footer(): pass\n"
        "render_images_page(render_sidebar_shell, require_admin_access, render_footer)")
    app.run()
    assert not app.exception and reader.call_count == 24 and zipper.call_count == 0
    app.run()
    assert not app.exception and reader.call_count == 24
    app.number_input(key="luxoptica_gallery_page_input").set_value(2).run()
    assert not app.exception and reader.call_count == 27
    app.button(key="image_gallery_refresh").click().run()
    assert not app.exception and reader.call_count == 27
    app.button(key="image_repository_prepare_zip").click().run()
    assert not app.exception and zipper.call_count == 1


def test_different_ean_same_content_reuses_generation_once_each_source_verified(repository, monkeypatch):
    first = save(repository)
    second = save(repository, ean="2", provider="Luxoptica")
    generator = Mock(wraps=thumbnails.generate)
    reader = Mock(wraps=repository.verified_bytes)
    monkeypatch.setattr(thumbnails, "generate", generator)
    monkeypatch.setattr(repository, "verified_bytes", reader)
    cache = thumbnails.ThumbnailCache(repository)
    assert cache.get(first, access(repository, first)) == cache.get(second, access(repository, second))
    assert generator.call_count == 1 and reader.call_count == 2
    cache.get(second, access(repository, second))
    assert reader.call_count == 2
