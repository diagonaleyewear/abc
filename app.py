from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from engine import load_input_file, run_abcd_engine
from cloud_design import BRAND_GREEN, inject_styles


APP_TITLE = "📦 Control ABCD de Productos"
APP_ICON = "📊"
LOCAL_INPUT_NAME = "input.xlsx"


def configure_page() -> None:
    st.set_page_config(
        page_title=APP_TITLE,
        page_icon=APP_ICON,
        layout="wide",
    )
    inject_styles()


@st.cache_data
def load_data(uploaded_file, local_path: Path) -> pd.DataFrame:
    if uploaded_file is not None:
        return load_input_file(uploaded_file)

    if local_path.exists():
        return load_input_file(local_path)

    raise FileNotFoundError(
        "No se encontró input.xlsx y no se ha subido ningún archivo." 
    )


@st.cache_data
def prepare_dataframe(raw_df: pd.DataFrame) -> pd.DataFrame:
    return run_abcd_engine(raw_df)


def format_currency(value: float) -> str:
    return f"{value:,.0f} €"


def style_table(df: pd.DataFrame) -> pd.DataFrame.style:
    pd.set_option("styler.render.max_elements", len(df) * len(df.columns))
    
    abcd_palette = {
        "A": f"background-color: {BRAND_GREEN}; color: white",
        "B": "background-color: #fca311; color: black",
        "C": "background-color: #ffdd57; color: black",
        "D": "background-color: #212121; color: white",
    }
    alert_palette = {
        "🔴 LIQUIDAR": "background-color: #d62828; color: white",
        "🟠 REVISAR": "background-color: #f77f00; color: white",
        "🟢 OK": "background-color: #2a9d8f; color: white",
        "⚪ BAJO IMPACTO": "background-color: #8d99ae; color: white",
    }

    def abcd_style(value):
        return abcd_palette.get(value, "")

    def alert_style(value):
        return alert_palette.get(value, "")

    styled = df.style
    styled = styled.map(abcd_style, subset=["ABCD"])
    styled = styled.map(alert_style, subset=["Alerta"])
    styled = styled.format({"Capital_Bloqueado (€)": "{0:,.0f}"})
    styled = styled.set_properties(**{"text-align": "center"})
    return styled


def build_sidebar_filters(df: pd.DataFrame) -> tuple[list[str], list[str], bool, bool]:
    st.sidebar.header("🔎 Filtros")

    selected_abcd = st.sidebar.multiselect(
        "ABCD",
        options=sorted(df["ABCD"].dropna().unique()),
        default=sorted(df["ABCD"].dropna().unique()),
    )

    selected_alerts = st.sidebar.multiselect(
        "Alerta",
        options=sorted(df["Alerta"].dropna().unique()),
        default=sorted(df["Alerta"].dropna().unique()),
    )

    only_d = st.sidebar.checkbox("Solo productos D")
    only_review = st.sidebar.checkbox("Solo productos a revisar (≤ 15 días)")

    return selected_abcd, selected_alerts, only_d, only_review


def filter_dataframe(
    df: pd.DataFrame,
    selected_abcd: list[str],
    selected_alerts: list[str],
    only_d: bool,
    only_review: bool,
) -> pd.DataFrame:
    df_filtered = df.copy()
    df_filtered = df_filtered[df_filtered["ABCD"].isin(selected_abcd)]
    df_filtered = df_filtered[df_filtered["Alerta"].isin(selected_alerts)]

    if only_d:
        df_filtered = df_filtered[df_filtered["ABCD"] == "D"]

    if only_review:
        df_filtered = df_filtered[df_filtered["Dias_para_D"] <= 15]

    return df_filtered


def render_kpis(df: pd.DataFrame) -> None:
    total_products = len(df)
    total_blocked = df["Capital_Bloqueado (€)"].sum()
    blocked_d = df.loc[df["ABCD"] == "D", "Capital_Bloqueado (€)"].sum()
    red_alerts = len(df[df["Alerta"] == "🔴 LIQUIDAR"])
    orange_alerts = len(df[df["Alerta"] == "🟠 REVISAR"])

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total productos", total_products)
    c2.metric("Capital bloqueado total", format_currency(total_blocked))
    c3.metric("Capital bloqueado en D", format_currency(blocked_d))
    c4.metric("Alertas 🔴 / 🟠", f"{red_alerts} / {orange_alerts}")


def render_main_table(df: pd.DataFrame) -> None:
    st.subheader("📋 Tabla principal de productos")
    st.caption("Ordena las columnas y usa los filtros para explorar la clasificación.")

    styled = style_table(df)
    st.dataframe(styled, use_container_width=True)


def render_secondary_views(df: pd.DataFrame) -> None:
    st.divider()
    left, right = st.columns(2)

    with left:
        st.subheader("🔴 Productos D (Liquidación)")
        d_products = df[df["ABCD"] == "D"].sort_values(
            by="Capital_Bloqueado (€)", ascending=False
        )
        st.dataframe(
            d_products[
                [
                    "ABCD",
                    "Marca",
                    "Cód Barras",
                    "Stock",
                    "Motivo",
                    "Capital_Bloqueado (€)",
                    "Fecha_Revision",
                    "Dias_para_D",
                    "Alerta",
                    "Accion_Recomendada",
                ]
            ],
            use_container_width=True,
        )

    with right:
        st.subheader("🟠 Productos B que pasan a D pronto")
        soon_to_d = df[
            (df["ABCD"] == "B") & (df["Dias_para_D"] <= 15)
        ].sort_values(by="Dias_para_D")
        st.dataframe(
            soon_to_d[
                [
                    "ABCD",
                    "Marca",
                    "Cód Barras",
                    "Stock",
                    "Motivo",
                    "Dias_para_D",
                    "Capital_Bloqueado (€)",
                    "Alerta",
                    "Accion_Recomendada",
                ]
            ],
            use_container_width=True,
        )


def main() -> None:
    configure_page()
    st.title(APP_TITLE)
    st.caption("Visualiza el motor ABCD sin duplicar la lógica de negocio en la interfaz.")

    project_dir = Path(__file__).resolve().parent
    local_input = project_dir / LOCAL_INPUT_NAME
    if local_input.exists():
        st.sidebar.success(f"Cargando datos locales desde `{LOCAL_INPUT_NAME}`")

    st.sidebar.header("⚙️ Origen de datos")
    uploaded_file = st.sidebar.file_uploader(
        "Sube un Excel de productos (input.xlsx)", type=["xlsx"]
    )

    try:
        raw_df = load_data(uploaded_file, local_input)
    except FileNotFoundError as error:
        st.info(str(error))
        st.stop()

    try:
        with st.spinner("Ejecutando motor ABCD..."):
            df = prepare_dataframe(raw_df)
    except ValueError as error:
        st.error(str(error))
        st.stop()

    render_kpis(df)

    selected_abcd, selected_alerts, only_d, only_review = build_sidebar_filters(df)
    filtered_df = filter_dataframe(df, selected_abcd, selected_alerts, only_d, only_review)

    if filtered_df.empty:
        st.warning("No hay productos que coincidan con los filtros seleccionados.")
    else:
        render_main_table(filtered_df)

    render_secondary_views(df)


if __name__ == "__main__":
    main()
