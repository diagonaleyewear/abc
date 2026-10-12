"""Isolated, network-free runtime smoke for persistent thumbnail storage."""
from concurrent.futures import ProcessPoolExecutor
from io import BytesIO
import json
import multiprocessing
import os
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from image_repository import ImageRepository
from image_thumbnails import ThumbnailCache, validate_visible


def worker(root):
    repository = ImageRepository(Path(root))
    record = repository.catalog_records("1")[0]
    proof = validate_visible(repository, [record])[record.id]
    return ThumbnailCache(repository).get(record, proof)


def main():
    os.umask(0o077)
    root = Path(os.environ["CLOUD_SMOKE_DIRECTORY"]) / "thumbnails-smoke"
    if root.exists():
        raise RuntimeError("Smoke requiere un destino aislado nuevo")
    root.mkdir()
    os.environ.update({
        "IMAGE_REPOSITORY_ROOT": str(root / "images"),
        "IMAGE_REPOSITORY_STATE_ROOT": str(root / "catalog"),
        "IMAGE_THUMBNAIL_CACHE_ROOT": str(root / "cache"),
        "IMAGE_THUMBNAIL_CACHE_RESERVE_BYTES": "1",
    })
    repository = ImageRepository()
    stream = BytesIO()
    Image.new("RGB", (1200, 600), (10, 30, 50)).save(stream, "PNG")
    record, _ = repository.save("1", "original.png", stream.getvalue(),
                                provider="Synthetic", origin="smoke")
    with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn")) as pool:
        results = list(pool.map(worker, [str(repository.root)] * 8))
    assert len(set(results)) == 1
    cache = ThumbnailCache(repository)
    proof = validate_visible(repository, [record])[record.id]
    with patch.object(repository, "verified_bytes", side_effect=AssertionError("Hot original read")):
        assert cache.get(record, proof) == results[0]
    assert len(list(cache.root.glob("*.jpg"))) == 1
    assert len(json.loads((cache.root / "index.json").read_text())) == 1
    assert not list(cache.root.glob("*.tmp"))
    assert cache.root.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in cache.root.iterdir())
    record.path.unlink()
    assert validate_visible(repository, [record])[record.id] is None
    print("Thumbnail smoke: cold/concurrent/hot zero original reads, permissions, missing original OK")


if __name__ == "__main__":
    main()
