from datetime import date
import hashlib
import random
import sqlite3
from contextlib import closing

import pytest

from image_repository import ImageRepository
from provider_images import (
    FAILED, IMPORTED, NEEDS_REVIEW, PARTIAL, PENDING, READY, ProviderService, ProviderSettings, ProviderStore,
    RequestStateError, UnverifiedProviderPortal, ean_valid, match_file, normalize_eans, parse_manifest,
    provider_demand, read_zip_package, PortalPackage,
)
from kering_images import AccessNotConfirmed, PortalTimeout
from provider_support import EANS, FakePortal, fake_odoo, make_ean, pfile, photo, zip_bytes


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    store = ProviderStore(tmp_path / "state", clock=clock)
    repo = ImageRepository(tmp_path / "images", state=tmp_path / "repo_state", backups=tmp_path / "backups")
    settings = ProviderSettings("marcolin", supplier_ids=(573,), max_eans_per_request=3, max_requests_per_day=5)
    return store, repo, settings, clock, ProviderService(store, repo, settings, rng=random.Random(1))


def test_ean_validation_and_normalization():
    assert ean_valid(EANS[0]) and not ean_valid(EANS[0][:-1] + "0" if EANS[0][-1] != "0" else EANS[0][:-1] + "1")
    valid, invalid = normalize_eans([EANS[0], EANS[0], "123", "", " " + EANS[1] + " "])
    assert valid == [EANS[0], EANS[1]] and invalid == ["123"]


def test_plan_chunks_and_never_duplicates_live_requests(env):
    store, _, settings, _, _ = env
    first = store.plan(settings, EANS[:7] + ["bad"])
    assert len(first["created"]) == 3 and first["invalid"] == ["bad"]
    again = store.plan(settings, EANS)
    assert set(again["already_live"]) == set(EANS[:7]) and len(again["created"]) == 1
    assert store.plan(settings, EANS)["created"] == []


def test_database_rejects_second_live_ean_even_bypassing_planner(env):
    store, _, settings, _, _ = env
    store.plan(settings, EANS[:1])
    rid = store.plan(settings, EANS[1:2])["created"][0]
    with closing(store.connect()) as c, pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO request_eans(request_id,provider,ean) VALUES(?,?,?)", (rid, "marcolin", EANS[0]))


def test_state_machine_rejects_invalid_transitions_and_events_are_append_only(env):
    store, _, settings, _, _ = env
    rid = store.plan(settings, EANS[:1])["created"][0]
    with pytest.raises(RequestStateError):
        store.transition(rid, IMPORTED)
    store.cancel(rid)
    with pytest.raises(RequestStateError):
        store.transition(rid, READY)
    with closing(store.connect()) as c, pytest.raises(sqlite3.DatabaseError):
        c.execute("DELETE FROM events")
    assert store.plan(settings, EANS[:1])["created"]  # cancelled request frees its EAN


def test_submit_is_dry_run_by_default_and_gated_by_flag(env, monkeypatch):
    store, _, settings, _, service = env
    store.plan(settings, EANS[:2])
    portal = FakePortal()
    assert service.run_cycle(portal) ["submitted"][0]["action"] == "dry_run"
    assert portal.submitted == []
    monkeypatch.delenv("MARCOLIN_AUTOMATION_ENABLED", raising=False)
    assert service.run_cycle(portal, dry_run=False) == {"skipped": "automatizacion_desactivada"}
    assert portal.submitted == []


def test_deferred_lifecycle_pending_then_available_then_imported(env, monkeypatch):
    store, repo, settings, clock, service = env
    monkeypatch.setenv("MARCOLIN_AUTOMATION_ENABLED", "true")
    rid = store.plan(settings, EANS[:2])["created"][0]
    portal = FakePortal(PortalPackage(files=[pfile(f"{EANS[0]}_front.png", view="frontal"), pfile(f"{EANS[1]}_front.png", view="frontal")]))
    service.run_cycle(portal, dry_run=False)
    assert store.get(rid)["state"] == PENDING and portal.submitted == [EANS[:2]]
    service.run_cycle(portal, dry_run=False)  # not due yet: no status call, no resubmission
    assert portal.status_calls == 0 and len(portal.submitted) == 1
    clock.now += 7200
    service.run_cycle(portal, dry_run=False)  # first poll: still pending
    assert store.get(rid)["state"] == PENDING and portal.status_calls == 1
    clock.now += 7 * 3600
    service.run_cycle(portal, dry_run=False)  # available -> imported in the same tick
    assert store.get(rid)["state"] == IMPORTED
    assert len(portal.submitted) == 1
    assert {r.ean for r in repo.records()} == set(EANS[:2])


