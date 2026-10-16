import hashlib
import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "validate_luxottica", ROOT / "deployment/cloud/validate_luxottica.py",
)
VALIDATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)


def test_synthetic_regression_checks_classification_eans_and_exports():
    result = VALIDATOR.validate_fixture()
    assert result == {
        "rows": 3,
        "fixture_version": "luxottica-synthetic-v1",
        "MONTURAS": 2,
        "GAFAS DE SOL": 1,
        "ean_8056266003066": "MONTURAS",
        "ean_sequence_preserved": True,
        "preview_xlsx_csv_match": True,
        "fixture_sha256": VALIDATOR.FIXTURE_SHA256,
        "dictionary_rules": 0,
    }


def test_synthetic_regression_remains_correct_with_dictionary_rules():
    rules = [
        {"Columna": "Colección", "Valor": "Gafas de vista", "Transformado": "Gafas de sol"},
        {"Columna": "Categoría", "Valor": "MONTURAS", "Transformado": "GAFAS DE SOL"},
    ]
    assert VALIDATOR.validate_fixture(rules)["ean_8056266003066"] == "MONTURAS"


def test_adulterated_fixture_is_rejected():
    source = VALIDATOR.synthetic_fixture()
    source.loc[0, "UPC"] = "8056266003067"
    with pytest.raises(VALIDATOR.RegressionError, match="fixture checksum"):
        VALIDATOR.validate_fixture(source=source)


def test_wrong_collection_classification_is_rejected(monkeypatch):
    transform = VALIDATOR.transform_masterdata

    def misclassify(*args, **kwargs):
        output, report, *rest = transform(*args, **kwargs)
        output.loc[output["Barcode"].eq("8056266003066"), "Categoría"] = "GAFAS DE SOL"
        return output, report, *rest

    monkeypatch.setattr(VALIDATOR, "transform_masterdata", misclassify)
    with pytest.raises(VALIDATOR.RegressionError, match="classification mismatch"):
        VALIDATOR.validate_fixture()


def test_lost_ean_is_rejected(monkeypatch):
    transform = VALIDATOR.transform_masterdata

    def remove_ean(*args, **kwargs):
        output, report, *rest = transform(*args, **kwargs)
        output.loc[0, "Barcode"] = ""
        return output, report, *rest

    monkeypatch.setattr(VALIDATOR, "transform_masterdata", remove_ean)
    with pytest.raises(VALIDATOR.RegressionError, match="EANs were changed"):
        VALIDATOR.validate_fixture()


def test_export_disagreement_is_rejected(monkeypatch):
    load_workbook = VALIDATOR.load_masterdata_file

    def alter_workbook(source):
        dataframe = load_workbook(source)
        dataframe.loc[0, "Categoría"] = "GAFAS DE SOL"
        return dataframe

    monkeypatch.setattr(VALIDATOR, "load_masterdata_file", alter_workbook)
    with pytest.raises(VALIDATOR.RegressionError, match="XLSX export differs"):
        VALIDATOR.validate_fixture()


def test_artifact_source_and_build_identity_are_verified(monkeypatch, tmp_path):
    files = {
        name: f"pinned {name}".encode()
        for name in VALIDATOR.EXPECTED_SOURCE_HASHES
    }
    for name, contents in files.items():
        (tmp_path / name).write_bytes(contents)
    monkeypatch.setattr(
        VALIDATOR, "load_build_info",
        lambda: {"commit": "a" * 40, "build": "actions-123-2"},
    )
    policy = {
        "commit": "a" * 40,
        "build_run_id": 123,
        "build_attempt": 2,
        "source_sha256": {
            name: hashlib.sha256(contents).hexdigest()
            for name, contents in files.items()
        },
    }
    VALIDATOR.verify_artifact(policy, tmp_path)

    (tmp_path / "app_exports.py").write_bytes(b"tampered")
    with pytest.raises(VALIDATOR.RegressionError, match="source mismatch"):
        VALIDATOR.verify_artifact(policy, tmp_path)


def test_artifact_build_attempt_mismatch_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(
        VALIDATOR, "load_build_info",
        lambda: {"commit": "a" * 40, "build": "actions-123-1"},
    )
    with pytest.raises(VALIDATOR.RegressionError, match="build mismatch"):
        VALIDATOR.verify_artifact({
            "commit": "a" * 40,
            "build_run_id": 123,
            "build_attempt": 2,
            "source_sha256": {name: "a" * 64 for name in VALIDATOR.EXPECTED_SOURCE_HASHES},
        }, tmp_path)
