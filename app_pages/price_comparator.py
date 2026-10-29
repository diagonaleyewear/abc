from __future__ import annotations

from io import BytesIO
import sqlite3
from zipfile import BadZipFile

import pandas as pd
import streamlit as st

from cloud_design import page_title
from price_comparator import PriceComparatorStorageError, PriceComparatorStore


OWN_PRICE_COLUMNS = [
    "EAN", "Precio", "Precio promocional", "Moneda", "Mercado",
    "Tipo cambio EUR", "Fecha consulta", "Origen",
]
OFFER_COLUMNS = [
    "EAN", "Competidor", "Precio", "Precio promocional", "Precio anterior",
    "Coste envío", "Moneda", "Tipo cambio EUR", "Mercado", "URL",
    "Disponibilidad", "Estado coincidencia", "Evidencia", "Método", "Fecha consulta",
]


def _report(label: str, report) -> None:
    st.success(f"{label}: {report.imported} importados, {report.updated} actualizados, "
               f"{len(report.rejected)} rechazados.")
    if report.rejected:
        with st.expander(f"Ver rechazos ({len(report.rejected)})"):
            for error in report.rejected:
                st.write(error)


def _show_pending_reports() -> None:
    reports = st.session_state.pop("price_comparator_import_reports", [])
    for label, report in reports:
        _report(label, report)


def _excel_bytes(sheets: dict[str, pd.DataFrame]) -> bytes:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for sheet, frame in sheets.items():
            frame.to_excel(writer, sheet_name=sheet[:31], index=False)
    return output.getvalue()


