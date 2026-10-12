from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd


DEFAULT_MARGIN_DAYS = 90
DEFAULT_DAYS_WITHOUT_SALES_FOR_D = 180
DEFAULT_DAYS_WITHOUT_SALES_FOR_C = 60   # Nuevo umbral para C
DEFAULT_PERCENTILE_A = 0.80
REQUIRED_COLUMNS = [
    "PVO",
    "Stock",
    "Primera Compra",
    "Última Venta",
]
OPTIONAL_COLUMNS = [
    "Num_Ventas_180D",
    "Ventas_180_Dias",
]


def run_abcd_engine(
    df: pd.DataFrame,
    days_without_sales_for_d: int = DEFAULT_DAYS_WITHOUT_SALES_FOR_D,
    reference_date: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """Transforma un DataFrame de productos en la clasificación ABCD.

    El motor devuelve un DataFrame con las columnas esperadas por la app.
    Se puede fijar una fecha de referencia para calcular la clasificación.
    """
    df = df.copy()
    _validate_input_columns(df)

    df["PVO"] = pd.to_numeric(df["PVO"], errors="coerce").fillna(0)
    df["Stock"] = pd.to_numeric(df["Stock"], errors="coerce").fillna(0)
    df["Primera Compra"] = pd.to_datetime(
        df["Primera Compra"], errors="coerce"
    )
    df["Última Venta"] = pd.to_datetime(
        df["Última Venta"], errors="coerce"
    )
    
    # Agregar columnas opcionales si no existen
    if "Num_Ventas_180D" not in df.columns:
        df["Num_Ventas_180D"] = 0
    else:
        df["Num_Ventas_180D"] = pd.to_numeric(df["Num_Ventas_180D"], errors="coerce").fillna(0)
    
    if "Ventas_180_Dias" not in df.columns:
        df["Ventas_180_Dias"] = 0
    else:
        df["Ventas_180_Dias"] = pd.to_numeric(df["Ventas_180_Dias"], errors="coerce").fillna(0)
    
    if "Última Reposicion" not in df.columns:
        df["Última Reposicion"] = pd.NaT
    else:
        df["Última Reposicion"] = pd.to_datetime(df["Última Reposicion"], errors="coerce")

    df["Capital_Bloqueado (€)"] = df["PVO"] * df["Stock"]
    df["Fecha_Revision"] = df["Primera Compra"] + pd.to_timedelta(
        DEFAULT_MARGIN_DAYS, unit="D"
    )

    today = (
        pd.Timestamp(reference_date).normalize()
        if reference_date is not None
        else pd.Timestamp.today().normalize()
    )
    df["Dias_desde_Primera_Compra"] = (
        today - df["Primera Compra"]
    ).dt.days
    df["Dias_para_D"] = (df["Fecha_Revision"] - today).dt.days

    p80 = df["PVO"].quantile(DEFAULT_PERCENTILE_A)

    has_stock = df["Stock"] > 0
    has_sales = df["Última Venta"].notna()
    
    # LÓGICA MEJORADA: Considerar ventanas de agotamiento
    # Si última reposición es posterior a última venta -> producto se agotó
    ultima_venta_norm = df["Última Venta"].dt.normalize()
    ultima_reposicion_norm = df["Última Reposicion"].dt.normalize()
    se_agoto = (ultima_reposicion_norm > ultima_venta_norm) & ultima_venta_norm.notna()
    
    # Calcular días sin ventas excluyendo períodos de agotamiento
    days_since_last_sale = np.where(
        has_sales,
        np.where(
            se_agoto,
            # Si se agotó: contar días desde la reposición (no hay stock para vender)
            (today - ultima_reposicion_norm).dt.days,
            # Si no se agotó: contar días desde la última venta
            (today - ultima_venta_norm).dt.days
        ),
        9999,
    )
    has_margin = df["Dias_desde_Primera_Compra"] >= DEFAULT_MARGIN_DAYS
    days_to_d = df["Dias_para_D"].fillna(9999)

    df["ABCD"] = ""
    df["Motivo"] = ""
    df["Alerta"] = ""
    df["Accion_Recomendada"] = ""

    # REGLA D: Sin ventas 180+ días (excluyendo ventanas de agotamiento)
    # Un producto es D si:
    # 1. Tiene stock
    # 2. Pasó período de margen
    # 3. No tiene ventas en últimos 180 días (sin contar agotamientos)
    # 4. No tiene ventas recientes en últimos 180 días
    has_recent_sales = df["Num_Ventas_180D"] > 0
    no_sales_for_d = days_since_last_sale >= days_without_sales_for_d
    mask_d = has_stock & has_margin & no_sales_for_d & ~has_recent_sales
    
    df.loc[mask_d, ["ABCD", "Motivo", "Alerta", "Accion_Recomendada"]] = [
        "D",
        "Sin ventas 180+ días (sin demanda reciente)",
        "LIQUIDAR",
        "Liquidar / No reponer",
    ]

    # REGLA A: Probados, ventas activas, alto valor
    mask_a = ~mask_d & has_sales & has_margin & (df["PVO"] >= p80)
    df.loc[mask_a, ["ABCD", "Motivo", "Alerta", "Accion_Recomendada"]] = [
        "A",
        "Producto probado, ventas activas y alto valor",
        "OK",
        "Reponer / Priorizar",
    ]

    # REGLA B: Con ventas pero valor medio
    mask_b_sales = ~mask_d & has_sales & ~mask_a
    df.loc[mask_b_sales, ["ABCD", "Motivo", "Alerta", "Accion_Recomendada"]] = [
        "B",
        "Producto con ventas pero valor medio",
        "OK",
        "Mantener controlado",
    ]

    # REGLA B: Productos nuevos en período de prueba
    mask_b_new = ~mask_d & ~has_sales & (df["Dias_desde_Primera_Compra"] < DEFAULT_MARGIN_DAYS)
    df.loc[mask_b_new, ["ABCD", "Motivo", "Accion_Recomendada"]] = [
        "B",
        "Producto nuevo en periodo de prueba",
        "Observar / No ampliar stock",
    ]
    df.loc[mask_b_new & (days_to_d <= 15), "Alerta"] = "REVISAR"
    df.loc[mask_b_new & (days_to_d > 15), "Alerta"] = "OK"

    # REGLA C: Sin ventas 60+ días (excluyendo agotamientos)
    # O bajo impacto económico/rotación limitada
    no_sales_for_c = days_since_last_sale >= DEFAULT_DAYS_WITHOUT_SALES_FOR_C
    mask_c = ~(mask_d | mask_a | mask_b_sales | mask_b_new) & (no_sales_for_c | ~has_sales)
    df.loc[mask_c, ["ABCD", "Motivo", "Alerta", "Accion_Recomendada"]] = [
        "C",
        "Sin ventas 60+ días o bajo impacto económico",
        "BAJO IMPACTO",
        "Mantener mínimo",
    ]
    
    # Productos que no caen en ninguna categoría (muy nuevos, poco stock, etc)
    mask_other = ~(mask_d | mask_a | mask_b_sales | mask_b_new | mask_c)
    df.loc[mask_other, ["ABCD", "Motivo", "Alerta", "Accion_Recomendada"]] = [
        "B",
        "Producto en evaluación",
        "OK",
        "Observar",
    ]

    output_columns = [
        c
        for c in df.columns
        if c not in [
            "ABCD",
            "Motivo",
            "Alerta",
            "Accion_Recomendada",
            "Capital_Bloqueado (€)",
            "Fecha_Revision",
            "Dias_para_D",
        ]
    ]
    output_columns.extend(
        [
            "ABCD",
            "Motivo",
            "Capital_Bloqueado (€)",
            "Fecha_Revision",
            "Dias_para_D",
            "Alerta",
            "Accion_Recomendada",
        ]
    )
    return df[output_columns]


def _validate_input_columns(df: pd.DataFrame) -> None:
    missing = [col for col in REQUIRED_COLUMNS if col not in df.columns]
    if missing:
        raise ValueError(
            f"El DataFrame de entrada debe incluir las columnas: {', '.join(missing)}"
        )


def _classify_row(
    row: pd.Series,
    p80: float,
    days_without_sales_for_d: int,
    reference_date: pd.Timestamp,
) -> tuple[str, str, str, str]:
    has_stock = row["Stock"] > 0
    has_sales = pd.notna(row["Última Venta"])
    days_since_last_sale = (
        (reference_date - pd.to_datetime(row["Última Venta"]).normalize()).days
        if has_sales
        else 9999
    )
    has_margin = row["Dias_desde_Primera_Compra"] >= DEFAULT_MARGIN_DAYS
    no_sales_for_long = days_since_last_sale >= days_without_sales_for_d
    days_to_d = row["Dias_para_D"] if pd.notna(row["Dias_para_D"]) else 9999

    if has_stock and has_margin and no_sales_for_long:
        return (
            "D",
            "Stock sin ventas tras periodo de seguridad",
            "🔴 LIQUIDAR",
            "Liquidar / No reponer",
        )

    if has_sales and row["Dias_desde_Primera_Compra"] >= DEFAULT_MARGIN_DAYS and row["PVO"] >= p80:
        return (
            "A",
            "Producto probado, ventas activas y alto valor",
            "🟢 OK",
            "Reponer / Priorizar",
        )

    if has_sales:
        return (
            "B",
            "Producto con ventas pero valor medio",
            "🟢 OK",
            "Mantener controlado",
        )

    if not has_sales and row["Dias_desde_Primera_Compra"] < DEFAULT_MARGIN_DAYS:
        alerta = "🟠 REVISAR" if days_to_d <= 15 else "🟢 OK"
        return (
            "B",
            "Producto nuevo en periodo de prueba",
            alerta,
            "Observar / No ampliar stock",
        )

    return (
        "C",
        "Bajo impacto económico o rotación limitada",
        "⚪ BAJO IMPACTO",
        "Mantener mínimo",
    )


def _normalize_header(value: object) -> str:
    if pd.isna(value):
        return ""
    return str(value).strip()


def _find_header_row(df: pd.DataFrame) -> int | None:
    for index, row in df.iterrows():
        headers = {_normalize_header(value) for value in row.tolist()}
        if set(REQUIRED_COLUMNS).issubset(headers):
            return int(index)
    return None


def load_input_file(path_or_buffer) -> pd.DataFrame:
    """Cargar un archivo Excel desde un path o desde un objeto de archivo."""
    raw = pd.read_excel(path_or_buffer, engine="openpyxl", header=None)
    header_row = _find_header_row(raw)
    if header_row is None:
        preview = []
        for index in range(min(10, len(raw))):
            row = [str(_normalize_header(value)) for value in raw.iloc[index].tolist()]
            preview.append(f"Fila {index}: {row}")
        raise ValueError(
            "El Excel no contiene una fila de cabecera con las columnas requeridas: "
            f"{', '.join(REQUIRED_COLUMNS)}.\n" +
            "Encabezados detectados en las primeras filas:\n" +
            "\n".join(preview)
        )

    raw.columns = [_normalize_header(value) for value in raw.iloc[header_row].tolist()]
    df = raw.iloc[header_row + 1 :].reset_index(drop=True)
    df = df.loc[:, [column for column in df.columns if column]]
    return df


if __name__ == "__main__":
    base_path = Path(__file__).resolve().parent
    sample_path = base_path / "input.xlsx"
    if not sample_path.exists():
        raise FileNotFoundError("No se ha encontrado input.xlsx en el directorio del proyecto.")
    df_sample = load_input_file(sample_path)
    df_result = run_abcd_engine(df_sample)
    print(df_result.head(5).to_string(index=False))
