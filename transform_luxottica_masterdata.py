from __future__ import annotations

import argparse
import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

from db_config import load_db_config


EXPECTED_OUTPUT_COLUMNS = [
    "Punto de venta",
    "Código del modelo",
    "Calibre",
    "Color",
    "UPC",
    "Nombre de la marca",
    "Código de marca",
    "Colección",
    "Género",
    "Forma",
    "Tipo",
    "Nombre del modelo",
    "Descripción del color",
    "Material del frente",
    "Color del frontal",
    "Material de las lentes",
    "Color de las lentes",
    "Fotocromático",
    "Polarizado",
    "Largo de varilla",
    "Dimensión del puente",
    "PVP sugerido",
    "PVO",
    "Categoría",
]

RESULT_EXPORT_COLUMNS = [
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

VALID_GENDER = {"Hombre", "Mujer", "Unisex", "Niño"}
VALID_SHAPE = {
    "Rectangular",
    "Cuadrada",
    "Ovalada",
    "Pantos",
    "Aviador",
    "Ojo de Gato",
    "Mariposa",
    "Irregular",
}
VALID_MATERIALS = {
    "Acetato",
    "Metal",
    "Acero",
    "Acetato y Metal",
    "Titanio",
    "Nylon",
    "Biopoliamida",
}
VALID_CATEGORIES = {"MONTURAS", "GAFAS DE SOL"}
BRAND_NAME_ALIASES = {
    "dolce & gabbana": "Dolce Gabbana",
    "dolce e gabbana": "Dolce Gabbana",
    "dolce y gabbana": "Dolce Gabbana",
    "ray-ban": "Ray Ban",
    "ray ban": "Ray Ban",
    "tiffany": "Tiffany & Co.",
    "emporio armani kids": "Emporio Armani",
}

ACCESSORY_TEXT_KEYWORDS = [
    "accesorio",
    "accessory",
    "cordon",
    "cadena",
    "funda",
    "estuche",
    "limpiador",
    "microbag",
    "hardcase",
]
ACCESSORY_MODEL_MARKERS = [" kit", " clip", " recambio", " replacement", " hardware"]
ACCESSORY_MODEL_PREFIXES = ("AOO", "ANB", "AOV")
ACCESSORY_MODEL_SUFFIXES = ("KT",)


@dataclass
class ValidationReport:
    total_input: int
    total_output: int
    discarded: int
    discarded_accessories: int = 0
    discarded_ralph_lauren: int = 0
    discarded_invalid_product: int = 0
    accessory_whitelist_kept: int = 0
    discarded_invalid_upc: int = 0
    upc_missing_after_transform: int = 0
    leading_zero_issues: int = 0
    leading_zero_real_loss_columns: int = 0
    leading_zero_summary: list[dict[str, Any]] = field(default_factory=list)
    unmatched_brand_names: list[str] = field(default_factory=list)
    invalid_yes_no_rows: int = 0
    anomalous_values: dict[str, list[str]] = field(default_factory=dict)
    dictionary_rules_not_found: list[dict[str, str]] = field(default_factory=list)
    dictionary_values_not_resolved: list[dict[str, Any]] = field(default_factory=list)
    category_review_rows: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_input": self.total_input,
            "total_output": self.total_output,
            "discarded": self.discarded,
            "discarded_accessories": self.discarded_accessories,
            "discarded_ralph_lauren": self.discarded_ralph_lauren,
            "discarded_invalid_product": self.discarded_invalid_product,
            "accessory_whitelist_kept": self.accessory_whitelist_kept,
            "discarded_invalid_upc": self.discarded_invalid_upc,
            "upc_missing_after_transform": self.upc_missing_after_transform,
            "leading_zero_issues": self.leading_zero_issues,
            "leading_zero_real_loss_columns": self.leading_zero_real_loss_columns,
            "leading_zero_summary": self.leading_zero_summary,
            "unmatched_brand_names": self.unmatched_brand_names,
            "invalid_yes_no_rows": self.invalid_yes_no_rows,
            "anomalous_values": self.anomalous_values,
            "dictionary_rules_not_found": self.dictionary_rules_not_found,
            "dictionary_values_not_resolved": self.dictionary_values_not_resolved,
            "category_review_rows": self.category_review_rows,
        }


def _starts_with_zero(value: str) -> bool:
    return _safe_text(value).startswith("0")


def _parse_whitelist_codes(codes_raw: str) -> set[str]:
    if not codes_raw:
        return set()
    return {token.strip().upper() for token in codes_raw.split(",") if token.strip()}


def _load_whitelist_file(path: Path) -> set[str]:
    if not path.exists():
        raise FileNotFoundError(f"No existe archivo de whitelist: {path}")

    codes: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        token = line.strip()
        if not token or token.startswith("#"):
            continue
        codes.add(token.upper())
    return codes


def _build_discard_reason(row: pd.Series) -> str:
    reasons: list[str] = []
    if bool(row.get("__is_accessory", False)):
        reasons.append("accesorio")
    if bool(row.get("__is_ralph", False)):
        reasons.append("ralph_lauren")
    if bool(row.get("__is_invalid_product", False)):
        reasons.append("producto_invalido")
    if bool(row.get("__upc_missing_after_filters", False)):
        reasons.append("upc_vacio")
    if bool(row.get("__dup_upc_after_filters", False)):
        reasons.append("upc_duplicado")
    return ";".join(reasons)


