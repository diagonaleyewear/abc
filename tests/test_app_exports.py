import json
from types import SimpleNamespace

import pandas as pd

from image_repository import ImageRepository
from app_exports import (
    dataframe_to_excel_bytes,
    extract_clean_eans,
    generate_luxoptica_request_files,
    normalize_result_export_schema,
    split_report_warnings,
)


def test_masterdata_export_schema_maps_aliases_and_preserves_text() -> None:
    source = pd.DataFrame(
        [{"UPC": "000123", "Nombre de la marca": "Brand", "Código del modelo": "0RB-12", "PVP sugerido": "100"}]
    )

    result = normalize_result_export_schema(source)

    assert len(result.columns) == 18
    assert result.loc[0, "Barcode"] == "000123"
    assert result.loc[0, "Marca"] == "Brand"
    assert result.loc[0, "Modelo"] == "RB-12"
    assert result.loc[0, "PVP"] == "100"


def test_luxoptica_eans_are_cleaned_and_deduplicated() -> None:
    source = pd.DataFrame(
        {
            "Barcode": ["ES.000123", "000123", "12345.0", "nan", ""],
            "Modelo": ["Model A", "Model B", "Model C", "", ""],
            "Color": ["01", "02", "03", "", ""],
        }
    )

    assert extract_clean_eans(source) == ["000123", "12345"]


def test_request_files_keep_manifest_and_txt_batch_content(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMAGE_REPOSITORY_ROOT", str(tmp_path / "image-repository"))
    source = pd.DataFrame(
        [
            {"Barcode": "ES.00123", "Modelo": "Model A", "Color": "01"},
            {"Barcode": "00456", "Modelo": "Model B", "Color": "02"},
        ]
    )

    files = generate_luxoptica_request_files(source, tmp_path, batch_size=1)

    assert len(files) == 2
    assert files[0].read_text(encoding="utf-8").strip() == "00123"
    manifest = json.loads(files[0].with_suffix(".manifest.json").read_text(encoding="utf-8"))
    assert manifest["products"] == [{"ean": "00123", "modelo": "Model A", "color": "01"}]


def test_request_files_only_include_eans_with_pending_views(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMAGE_REPOSITORY_ROOT", str(tmp_path / "image-repository"))
    monkeypatch.setattr(
        ImageRepository,
        "pending_views",
        lambda self, ean, required: () if ean == "00123" else required,
    )
    source = pd.DataFrame(
        [
            {"Barcode": "00123", "Modelo": "Model A", "Color": "01"},
            {"Barcode": "00456", "Modelo": "Model B", "Color": "02"},
        ]
    )

    files = generate_luxoptica_request_files(source, tmp_path)

    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8").strip() == "00456"


def test_excel_export_and_warning_split_remain_data_only() -> None:
    workbook = dataframe_to_excel_bytes(pd.DataFrame([{"value": "text"}]))
    report = SimpleNamespace(
        discarded=1,
        discarded_accessories=1,
        discarded_invalid_product=0,
        leading_zero_real_loss_columns=0,
        invalid_yes_no_rows=0,
        unmatched_brand_names=["Unknown"],
        anomalous_values={"Forma": ["Odd"]},
    )

    blocking, medium, info = split_report_warnings(report)

    assert workbook.startswith(b"PK")
    assert len(blocking) == 1
    assert len(medium) == 2
    assert len(info) == 1