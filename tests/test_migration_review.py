import json

import pytest

from scripts.report_migration_review import build_review


def test_ambiguities_keep_all_candidates_and_nonreusable_originals(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"products": [
        {"modelo": "000model", "color": "01", "ean": "123"},
        {"modelo": "000model", "color": "01", "ean": "456"},
        {"modelo": "000model", "color": "01", "ean": "123"},
    ]}))
    entry = {"source": "legacy/same.png", "provider": "Kering", "name": "same.png",
             "checksum": "original", "size": 10, "ean": "", "image": True,
             "valid": False, "view": "unknown"}
    plan = {"id": "prior", "entries": [entry], "blockers": ["EAN unresolved", "invalid"]}
    before = path.read_bytes()
    report = build_review(plan, [path])
    assert set(report["ambiguous_mappings"][0]["candidates"]) == {"123", "456"}
    assert len(report["unresolved_images"]) == len(report["nonreusable_images"]) == 1
    assert report["nonreusable_images"][0]["checksum"] == "original"
    assert report["original_blockers"] == plan["blockers"]
    assert report["summary"]["prior_inventory_not_fresh_scan"]
    assert not report["unknown_views"]
    assert path.read_bytes() == before


def test_unknown_valid_does_not_become_nonreusable():
    plan = {"id": "prior", "entries": [
        {"source": "legacy/opaque", "provider": "Kering", "ean": "123",
         "image": True, "valid": True, "view": "unknown"},
    ], "blockers": []}
    report = build_review(plan, [])
    assert report["summary"]["unknown_prior_views"] == 1
    assert not report["nonreusable_images"] and not report["unresolved_images"]


def test_affected_files_follow_existing_naming_normalization(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"products": [
        {"modelo": "000model", "color": "01", "ean": "123"},
        {"modelo": "000model", "color": "01", "ean": "456"},
    ]}))
    plan = {"id": "prior", "entries": [
        {"source": "legacy/file", "name": "000model__01_001A.png", "ean": "",
         "checksum": "original", "image": True, "valid": True, "view": "unknown"},
    ], "blockers": ["unresolved"]}
    report = build_review(plan, [manifest])
    assert report["summary"]["prior_images_matching_ambiguous_keys"] == 1
    assert report["ambiguous_mappings"][0]["prior_matching_images"][0]["checksum"] == "original"
    assert report["original_blockers"] == ["unresolved"]


def test_cli_rejects_changed_signature_without_writing_report(tmp_path, monkeypatch):
    import sys
    from scripts.report_migration_review import main
    source, output = tmp_path / "plan.json", tmp_path / "report.json"
    source.write_text(json.dumps({"id": "untrusted", "entries": [], "blockers": []}))
    monkeypatch.setattr(sys, "argv", ["report", "--plan", str(source), "--output", str(output)])
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 2
    assert not output.exists()
