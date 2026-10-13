"""User-confirmed Kering viewer rule: slot 1 lateral (V3), slot 2 frontal (V1), slot 3 detail."""
from io import BytesIO
from unittest.mock import Mock

from cryptography.fernet import Fernet
from PIL import Image
import pytest

from image_exports import order_export_plan
from image_naming import kering_filename
from kering_images import ImageStore, VIEWS, count_complete_lines, order_status, process_ean
from kering_media import POSITIONAL_RULE, classify_viewer
from kering_portal import KeringPortal


EAN = "0012345678901"
CDN = "https://picture.kecdn.net/"


def photo(index):
    stream = BytesIO()
    Image.new("RGB", (600, 600), (index * 20, 50, 70)).save(stream, "PNG")
    return stream.getvalue()


def order():
    return {"id": 1, "name": "PO/1", "supplier_id": 7, "state": "purchase", "date_order": "2026-10-09 12:00:00",
            "lines": [{"id": 1, "ean": EAN, "product_id": 1, "product_name": "Modelo"}]}


@pytest.fixture(autouse=True)
def offline(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAGE_REPOSITORY_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("KERING_DATA_ROOT", str(tmp_path / "kering"))
    monkeypatch.setenv("KERING_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr("kering_portal.sync_playwright", Mock(side_effect=AssertionError("offline")))


def viewer(monkeypatch, urls, extra=None):
    portal = KeringPortal({})
    images = [{"url": url if "://" in url else CDN + url, "reference": "MODEL-001", **(extra or {}).get(url, {})}
              for url in urls]
    monkeypatch.setattr(portal, "_start", lambda: None)
    monkeypatch.setattr(portal, "_find_product", lambda ean: {"ean": EAN, "reference": "MODEL-001", "images": images})
    distinct = list(dict.fromkeys(image["url"].split("?")[0].split("#")[0] for image in images))
    reader = Mock(side_effect=lambda url: photo(distinct.index(url.split("?")[0].split("#")[0]) + 1))
    monkeypatch.setattr(portal, "_read_image", reader)
    return portal, reader


def by_origin(store):
    return {record.origin.rsplit("/", 1)[1]: record for record in store.repository.records(EAN)}


def test_opaque_viewer_maps_lateral_frontal_detail_with_shared_naming(monkeypatch, tmp_path):
    portal, reader = viewer(monkeypatch, ["a.png", "b.png", "c.png"])
    store = ImageStore(tmp_path / "kering")
    result = process_ean(store, portal, EAN)
    records = by_origin(store)
    assert {name: record.view for name, record in records.items()} == {
        "a.png": "lateral", "b.png": "frontal", "c.png": "detalle"}
    assert records["a.png"].name == kering_filename("MODEL", "001", "lateral", ".png", "a.png") == "MODEL__001__shad__lt.png"
    assert records["b.png"].name == "MODEL__001__noshad__fr.png"
    assert records["c.png"].name == "c.png"
    detection = records["a.png"].metadata["view_detection"]
    assert detection["rule"] == POSITIONAL_RULE and detection["explicit_view"] == "unknown"
    assert detection["provenance"]["source"] == "kering_positional_rule"
    assert detection["provenance"]["confirmed_by"] == "user"
    assert detection["viewer_position"] == 1 and detection["viewer_size"] == 3
    # Detail never becomes perspectiva (V2): the EAN stays partial.
    assert result["views"]["perspectiva"] == "Pendiente"
    assert result["views"]["lateral"] == result["views"]["frontal"] == "Descargada"
    assert "view_review" not in result and order_status(order(), {EAN: result}) == "Parcial"
    assert count_complete_lines(store, order(), {}) == 0


def test_viewer_order_not_download_order():
    images = [{"url": CDN + name} for name in ("z.png", "a.png", "m.png")]
    assert [item["normalized_view"] for item in classify_viewer(images)] == ["lateral", "frontal", "detalle"]
    assert [item["normalized_view"] for item in classify_viewer(images[::-1])] == ["lateral", "frontal", "detalle"]
    assert [item["url"] for item in classify_viewer(images)] == [image["url"] for image in images]


def test_downloads_finishing_out_of_order_keep_viewer_slots(monkeypatch, tmp_path):
    portal, reader = viewer(monkeypatch, ["a.png", "b.png", "c.png"])
    contents = {CDN + "a.png": photo(1), CDN + "b.png": photo(2), CDN + "c.png": photo(3)}
    finished = []

    def read(url):
        finished.insert(0, url)  # last request "finishes" first
        return contents[url]
    reader.side_effect = read
    store = ImageStore(tmp_path / "kering")
    process_ean(store, portal, EAN)
    assert {name: record.view for name, record in by_origin(store).items()} == {
        "a.png": "lateral", "b.png": "frontal", "c.png": "detalle"}


def test_duplicates_removed_before_assigning_positions(monkeypatch, tmp_path):
    portal, reader = viewer(monkeypatch, ["a.png", "a.png?v=2", "a.png#zoom", "b.png", "b.png", "c.png"])
    store = ImageStore(tmp_path / "kering")
    result = process_ean(store, portal, EAN)
    assert reader.call_count == 3
    assert {name: record.view for name, record in by_origin(store).items()} == {
        "a.png": "lateral", "b.png": "frontal", "c.png": "detalle"}
    assert "view_review" not in result


def test_incomplete_viewer_flags_review_and_skips_position(monkeypatch, tmp_path):
    portal, reader = viewer(monkeypatch, ["a.png", "a.png?dup", "b.png"])
    store = ImageStore(tmp_path / "kering")
    result = process_ean(store, portal, EAN)
    assert result["view_review"]["reasons"] == ["visor_incompleto"]
    assert {record.view for record in store.repository.records(EAN)} == {"unknown"}
    assert all("provenance" not in record.metadata["view_detection"] for record in store.repository.records(EAN))
    assert not result.get("review_blocking")


def test_contradictory_label_kept_flagged_blocks_completion_and_still_exported(monkeypatch, tmp_path):
    names = ["MODEL__001__noshad__fr.png", "b.png", "c.png", "MODEL__001__shad__lt.png", "MODEL__001__noshad__qt.png"]
    portal, reader = viewer(monkeypatch, names)
    store = ImageStore(tmp_path / "kering")
    result = process_ean(store, portal, EAN)
    records = by_origin(store)
    assert records[names[0]].view == "frontal" and records[names[0]].name == names[0]
    assert records["b.png"].view == records["c.png"].view == "unknown"
    assert set(store.valid_views(EAN)) == set(VIEWS)
    assert result["review_blocking"] and result["reason"] == "revision_vistas"
    assert "etiquetas_contradictorias" in result["view_review"]["reasons"]
    contradictions = {item["viewer_position"]: item for item in result["view_review"]["contradictions"]}
    assert contradictions[1]["explicit_view"] == "frontal" and contradictions[1]["position_expected"] == "lateral"
    assert set(contradictions) == {1, 4}
    assert order_status(order(), {EAN: result}) == "Parcial"
    assert count_complete_lines(store, order(), {}) == 0
    # Persisted: the "three valid views" shortcut cannot declare it complete on a later run.
    again = process_ean(store, portal, EAN)
    assert again["review_blocking"] and reader.call_count == 5
    plan = order_export_plan(store.repository, order())
    assert len(plan.files) == 5
    # A person confirming every contradicted image unblocks completion; files are never renamed.
    for name in (names[0], names[3]):
        record = by_origin(store)[name]
        store.repository.review_view(record.id, record.view, reviewer="operator", reason="Checked in viewer")
    assert store.blocking_view_review(EAN) is None
    assert count_complete_lines(store, order(), {}) == 1
    assert by_origin(store)[names[0]].name == names[0]


def test_conflicting_signals_are_unknown_and_reviewed():
    result = classify_viewer([
        {"url": CDN + "M__001__noshad__fr.png", "original_name": "M__001__shad__lt.png"},
        {"url": CDN + "b.png"}, {"url": CDN + "c.png"}])
    assert [item["normalized_view"] for item in result] == ["unknown"] * 3
    assert result[0]["review"]["reasons"] == ["etiquetas_contradictorias"]


def test_extra_images_preserved_and_explicit_v2_completes(monkeypatch, tmp_path):
    names = ["a.png", "b.png", "c.png", "d.png", "MODEL__001__noshad__qt.png", "e.png"]
    portal, reader = viewer(monkeypatch, names)
    store = ImageStore(tmp_path / "kering")
    result = process_ean(store, portal, EAN)
    views = {name: record.view for name, record in by_origin(store).items()}
    assert views == {"a.png": "lateral", "b.png": "frontal", "c.png": "detalle", "d.png": "unknown",
                     names[4]: "perspectiva", "e.png": "unknown"}
    assert "view_review" not in result and "Pendiente" not in result["views"].values()
    assert order_status(order(), {EAN: result}) == "Completo"
    assert count_complete_lines(store, order(), {}) == 1


def test_matching_explicit_labels_keep_original_rule(monkeypatch, tmp_path):
    names = ["MODEL__001__shad__lt.png", "b.png", "c.png"]
    portal, reader = viewer(monkeypatch, names)
    store = ImageStore(tmp_path / "kering")
    process_ean(store, portal, EAN)
    records = by_origin(store)
    assert records[names[0]].metadata["view_detection"]["rule"] == "luxoptica_original_filename_v1"
    assert records["b.png"].view == "frontal" and records["b.png"].metadata["view_detection"]["rule"] == POSITIONAL_RULE


def test_historical_unknown_and_generated_names_not_reclassified(monkeypatch, tmp_path):
    portal, reader = viewer(monkeypatch, ["a.png", "b.png", "c.png"])
    store = ImageStore(tmp_path / "kering")
    # Historical record with an app-generated frontal name but unknown view: neither position nor name is evidence.
    old, _ = store.repository.save(EAN, "MODEL__001__noshad__fr.png", photo(1), provider="Kering",
                                   origin=CDN + "a.png", view="unknown")
    process_ean(store, portal, EAN)
    records = by_origin(store)
    assert records["a.png"].id == old.id and records["a.png"].view == "unknown"
    assert not records["a.png"].metadata.get("view_review")
    assert reader.call_count == 2
    assert records["b.png"].view == "frontal" and records["c.png"].view == "detalle"
    with store.repository.connect() as connection:
        assert not connection.execute("SELECT 1 FROM events WHERE action='view_review'").fetchall()
