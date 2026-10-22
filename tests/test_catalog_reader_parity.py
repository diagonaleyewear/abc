"""The standalone #76 script mirrors repository_catalog_readonly (production lacks the shared module).

If the two drift apart the audit and the Cloud preview would disagree, so the verdicts are compared.
"""
import sys
from pathlib import Path

import pytest

import repository_catalog_readonly as shared
from image_repository import ImageRepository
from provider_support import EANS, photo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import marcolin_odoo_pilot as script  # noqa: E402

REQUIRED = ("frontal", "lateral", "perspectiva")


@pytest.fixture
def populated(tmp_path):
    repo = ImageRepository(tmp_path / "i", state=tmp_path / "r", backups=tmp_path / "b")
    for n, view in enumerate(REQUIRED):
        repo.save(EANS[0], f"{view}.png", photo(n + 1), provider="marcolin", origin="t", view=view)
    repo.save(EANS[1], "front.png", photo(7), provider="marcolin", origin="t", view="frontal")
    return tmp_path


def run_script(codes, catalog, image_root, verify):
    connection, _ = script.open_catalog(catalog)
    try:
        found = script.fetch_records(connection, codes)
    finally:
        connection.close()
    helpers = script.repository_helpers()
    return {c: script.coverage(found.get(script.code_key(c), []), REQUIRED, helpers, image_root, verify)
            for c in codes}


@pytest.mark.parametrize("verify", [False, True])
def test_script_and_shared_reader_give_the_same_verdicts(populated, verify):
    catalog = populated / "r" / ".catalog.sqlite3"
    codes = [EANS[0], EANS[1], EANS[2], "0" + EANS[1]]
    shared_report = shared.catalog_coverage(
        codes, catalog=catalog, required_views=REQUIRED, image_root=populated / "i", verify_files=verify)
    assert shared_report["catalog"]["status"] == "consultado"
    assert run_script(codes, catalog, populated / "i", verify) == shared_report["coverage"]
    labels = {c: v["label"] for c, v in shared_report["coverage"].items()}
    assert labels[EANS[0]] == "completas" and labels[EANS[1]] == "parciales" and labels[EANS[2]] == "sin_registros"


def test_code_helpers_and_constants_agree():
    for code in ("036000291452", "0036000291452", "8400000000017", "123"):
        assert script.code_key(code) == shared.code_key(code)
        assert script.code_variants(code) == shared.code_variants(code)
    assert script.CATALOG_TABLES == shared.CATALOG_TABLES
    assert script.EAN_LENGTHS == shared.EAN_LENGTHS and script.MAX_FILE_BYTES == shared.MAX_FILE_BYTES
    assert script.DEFAULT_REQUIRED_VIEWS == shared.DEFAULT_REQUIRED_VIEWS


def test_both_refuse_a_missing_catalog_without_creating_it(tmp_path):
    missing = tmp_path / "state" / ".catalog.sqlite3"
    assert shared.catalog_coverage([EANS[0]], catalog=missing)["coverage"][EANS[0]]["label"] == "desconocida"
    with pytest.raises(script.Blocker):
        script.open_catalog(missing)
    assert not (tmp_path / "state").exists()