def test_ambiguous_submit_failure_goes_to_review_and_is_never_resent(env, monkeypatch):
    store, _, settings, _, service = env
    monkeypatch.setenv("MARCOLIN_AUTOMATION_ENABLED", "true")
    rid = store.plan(settings, EANS[:1])["created"][0]
    portal = FakePortal()
    portal.submit_error = PortalTimeout()
    service.run_cycle(portal, dry_run=False)
    assert store.get(rid)["state"] == NEEDS_REVIEW
    portal.submit_error = None
    service.run_cycle(portal, dry_run=False)
    assert portal.submitted == [] and store.plan(settings, EANS[:1])["created"] == []
    store.reconcile(rid, remote_id="REQ-FROM-PORTAL", sent=True)
    assert store.get(rid)["state"] == PENDING and store.get(rid)["remote_id"] == "REQ-FROM-PORTAL"


def test_definite_not_sent_returns_to_ready(env, monkeypatch):
    store, _, settings, _, service = env
    monkeypatch.setenv("MARCOLIN_AUTOMATION_ENABLED", "true")
    rid = store.plan(settings, EANS[:1])["created"][0]
    service.run_cycle(UnverifiedProviderPortal(), dry_run=False)
    request = store.get(rid)
    assert request["state"] == READY and request["last_error"] == "PortalUnavailable"


def test_interrupted_submit_is_recovered_as_ambiguous(env):
    store, _, settings, _, _ = env
    rid = store.plan(settings, EANS[:1])["created"][0]
    store.transition(rid, "submitting")
    assert store.recover_interrupted() == 1 and store.get(rid)["state"] == NEEDS_REVIEW


def test_daily_request_limit(env, monkeypatch):
    store, _, settings, _, _ = env
    monkeypatch.setenv("MARCOLIN_AUTOMATION_ENABLED", "true")
    limited = ProviderSettings("marcolin", max_eans_per_request=1, max_requests_per_day=2)
    service = ProviderService(store, env[1], limited)
    store.plan(limited, EANS[:4])
    portal = FakePortal()
    service.run_cycle(portal, dry_run=False)
    assert len(portal.submitted) == 2


def test_import_evidence_provenance_and_unchanged_original(env):
    store, repo, settings, _, service = env
    rid = store.plan(settings, EANS[:2])["created"][0]
    data = photo()
    package = PortalPackage(
        files=[pfile("a.png", data), pfile("b.png"), pfile("0RB2140__901__noshad__fr.png"),
               pfile("zzz.png")],
        manifest=f"filename,ean,view\na.png,{EANS[0]},frontal\nb.png,{EANS[1]},\n".encode())
    result = service.import_package(rid, package)
    assert result["imported"] == 2 and result["review"] == 3  # b.png unknown view, 2 without EAN evidence
    record = next(r for r in repo.records(EANS[0]))
    assert record.provider == "marcolin" and record.view == "frontal" and record.origin == f"marcolin:{rid}"
    assert record.checksum == hashlib.sha256(data).hexdigest() == hashlib.sha256(record.path.read_bytes()).hexdigest()
    assert record.metadata["evidence"] == "manifest" and record.metadata["source_name"] == "a.png"
    assert next(r for r in repo.records(EANS[1])).view == "unknown"
    assert store.get(rid)["state"] == NEEDS_REVIEW


def test_review_resolution_completes_request(env):
    store, repo, settings, _, service = env
    rid = store.plan(settings, EANS[:1])["created"][0]
    service.import_package(rid, PortalPackage(files=[pfile("mystery.png")]))
    (item,) = store.review_items()
    assert item["reason"] == "sin_evidencia_de_ean" and repo.records() == []
    with pytest.raises(ValueError):
        service.resolve_review(item["id"], reviewer="", reason="x", ean=EANS[0], view="frontal")
    with pytest.raises(ValueError):
        service.resolve_review(item["id"], reviewer="ana", reason="visto", ean=EANS[5], view="frontal")
    service.resolve_review(item["id"], reviewer="ana", reason="comprobado en ficha", ean=EANS[0], view="frontal")
    assert store.get(rid)["state"] == IMPORTED
    record = repo.records(EANS[0])[0]
    assert record.view == "frontal" and record.metadata["evidence"] == "manual_review"


def test_unknown_view_review_updates_repository_view(env):
    store, repo, settings, _, service = env
    rid = store.plan(settings, EANS[:1])["created"][0]
    service.import_package(rid, PortalPackage(files=[pfile(f"{EANS[0]}.png")]))
    (item,) = store.review_items()
    assert item["reason"] == "vista_sin_evidencia"
    service.resolve_review(item["id"], reviewer="ana", reason="vista lateral confirmada", view="lateral")
    assert repo.records(EANS[0])[0].view == "lateral" and store.get(rid)["state"] == IMPORTED


def test_partial_and_failed_finalization_and_retry(env):
    store, _, settings, _, service = env
    rid = store.plan(settings, EANS[:2])["created"][0]
    service.import_package(rid, PortalPackage(files=[pfile(f"{EANS[0]}_fr.png", view="frontal")]))
    assert store.get(rid)["state"] == PARTIAL
    new = store.retry(settings, rid)
    assert new and [e["ean"] for e in store.get(new)["eans"]] == [EANS[1]] and store.get(new)["attempts"] == 2
    rid2 = store.plan(settings, EANS[4:5])["created"][0]
    service.import_package(rid2, PortalPackage(files=[]))
    assert store.get(rid2)["state"] == FAILED