def render_price_comparator_page() -> None:
    page_title(
        "Comparador de precios",
        "Comparación trazable por EAN. Las ofertas sólo cuentan como confirmadas "
        "cuando son exactas, disponibles y del mismo mercado.",
    )
    try:
        store = PriceComparatorStore()
    except PriceComparatorStorageError as exc:
        st.error(str(exc))
        return
    _show_pending_reports()
    products = store.products()
    max_age = st.number_input("Antigüedad máxima de ofertas (días)", min_value=1, max_value=365,
                              value=30, step=1)
    summary, counts = store.dashboard(int(max_age))
    current_offers = store.current_offers(int(max_age))
    current_search_results = store.current_search_results(int(max_age))
    search_summary = store.search_result_summary(int(max_age))
    competitors = store.competitors()
    metric_names = (
        ("Productos monitorizados", "products"),
        ("Con oferta confirmada", "confirmed"),
        ("Sin resultados vigentes", "no_results"),
        ("Competidores activos", "competitors"),
    )
    cols = st.columns(len(metric_names))
    for column, (label, key) in zip(cols, metric_names):
        column.metric(label, counts[key])
    coverage = (counts["confirmed"] / counts["products"] * 100) if counts["products"] else 0
    st.caption(f"Cobertura de ofertas exactas y disponibles: {coverage:.1f}% · "
               f"Vytria más caro: {counts['vytria_more_expensive']} · "
               f"Vytria más barato: {counts['vytria_less_expensive']} · "
               f"Última oferta importada: {current_offers['captured_at'].max() if not current_offers.empty else '—'}")
    st.caption(f"Pendientes de revisión: {counts['pending_review']} · "
               f"Búsquedas sin resultado: {search_summary.get('not_found', 0)} · "
               f"Búsquedas con error: {search_summary.get('error', 0)} · "
               f"Última consulta manual: {competitors['last_checked_at'].max() if not competitors.empty else '—'}")
    st.caption("Las diferencias porcentuales usan como denominador el precio mínimo confirmado de competencia. "
               "Un resultado positivo indica que Vytria es más caro.")

    products_tab, offers_tab, competitors_tab = st.tabs(
        ["Catálogo y precio Vytria", "Ofertas e histórico", "Competidores"]
    )
    with products_tab:
        st.subheader("Importar catálogo piloto")
        workbook = st.file_uploader("Libro con las hojas «100 gafas» y «Competidores»",
                                    type=["xlsx"], key="price_catalog_upload")
        if workbook and st.button("Importar catálogo", type="primary"):
            try:
                result = store.import_workbook(workbook.getvalue())
            except (OSError, ValueError, sqlite3.Error, BadZipFile) as exc:
                st.error(f"No se pudo importar el catálogo: {exc}")
            else:
                st.session_state["price_comparator_import_reports"] = [
                    ("Productos", result["products"]),
                    ("Competidores", result["competitors"]),
                ]
                st.rerun()

        st.divider()
        st.subheader("Precio publicado por Vytria")
        st.caption("No se consulta Odoo automáticamente: hasta confirmar la fuente del precio final de tienda, "
                   "cárguelo con su mercado, moneda, origen y fecha.")
        own_file = st.file_uploader("Importar precios Vytria (.xlsx)", type=["xlsx"], key="own_price_upload")
        if own_file and st.button("Importar precios Vytria", key="import_own_prices"):
            try:
                frame = pd.read_excel(BytesIO(own_file.getvalue()), dtype=str).fillna("")
                result = store.import_own_prices(frame)
            except (OSError, ValueError, sqlite3.Error, BadZipFile) as exc:
                st.error(f"No se pudieron importar los precios: {exc}")
            else:
                st.session_state["price_comparator_import_reports"] = [("Precios Vytria", result)]
                st.rerun()
        template = pd.DataFrame(columns=OWN_PRICE_COLUMNS)
        st.download_button("Descargar plantilla de precios Vytria",
                           _excel_bytes({"Precios Vytria": template}),
                           "plantilla_precios_vytria.xlsx",
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        st.dataframe(products, width="stretch", hide_index=True)

    with offers_tab:
        st.subheader("Importar ofertas verificadas")
        st.warning("No se realizan consultas web en esta fase. Use sólo datos obtenidos por métodos permitidos "
                   "y registre URL, método, mercado y evidencia.")
        st.caption("Valores válidos: disponibilidad `available`, `unavailable` o `unknown`; "
                   "coincidencia `exact_match`, `probable_match`, `manual_review`, `not_found` o `error`. "
                   "El tipo de cambio se expresa en EUR por unidad de moneda.")
        offer_file = st.file_uploader("Importar ofertas (.xlsx)", type=["xlsx"], key="offer_upload")
        if offer_file and st.button("Importar ofertas", type="primary", key="import_offers"):
            try:
                frame = pd.read_excel(BytesIO(offer_file.getvalue()), dtype=str).fillna("")
                result = store.import_offers(frame)
            except (OSError, ValueError, sqlite3.Error, BadZipFile) as exc:
                st.error(f"No se pudieron importar las ofertas: {exc}")
            else:
                st.session_state["price_comparator_import_reports"] = [("Ofertas", result)]
                st.rerun()
        st.download_button("Descargar plantilla de ofertas",
                           _excel_bytes({"Ofertas": pd.DataFrame(columns=OFFER_COLUMNS)}),
                           "plantilla_ofertas_competencia.xlsx",
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

        if not current_offers.empty:
            st.subheader("Ofertas recientes")
            st.dataframe(current_offers, width="stretch", hide_index=True)
        if not current_search_results.empty:
            st.subheader("Resultados de búsqueda sin oferta")
            st.dataframe(current_search_results, width="stretch", hide_index=True)

        if not summary.empty:
            brand_options = sorted(value for value in summary["brand"].dropna().unique() if value)
            selected_brands = st.multiselect("Marca", brand_options, default=brand_options, key="price_brand_filter")
            comparison_states = st.multiselect("Estado comparativo", ["Con ofertas confirmadas", "Sin ofertas vigentes"],
                                    default=["Con ofertas confirmadas", "Sin ofertas vigentes"])
            competitor_options = sorted(current_offers["competitor_name"].dropna().unique()) if not current_offers.empty else []
            selected_competitors = st.multiselect("Competidor", competitor_options, default=[])
            match_options = sorted(current_offers["match_status"].dropna().unique()) if not current_offers.empty else []
            selected_matches = st.multiselect("Estado de coincidencia", match_options, default=[])
            availability_options = sorted(current_offers["availability"].dropna().unique()) if not current_offers.empty else []
            selected_availability = st.multiselect("Disponibilidad", availability_options, default=[])
            difference_filter = st.selectbox("Diferencia de precio", [
                "Todas", "Vytria más caro", "Vytria más barato", "Sin diferencia comparable",
            ])
            filtered = summary[summary["brand"].isin(selected_brands)]
            if "Con ofertas confirmadas" not in comparison_states:
                filtered = filtered[filtered["confirmed_offer_count"] == 0]
            if "Sin ofertas vigentes" not in comparison_states:
                filtered = filtered[filtered["confirmed_offer_count"] > 0]
            filtered_offers = current_offers.copy()
            if selected_competitors:
                filtered_offers = filtered_offers[filtered_offers["competitor_name"].isin(selected_competitors)]
            if selected_matches:
                filtered_offers = filtered_offers[filtered_offers["match_status"].isin(selected_matches)]
            if selected_availability:
                filtered_offers = filtered_offers[filtered_offers["availability"].isin(selected_availability)]
            if selected_competitors or selected_matches or selected_availability:
                filtered = filtered[filtered["ean"].isin(filtered_offers["ean"].unique())]
            if difference_filter == "Vytria más caro":
                filtered = filtered[filtered["difference_eur"] > 0]
            elif difference_filter == "Vytria más barato":
                filtered = filtered[filtered["difference_eur"] < 0]
            elif difference_filter == "Sin diferencia comparable":
                filtered = filtered[filtered["difference_eur"].isna()]
            columns = [
                "brand", "commercial_name", "ean", "own_price_eur", "competitor_min_eur",
                "difference_eur", "difference_pct", "competitor_count", "position", "market",
            ]
            st.dataframe(filtered[columns].rename(columns={
                "brand": "Marca", "commercial_name": "Producto", "ean": "EAN",
                "own_price_eur": "Vytria EUR", "competitor_min_eur": "Mínimo competencia EUR",
                "difference_eur": "Diferencia EUR", "difference_pct": "Diferencia %",
                "competitor_count": "Competidores comparables", "position": "Posición", "market": "Mercado",
            }), width="stretch", hide_index=True)
            st.download_button("Descargar comparación Excel",
                               _excel_bytes({"Comparación": filtered}),
                               "comparador_precios.xlsx",
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

            selected_ean = st.selectbox("Ficha e histórico por EAN", options=summary["ean"].tolist(),
                                        format_func=lambda ean: f"{ean} · {summary.loc[summary.ean == ean, 'commercial_name'].iloc[0]}")
            history = store.offer_history(selected_ean)
            search_history = store.search_history(selected_ean)
            own_history = store.own_price_history(selected_ean)
            if not own_history.empty:
                st.subheader("Histórico de precio Vytria")
                st.dataframe(own_history, width="stretch", hide_index=True)
            if history.empty:
                st.info("Este producto no tiene ofertas importadas.")
            else:
                st.dataframe(history, width="stretch", hide_index=True)
                chart = history[history["match_status"].eq("exact_match")].copy()
                chart["precio EUR"] = chart.apply(
                    lambda row: (row["promotional_price"] if pd.notna(row["promotional_price"])
                                 else row["price"]) * row["fx_to_eur"]
                    if pd.notna(row["fx_to_eur"]) else None, axis=1)
                chart = chart.dropna(subset=["precio EUR"])
                if not chart.empty:
                    pivot = chart.pivot_table(index="captured_at", columns="competitor_name",
                                              values="precio EUR", aggfunc="last")
                    st.line_chart(pivot)
            if not search_history.empty:
                st.subheader("Evidencia e historial de coincidencias")
                st.dataframe(search_history, width="stretch", hide_index=True)
        else:
            st.info("Importa primero el catálogo piloto.")

    with competitors_tab:
        if competitors.empty:
            st.info("Importa el libro piloto para registrar los 14 competidores.")
        else:
            st.caption("Mercado y moneda requieren confirmación. El método permanece manual hasta validar feeds, "
                       "condiciones y límites de consulta por dominio.")
            edited = st.data_editor(
                competitors[["id", "name", "domain", "market", "currency", "active", "query_method",
                             "last_checked_at", "connectivity"]],
                disabled=["id", "name", "domain", "query_method", "last_checked_at", "connectivity"],
                column_config={
                    "active": st.column_config.CheckboxColumn("Activo"),
                    "market": st.column_config.TextColumn("Mercado"),
                    "currency": st.column_config.TextColumn("Moneda ISO"),
                },
                width="stretch", hide_index=True, key="price_competitors_editor",
            )
            if st.button("Guardar configuración de competidores"):
                try:
                    store.save_competitors(edited[["id", "market", "currency", "active", "query_method"]])
                except (ValueError, sqlite3.Error) as exc:
                    st.error(f"No se pudo guardar la configuración: {exc}")
                else:
                    st.success("Configuración guardada.")
                    st.rerun()
            st.dataframe(summary[summary["competitor_count"] > 0][
                ["ean", "commercial_name", "own_price_eur", "competitor_min_eur", "difference_eur"]
            ] if not summary.empty else pd.DataFrame(), width="stretch", hide_index=True)
