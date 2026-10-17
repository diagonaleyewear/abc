from hashlib import sha256
from io import BytesIO
import json
import sqlite3

from PIL import Image
import pytest

from image_naming import kering_filename
from image_repository import ImageRepository
from kering_media import POSITIONAL_PROVENANCE, POSITIONAL_RULE, REVIEW_CONFLICT
from market_publication import (
    MANIFEST_NAME,
    create_publication_plan,
    publish_market_images,
    ReadOnlyKeringReviewReader,
    ReadOnlyImageRepository,
)


EAN = "0012345678901"


class NoBlockingKeringReview:
    def has_blocking_review(self, ean, records) -> bool:
        return False


REVIEW_READER = NoBlockingKeringReview()


def photo(color: tuple[int, int, int], format: str = "PNG") -> bytes:
    stream = BytesIO()
    Image.new("RGB", (600, 600), color).save(stream, format=format)
    return stream.getvalue()


@pytest.fixture
def repository(tmp_path, monkeypatch) -> ImageRepository:
    root = tmp_path / "canonical-images"
    monkeypatch.setenv("IMAGE_REPOSITORY_ROOT", str(root))
    return ImageRepository(root)


def save_lux(repository: ImageRepository, view: str, content: bytes | None = None):
    file_views = {"frontal": "noshad__fr", "perspectiva": "noshad__qt", "lateral": "shad__lt"}
    record, _ = repository.save(
        EAN,
        f"MODEL__COLOR__{file_views[view]}.png",
        content or photo((20, 40, 60)),
        provider="Luxoptica",
        origin="synthetic",
        view=view,
    )
    return record


def save_kering(repository: ImageRepository, view: str, index: int, *, detection=None):
    filename = kering_filename("MODEL", "COLOR", view, ".png", "opaque.png")
    metadata = {
        "view_detection": detection or {
            "normalized_view": view,
            "rule": "luxoptica_original_filename_v1",
        },
    }
    return repository.save(
        EAN,
        filename,
        photo((index * 40, 30, 90)),
        provider="Kering",
        origin=f"synthetic-{index}",
        view=view,
        metadata=metadata,
    )[0]


def ids(**markets) -> dict[str, dict[str, str]]:
    return {EAN: markets}