def _build_audit_discarded(df: pd.DataFrame) -> pd.DataFrame:
    audit = df.copy()
    audit["__row_excel"] = audit.index + 2
    audit["motivo_descarte"] = audit.apply(_build_discard_reason, axis=1)
    discarded = audit[audit["motivo_descarte"].astype(bool)].copy()
    if discarded.empty:
        return pd.DataFrame(columns=["__row_excel", "motivo_descarte"])

    keep_cols = [
        "__row_excel",
        "motivo_descarte",
        "UPC",
        "Nombre de la marca",
        "Código de marca",
        "Código del modelo",
        "Colección",
        "Nombre del modelo",
        "Categoría",
        "Tipo",
        "Color",
        "Descripción del color",
        "Color del frontal",
        "Color de las lentes",
    ]
    extra_cols = [c for c in df.columns if c not in keep_cols and not c.startswith("__")]
    ordered = [c for c in keep_cols + extra_cols if c in discarded.columns]
    return discarded[ordered].copy()


def _build_leading_zero_summary(
    original_df: pd.DataFrame,
    kept_df: pd.DataFrame,
    output_df: pd.DataFrame,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for col in ["Color", "Código del modelo"]:
        src = original_df[col].map(_safe_text) if col in original_df.columns else pd.Series(dtype=str)
        kept = kept_df[col].map(_safe_text) if col in kept_df.columns else pd.Series(dtype=str)
        out = output_df[col].map(_safe_text) if col in output_df.columns else pd.Series(dtype=str)

        c_all = int(src.map(_starts_with_zero).sum())
        c_kept = int(kept.map(_starts_with_zero).sum())
        c_out = int(out.map(_starts_with_zero).sum())

        legacy_issue = c_out < min(c_all, len(out))
        real_loss = c_out < c_kept

        rows.append(
            {
                "columna": col,
                "con_cero_input_total": c_all,
                "con_cero_input_post_filtros": c_kept,
                "con_cero_output": c_out,
                "incidencia_reportada_por_script": "SI" if legacy_issue else "NO",
                "perdida_real_post_filtros": "SI" if real_loss else "NO",
            }
        )

    return rows


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    return str(value).strip()


def _normalize_text(value: str) -> str:
    value = _safe_text(value).lower()
    return re.sub(r"\s+", " ", value).strip()


def apply_custom_dictionary(
    df: pd.DataFrame,
    dictionary_rules: list[dict[str, str]] | None = None,
    source_df: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, list[dict[str, str]]]:
    """Aplica reglas exactas por columna y devuelve las reglas no encontradas."""
    working = df.copy()
    rules = dictionary_rules or []
    normalized_rules: list[dict[str, str]] = []
    for rule in rules:
        column = _safe_text(rule.get("Columna", rule.get("column", "")))
        source = _safe_text(rule.get("Valor", rule.get("value", "")))
        target = _safe_text(rule.get("Transformado", rule.get("transformed", "")))
        if column and source and column in working.columns:
            normalized_rules.append({"Columna": column, "Valor": source, "Transformado": target})

    not_found: list[dict[str, str]] = []
    for rule in normalized_rules:
        column = rule["Columna"]
        source = rule["Valor"]
        comparison = source_df[column] if source_df is not None and column in source_df.columns else working[column]
        mask = comparison.map(lambda value: _normalize_text(value) == _normalize_text(source))
        if not bool(mask.any()):
            not_found.append(rule)
            continue
        working.loc[mask, column] = rule["Transformado"]
    return working, not_found


def find_dictionary_values_not_resolved(
    df: pd.DataFrame,
    color_map: dict[str, str],
    shape_map: dict[str, str] | None = None,
    dictionary_rules: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Detecta valores no relacionados con Odoo ni con reglas personalizadas."""
    custom_keys = {
        (
            _safe_text(rule.get("Columna", rule.get("column", ""))),
            _normalize_text(rule.get("Valor", rule.get("value", ""))),
        )
        for rule in dictionary_rules or []
    }
    color_columns = {"Descripción del color", "Color del frontal", "Color de las lentes"}
    shape_columns = {"Forma"}
    unresolved: list[dict[str, Any]] = []
    for column in [*color_columns, *shape_columns]:
        if column not in df.columns:
            continue
        for value, count in df[column].map(_safe_text).value_counts().items():
            normalized = _normalize_text(value)
            if not normalized or (column, normalized) in custom_keys:
                continue
            if column in color_columns and value.upper() in color_map:
                continue
            if column in shape_columns and shape_map and normalized in shape_map:
                continue
            unresolved.append({"Columna": column, "Valor": value, "Filas": int(count)})
    return sorted(unresolved, key=lambda item: (item["Columna"], item["Valor"]))


def load_excel_as_text(path: Path) -> pd.DataFrame:
    df = pd.read_excel(path, dtype=str)
    df.columns = [str(c).strip() for c in df.columns]
    for col in df.columns:
        df[col] = df[col].map(_safe_text)
    if "PVP Sugerido" in df.columns and "PVP sugerido" not in df.columns:
        df = df.rename(columns={"PVP Sugerido": "PVP sugerido"})
    return df


def _normalize_input_column_aliases(df: pd.DataFrame) -> pd.DataFrame:
    """Normaliza alias frecuentes del Excel origen a nombres canónicos internos."""
    alias_map = {
        "PVP sugerido": ["PVP sugerido", "PVP Sugerido", "PVP"],
    }

    normalized = df.copy()
    for canonical, aliases in alias_map.items():
        if canonical in normalized.columns:
            continue
        found = next((name for name in aliases if name in normalized.columns), None)
        if found:
            normalized[canonical] = normalized[found]

    return normalized


def build_color_dictionary_from_db() -> dict[str, str]:
    cfg = load_db_config()
    url = URL.create(
        "postgresql+psycopg2",
        username=cfg.user,
        password=cfg.password,
        host=cfg.host,
        port=cfg.port,
        database=cfg.database,
    )
    engine = create_engine(url)
    query = text(
        """
        SELECT UPPER(TRIM(d.name)) AS alias, c.name AS canonical
        FROM diagonal_product_color_dictionary d
        LEFT JOIN diagonal_product_color c ON c.id = d.color_id
        WHERE d.name IS NOT NULL
        UNION
        SELECT UPPER(TRIM(c.name)) AS alias, c.name AS canonical
        FROM diagonal_product_color c
        WHERE c.name IS NOT NULL
        """
    )
    mapping: dict[str, str] = {}
    with engine.connect() as conn:
        rows = conn.execute(query).fetchall()
        for alias, canonical in rows:
            if alias and canonical:
                mapping[str(alias).strip()] = str(canonical).strip()
    return mapping


def build_shape_dictionary_from_db() -> dict[str, str]:
    """Carga alias y nombres canónicos de formas desde Odoo."""
    cfg = load_db_config()
    url = URL.create(
        "postgresql+psycopg2",
        username=cfg.user,
        password=cfg.password,
        host=cfg.host,
        port=cfg.port,
        database=cfg.database,
    )
    engine = create_engine(url)
    query = text(
        """
        SELECT LOWER(TRIM(name)) AS alias, name AS canonical
        FROM diagonal_product_forma
        WHERE name IS NOT NULL
        """
    )
    mapping: dict[str, str] = {}
    with engine.connect() as conn:
        for alias, canonical in conn.execute(query).fetchall():
            if alias and canonical:
                mapping[str(alias)] = str(canonical).strip()
    return mapping


def build_brand_dictionary_from_db() -> dict[str, str]:
    cfg = load_db_config()
    url = URL.create(
        "postgresql+psycopg2",
        username=cfg.user,
        password=cfg.password,
        host=cfg.host,
        port=cfg.port,
        database=cfg.database,
    )
    engine = create_engine(url)
    query = text(
        """
        SELECT name
        FROM diagonal_product_brand
        WHERE name IS NOT NULL
        """
    )
    mapping: dict[str, str] = {}
    with engine.connect() as conn:
        rows = conn.execute(query).fetchall()
        for (name,) in rows:
            canonical = _safe_text(name)
            if canonical:
                mapping[_normalize_text(canonical)] = canonical
    return mapping


def canonicalize_brand_name(name: str, brand_map: dict[str, str]) -> str:
    normalized = _normalize_text(name)
    alias = BRAND_NAME_ALIASES.get(normalized)
    if alias:
        return alias
    canonical = brand_map.get(normalized)
    if canonical:
        return canonical
    return _safe_text(name)


def _build_brand_audit(original_df: pd.DataFrame, brand_map: dict[str, str]) -> pd.DataFrame:
    if "Nombre de la marca" not in original_df.columns:
        return pd.DataFrame(columns=["marca_origen", "marca_resuelta", "existe_en_bd", "filas"])

    rows: list[dict[str, Any]] = []
    counts = original_df["Nombre de la marca"].map(_safe_text).value_counts()
    for source_brand, row_count in counts.items():
        if not source_brand:
            continue
        resolved = canonicalize_brand_name(source_brand, brand_map)
        exists_in_db = _normalize_text(resolved) in brand_map
        rows.append(
            {
                "marca_origen": source_brand,
                "marca_resuelta": resolved,
                "existe_en_bd": "SI" if exists_in_db else "NO",
                "filas": int(row_count),
            }
        )

    return pd.DataFrame(rows).sort_values(["existe_en_bd", "filas", "marca_origen"], ascending=[True, False, True])


def build_executive_summary_markdown(
    report: ValidationReport,
    input_path: Path,
    output_path: Path,
    report_path: Path | None,
) -> str:
    lines = [
        "# Resumen Ejecutivo MASTERDATA",
        "",
        f"- Archivo origen: {input_path}",
        f"- Archivo salida: {output_path}",
        f"- Registros procesados: {report.total_input}",
        f"- Registros exportados: {report.total_output}",
        f"- Registros descartados: {report.discarded}",
        f"- Descartados por accesorios: {report.discarded_accessories}",
        f"- Descartados por Ralph Lauren: {report.discarded_ralph_lauren}",
        f"- Descartados por producto invalido: {report.discarded_invalid_product}",
        f"- Excepciones whitelist conservadas: {report.accessory_whitelist_kept}",
        f"- UPC faltantes tras transformacion: {report.upc_missing_after_transform}",
        f"- Incidencias legacy de ceros iniciales: {report.leading_zero_issues}",
        f"- Perdida real de ceros iniciales: {report.leading_zero_real_loss_columns}",
        f"- Filas invalidas SI/NO: {report.invalid_yes_no_rows}",
        f"- Marcas no resueltas contra BD: {len(report.unmatched_brand_names)}",
        "",
        "## Estado",
        "",
    ]

    category_review_rows = getattr(report, "category_review_rows", [])
    if report.invalid_yes_no_rows == 0 and report.leading_zero_real_loss_columns == 0 and not category_review_rows:
        lines.append("- Lote apto para revision final de negocio e importacion en Odoo.")
    else:
        lines.append("- Lote con incidencias que requieren revision antes de importar.")
    if category_review_rows:
        lines.append(f"- Filas sin categoria por Coleccion vacia o no reconocida: {len(category_review_rows)}.")

    if report.unmatched_brand_names:
        lines.append(f"- Marcas con incidencia abierta: {', '.join(report.unmatched_brand_names)}")
    else:
        lines.append("- Sin incidencias abiertas de marcas contra BD.")

    lines.extend(["", "## Artefactos", ""])
    lines.append(f"- Excel MASTERDATA: {output_path}")
    if report_path:
        lines.append(f"- Reporte JSON: {report_path}")
    lines.append(f"- Auditoria descartes: {output_path.with_name(f'{output_path.stem}_descartes_auditoria.csv')}")
    lines.append(f"- Auditoria ceros iniciales: {output_path.with_name(f'{output_path.stem}_ceros_iniciales_resumen.csv')}")
    lines.append(f"- Auditoria marcas: {output_path.with_name(f'{output_path.stem}_marcas_auditoria.csv')}")
    return "\n".join(lines) + "\n"


def _is_rayban_meta(collection: str, model_name: str, brand_name: str, model_type: str) -> bool:
    joined = " ".join(
        [
            _normalize_text(collection),
            _normalize_text(model_name),
            _normalize_text(brand_name),
            _normalize_text(model_type),
        ]
    )
    meta_markers = [" meta ", "meta eyewear", "ray-ban meta", "ray ban meta"]
    padded = f" {joined} "
    return any(marker in padded for marker in meta_markers)


def normalize_brand(
    name: str,
    code: str,
    collection: str,
    model_name: str,
    model_type: str,
    brand_map: dict[str, str] | None = None,
) -> tuple[str, str]:
    n_name = _normalize_text(name)
    n_code = _safe_text(code).upper()
    joined = " ".join([_normalize_text(collection), _normalize_text(model_name), n_name, _normalize_text(model_type)])
    brand_map = brand_map or {}

    if n_name in {"dolce & gabbana", "dolce e gabbana", "dolce y gabbana"}:
        return "Dolce Gabbana", n_code

    if "ray-ban" in n_name or "ray ban" in n_name or n_code in {"RB", "RJ", "RX", "RY"}:
        if _is_rayban_meta(collection, model_name, name, model_type):
            return "Ray Ban Meta", "RB"
        if "junior" in joined:
            return "Ray Ban Junior", "RB"
        return "Ray Ban", "RB"

    if "oakley" in n_name or n_code in {"OO", "OJ", "OY", "OX"}:
        # Normalizacion acordada para Oakley.
        return "Oakley", "OO"

    return canonicalize_brand_name(name, brand_map), n_code


def normalize_model(value: str) -> str:
    text_value = _safe_text(value)
    cleaned = text_value.replace("/", "").replace("-", "")
    if cleaned.startswith("0") and len(cleaned) > 1:
        cleaned = cleaned[1:]
    return cleaned


def derive_brand_code_from_model(model_code: str) -> str:
    normalized = normalize_model(model_code).upper()
    match = re.match(r"^0?([A-Z]{2})", normalized)
    if not match:
        return ""
    return match.group(1)


def normalize_gender(value: str, brand_name: str, category_hint: str) -> str:
    v = _normalize_text(value)
    if v in {"niño", "nino", "niña", "nina", "kid", "kids", "junior"}:
        return "Niño"
    if "junior" in _normalize_text(brand_name) or "junior" in _normalize_text(category_hint):
        return "Niño"
    if v in {"hombre", "man", "male", "caballero"}:
        return "Hombre"
    if v in {"mujer", "woman", "female", "dama"}:
        return "Mujer"
    if v in {"unisex", "uni"}:
        return "Unisex"
    return "Unisex"


def normalize_shape(value: str, shape_map: dict[str, str] | None = None) -> str:
    v = _normalize_text(value)
    if shape_map and v in shape_map:
        return shape_map[v]
    mapping = {
        "rectangular": "RECTANGULAR/CUADRADA",
        "cuadrada": "RECTANGULAR/CUADRADA",
        "ovalada": "REDONDA/OVALADA",
        "redonda": "REDONDA/OVALADA",
        "pantos": "REDONDA/OVALADA",
        "phantos": "REDONDA/OVALADA",
        "aviador": "AVIATOR",
        "aviator": "AVIATOR",
        "ojo de gato": "CAT EYE",
        "cat eye": "CAT EYE",
        "mariposa": "CAT EYE",
        "imperfecta": "RECTANGULAR/CUADRADA",
        "irregular": "RECTANGULAR/CUADRADA",
        "pantalla": "RECTANGULAR/CUADRADA",
        "pillow": "RECTANGULAR/CUADRADA",
        "geometrica": "RECTANGULAR/CUADRADA",
        "geométrica": "RECTANGULAR/CUADRADA",
    }
    return mapping.get(v, "RECTANGULAR/CUADRADA")


def normalize_material(value: str) -> str:
    v = _normalize_text(value)
    if not v:
        return "ACETATO"
    if "acet" in v and "metal" in v:
        return "ACETATO/METAL"
    if "acet" in v:
        return "ACETATO"
    if "acero" in v:
        return "METAL"
    if "metal" in v:
        return "METAL"
    if "titan" in v:
        return "TITANIO"
    if "nylon" in v:
        return "ACETATO"
    if "bio" in v and ("poly" in v or "poli" in v):
        return "BIOPOLIAMIDA"
    return "ACETATO"


def normalize_yes_no(value: str) -> str:
    v = _normalize_text(value)
    if v in {"si", "s", "yes", "y", "true", "1", "x"}:
        return "SI"
    return "NO"


def normalize_color(value: str, color_map: dict[str, str]) -> str:
    raw = _safe_text(value)
    if not raw:
        return ""

    upper_raw = raw.upper().strip()
    if upper_raw == "NEGRO/TALCO":
        return "BLANCO"

    if upper_raw in color_map:
        return color_map[upper_raw]

    compact_raw = raw.replace("/", "")
    compact_upper = upper_raw.replace("/", "")
    if compact_upper in color_map:
        return color_map[compact_upper]

    if re.fullmatch(r"[0-9A-Z]+", compact_upper):
        return compact_raw

    fallback = {
        "BLACK": "Negro",
        "BROWN": "Marrón",
        "BLUE": "Azul",
        "GREEN": "Verde",
        "GREY": "Gris",
        "GRAY": "Gris",
        "GOLD": "Oro",
        "SILVER": "Plateado",
        "WHITE": "Blanco",
        "CLEAR": "Transparente",
    }
    for src, dst in fallback.items():
        if src in upper_raw:
            return dst

    return compact_raw


def normalize_category(value: str) -> str:
    v = "".join(
        character for character in unicodedata.normalize("NFKD", _normalize_text(value))
        if not unicodedata.combining(character)
    )
    if v in {"gafas de vista", "gafas de vista junior"}:
        return "MONTURAS"
    if v in {"gafas de sol", "gafas de sol junior"}:
        return "GAFAS DE SOL"
    return ""


def infer_category(row: pd.Series) -> str:
    """Classify only the supplier's explicit collection, never lens or model hints."""
    return normalize_category(row.get("Colección", ""))


def mark_accessory(row: pd.Series, accessory_whitelist: set[str] | None = None) -> bool:
    whitelist = accessory_whitelist or set()
    model_code = _safe_text(row.get("Código del modelo", "")).upper()
    model_name = f" {_normalize_text(row.get('Nombre del modelo', ''))} "

    if model_code and model_code in whitelist:
        return False

    if model_code.startswith(ACCESSORY_MODEL_PREFIXES):
        return True
    if model_code.endswith(ACCESSORY_MODEL_SUFFIXES):
        return True
    if any(marker in model_name for marker in ACCESSORY_MODEL_MARKERS):
        return True

    haystack = " ".join(
        [
            _normalize_text(row.get("Categoría", "")),
            _normalize_text(row.get("Tipo", "")),
            _normalize_text(row.get("Nombre del modelo", "")),
            _normalize_text(row.get("Colección", "")),
        ]
    )
    return any(k in haystack for k in ACCESSORY_TEXT_KEYWORDS)


def mark_ralph_lauren(row: pd.Series) -> bool:
    brand = _normalize_text(row.get("Nombre de la marca", ""))
    code = _safe_text(row.get("Código de marca", "")).upper()
    if "polo" in brand:
        return False
    if "ralph lauren" in brand:
        return True
    return code in {"RL", "RA"}


def _is_valid_product(row: pd.Series) -> bool:
    # Producto valido para importacion: debe identificarse por UPC + marca + codigo de modelo.
    return bool(_safe_text(row.get("UPC", ""))) and bool(_safe_text(row.get("Nombre de la marca", ""))) and bool(
        _safe_text(row.get("Código del modelo", ""))
    )


def transform_masterdata(
    df: pd.DataFrame,
    color_map: dict[str, str],
    accessory_whitelist: set[str] | None = None,
    brand_map: dict[str, str] | None = None,
    shape_map: dict[str, str] | None = None,
    dictionary_rules: list[dict[str, str]] | None = None,
) -> tuple[pd.DataFrame, ValidationReport, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    input_count = len(df)
    df = _normalize_input_column_aliases(df)
    original_df = df.copy()
    source_categories = original_df.apply(infer_category, axis=1)
    df, dictionary_rules_not_found = apply_custom_dictionary(df, dictionary_rules)
    dictionary_values_not_resolved = find_dictionary_values_not_resolved(
        df,
        color_map,
        shape_map=shape_map,
        dictionary_rules=dictionary_rules,
    )

    # Regla de negocio: la colección debe salir de la columna L del origen.
    if df.shape[1] > 11:
        collection_from_col_l = df.iloc[:, 11].map(_safe_text)
    else:
        collection_from_col_l = df.get("Nombre del modelo", pd.Series("", index=df.index)).map(_safe_text)

    whitelist = accessory_whitelist or set()
    brand_map = brand_map or {}

    for col in EXPECTED_OUTPUT_COLUMNS:
        if col not in df.columns:
            df[col] = ""

    df["__is_accessory"] = df.apply(lambda row: mark_accessory(row, whitelist), axis=1)
    df["__is_ralph"] = df.apply(mark_ralph_lauren, axis=1)
    df["__is_invalid_product"] = ~df.apply(_is_valid_product, axis=1)
    df["__upc_missing_after_filters"] = False
    df["__dup_upc_after_filters"] = False

    discarded_accessories = int(df["__is_accessory"].sum())
    discarded_ralph = int(df["__is_ralph"].sum())
    discarded_invalid_product = int(df["__is_invalid_product"].sum())
    accessory_whitelist_kept = int(
        df["Código del modelo"].map(lambda x: _safe_text(x).upper() in whitelist).sum()
    )

    working = df[(~df["__is_accessory"]) & (~df["__is_ralph"]) & (~df["__is_invalid_product"])].copy()
    invalid_upc_after_filters = working["UPC"].map(lambda x: not bool(_safe_text(x)))
    if len(working):
        df.loc[working.index, "__upc_missing_after_filters"] = invalid_upc_after_filters

    # Transformaciones columna a columna.
    brand_data = working.apply(
        lambda r: normalize_brand(
            r.get("Nombre de la marca", ""),
            r.get("Código de marca", ""),
            r.get("Colección", ""),
            r.get("Nombre del modelo", ""),
            r.get("Tipo", ""),
            brand_map,
        ),
        axis=1,
    )
    working["Nombre de la marca"] = brand_data.map(lambda x: x[0])
    working["Código de marca"] = brand_data.map(lambda x: x[1])

    # Regla de negocio: la Colección se toma de la columna L del Excel origen.
    working["Colección"] = collection_from_col_l.reindex(working.index).fillna("").map(_safe_text)

    working["Código del modelo"] = working["Código del modelo"].map(normalize_model)
    working["Género"] = working.apply(
        lambda r: normalize_gender(r.get("Género", ""), r.get("Nombre de la marca", ""), r.get("Colección", "")),
        axis=1,
    )

    # Reglas especiales Ray Ban Junior.
    is_rb_junior = working["Nombre de la marca"].str.lower().eq("ray ban junior")
    working.loc[is_rb_junior, "Género"] = "Niño"

    working["Categoría"] = source_categories.reindex(working.index)

    rb_junior_sol = is_rb_junior & working["Categoría"].eq("GAFAS DE SOL")
    rb_junior_vista = is_rb_junior & working["Categoría"].eq("MONTURAS")
    working.loc[rb_junior_sol, "Código de marca"] = "RJ"
    working.loc[rb_junior_vista, "Código de marca"] = "RB"

    working["Código de marca"] = working["Código de marca"].str.replace(r"^RX", "RB", regex=True)
    working["Código de marca"] = working["Código de marca"].str.replace(r"^RY", "RB", regex=True)
    working["Código del modelo"] = working["Código del modelo"].str.replace(r"^RX", "RB", regex=True)
    working["Código del modelo"] = working["Código del modelo"].str.replace(r"^RY", "RB", regex=True)

    missing_brand_code = working["Código de marca"].map(lambda x: not bool(_safe_text(x)))
    working.loc[missing_brand_code, "Código de marca"] = working.loc[missing_brand_code, "Código del modelo"].map(
        derive_brand_code_from_model
    )

    working["Forma"] = working["Forma"].map(lambda value: normalize_shape(value, shape_map))
    working["Material del frente"] = working["Material del frente"].map(normalize_material)
    working["Color"] = working["Color"].map(lambda value: _safe_text(value).replace("/", ""))

    working["Descripción del color"] = working["Descripción del color"].map(lambda x: normalize_color(x, color_map))
    working["Color del frontal"] = working["Color del frontal"].map(lambda x: normalize_color(x, color_map))
    working["Color de las lentes"] = working["Color de las lentes"].map(lambda x: normalize_color(x, color_map))

    is_vista = working["Categoría"].eq("MONTURAS")
    working.loc[is_vista, "Color de las lentes"] = ""
    working.loc[is_vista, "Material de las lentes"] = ""

    working["Fotocromático"] = working["Fotocromático"].map(normalize_yes_no)
    working["Polarizado"] = working["Polarizado"].map(normalize_yes_no)

    working["PVP sugerido"] = working["PVP sugerido"].map(_safe_text)

    # Las reglas personalizadas se reaplican sobre el valor original para que
    # también puedan corregir el valor canónico final o vaciarlo.
    working, _ = apply_custom_dictionary(working, dictionary_rules, source_df=original_df)
    # Supplier classification is authoritative, including unresolved collections.
    working["Categoría"] = source_categories.reindex(working.index)
    working["PVO"] = working["PVO"].map(_safe_text)

    # Validaciones post-transformacion.
    invalid_upc_mask = working["UPC"].map(lambda x: not bool(_safe_text(x)))
    discarded_invalid_upc = int(invalid_upc_mask.sum())
    if discarded_invalid_upc:
        working = working[~invalid_upc_mask].copy()

    dup_mask = working.duplicated(subset=["UPC"], keep="first")
    if len(working):
        df.loc[working.index[dup_mask], "__dup_upc_after_filters"] = True

    kept_for_zero = working[~dup_mask].copy()
    working = working.drop_duplicates(subset=["UPC"], keep="first").copy()

    for col in EXPECTED_OUTPUT_COLUMNS:
        working[col] = working[col].map(_safe_text)

    output_canonical = working[EXPECTED_OUTPUT_COLUMNS].copy()

    # Export final con estructura acordada para result.xlsx.
    output = output_canonical.rename(
        columns={
            "UPC": "Barcode",
            "Nombre de la marca": "Marca",
            "Código del modelo": "Modelo",
            "Dimensión del puente": "Ancho Puente",
            "Largo de varilla": "Longitud Varilla",
            "Color del frontal": "Color Frontal",
            "Color de las lentes": "Color Lente",
            "Material del frente": "Material Principal",
            "PVP sugerido": "PVP",
        }
    )

    for col in RESULT_EXPORT_COLUMNS:
        if col not in output.columns:
            output[col] = ""
    output = output[RESULT_EXPORT_COLUMNS].copy()

    source_upc = {u for u in df["UPC"].map(_safe_text).tolist() if u}
    output_upc = {u for u in output["Barcode"].map(_safe_text).tolist() if u}

    leading_zero_summary = _build_leading_zero_summary(df, kept_for_zero, output_canonical)
    leading_zero_issues = sum(
        1 for row in leading_zero_summary if row["incidencia_reportada_por_script"] == "SI"
    )
    leading_zero_real_loss_columns = sum(
        1 for row in leading_zero_summary if row["perdida_real_post_filtros"] == "SI"
    )

    invalid_yes_no_rows = int(
        (~output["Fotocromático"].isin(["SI", "NO"]) | ~output["Polarizado"].isin(["SI", "NO"])).sum()
    )

    anomalous: dict[str, list[str]] = {}
    unknown_shapes = sorted(set(output_canonical[~output_canonical["Forma"].isin(VALID_SHAPE)]["Forma"].tolist()))
    unknown_gender = sorted(set(output_canonical[~output_canonical["Género"].isin(VALID_GENDER)]["Género"].tolist()))
    unknown_materials = sorted(
        set(
            output_canonical[
                ~output_canonical["Material del frente"].isin(VALID_MATERIALS)
            ]["Material del frente"].tolist()
        )
    )
    unknown_cats = sorted(
        set(output_canonical[~output_canonical["Categoría"].isin(VALID_CATEGORIES)]["Categoría"].tolist())
    )
    category_review_rows = [
        {
            "fila_excel": int(index) + 2,
            "UPC": _safe_text(original_df.loc[index].get("UPC", "")),
            "Modelo": _safe_text(original_df.loc[index].get("Código del modelo", "")),
            "Colección": _safe_text(original_df.loc[index].get("Colección", "")),
            "motivo": "Coleccion vacia o no reconocida; revisar antes de importar",
        }
        for index in output_canonical.index[output_canonical["Categoría"].eq("")]
    ]

    if unknown_shapes:
        anomalous["forma"] = unknown_shapes
    if unknown_gender:
        anomalous["genero"] = unknown_gender
    if unknown_materials:
        anomalous["material_frente"] = unknown_materials
    if unknown_cats:
        anomalous["categoria"] = unknown_cats

    brand_audit = _build_brand_audit(df, brand_map)
    unmatched_brand_names = brand_audit[brand_audit["existe_en_bd"].eq("NO")]["marca_origen"].tolist()

    report = ValidationReport(
        total_input=input_count,
        total_output=len(output),
        discarded=input_count - len(output),
        discarded_accessories=discarded_accessories,
        discarded_ralph_lauren=discarded_ralph,
        discarded_invalid_product=discarded_invalid_product,
        accessory_whitelist_kept=accessory_whitelist_kept,
        discarded_invalid_upc=discarded_invalid_upc,
        upc_missing_after_transform=len(source_upc - output_upc),
        leading_zero_issues=leading_zero_issues,
        leading_zero_real_loss_columns=leading_zero_real_loss_columns,
        leading_zero_summary=leading_zero_summary,
        unmatched_brand_names=unmatched_brand_names,
        invalid_yes_no_rows=invalid_yes_no_rows,
        anomalous_values=anomalous,
        dictionary_rules_not_found=dictionary_rules_not_found,
        dictionary_values_not_resolved=dictionary_values_not_resolved,
        category_review_rows=category_review_rows,
    )

    discarded_audit = _build_audit_discarded(df)
    leading_zero_summary_df = pd.DataFrame(leading_zero_summary)

    return output, report, discarded_audit, leading_zero_summary_df, brand_audit


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Transforma un Excel de Luxottica a MASTERDATA normalizado para Odoo."
    )
    parser.add_argument("--input", required=True, help="Ruta del Excel origen Luxottica")
    parser.add_argument("--output", required=True, help="Ruta del Excel de salida MASTERDATA")
    parser.add_argument(
        "--report",
        default="",
        help="Ruta opcional para exportar un reporte JSON de validaciones",
    )
    parser.add_argument(
        "--no-db-colors",
        action="store_true",
        help="No consultar tablas de color en BD y usar solo diccionario fallback",
    )
    parser.add_argument(
        "--export-audits",
        action="store_true",
        help="Exportar automaticamente CSV de descartes y resumen de ceros iniciales",
    )
    parser.add_argument(
        "--accessory-whitelist-codes",
        default="",
        help="Lista CSV de codigos de modelo a conservar aunque parezcan accesorios",
    )
    parser.add_argument(
        "--accessory-whitelist-file",
        default="",
        help="Archivo txt con un codigo de modelo por linea (se ignoran lineas vacias o comentarios #)",
    )
    parser.add_argument(
        "--executive-summary",
        default="",
        help="Ruta opcional para exportar un resumen ejecutivo en Markdown",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)
    report_path = Path(args.report) if args.report else None
    executive_summary_path = Path(args.executive_summary) if args.executive_summary else None
    whitelist_codes = _parse_whitelist_codes(args.accessory_whitelist_codes)
    if args.accessory_whitelist_file:
        whitelist_codes |= _load_whitelist_file(Path(args.accessory_whitelist_file))

    if not input_path.exists():
        raise FileNotFoundError(f"No existe el archivo de entrada: {input_path}")

    df = load_excel_as_text(input_path)

    color_map: dict[str, str] = {}
    brand_map: dict[str, str] = {}
    shape_map: dict[str, str] = {}
    if not args.no_db_colors:
        try:
            color_map = build_color_dictionary_from_db()
        except Exception as exc:
            print(f"[WARN] No se pudo cargar diccionario de color desde BD: {exc}")
            color_map = {}

    try:
        brand_map = build_brand_dictionary_from_db()
    except Exception as exc:
        print(f"[WARN] No se pudo cargar diccionario de marca desde BD: {exc}")
        brand_map = {}

    try:
        shape_map = build_shape_dictionary_from_db()
    except Exception as exc:
        print(f"[WARN] No se pudo cargar diccionario de formas desde BD: {exc}")
        shape_map = {}

    result, report, discarded_audit, leading_zero_summary_df, brand_audit = transform_masterdata(
        df,
        color_map,
        accessory_whitelist=whitelist_codes,
        brand_map=brand_map,
        shape_map=shape_map,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    result.to_excel(output_path, index=False)

    print("=" * 80)
    print("TRANSFORMACION COMPLETADA")
    print("=" * 80)
    print(f"Registros procesados: {report.total_input}")
    print(f"Registros exportados: {report.total_output}")
    print(f"Registros descartados: {report.discarded}")
    print(f" - Accesorios: {report.discarded_accessories}")
    print(f" - Ralph Lauren: {report.discarded_ralph_lauren}")
    print(f" - Producto invalido: {report.discarded_invalid_product}")
    print(f" - Excepciones whitelist conservadas: {report.accessory_whitelist_kept}")
    print(f" - UPC vacio: {report.discarded_invalid_upc}")
    print(f"UPC faltantes tras transformacion: {report.upc_missing_after_transform}")
    print(f"Incidencias de ceros iniciales: {report.leading_zero_issues}")
    print(f"Perdida real de ceros iniciales (columnas): {report.leading_zero_real_loss_columns}")
    print(f"Marcas no resueltas contra BD: {len(report.unmatched_brand_names)}")
    print(f"Filas invalidas SI/NO: {report.invalid_yes_no_rows}")

    if report.anomalous_values:
        print("Valores anómalos detectados:")
        for key, values in report.anomalous_values.items():
            print(f" - {key}: {', '.join(values)}")
    else:
        print("No se detectaron valores anómalos en catálogos normalizados.")

    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Reporte JSON: {report_path}")

    if args.export_audits:
        discarded_path = output_path.with_name(f"{output_path.stem}_descartes_auditoria.csv")
        zeros_path = output_path.with_name(f"{output_path.stem}_ceros_iniciales_resumen.csv")
        brands_path = output_path.with_name(f"{output_path.stem}_marcas_auditoria.csv")
        discarded_audit.to_csv(discarded_path, index=False, encoding="utf-8-sig")
        leading_zero_summary_df.to_csv(zeros_path, index=False, encoding="utf-8-sig")
        brand_audit.to_csv(brands_path, index=False, encoding="utf-8-sig")
        print(f"Auditoria descartes CSV: {discarded_path}")
        print(f"Auditoria ceros iniciales CSV: {zeros_path}")
        print(f"Auditoria marcas CSV: {brands_path}")

    if executive_summary_path:
        executive_summary_path.parent.mkdir(parents=True, exist_ok=True)
        executive_summary_path.write_text(
            build_executive_summary_markdown(report, input_path, output_path, report_path),
            encoding="utf-8",
        )
        print(f"Resumen ejecutivo MD: {executive_summary_path}")


if __name__ == "__main__":
    main()