def test_manifest_conflicts_and_matching_rules():
    requested = {e.zfill(14): e for e in EANS[:2]}
    manifest = parse_manifest(f"file;ean\nx.png;{EANS[0]}\nx.png;{EANS[1]}\n".encode())
    assert match_file(pfile("x.png"), requested, manifest, True)[3] == "manifiesto_contradictorio"
    both = pfile(f"{EANS[0]}_{EANS[1]}.png")
    assert match_file(both, requested, {}, True)[3] == "varios_ean_en_nombre"
    assert match_file(pfile(f"{EANS[0]}.png"), requested, {}, False)[3] == "sin_evidencia_de_ean"
    assert match_file(pfile(f"{EANS[0]}.png"), requested, {}, True)[:2] == (EANS[0], "filename_ean")
    assert match_file(pfile("n.png"), requested, parse_manifest(f"filename,ean\nn.png,{EANS[6]}".encode()), True)[3] \
        == "ean_no_solicitado"
    assert match_file(pfile("n.png", ean=EANS[1], view="V3", evidence="portal"), requested, {}, True)[:3] \
        == (EANS[1], "portal", "lateral")


def test_read_zip_ignores_traversal_junk_and_finds_manifest():
    archive = zip_bytes({"../../evil/a.png": photo(), "__MACOSX/b.png": photo(), "dir/manifest.csv": b"filename,ean\n",
                         "notes.txt": b"x", "c.JPG": photo()})
    package = read_zip_package(archive)
    assert [f.name for f in package.files] == ["a.png", "c.JPG"] and package.manifest == b"filename,ean\n"
    with pytest.raises(ValueError):
        read_zip_package(b"not a zip")


def test_demand_uses_supplier_family_not_brand_and_skips_covered(env):
    store, repo, settings, _, _ = env
    odoo = fake_odoo({573: [(1, "P1", EANS[0]), (2, "P2", EANS[1])], 574: [(3, "P3", EANS[2])],
                      900: [(4, "P4", EANS[3])]})
    demand = provider_demand(odoo, repo, settings, date(2026, 9, 1), date(2026, 9, 30))
    assert set(demand["eans"]) == set(EANS[:3])  # partner 574 is a contact of 573; 900 excluded
    for view in ("frontal", "lateral", "perspectiva"):
        repo.save(EANS[0], f"{view}.png", photo(), provider="marcolin", origin="t", view=view)
    assert EANS[0] not in provider_demand(odoo, repo, settings, date(2026, 9, 1), date(2026, 9, 30))["eans"]
    with pytest.raises(AccessNotConfirmed):
        provider_demand(odoo, repo, ProviderSettings("safilo"), date(2026, 9, 1), date(2026, 9, 30))


def test_direct_ean_plan_does_not_require_odoo(env):
    from provider_images import ProviderConnector
    store, repo, _, _, _ = env
    settings = ProviderSettings("safilo", max_eans_per_request=2)
    connector = ProviderConnector(ProviderService(store, repo, settings), engine=None)
    result = connector.plan_eans(f"{EANS[0]};{EANS[1]}\\n{EANS[0]} bad", mode="manual")
    assert len(result["created"]) == 1
    assert result["invalid"] == ["bad"]
    assert connector.export_ean_list(result["created"][0]) == f"{EANS[0]},{EANS[1]}"
    assert connector.plan_eans([EANS[0]], mode="manual")["created"] == []


def test_load_settings_and_connector_manual_flow(env, tmp_path):
    from provider_images import ProviderConnector, load_settings
    import json
    (tmp_path / "cfg").mkdir()
    (tmp_path / "cfg" / "provider_config.json").write_text(json.dumps(
        {"marcolin": {"supplier_ids": [573], "max_eans_per_request": 2, "ignored": 1}}))
    settings = load_settings("marcolin", tmp_path / "cfg")
    assert settings.supplier_ids == (573,) and settings.max_eans_per_request == 2
    assert load_settings("safilo", tmp_path / "cfg").supplier_ids == ()
    store, repo, _, _, _ = env
    connector = ProviderConnector(ProviderService(store, repo, settings),
                                  fake_odoo({573: [(1, "P1", EANS[0]), (2, "P2", EANS[1]), (3, "P3", EANS[2])]}))
    result = connector.detect(date(2026, 9, 1), date(2026, 9, 30), mode="manual")
    assert len(result["created"]) == 2 and connector.export_ean_list(result["created"][0]) == f"{EANS[0]},{EANS[1]}"
    again = connector.detect(date(2026, 9, 1), date(2026, 9, 30), mode="manual")
    assert again["created"] == [] and len(again["already_live"]) == 3
    archive = zip_bytes({"manifest.csv": f"filename,ean,view\na.png,{EANS[0]},frontal\n".encode(), "a.png": photo()})
    assert connector.import_zip(result["created"][0], archive)["imported"] == 1