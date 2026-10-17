import json
from hashlib import sha256
from io import BytesIO
import sys
from pathlib import Path

from PIL import Image, ImageDraw
import pytest

import image_thumbnails as thumbnails
from image_repository import ImageRepository

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import regenerate_image_thumbnails as regen  # noqa: E402


def photo(seed, mode="RGBA"):
    image = Image.new("RGBA", (1600, 900), (0, 0, 0, 0))
    ImageDraw.Draw(image).line([(100, 100 + seed), (1500, 800)], fill=(20, 20, 20, 255), width=14)
    if mode == "P":
        image = image.convert("RGB").convert("P", palette=Image.Palette.ADAPTIVE, colors=8)
    stream = BytesIO()
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
    repository = ImageRepository()
    for number in range(6):
        provider = "Kering" if number % 2 else "Luxoptica"
        repository.save(f"{number + 1:013d}", f"{number}.png", photo(number, "P" if number % 3 == 0 else "RGBA"),
                        provider=provider, origin="synthetic", view="frontal")
    return repository


def run(*arguments):
    return regen.main(list(arguments))


def cache_files(repository):
    return sorted(p.name for p in thumbnails.ThumbnailCache(repository).root.glob("*.jpg"))


def originals(repository):
    return {r.path: sha256(r.path.read_bytes()).hexdigest() for r in repository.catalog_records()}


def test_report_and_default_are_dry_runs_that_write_nothing(repository, capsys):
    before = originals(repository)
    assert run("report", "--sample", "3") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["to_regenerate"] == 6 and data["already_regenerated"] == 0 and data["extra_bytes_estimate"] > 0
    assert run("regenerate") == 0
    assert cache_files(repository) == [] and originals(repository) == before


def test_regeneration_is_idempotent_and_never_touches_originals(repository, capsys):
    before = originals(repository)
    assert run("regenerate", "--execute", "--batch-size", "2") == 0
    first = cache_files(repository)
    assert len(first) == 6
    capsys.readouterr()
    assert run("regenerate", "--execute") == 0
    assert cache_files(repository) == first and json.loads(capsys.readouterr().out)["generated"] == 0
    assert originals(repository) == before
    assert not list(thumbnails.ThumbnailCache(repository).root.glob("*.tmp"))


def test_output_is_lanczos_flat_retina_and_keeps_proportions(repository):
    run("regenerate", "--execute")
    record = repository.catalog_records()[1]
    path = thumbnails.ThumbnailCache(repository).root / f"{regen._key(record)}.jpg"
    with Image.open(path) as image:
        assert image.size == (640, 360) and image.format == "JPEG"
        assert all(abs(c - e) <= 3 for c, e in zip(image.convert("RGB").getpixel((3, 3)), (0xf4, 0xf4, 0xf2)))


def test_resume_continues_after_limited_run(repository):
    assert run("regenerate", "--execute", "--limit", "2", "--batch-size", "1") == 0
    assert len(cache_files(repository)) == 2
    assert run("regenerate", "--execute", "--resume") == 0
    assert len(cache_files(repository)) == 6


def test_errors_are_logged_and_do_not_stop_the_batch(repository):
    broken = repository.catalog_records()[1]
    broken.path.unlink()
    assert run("regenerate", "--execute") == 1
    assert len(cache_files(repository)) == 5
    cache = thumbnails.ThumbnailCache(repository)
    lines = (cache.root / "regeneration-errors.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["id"] == broken.id


def test_records_sharing_an_original_share_one_derived_file(repository):
    content = photo(99)
    repository.save("0000000000099", "a.png", content, provider="Kering", origin="synthetic", view="frontal")
    repository.save("0000000000099", "b.png", content, provider="Kering", origin="synthetic", view="lateral")
    run("regenerate", "--execute")
    records = repository.catalog_records()
    assert len(cache_files(repository)) == len({r.checksum for r in records})


def test_provider_filter_limits_the_run(repository):
    run("regenerate", "--execute", "--provider", "kering")
    assert len(cache_files(repository)) == 3


def test_purge_legacy_removes_only_legacy_entries(repository, capsys):
    cache = thumbnails.ThumbnailCache(repository)
    records = repository.catalog_records()
    proofs = thumbnails.validate_visible(repository, records)
    for record in records:
        cache.get(record, proofs[record.id])
    run("regenerate", "--execute")
    assert len(cache_files(repository)) == 12
    capsys.readouterr()
    run("purge-legacy")
    assert json.loads(capsys.readouterr().out)["legacy_entries"] == 6 and len(cache_files(repository)) == 12
    run("purge-legacy", "--execute")
    assert len(cache_files(repository)) == 6
    assert {p.name for p in cache.root.glob("*.jpg")} == {f"{regen._key(r)}.jpg" for r in records}
    assert run("verify", "--per-provider", "5") == 0


def test_verify_reports_missing_and_detects_modified_original(repository, capsys):
    assert run("verify", "--per-provider", "2") == 1
    run("regenerate", "--execute")
    capsys.readouterr()
    assert run("verify", "--per-provider", "2") == 0
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["failures"] == 0 and summary["providers"] == ["Kering", "Luxoptica"]
