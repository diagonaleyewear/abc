from datetime import date
import random
from dataclasses import replace

import pytest

from image_repository import ImageRepository
from provider_images import IMPORTED, NEEDS_REVIEW, PARTIAL, ProviderService, ProviderStore
from provider_support import EANS, fake_odoo, photo, zip_bytes
from safilo_images import DEFAULTS, SafiloConnector, SafiloDriver, SafiloPortal, SelectionResult, strict_settings


class FakeDriver(SafiloDriver):
    def __init__(self, archive, found=None):
        self.archive, self.found, self.searches = archive, found, []

    def search_and_select(self, search_text):
        self.searches.append(search_text)
        eans = search_text.split(",")
        return SelectionResult("SEL-1", eans if self.found is None else self.found)

    def download_selection(self, selection_id):
        return self.archive


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("SAFILO_AUTOMATION_ENABLED", "true")
    store = ProviderStore(tmp_path / "s")
    repo = ImageRepository(tmp_path / "i", state=tmp_path / "r", backups=tmp_path / "b")
    settings = strict_settings(replace(DEFAULTS, supplier_ids=(900,), max_eans_per_request=10))
    odoo = fake_odoo({900: [(1, "P1", EANS[0]), (2, "P2", EANS[1]), (3, "P3", EANS[2])]})
    return SafiloConnector(ProviderService(store, repo, settings, rng=random.Random(0)), odoo), store, repo


def test_all_views_with_manifest_prove_file_to_product(setup):
    connector, store, repo = setup
    views = ["frontal", "lateral", "perspectiva"]
    files, rows = {}, ["filename,ean,view"]
    for ean in EANS[:2]:
        for view in views:
            name = f"IMG_{len(files):05d}.png"  # unrecognisable names
            files[name] = photo()
            rows.append(f"{name},{ean},{view}")
    files["manifest.csv"] = "\n".join(rows).encode()
    driver = FakeDriver(zip_bytes(files))
    created = connector.detect(date(2026, 9, 1), date(2026, 9, 30))["created"]
    result = connector.service.run_cycle(SafiloPortal(driver), dry_run=False)
    assert driver.searches == [",".join(EANS[:3])]
    assert result["downloaded"][0]["imported"] == 6
    # EANS[2] returned nothing: partial, never guessed
    assert store.get(created[0])["state"] == PARTIAL
    for ean in EANS[:2]:
        assert {r.view for r in repo.records(ean)} == set(views)
    assert repo.records(EANS[2]) == []


def test_no_manifest_means_everything_goes_to_review(setup):
    connector, store, repo = setup
    archive = zip_bytes({f"{EANS[0]}_1.png": photo(), "a.png": photo()})
    created = connector.detect(date(2026, 9, 1), date(2026, 9, 30))["created"]
    connector.service.run_cycle(SafiloPortal(FakeDriver(archive)), dry_run=False)
    assert store.get(created[0])["state"] == NEEDS_REVIEW and repo.records() == []
    assert {i["reason"] for i in store.review_items()} == {"sin_evidencia_de_ean"}


def test_contradictory_manifest_is_not_imported(setup):
    connector, store, repo = setup
    archive = zip_bytes({"a.png": photo(), "manifest.csv": f"filename,ean\na.png,{EANS[0]}\na.png,{EANS[1]}\n".encode()})
    connector.detect(date(2026, 9, 1), date(2026, 9, 30))
    connector.service.run_cycle(SafiloPortal(FakeDriver(archive)), dry_run=False)
    assert repo.records() == [] and store.review_items()[0]["reason"] == "manifiesto_contradictorio"


def test_strict_settings_override_config():
    assert strict_settings(replace(DEFAULTS, allow_filename_ean=True)).allow_filename_ean is False
