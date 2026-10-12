"""Offline synthetic benchmark. No production/NFS/provider access."""
import argparse
from io import BytesIO
import json
import os
from pathlib import Path
import time
from unittest.mock import patch

from PIL import Image
import numpy as np
from streamlit.testing.v1 import AppTest

from image_repository import ImageRepository
from image_thumbnails import ThumbnailCache, validate_visible
import image_thumbnails


def run(directory: Path) -> dict:
    if directory.exists():
        raise ValueError("Benchmark requiere un directorio nuevo y aislado")
    directory.mkdir(parents=True)
    os.environ.update({
        "IMAGE_REPOSITORY_ROOT": str(directory / "originals"),
        "IMAGE_REPOSITORY_STATE_ROOT": str(directory / "catalog"),
        "IMAGE_THUMBNAIL_CACHE_ROOT": str(directory / "thumbnails"),
        "IMAGE_THUMBNAIL_CACHE_RESERVE_BYTES": "1",
    })
    os.umask(0o077)
    repository = ImageRepository()
    random = np.random.default_rng(40)
    for index in range(48):
        stream = BytesIO()
        Image.fromarray(random.integers(0, 256, (900, 1200, 3), dtype=np.uint8)).save(
            stream, format="JPEG", quality=90)
        repository.save(str(index + 1), f"original-{index}.jpg", stream.getvalue(),
                        provider="Synthetic", origin="benchmark")
    report = {"sample": "48 seeded synthetic JPEGs 1200x900; same 24 visible; local disk",
              "real_nfs_measurements": None, "production_data_used": False, "runs": []}
    original_reader = Path.read_bytes
    original_generator = image_thumbnails.generate
    for scenario in ("before-general", "after-cold", "after-hot"):
        metrics = {"scenario": scenario, "catalog_ms": 0, "generation_ms": 0,
                   "original_read_ms": 0, "original_reads": 0, "original_bytes": 0,
                   "render_ms": 0, "access_validation_ms": 0}
        def read(path):
            started = time.perf_counter()
            result = original_reader(path)
            if repository.root in path.parents:
                metrics["original_read_ms"] += (time.perf_counter() - started) * 1000
                metrics["original_reads"] += 1
                metrics["original_bytes"] += len(result)
            return result
        def generate(*args):
            started = time.perf_counter()
            result = original_generator(*args)
            metrics["generation_ms"] += (time.perf_counter() - started) * 1000
            return result
        started = time.perf_counter()
        with patch.object(Path, "read_bytes", read), patch.object(image_thumbnails, "generate", generate):
            queried = time.perf_counter()
            if scenario == "before-general":
                rows = [record for record in repository.records() if record.path.is_file()]
            else:
                rows = repository.catalog_records()
            visible = rows[:24]
            metrics["catalog_ms"] = (time.perf_counter() - queried) * 1000
            if scenario == "before-general":
                repository.zip_eans([record.ean for record in rows])
                previews = [record.path.read_bytes() for record in visible]
            else:
                queried = time.perf_counter()
                proofs = validate_visible(repository, visible)
                metrics["access_validation_ms"] = (time.perf_counter() - queried) * 1000
                with ThumbnailCache(repository).batch(visible) as cache:
                    previews = [cache.get(record, proofs[record.id]) for record in visible]
            app = AppTest.from_string(
                "import streamlit as st\nimport time\n"
                "started=time.perf_counter()\n"
                "for data in st.session_state['previews']:\n    st.image(data, width='stretch')\n"
                "st.session_state['render_ms']=(time.perf_counter()-started)*1000")
            app.session_state["previews"] = previews
            app.run()
            if app.exception:
                raise RuntimeError(str(app.exception))
            metrics["render_ms"] = app.session_state["render_ms"]
        metrics["total_ms"] = (time.perf_counter() - started) * 1000
        report["runs"].append(metrics)
    assert report["runs"][-1]["original_reads"] == 0
    assert report["runs"][-1]["original_bytes"] == 0
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    result = run(arguments.directory.absolute())
    text = json.dumps(result, indent=2)
    if arguments.output:
        arguments.output.write_text(text + "\n", encoding="utf-8")
    print(text)
