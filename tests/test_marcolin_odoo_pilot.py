import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import marcolin_odoo_pilot as pilot  # noqa: E402
from image_repository import ImageRepository  # noqa: E402
from provider_support import EANS, photo  # noqa: E402
from test_provider_odoo_orders import RANGE, odoo_fixture  # noqa: E402


@pytest.fixture
def repo(tmp_path):
    return ImageRepository(tmp_path / "i", state=tmp_path / "r", backups=tmp_path / "b")


def test_report_verifies_partner_and_proposes_without_side_effects(repo, tmp_path):
    report = pilot.build_report(odoo_fixture(), repo, 573, *RANGE, size=8, max_lines=100)
    assert report["identity"]["is_marcolin_by_name"] and len(report["identity"]["family"]) == 2
    assert report["association_saved"] is False and report["requests_created"] == 0
    assert report["status"] == "propuesta_pendiente_de_aprobacion"
    assert {p["EAN"] for p in report["proposal"]} == {EANS[0], EANS[1], EANS[2]}
    assert all(p["Motivo"] and p["Pedido"] and p["Fecha"] for p in report["proposal"])
    assert report["summary"]["sin_ean"] == 1 and report["summary"]["ean_invalido"] == 1
    path = pilot.write_report(report, tmp_path / "out")
    assert json.loads(path.read_text(encoding="utf-8"))["proposal"] and path.with_suffix(".csv").exists()


def test_complete_images_are_not_proposed(repo):
    for view in ("frontal", "lateral", "perspectiva"):
        repo.save(EANS[2], f"{view}.png", photo(), provider="kering", origin="t", view=view)
    report = pilot.build_report(odoo_fixture(), repo, 573, *RANGE, size=8, max_lines=100)
    assert EANS[2] not in {p["EAN"] for p in report["proposal"]}
    assert report["images"]["Completas"] >= 1


def test_partner_that_is_not_marcolin_is_blocked(repo):
    report = pilot.build_report(odoo_fixture(), repo, 900, *RANGE, size=8, max_lines=100)
    assert report["blocker"] == "partner_no_coincide_con_marcolin" and "proposal" not in report


def test_missing_odoo_connection_records_blocker_and_exit_code(monkeypatch, tmp_path):
    for name in ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("db_config.load_env_file", lambda *a, **k: None)
    code = pilot.main(["--out", str(tmp_path)])
    assert code == 2
    (saved,) = tmp_path.glob("*.json")
    data = json.loads(saved.read_text(encoding="utf-8"))
    assert data["blocker"] == "odoo_no_disponible" and "proposal" not in data
    assert "password" not in saved.read_text(encoding="utf-8").lower()
