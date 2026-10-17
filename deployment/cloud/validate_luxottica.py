"""Run a private-data-free Luxottica regression inside a candidate or production app."""
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import sys

import pandas as pd

from app_exports import dataframe_to_excel_bytes, load_masterdata_file, normalize_result_export_schema
from build_info import load_build_info
from transform_luxottica_masterdata import transform_masterdata


EXPECTED_SOURCE_HASHES = {
    "app_enhanced.py", "app_exports.py", "transform_luxottica_masterdata.py",
}
FIXTURE_VERSION = "luxottica-synthetic-v1"
EXPECTED_EANS = ["8056266003066", "0001234567890", "0009876543210"]
EXPECTED_CATEGORIES = ["MONTURAS", "MONTURAS", "GAFAS DE SOL"]
FIXTURE_SHA256 = "b48daaf76b72602368115351f1a6ad596e20185c8c70cd31cdd40ccd129dc45b"


class RegressionError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise RegressionError(message)


def synthetic_fixture():
    rows = []
    for ean, collection in zip(
        EXPECTED_EANS, ["Gafas de vista", "Gafas de vista", "Gafas de sol"],
    ):
        row = {
            "Punto de venta": "0001",
            "Código del modelo": "0DG5116" if ean == EXPECTED_EANS[0] else "0RB-1234/56",
            "Calibre": "54" if ean == EXPECTED_EANS[0] else "52",
            "Color": "3091" if ean == EXPECTED_EANS[0] else "000123",
            "UPC": ean,
            "Nombre de la marca": "DOLCE & GABBANA" if ean == EXPECTED_EANS[0] else "RAY BAN",
            "Código de marca": "DG" if ean == EXPECTED_EANS[0] else "RB",
            "Colección": collection,
            "Género": "Unisex",
            "Forma": "Rectangular",
            "Tipo": "Aro Completo",
            "Nombre del modelo": "DG5116" if ean == EXPECTED_EANS[0] else "WAYFARER",
            "Descripción del color": "BLACK",
            "Material del frente": "Metal",
            "Color del frontal": "BLACK",
            "Material de las lentes": "Cristal",
            "Color de las lentes": "" if collection == "Gafas de vista" else "GREEN",
            "Fotocromático": "",
            "Polarizado": "X" if collection == "Gafas de sol" else "",
            "Largo de varilla": "145",
            "Dimensión del puente": "18",
            "PVP sugerido": "100",
            "PVO": "50",
            "Categoría": "GAFAS DE SOL" if collection == "Gafas de vista" else "MONTURAS",
        }
        rows.append(row)

    columns = list(rows[0])
    columns.remove("Colección")
    columns.insert(11, "Colección")
    return pd.DataFrame(rows, columns=columns)


def fixture_sha256(source):
    payload = {
        "fixture_version": FIXTURE_VERSION,
        "columns": list(source.columns),
        "rows": source.fillna("").astype(str).to_dict(orient="records"),
    }
    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def require_same_frame(actual, expected, message):
    try:
        pd.testing.assert_frame_equal(actual, expected)
    except AssertionError as error:
        raise RegressionError(message) from error


def verify_artifact(policy, app_root=Path("/app")):
    info = load_build_info()
    require(info["commit"] == policy["commit"], "Artifact commit mismatch")
    require(
        info["build"] == f"actions-{policy['build_run_id']}-{policy['build_attempt']}",
        "Artifact build mismatch",
    )
    source_hashes = policy.get("source_sha256", {})
    require(set(source_hashes) == EXPECTED_SOURCE_HASHES, "Incomplete source provenance")
    for name, checksum in source_hashes.items():
        require(
            hashlib.sha256((app_root / name).read_bytes()).hexdigest() == checksum,
            f"Artifact source mismatch: {name}",
        )


def validate_fixture(dictionary_rules=None, source=None):
    source = synthetic_fixture() if source is None else source
    require(fixture_sha256(source) == FIXTURE_SHA256, "Synthetic fixture checksum mismatch")
    require(source["UPC"].tolist() == EXPECTED_EANS, "Synthetic fixture EAN sequence mismatch")
    require(source["Colección"].tolist() == [
        "Gafas de vista", "Gafas de vista", "Gafas de sol",
    ], "Synthetic fixture collections mismatch")

    output, report, *_ = transform_masterdata(
        source, {}, dictionary_rules=dictionary_rules or [],
    )
    require(len(output) == len(source) == 3, "Unexpected synthetic regression row count")
    require(report.discarded == 0, "Synthetic regression discarded products")
    require(report.category_review_rows == [], "Synthetic regression needs category review")
    require(output["Barcode"].tolist() == EXPECTED_EANS, "EANs were changed or reordered")
    require(
        output["Categoría"].tolist() == EXPECTED_CATEGORIES,
        "Luxottica collection classification mismatch",
    )
    require(
        output.loc[output["Barcode"].eq("8056266003066"), "Categoría"].tolist() == ["MONTURAS"],
        "EAN 8056266003066 must be classified as MONTURAS",
    )

    export = normalize_result_export_schema(output)
    preview = export.apply(
        lambda column: column.map(lambda value: str(value).upper() if pd.notna(value) else ""),
    )
    excel = load_masterdata_file(BytesIO(dataframe_to_excel_bytes(export)))
    csv = pd.read_csv(
        BytesIO(export.to_csv(index=False, sep=";").encode("utf-8-sig")),
        dtype=str,
        sep=";",
    ).fillna("")
    require_same_frame(excel, export.reset_index(drop=True), "XLSX export differs from preview data")
    require_same_frame(csv, export.reset_index(drop=True), "CSV export differs from preview data")
    for exported in (excel, csv):
        export_preview = exported.apply(
            lambda column: column.map(lambda value: str(value).upper() if pd.notna(value) else ""),
        )
        require_same_frame(export_preview, preview.reset_index(drop=True), "Export differs from preview")

    return {
        "rows": len(output),
        "fixture_version": FIXTURE_VERSION,
        "MONTURAS": output["Categoría"].tolist().count("MONTURAS"),
        "GAFAS DE SOL": output["Categoría"].tolist().count("GAFAS DE SOL"),
        "ean_8056266003066": "MONTURAS",
        "ean_sequence_preserved": True,
        "preview_xlsx_csv_match": True,
        "fixture_sha256": FIXTURE_SHA256,
        "dictionary_rules": len(dictionary_rules or []),
    }


def load_dictionary_rules():
    dictionary_path = Path(os.environ["MASTERDATA_DICTIONARY_PATH"])
    if not dictionary_path.exists():
        return []
    payload = json.loads(dictionary_path.read_text(encoding="utf-8"))
    rules = payload if isinstance(payload, list) else payload["rules"]
    require(isinstance(rules, list), "Invalid production dictionary rules")
    return rules


def main(policy, mode):
    require(mode in {"candidate", "production"}, "Unknown regression mode")
    verify_artifact(policy)
    rules = load_dictionary_rules() if mode == "production" else []
    print(json.dumps(validate_fixture(rules), ensure_ascii=True))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("Usage: validate_luxottica.py POLICY_JSON candidate|production")
    main(json.loads(sys.argv[1]), sys.argv[2])
