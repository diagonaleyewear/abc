from __future__ import annotations

from datetime import datetime
from io import BytesIO
import json
from pathlib import Path

import pandas as pd

from image_repository import ImageRepository


def load_masterdata_file(file_source) -> pd.DataFrame:
    """Load a MASTERDATA workbook while preserving identifiers as text."""
    dataframe = pd.read_excel(file_source, dtype=str).fillna("")
    dataframe.columns = [str(column).strip() for column in dataframe.columns]
    for column in dataframe.columns:
        dataframe[column] = dataframe[column].astype(str).str.strip()
    return dataframe


def dataframe_to_excel_bytes(dataframe: pd.DataFrame) -> bytes:
    """Serialize a DataFrame to an in-memory Excel workbook."""
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        dataframe.to_excel(writer, index=False)
    return buffer.getvalue()


def normalize_result_export_schema(dataframe: pd.DataFrame) -> pd.DataFrame:
    """Ensure the canonical 18-column MASTERDATA export schema."""
    target_columns = [
        "Marca",
        "Colección",
        "Modelo",
        "Color",
        "Calibre",
        "Ancho Puente",
        "Longitud Varilla",
        "Género",
        "Color Frontal",
        "Color Lente",
        "Forma",
        "Material Principal",
        "Fotocromático",
        "Polarizado",
        "PVO",
        "PVP",
        "Barcode",
        "Categoría",
    ]
    alias_map = {
        "Barcode": ["Barcode", "UPC"],
        "Marca": ["Marca", "Nombre de la marca"],
        "Modelo": ["Modelo", "Código del modelo"],
        "Ancho Puente": ["Ancho Puente", "Dimensión del puente"],
        "Longitud Varilla": ["Longitud Varilla", "Largo de varilla"],
        "Color Frontal": ["Color Frontal", "Color del frontal"],
        "Color Lente": ["Color Lente", "Color de las lentes"],
        "Material Principal": ["Material Principal", "Material del frente"],
        "PVP": ["PVP", "PVP sugerido"],
    }

    normalized = dataframe.copy()
    for target, aliases in alias_map.items():
        if target in normalized.columns:
            continue
        source = next((name for name in aliases if name in normalized.columns), None)
        if source:
            normalized[target] = normalized[source]

    for column in target_columns:
        if column not in normalized.columns:
            normalized[column] = ""

    normalized["Modelo"] = normalized["Modelo"].map(lambda value: str(value).strip())
    normalized["Modelo"] = normalized["Modelo"].map(
        lambda value: value[1:] if value.startswith("0") and len(value) > 1 else value
    )
    return normalized[target_columns].copy()


def split_report_warnings(report) -> tuple[list[str], list[str], list[str]]:
    """Separate blocking, medium-risk, and informational transformation warnings."""
    blocking: list[str] = []
    medium: list[str] = []
    info: list[str] = []

    if report.discarded > 0:
        medium.append(f"Se descartaron {report.discarded} filas durante la transformacion.")
    if report.discarded_accessories > 0:
        info.append(f"{report.discarded_accessories} filas fueron marcadas como accesorios.")
    if report.discarded_invalid_product > 0:
        medium.append(f"{report.discarded_invalid_product} filas no cumplian criterio de producto valido.")
    if report.leading_zero_real_loss_columns > 0:
        blocking.append("Hay perdida real de ceros iniciales en al menos una columna critica.")
    if report.invalid_yes_no_rows > 0:
        blocking.append(f"Hay {report.invalid_yes_no_rows} filas con valores invalidos SI/NO.")
    if report.unmatched_brand_names:
        medium.append(f"Marcas con incidencia abierta: {', '.join(report.unmatched_brand_names)}")
    for key, values in report.anomalous_values.items():
        blocking.append(f"Valores anómalos en {key}: {', '.join(values)}")
    return blocking, medium, info


def clean_ean(raw_value: object) -> str:
    value = str(raw_value).strip()
    if not value or value.lower() == "nan":
        return ""
    if value.lower().startswith("es."):
        value = value[3:]
    if value.endswith(".0"):
        value = value[:-2]
    return value.replace(" ", "")


def extract_luxoptica_products(dataframe: pd.DataFrame) -> list[dict[str, str]]:
    """Extract one request-manifest product per clean EAN."""
    if "Barcode" not in dataframe.columns:
        return []
    seen: set[str] = set()
    products: list[dict[str, str]] = []
    for _, row in dataframe.iterrows():
        ean = clean_ean(row["Barcode"])
        if not ean or ean in seen:
            continue
        seen.add(ean)
        products.append(
            {
                "ean": ean,
                "modelo": str(row.get("Modelo", "")).strip(),
                "color": str(row.get("Color", "")).strip(),
            }
        )
    return products


def extract_clean_eans(dataframe: pd.DataFrame) -> list[str]:
    return [product["ean"] for product in extract_luxoptica_products(dataframe)]


def _chunk_list(items: list[dict[str, str]], chunk_size: int) -> list[list[dict[str, str]]]:
    return [items[index : index + chunk_size] for index in range(0, len(items), chunk_size)]


def generate_luxoptica_request_files(
    dataframe: pd.DataFrame,
    output_dir: Path,
    batch_size: int = 250,
    max_total_eans: int | None = None,
) -> list[Path]:
    """Generate the TXT batches and JSON manifests for Luxottica image requests."""
    products = extract_luxoptica_products(dataframe)
    repository = ImageRepository()
    products = [
        product for product in products
        if repository.pending_views(product["ean"], ("frontal", "lateral", "perspectiva"))
    ]
    if max_total_eans is not None and max_total_eans > 0:
        products = products[:max_total_eans]
    if not products:
        return []

    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    generated_files: list[Path] = []
    for index, batch in enumerate(_chunk_list(products, batch_size), start=1):
        file_name = f"upc-products-images-request-{timestamp}-lote-{index:03d}.txt"
        file_path = output_dir / file_name
        file_path.write_text("\n".join(product["ean"] for product in batch) + "\n", encoding="utf-8")
        manifest_path = file_path.with_suffix(".manifest.json")
        manifest_path.write_text(
            json.dumps({"products": batch}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        generated_files.append(file_path)
    return generated_files