def test_luxoptica_dry_run_and_idempotent_publish_keep_originals(repository, tmp_path):
    original = save_lux(repository, "frontal")
    canonical_records = repository.records(EAN)
    destination = tmp_path / "publication"
    plan = create_publication_plan(
        repository, ids(Farfetch="12345", Miinto="market-uuid"), destination
    )

    assert plan.report()["create"] == 2
    simulated_signature = plan.signature()
    assert not destination.exists()
    assert original.path.read_bytes() == photo((20, 40, 60))
    assert repository.records(EAN) == canonical_records

    result = publish_market_images(repository, plan)
    assert plan.signature() == simulated_signature
    assert result == {"created": 2, "unchanged": 0, "manifest_unchanged": False, "skipped": 2}
    farfetch = destination / "Farfetch" / EAN / "12345_V1.png"
    miinto = destination / "Miinto" / EAN / "market-uuid_V1.jpeg"
    assert farfetch.read_bytes() == original.path.read_bytes()
    assert miinto.read_bytes() == original.path.read_bytes()
    assert Image.open(miinto).format == "PNG"

    manifest = json.loads((destination / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert {item["output_sha256"] for item in manifest["files"]} == {
        sha256(original.path.read_bytes()).hexdigest()
    }
    assert all(item["source_record_id"] == original.id for item in manifest["files"])
    assert all(item["source"].startswith(f"{EAN}/") for item in manifest["files"])
    next_plan = create_publication_plan(
        repository, ids(Farfetch="12345", Miinto="market-uuid"), destination
    )
    assert {entry.action for entry in next_plan.entries} == {"unchanged"}
    assert publish_market_images(repository, next_plan) == {
        "created": 0, "unchanged": 2, "manifest_unchanged": True, "skipped": 2,
    }


def test_kering_partial_photos_publish_only_accredited_views(repository, tmp_path):
    frontal = save_kering(repository, "frontal", 1)
    lateral = save_kering(
        repository,
        "lateral",
        2,
        detection={
            "normalized_view": "lateral",
            "rule": POSITIONAL_RULE,
            "provenance": POSITIONAL_PROVENANCE,
        },
    )
    detail, _ = repository.save(
        EAN,
        "detail.png",
        photo((120, 30, 90)),
        provider="Kering",
        origin="synthetic-detail",
        view="detalle",
    )
    plan = create_publication_plan(
        repository, ids(Farfetch="F-1", Miinto="M-1"), tmp_path / "publication",
        kering_reviews=REVIEW_READER,
    )

    assert {(entry.market, entry.view) for entry in plan.entries} == {
        ("Farfetch", "frontal"), ("Farfetch", "lateral"),
        ("Miinto", "frontal"), ("Miinto", "lateral"),
    }
    assert all("_V2." not in entry.relative_path for entry in plan.entries)
    assert any(item["source"] == detail.name and item["reason"] == "vista_no_publicable"
               for item in plan.skipped)
    assert all(entry.record.path in {frontal.path, lateral.path} for entry in plan.entries)


def test_kering_webp_is_exported_only_to_farfetch_without_changing_source(repository, tmp_path):
    source = save_kering(repository, "frontal", 2)
    webp, _ = repository.save(
        EAN,
        kering_filename("MODEL", "COLOR", "lateral", ".webp", "opaque.webp"),
        photo((70, 20, 110), format="WEBP"),
        provider="Kering",
        origin="synthetic-webp",
        view="lateral",
        metadata={"view_detection": {
            "normalized_view": "lateral",
            "rule": "luxoptica_original_filename_v1",
        }},
    )
    source_bytes = webp.path.read_bytes()
    plan = create_publication_plan(
        repository, ids(Farfetch="F", Miinto="M"), tmp_path / "publication",
        kering_reviews=REVIEW_READER,
    )
    publish_market_images(repository, plan)

    farfetch = tmp_path / "publication" / "Farfetch" / EAN / "F_V3.png"
    miinto = tmp_path / "publication" / "Miinto" / EAN / "M_V3.jpeg"
    assert Image.open(farfetch).format == "PNG"
    assert not miinto.exists()
    assert any(
        item.get("market") == "Miinto" and item["reason"] == "formato_no_admitido"
        for item in plan.skipped
    )
    assert webp.path.read_bytes() == source_bytes
    assert source.path.exists()


def test_unaccredited_kering_view_and_unresolved_conflict_are_not_published(repository, tmp_path):
    save_kering(repository, "frontal", 1, detection={
        "normalized_view": "unknown",
        "rule": "conflicting_signals",
        "review": {
            "required": True,
            "reasons": [REVIEW_CONFLICT],
            "contradicts_position": True,
        },
    })
    plan = create_publication_plan(
        repository, ids(Farfetch="F"), tmp_path / "publication"
    )

    assert not plan.entries
    assert any(item["reason"] == "revision_kering_pendiente" for item in plan.skipped)
    assert not (tmp_path / "publication").exists()


def test_automatic_kering_adapter_review_does_not_clear_a_classification_conflict(repository, tmp_path):
    record = save_kering(repository, "unknown", 1, detection={
        "normalized_view": "unknown",
        "rule": "conflicting_signals",
        "review": {
            "required": True,
            "reasons": [REVIEW_CONFLICT],
            "contradicts_position": True,
        },
    })
    repository.review_view(
        record.id,
        "frontal",
        reviewer="kering-adapter",
        reason="Verified original filename pattern",
    )
    plan = create_publication_plan(
        repository, ids(Farfetch="F"), tmp_path / "publication",
        kering_reviews=REVIEW_READER,
    )

    assert not plan.entries
    assert any(item["reason"] == "revision_kering_pendiente" for item in plan.skipped)


def test_unreviewed_view_classification_is_excluded(repository, tmp_path):
    record = save_kering(repository, "frontal", 1, detection={
        "normalized_view": "frontal",
        "rule": "insufficient_evidence",
    })
    plan = create_publication_plan(
        repository, ids(Farfetch="F"), tmp_path / "publication", kering_reviews=REVIEW_READER
    )

    assert not plan.entries
    assert any(item["source"] == record.name and item["reason"] == "vista_no_acreditada"
               for item in plan.skipped)


def test_missing_market_id_is_pending_without_fabricating_a_name(repository, tmp_path):
    save_lux(repository, "frontal")
    plan = create_publication_plan(repository, {EAN: {"Farfetch": "", "Miinto": None}},
                                   tmp_path / "publication")

    assert not plan.entries
    assert {item["market"] for item in plan.skipped if item["reason"] == "id_comercial_pendiente"} == {
        "Farfetch", "Miinto",
    }


def test_unsafe_market_id_is_rejected(repository, tmp_path):
    save_lux(repository, "frontal")

    with pytest.raises(ValueError, match="id_comercial_invalido"):
        create_publication_plan(repository, ids(Farfetch="../outside"), tmp_path / "publication")
    assert not (tmp_path / "outside").exists()


def test_read_only_repository_does_not_change_canonical_tree(repository):
    original = save_lux(repository, "frontal")
    before = {
        path.relative_to(repository.root): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in repository.root.rglob("*")
        if path.is_file()
    }
    reader = ReadOnlyImageRepository(repository.root)

    assert reader.records(EAN)[0].path == original.path
    assert reader.verified_bytes(reader.records(EAN)[0]) == original.path.read_bytes()
    after = {
        path.relative_to(repository.root): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in repository.root.rglob("*")
        if path.is_file()
    }
    assert after == before
    with pytest.raises(sqlite3.OperationalError):
        with reader._connect() as connection:
            connection.execute("DELETE FROM assets")


def test_missing_kering_history_fails_closed_for_kering_images(repository, tmp_path):
    save_kering(repository, "frontal", 1)
    plan = create_publication_plan(repository, ids(Farfetch="F"), tmp_path / "publication")

    assert not plan.entries
    assert any(item["reason"] == "historia_revision_no_disponible" for item in plan.skipped)


def test_read_only_kering_history_honors_review_timestamp(repository, tmp_path):
    record = save_kering(repository, "frontal", 1, detection={
        "normalized_view": "unknown",
        "rule": "conflicting_signals",
        "review": {
            "required": True,
            "reasons": [REVIEW_CONFLICT],
            "contradicts_position": True,
        },
    })
    repository.review_view(
        record.id, "frontal", reviewer="operator", reason="Checked original filename"
    )
    review_root = tmp_path / "kering"
    review_root.mkdir()
    database = review_root / "history.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE view_reviews (ean TEXT PRIMARY KEY, review TEXT NOT NULL, created TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO view_reviews VALUES (?, ?, ?)",
            (EAN, json.dumps({
                "reasons": [REVIEW_CONFLICT],
                "contradictions": [{"url": record.origin}],
            }), "2999-01-01T00:00:00+00:00"),
        )
    reader = ReadOnlyKeringReviewReader(review_root)
    plan = create_publication_plan(
        repository, ids(Farfetch="F"), tmp_path / "publication", kering_reviews=reader
    )

    assert not plan.entries
    assert any(item["reason"] == "revision_kering_pendiente" for item in plan.skipped)


def test_existing_different_bytes_are_a_collision_and_recovery_is_safe(repository, tmp_path):
    original = save_lux(repository, "frontal")
    destination = tmp_path / "publication"
    target = destination / "Farfetch" / EAN / "F_V1.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(photo((240, 0, 0)))

    with pytest.raises(ValueError, match="colision_destino"):
        create_publication_plan(repository, ids(Farfetch="F"), destination)
    assert target.read_bytes() == photo((240, 0, 0))
    assert original.path.read_bytes() == photo((20, 40, 60))

    target.unlink()
    plan = create_publication_plan(repository, ids(Farfetch="F"), destination)
    assert publish_market_images(repository, plan)["created"] == 1
    assert target.read_bytes() == original.path.read_bytes()


def test_stale_managed_outputs_are_reported_and_never_deleted(repository, tmp_path):
    save_lux(repository, "frontal")
    destination = tmp_path / "publication"
    first_plan = create_publication_plan(repository, ids(Farfetch="old-id"), destination)
    publish_market_images(repository, first_plan)
    old_file = destination / "Farfetch" / EAN / "old-id_V1.png"

    next_plan = create_publication_plan(repository, ids(Farfetch="new-id"), destination)

    assert next_plan.stale_files[0]["path"] == f"Farfetch/{EAN}/old-id_V1.png"
    assert next_plan.stale_files[0]["reason"] == "stale"
    with pytest.raises(ValueError, match="salidas_obsoletas_requieren_revision_manual"):
        publish_market_images(repository, next_plan)
    assert old_file.is_file()
    assert not (destination / "Farfetch" / EAN / "new-id_V1.png").exists()


def test_destination_change_after_simulation_requires_new_plan(repository, tmp_path):
    original = save_lux(repository, "frontal")
    destination = tmp_path / "publication"
    plan = create_publication_plan(repository, ids(Farfetch="F"), destination)
    target = destination / "Farfetch" / EAN / "F_V1.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(original.path.read_bytes())

    with pytest.raises(ValueError, match="destino_cambio_despues_de_simulacion"):
        publish_market_images(repository, plan)
    assert target.read_bytes() == original.path.read_bytes()


def test_checksum_failure_during_recovery_does_not_publish(repository, tmp_path):
    original = save_lux(repository, "frontal")
    plan = create_publication_plan(
        repository, ids(Farfetch="F"), tmp_path / "publication"
    )
    original.path.write_bytes(b"tampered")

    with pytest.raises(ValueError, match="checksum_incorrecto"):
        publish_market_images(repository, plan)
    assert not (tmp_path / "publication" / "Farfetch").exists()


def test_foreign_manifest_is_never_overwritten(repository, tmp_path):
    save_lux(repository, "frontal")
    destination = tmp_path / "publication"
    destination.mkdir()
    manifest = destination / MANIFEST_NAME
    manifest.write_text('{"managed_by":"other"}', encoding="utf-8")

    with pytest.raises(ValueError, match="colision_con_manifiesto_no_gestionado"):
        create_publication_plan(repository, ids(Farfetch="F"), destination)
    assert manifest.read_text(encoding="utf-8") == '{"managed_by":"other"}'
