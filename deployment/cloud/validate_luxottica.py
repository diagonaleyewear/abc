"""Installed root-owned; runs inside the candidate/container as its non-root UID."""
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


policy = json.loads(sys.argv[1])
assert load_build_info()["commit"] == policy["commit"], "Artifact commit mismatch"
assert load_build_info()["build"] == f"actions-{policy['build_run_id']}-{policy['build_attempt']}"
for name, checksum in policy["source_sha256"].items():
    assert hashlib.sha256((Path("/app") / name).read_bytes()).hexdigest() == checksum, name
raw = sys.stdin.buffer.read(2_000_001)
assert len(raw) <= 2_000_000, "Workbook too large"
assert hashlib.sha256(raw).hexdigest() == policy["workbook_sha256"], "Workbook checksum mismatch"
source = load_masterdata_file(BytesIO(raw))
assert len(source) == 902
assert source["Colección"].value_counts().to_dict() == {"Gafas de vista": 464, "Gafas de sol": 438}
rules = []
if sys.argv[2] == "production":
    dictionary_path = Path(os.environ["MASTERDATA_DICTIONARY_PATH"])
    if dictionary_path.exists():
        payload = json.loads(dictionary_path.read_text(encoding="utf-8"))
        rules = payload if isinstance(payload, list) else payload["rules"]
assert isinstance(rules, list)
output, report, *_ = transform_masterdata(source, {}, dictionary_rules=rules)
assert len(output) == 902 and report.discarded == 0
assert output["Categoría"].value_counts().to_dict() == {"MONTURAS": 464, "GAFAS DE SOL": 438}
assert output["Barcode"].tolist() == source["UPC"].tolist()
assert output.loc[output["Barcode"].eq("8056266003066"), "Categoría"].tolist() == ["MONTURAS"]
assert report.category_review_rows == []
export = normalize_result_export_schema(output)
download = load_masterdata_file(BytesIO(dataframe_to_excel_bytes(export)))
pd.testing.assert_frame_equal(download, export.reset_index(drop=True))
csv = pd.read_csv(
    BytesIO(export.to_csv(index=False, sep=";").encode("utf-8-sig")), dtype=str, sep=";",
).fillna("")
assert csv["Categoría"].tolist() == download["Categoría"].tolist() == export["Categoría"].str.upper().tolist()
print(json.dumps({
    "rows": 902, "MONTURAS": 464, "GAFAS DE SOL": 438,
    "ean_8056266003066": "MONTURAS", "ean_sequence_preserved": True,
    "preview_xlsx_csv_match": True, "dictionary_rules": len(rules),
}))
