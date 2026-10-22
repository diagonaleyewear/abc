from datetime import date
import random
from dataclasses import replace

import pytest

from image_repository import ImageRepository
from kering_images import AccessNotConfirmed
from nuorder_images import CAPABILITIES, DEFAULTS, NuorderConnector, NuorderPortal
from provider_images import IMPORTED, READY, ProviderService, ProviderStore
from provider_support import EANS, fake_odoo, photo, zip_bytes


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("NUORDER_AUTOMATION_ENABLED", "true")
    store = ProviderStore(tmp_path / "s")
    repo = ImageRepository(tmp_path / "i", state=tmp_path / "r", backups=tmp_path / "b")
    settings = replace(DEFAULTS, supplier_ids=(900,))
    odoo = fake_odoo({900: [(1, "P1", EANS[0]), (2, "P2", EANS[1])]})
    return NuorderConnector(ProviderService(store, repo, settings, rng=random.Random(0)), odoo), store, repo


def test_capabilities_are_documented_as_unconfirmed():
    assert "brand-initiated" in CAPABILITIES["official_api"] and "unconfirmed" in CAPABILITIES["web_automation"]


def test_only_manual_mode_and_scheduler_never_submits(setup):
    connector, store, _ = setup
    with pytest.raises(AccessNotConfirmed):
        connector.detect(date(2026, 9, 1), date(2026, 9, 30), mode="portal")
    created = connector.detect(date(2026, 9, 1), date(2026, 9, 30))["created"]
    assert connector.service.run_cycle(NuorderPortal(), dry_run=False)["submitted"] == []
    assert store.get(created[0])["state"] == READY and store.get(created[0])["mode"] == "manual"


def test_manual_package_import_with_manifest(setup):
    connector, store, repo = setup
    rid = connector.detect(date(2026, 9, 1), date(2026, 9, 30))["created"][0]
    assert connector.export_ean_list(rid) == f"{EANS[0]},{EANS[1]}"
    archive = zip_bytes({"p/x1.png": photo(), "p/x2.png": photo(), "p/manifest.json":
                         ('[{"file":"x1.png","ean":"%s","view":"frontal"},'
                          '{"file":"x2.png","ean":"%s","view":"lateral"}]' % (EANS[0], EANS[1])).encode()})
    assert connector.import_zip(rid, archive)["imported"] == 2
    assert store.get(rid)["state"] == IMPORTED
    assert repo.records(EANS[1])[0].provider == "nuorder"
