from __future__ import annotations

import hashlib
from io import BytesIO
import logging
import json
import sqlite3
import threading
from zipfile import BadZipFile

import pandas as pd
import streamlit as st

from cloud_design import page_title
from competitor_requirements import requirement_for
import price_analytics as analytics
from price_comparator import (
    RETENTION_MONTHS_OPTIONS, PriceComparatorStorageError, PriceComparatorStore, exclusion_reason,
)
from price_connectors import CONNECTORS, ConnectorError
from price_scheduler import CompetitorScheduler, SchedulerConfigError
from price_pilot import MAX_PRODUCTS, SAMPLE_EANS, PilotError, PricePilot, pilot_scope
from price_discovery import PilotDiscovery
from price_web import (
    EXTRACTION_TEXT, LOCATION_TEXT, OPERABILITY_TEXT, WebPriceSearch, WebSearchError, validate_competitor_url,
)
from vytria_price_sync import ISSUE_LABELS, STATUS_LABELS, SyncInProgress, VytriaPriceSync


OWN_PRICE_COLUMNS = [
    "EAN", "Precio", "Precio promocional", "Moneda", "Mercado",
    "Tipo cambio EUR", "Fecha consulta", "Origen",
]
OFFER_COLUMNS = [
    "EAN", "Competidor", "Precio", "Precio promocional", "Precio anterior",
    "Coste envío", "Moneda", "Tipo cambio EUR", "Mercado", "URL",
    "Disponibilidad", "Estado coincidencia", "Evidencia", "Método", "Fecha consulta",
]
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
STATUS_TEXT = {
    "not_checked": "Sin comprobar", "no_authorized_connector": "Sin conector autorizado",
    "pending_authorization": "Pendiente de autorización", "blocked": "Bloqueado por el sitio",
    "manual_verified": "Solo importación manual verificada", "operational": "Operativo",
}
PAGE_STYLE = """<style>
[data-testid="stMetricValue"] { font-size: 1.5rem; line-height: 1.2; }
[data-testid="stMetricLabel"] p, [data-testid="stMetricValue"] div { white-space: normal; overflow-wrap: anywhere; line-height: 1.25; }
</style>"""
WEB_MAX_COMPETITORS = 3
WEB_MAX_PRODUCTS = 5
WEB_STATE_TEXT = {
    "exact": "Coincidencia exacta", "probable": "Probable: requiere revisión", "rejected": "Descartada",
    "not_found": "Sin ficha localizada", "blocked": "Bloqueado por el sitio", "not_allowed": "No consultable",
    "error": "Error de consulta",
}
WEB_LEVEL_TEXT = {"ean": "EAN exacto", "brand_model_color": "marca + modelo + color", "model_color": "modelo + color",
                  "normalized_variants": "variante normalizada de la referencia"}
WEB_STATUS_TEXT = {
    "implemented": "Implementado, sin verificar", "verified": "Extracción verificada; condiciones sin revisar",
    "operational": "Autorizado y con extracción verificada", "blocked": "Bloqueado por el sitio",
    "not_allowed": "robots.txt no lo permite",
    "pending_authorization": "Pendiente de autorización",
}
WEB_ACCESS_TEXT = {
    "unknown": "Sin comprobar", "robots_allowed": "robots.txt lo permite", "robots_restricted": "robots.txt lo restringe",
    "blocked": "Bloqueado (403/429/CAPTCHA)", "pending_authorization": "Pendiente de autorización",
}
WEB_STRATEGY_TEXT = {"index": "Fichas conocidas", "sitemap": "Sitemap público", "site_search": "Buscador interno",
                     "external": "Buscador externo (no disponible)"}
WEB_TAX_TEXT = {"unknown": "Sin comprobar (según cada ficha)", "included": "IVA incluido", "excluded": "IVA excluido"}
TABLE_LABELS = {
    "brand": "Marca", "model": "Modelo", "color": "Color", "ean": "EAN", "own_price_eur": "Precio Vytria",
    "best_competitor": "Mejor competidor", "competitor_min_eur": "Precio competidor",
    "difference_eur": "Diferencia €", "difference_pct": "% vs Vytria",
    "difference_vs_competitor_pct": "% vs mejor competidor",
    "last_checked_at": "Última comprobación", "position": "Posición", "own_price_status": "Estado precio Vytria",
}
DASHBOARD_WINDOWS = {label: days for label, days in analytics.WINDOWS.items() if days in {7, 30, 90}}


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


def _make_sync(store: PriceComparatorStore) -> VytriaPriceSync:
    return VytriaPriceSync(store)


def _make_scheduler(store: PriceComparatorStore) -> CompetitorScheduler:
    return CompetitorScheduler(store)


def _run_sync(sync: VytriaPriceSync) -> None:
    # Outcome and errors are persisted in the run log; nothing may escape the worker thread.
    try:
        sync.run(trigger="manual")
    except SyncInProgress:
        pass
    except Exception:  # noqa: BLE001
        pass


def _run_competitors(scheduler: CompetitorScheduler) -> None:
    try:
        scheduler.run_once(force=True, trigger="manual")
    except Exception:  # noqa: BLE001 - runs and errors are persisted by the scheduler
        pass


def _launch(target, *args) -> None:
    """Run long reads outside the Streamlit script thread so the page stays responsive."""
    threading.Thread(target=target, args=args, name="price-comparator-task", daemon=True).start()


def _run_pilot(pilot: PricePilot, resume: bool) -> None:
    try:
        pilot.run(resume=resume)
    except Exception as exc:
        logging.getLogger(__name__).exception("Prueba manual del piloto interrumpida")
        pilot.report_error(exc)


def _run_price_refresh(pilot: PricePilot, selection: list[tuple[str, str]]) -> None:
    try:
        pilot.run(selection=selection)
    except Exception as exc:
        logging.getLogger(__name__).exception("Actualización supervisada de precios interrumpida")
        pilot.report_error(exc)


def _run_discovery(pilot: PricePilot, resume: bool, selection=None) -> None:
    try:
        PilotDiscovery(pilot.store).run(resume=resume, selection=selection)
    except Exception as exc:
        logging.getLogger(__name__).exception("Localización supervisada del piloto interrumpida")
        pilot.report_error(exc)


@st.fragment(run_every=2)
def _watch_running_pilot(pilot: PricePilot) -> None:
    if pilot.is_running():
        st.info("Piloto supervisado en curso. Puede desactivarlo; no se inicia otra ejecución en paralelo.")
    else:
        st.rerun()


@st.fragment(run_every=2)
def _watch_running_sync(sync: VytriaPriceSync) -> None:
    if sync.is_running():
        st.info("Sincronización con Odoo en curso. Puede seguir usando el comparador; esta vista se actualiza sola.")
    else:
        st.rerun()


@st.fragment(run_every=2)
def _watch_running_competitors(scheduler: CompetitorScheduler) -> None:
    if scheduler.is_running():
        st.info("Actualizando la competencia. Puede seguir usando el comparador; esta vista se actualiza sola.")
    else:
        st.rerun()


def _render_vytria_prices(store: PriceComparatorStore) -> None:
    sync = _make_sync(store)
    st.subheader("Precio de venta Vytria")
    st.caption("Fuente: Odoo, tarifa 59 (Vytria España, EUR). El precio ya incluye IVA y descuentos de la tarifa; "
               "no se recalcula ni se le suma IVA. Integración de solo lectura. "
               "Contraste directo con PrestaShop: pendiente (no validado).")
    last = sync.last_run()
    prices = sync.prices()
    confirmed = int(prices["usable"].sum()) if not prices.empty else 0
    columns = st.columns(4)
    columns[0].metric("Estado de conexión", {"ok": "Correcta", "partial": "Correcta (con incidencias)",
                                              "error": "Error", "running": "En curso"}.get(
        last["status"], last["status"]) if last else "Sin sincronizar")
    good = sync.last_successful_run()
    columns[1].metric("Última sincronización correcta",
                      good["finished_at"][:16].replace("T", " ") if good else "—")
    columns[2].metric("Con precio confirmado", f"{confirmed}/{len(prices)}")
    columns[3].metric("Sin precio confirmado", len(prices) - confirmed)
    if last and last["status"] == "error":
        st.error(f"La última sincronización falló: {last['error']} Se conservan los últimos precios, "
                 "marcados como desactualizados cuando superan el plazo de vigencia.")

    st.caption("La sincronización manual se inicia desde Administración → Competidores y ejecuciones.")
    auto = st.toggle("Sincronización diaria automática", value=sync.auto_enabled, key="vytria_auto_sync",
                     help="Desactivada por defecto. Requiere además un disparador diario aprobado que ejecute "
                          "`python vytria_price_sync.py --scheduled`.")
    if auto != sync.auto_enabled:
        sync.set_auto_enabled(auto)
    if auto:
        st.info("Activada: la sincronización sólo se ejecuta cuando el disparador diario aprobado invoque el comando.")

    table = prices.assign(
        validation=prices["current_status"].map(lambda s: STATUS_LABELS.get(s, s) if isinstance(s, str) else "Sin precio"),
        iva=prices["price_includes_tax"].map({1: "Incluido", 0: "No incluido"}),
    )
    st.dataframe(table[["brand", "commercial_name", "ean", "price", "currency", "iva", "vat_rate", "source",
                        "synced_at", "validation", "validation_detail", "stale_reason"]].rename(columns={
        "brand": "Marca", "commercial_name": "Producto", "ean": "EAN", "price": "Precio actual",
        "currency": "Moneda", "iva": "IVA", "vat_rate": "IVA %", "source": "Origen",
        "synced_at": "Última sincronización", "validation": "Validación", "validation_detail": "Detalle", "stale_reason": "Incidencia",
    }), width="stretch", hide_index=True)
    st.caption("Precio promocional: no aplica; la tarifa 59 ya incorpora los descuentos. "
               "Disponibilidad: no proviene de esta fuente.")
    issues = sync.last_issues()
    if issues:
        with st.expander(f"Productos sin precio confirmado en la última sincronización ({len(issues)})"):
            for item in issues:
                st.write(f"{item['ean']}: {ISSUE_LABELS.get(item['code'], item['code'])} {item['detail']}")

    with st.expander("Contingencia administrativa: importar precios Vytria (.xlsx)"):
        st.warning("Uso excepcional. No es la operativa habitual. Un precio sincronizado desde Odoo "
                   "tiene prioridad sobre el importado manualmente para el mismo EAN.")
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
        st.download_button("Descargar plantilla de precios Vytria",
                           _excel_bytes({"Precios Vytria": pd.DataFrame(columns=OWN_PRICE_COLUMNS)}),
                           "plantilla_precios_vytria.xlsx", XLSX_MIME)


def _render_actions(store: PriceComparatorStore) -> None:
    """The two data refreshes are independent from the comparison, which only reads stored data."""
    sync, scheduler = _make_sync(store), _make_scheduler(store)
    overview = scheduler.overview()
    ready = overview[overview["state"] == "active"]
    left, right = st.columns(2)
    with left:
        running = sync.is_running()
        if st.button("Sincronizar precios Vytria desde Odoo", key="sync_vytria_prices", disabled=running,
                     help="Lee la tarifa 59 de Odoo (solo lectura)."):
            _launch(_run_sync, sync)
            st.rerun()
    with right:
        busy = scheduler.is_running()
        scoped = pilot_scope(store) is not None
        if st.button("Actualizar competencia", key="update_competitors", disabled=busy or ready.empty or scoped,
                     help="Consulta solo los competidores habilitados con acceso autorizado."):
            _launch(_run_competitors, scheduler)
            st.rerun()
        if scoped:
            st.caption("Piloto configurado: ejecute la prueba supervisada en Competidores → Piloto de comparación.")
        elif ready.empty:
            st.caption("Ningún competidor está habilitado todavía. Se activan en la pestaña Configuración.")
    if running:
        _watch_running_sync(sync)
    if busy:
        _watch_running_competitors(scheduler)


def _load(store: PriceComparatorStore) -> dict:
    expiry = store.price_expiry_days()
    window = st.session_state.get("dashboard_period", "30 días")
    snapshot_days = min(expiry, DASHBOARD_WINDOWS.get(window, 30))
    summary, _ = store.dashboard(snapshot_days)
    offers = store.current_offers(snapshot_days)
    threshold = float(store.get_setting("alert_threshold_pct"))
    pairs = analytics.comparable_pairs(summary, offers)
    return {
        "expiry": expiry, "snapshot_days": snapshot_days, "summary": summary, "offers": offers,
        "threshold": threshold, "pairs": pairs,
    }


def _pct(value: float | None, *, signed: bool = False) -> str:
    if value is None or pd.isna(value):
        return "Sin datos"
    return f"{value:+.1f} %".replace(".", ",") if signed else f"{value:.1f} %".replace(".", ",")


def _render_shared_filters(data: dict) -> None:
    summary, offers = data["summary"], data["offers"]
    competitor_names = sorted(offers["competitor_name"].dropna().unique()) if not offers.empty else []
    with st.expander("Filtros compartidos", expanded=False):
        brands = st.multiselect("Marca", sorted(value for value in summary["brand"].dropna().unique() if value),
                                key="dashboard_brand")
        selected_competitors = st.multiselect(
            "Competidor", competitor_names, key="dashboard_competitor",
            help="Vacío = todos los competidores con datos vigentes.")
        state = st.selectbox("Estado competitivo", analytics.STATE_OPTIONS, key="dashboard_state")
        window = st.selectbox(
            "Periodo", list(DASHBOARD_WINDOWS), index=1, key="dashboard_period",
            help="Limita los datos actuales al menor entre el periodo y la vigencia configurada; delimita también los gráficos.")
        query = st.text_input("Buscar EAN, modelo o referencia", key="dashboard_search",
                              placeholder="EAN, modelo o código de color")
    data["filtered_summary"] = analytics.dashboard_scope(
        summary, offers, brands=brands, competitors=selected_competitors, query=query,
        states=() if state == analytics.STATE_ALL else [state])
    data["selected_competitors"] = selected_competitors
    data["dashboard_window"] = window


def _render_summary(store: PriceComparatorStore, data: dict) -> None:
    summary = data["filtered_summary"]
    if summary.empty:
        st.info("No hay productos para los filtros seleccionados. Ajuste los filtros o importe el catálogo desde Administración.")
        return
    st.header("Cómo estamos")
    scoped = summary
    selected_competitors = data["selected_competitors"]
    window = data["dashboard_window"]
    indicators = analytics.kpis(scoped, 0)
    st.caption(
        "Se comparan precios verificados y vigentes del mismo mercado, con IVA incluido. "
        f"El resumen usa datos de hasta {data['snapshot_days']} días.")
    cols = st.columns(4)
    cols[0].metric("Productos comparados", f"{indicators['compared']} / {len(scoped)}")
    cols[1].metric("Igual o más barato", _pct(indicators["best_price_pct"]),
                   help=f"{int((scoped['difference_eur'] <= 0).sum()) if not scoped.empty else 0} de "
                        f"{indicators['compared']} productos comparados; el empate cuenta como competitivo.")
    cols[2].metric("Más caro", _pct(indicators["vytria_more_expensive_pct"]),
                   help=f"{indicators['vytria_more_expensive']} de {indicators['compared']} productos comparados.")
    cols[3].metric(
        "Diferencia media vs. mejor", _pct(indicators["mean_gap_vs_best_pct"], signed=True),
        help="Media por producto de (precio Vytria − mejor competidor) / mejor competidor. "
             "Un resultado positivo indica que Vytria es más cara.")

    no_own = int(scoped["own_price_eur"].isna().sum())
    no_competitor = int(((scoped["own_price_eur"].notna()) & (scoped["competitor_count"] == 0)).sum())
    if not indicators["compared"]:
        st.info("Datos insuficientes para medir competitividad. "
                f"Sin precio Vytria: {no_own}; sin oferta comparable: {no_competitor}.")
    elif indicators["no_information"]:
        st.caption(f"{indicators['no_information']} productos quedan fuera del porcentaje: "
                   f"{no_own} sin precio Vytria y {no_competitor} sin competencia comparable.")

    st.header("Cómo evolucionamos")
    st.plotly_chart(analytics.positioning_distribution_figure(scoped), width="stretch",
                    key="dashboard_positioning_chart")
    history = analytics.competitiveness_series(
        store, DASHBOARD_WINDOWS[window], expiry_days=data["expiry"], eans=scoped["ean"].tolist(),
        competitors=selected_competitors or None)
    if history.empty:
        st.info("Todavía no hay observaciones históricas recientes suficientes para calcular una evolución.")
    else:
        st.plotly_chart(analytics.competitiveness_figure(history), width="stretch",
                        key="dashboard_competitiveness_chart")
        if history["day"].nunique() < 2:
            st.info("Solo hay observaciones comparables en un día de este periodo; no se puede inferir una tendencia.")
        st.caption("Cada punto usa solo precios validados y comprobados dentro del plazo vigente. "
                   "Los días sin una muestra comparable no se rellenan; el número de productos por punto "
                   "aparece al pasar el cursor.")

    st.header("Dónde debemos actuar")
    attention = analytics.attention_table(scoped)
    if attention.empty:
        st.success("No hay productos comparables donde Vytria sea más cara en el alcance seleccionado.")
    else:
        display = attention[["brand", "model", "color", "ean", "own_price_eur", "competitor_min_eur",
                            "difference_eur", "difference_vs_competitor_pct", "best_competitor"]].rename(
            columns={**TABLE_LABELS, "competitor_min_eur": "Mejor precio competidor",
                     "difference_vs_competitor_pct": "Diferencia %"})
        selected_attention = st.dataframe(display, width="stretch", hide_index=True, key="dashboard_attention_table",
                                          on_select="rerun", selection_mode="single-row", column_config={
            "Precio Vytria": st.column_config.NumberColumn("Precio Vytria", format="%.2f €"),
            "Mejor precio competidor": st.column_config.NumberColumn("Mejor precio competidor", format="%.2f €"),
            "Diferencia €": st.column_config.NumberColumn("Diferencia €", format="%.2f €"),
            "Diferencia %": st.column_config.NumberColumn("Diferencia %", format="%.1f %%"),
        })
        selected_rows = selected_attention.selection.rows
        if selected_rows and selected_rows[0] < len(attention):
            st.session_state["dashboard_product"] = attention.iloc[selected_rows[0]]["ean"]

    st.header("Consultar una gafa")
    if scoped.empty:
        st.info("No hay productos para los filtros seleccionados.")
        return
    labels = {row["ean"]: f"{row['brand']} · {row['model']} · {row['color']} · {row['ean']}"
              for row in scoped.to_dict("records")}
    selected_ean = st.selectbox("Producto", scoped["ean"].tolist(), key="dashboard_product",
                                format_func=lambda ean: labels.get(ean, ean))
    _render_dashboard_product(store, data, scoped, selected_ean, DASHBOARD_WINDOWS[window],
                              selected_competitors)


def _render_products(store: PriceComparatorStore, data: dict) -> None:
    summary = data["filtered_summary"]
    st.header("Productos")
    if summary.empty:
        st.info("No hay productos para los filtros seleccionados.")
        return
    table = analytics.commercial_table(summary)
    columns = ["brand", "model", "color", "ean", "own_price_eur", "best_competitor",
               "competitor_min_eur", "difference_vs_competitor_pct", "position"]
    display = table[columns].rename(columns=TABLE_LABELS)
    selected = st.dataframe(
        display, width="stretch", hide_index=True, key="products_table",
        on_select="rerun", selection_mode="single-row", column_config={
            "Precio Vytria": st.column_config.NumberColumn("Precio Vytria", format="%.2f €"),
            "Precio competidor": st.column_config.NumberColumn("Precio competidor", format="%.2f €"),
            "% vs mejor competidor": st.column_config.NumberColumn("% vs mejor competidor", format="%.1f %%"),
        })
    st.download_button("Descargar productos Excel", _excel_bytes({"Productos": display}),
                       "comparador_precios.xlsx", XLSX_MIME, key="products_export")
    if selected.selection.rows and selected.selection.rows[0] < len(summary):
        st.session_state["products_selected_ean"] = summary.iloc[selected.selection.rows[0]]["ean"]
    options = summary["ean"].tolist()
    current = st.session_state.get("products_selected_ean", st.session_state.get("dashboard_product"))
    if current not in options:
        current = options[0]
    st.selectbox("Producto", options, index=options.index(current), key="products_selected_ean",
                 format_func=lambda value: (
                     f"{summary.loc[summary.ean == value, 'brand'].iloc[0]} · "
                     f"{summary.loc[summary.ean == value, 'model'].iloc[0]} · "
                     f"{summary.loc[summary.ean == value, 'color'].iloc[0]} · {value}"))
    ean = st.session_state["products_selected_ean"]
    _render_dashboard_product(store, data, summary, ean, DASHBOARD_WINDOWS[data["dashboard_window"]],
                              data["selected_competitors"], key_prefix="products")


def _render_dashboard_product(store: PriceComparatorStore, data: dict, summary: pd.DataFrame, ean: str,
                              days: int, selected_competitors, key_prefix: str = "dashboard") -> None:
    row = summary[summary["ean"] == ean].iloc[0]
    own = row["own_price_eur"]
    competitor = row["competitor_min_eur"]
    delta = row["difference_eur"]
    if pd.isna(own):
        st.info("No hay un precio validado de Vytria; no se puede determinar la posición competitiva.")
    elif pd.isna(competitor):
        st.info("No hay ofertas verificadas y comparables para este producto.")
    else:
        state = "igual o más barato" if delta <= 0 else "más caro"
        c1, c2, c3 = st.columns(3)
        c1.metric("Precio Vytria", f"{own:.2f} €")
        c2.metric(f"Mejor precio · {row['best_competitor']}", f"{competitor:.2f} €")
        c3.metric("Diferencia vs. mejor",
                  f"{delta:+.2f} € · {_pct(row['difference_vs_competitor_pct'], signed=True)}")
        st.write(f"**Posición:** Vytria está {state}. "
                 f"Posición {int(row['position_rank'])} entre Vytria y {int(row['competitor_count'])} competidores; "
                 "los empates conservan la misma posición.")

    product_offers = store.current_offers(max(data["expiry"] * 2, 1))
    product_offers = product_offers[product_offers["ean"] == ean].copy()
    if selected_competitors:
        product_offers = product_offers[product_offers["competitor_name"].isin(selected_competitors)]
    if not product_offers.empty:
        latest = product_offers.sort_values(["captured_at", "id"]).groupby(
            ["competitor_id", "market"]).tail(1).copy()
        own_market = row["market"]

        def comparison_status(offer: pd.Series) -> str:
            reason = exclusion_reason(offer.to_dict())
            if reason:
                return reason
            if offer["market"] != own_market:
                return "Mercado distinto"
            freshness = analytics.price_state(offer["checked_at"], data["expiry"])
            return "Comparable" if freshness == analytics.STATE_CURRENT else freshness

        latest["comparison_status"] = latest.apply(comparison_status, axis=1)
        latest["price_eur"] = (latest["promotional_price"].where(
            latest["promotional_price"].notna(), latest["price"]) * latest["fx_to_eur"])
        st.dataframe(latest[["competitor_name", "price_eur", "market", "availability", "comparison_status",
                             "checked_at", "url"]].rename(columns={
            "competitor_name": "Competidor", "price_eur": "Precio EUR", "market": "Mercado",
            "availability": "Disponibilidad", "comparison_status": "Uso en comparación",
            "checked_at": "Última comprobación", "url": "URL"}), width="stretch", hide_index=True,
            column_config={"Precio EUR": st.column_config.NumberColumn("Precio EUR", format="%.2f €"),
                           "URL": st.column_config.LinkColumn("URL")})
        history_offers = store.offer_history(ean)
        if not history_offers.empty:
            history_offers["price_eur"] = (history_offers["promotional_price"].where(
                history_offers["promotional_price"].notna(), history_offers["price"])
                * history_offers["fx_to_eur"])
            with st.expander("Histórico completo de precios"):
                st.dataframe(history_offers[["competitor_name", "price_eur", "currency", "availability",
                                             "market", "match_status", "captured_at", "url"]].rename(columns={
                    "competitor_name": "Competidor", "price_eur": "Precio EUR", "currency": "Moneda",
                    "availability": "Disponibilidad", "market": "Mercado", "match_status": "Coincidencia",
                    "captured_at": "Fecha de observación", "url": "URL",
                }), hide_index=True, width="stretch")
    else:
        st.info("No hay observaciones recientes para los competidores seleccionados.")

    history = analytics.price_series(store, ean, days, selected_competitors or None, market=row["market"])
    if not history.empty:
        st.plotly_chart(analytics.price_figure(history, "Evolución de precios verificados"),
                        width="stretch", key=f"{key_prefix}_price_history_{ean}")
        st.caption("Precios históricos por fecha de observación; los periodos sin datos no se interpolan.")
    _render_product_refresh(store, ean, key_prefix)


def _render_product_refresh(store: PriceComparatorStore, ean: str, key_prefix: str) -> None:
    pilot = PricePilot(store)
    st.subheader("Actualizar precios")
    try:
        scope = pilot_scope(store)
        if scope is None:
            st.info("Configure un alcance supervisado en Administración para consultar precios.")
            return
        if ean not in scope["eans"]:
            st.info("Este producto no está dentro del alcance autorizado del piloto. "
                    "La consulta de precios solo está disponible para productos seleccionados en Administración.")
            return
        plan = pilot.price_refresh_plan(ean)
    except PilotError as exc:
        st.error(f"No se pudo preparar la actualización: {exc}")
        return
    st.dataframe(plan[["competitor", "status", "url", "reason"]].rename(columns={
        "competitor": "Competidor", "status": "Estado", "url": "Ficha confirmada", "reason": "Motivo",
    }), width="stretch", hide_index=True,
        column_config={"Ficha confirmada": st.column_config.LinkColumn("Ficha confirmada")})
    available = plan[plan["status"] == "Consultable"]
    with store._connect() as connection:
        previous_run = connection.execute(
            "SELECT MAX(id) FROM pilot_runs WHERE operation='prices'").fetchone()[0] or 0
    if available.empty:
        st.info("No hay fichas confirmadas y autorizadas para consultar. Puede localizar una ficha cuando "
                "la combinación esté pendiente y el competidor tenga autorización.")
    else:
        options = dict(zip(available["competitor_id"], available["competitor"]))
        selected = st.multiselect("Tiendas que se van a consultar", list(options),
                                  default=list(options), format_func=options.get,
                                  key=f"{key_prefix}_refresh_competitors_{ean}")
        approved = st.checkbox("He revisado las fichas y autorizo esta consulta supervisada",
                               key=f"{key_prefix}_refresh_approved_{ean}")
        busy = pilot.is_running()
        if st.button("Actualizar precios", type="primary", key=f"{key_prefix}_refresh_prices_{ean}",
                     disabled=busy or not selected or not approved):
            try:
                pilot.enable()
                st.session_state[f"{key_prefix}_refresh_run_before_{ean}"] = previous_run
                _launch(_run_price_refresh, pilot, [(cid, ean) for cid in selected])
            except (PilotError, ValueError, sqlite3.Error) as exc:
                st.error(f"No se pudo iniciar la actualización: {exc}")
            else:
                st.rerun()
        if busy:
            _watch_running_pilot(pilot)

    discovery = PilotDiscovery(store)
    coverage = discovery.coverage()
    if coverage.empty:
        st.info("Para localizar fichas, configure el alcance supervisado en Administración.")
        return
    missing = coverage[(coverage["ean"] == ean) & (coverage["state"] == "pending")]
    if not missing.empty:
        names = dict(zip(missing["competitor_id"], missing["competitor"]))
        selected_missing = st.multiselect(
            "Competidores sin ficha: localizar de forma supervisada",
            missing["competitor_id"].tolist(), format_func=names.get,
            key=f"{key_prefix}_locate_competitors_{ean}")
        pairs = [(cid, ean) for cid in selected_missing]
        preview = None
        try:
            if pairs:
                preview = discovery.preview(pairs)
        except PilotError as exc:
            st.error(f"No se pudo preparar la localización: {exc}")
        if preview and preview["unauthorized"]:
            st.warning("No consultable: " + "; ".join(preview["unauthorized"]))
        approved = st.checkbox("He revisado el alcance y autorizo buscar las fichas seleccionadas",
                               key=f"{key_prefix}_locate_approved_{ean}")
        busy = pilot.is_running()
        if st.button("Localizar ficha", key=f"{key_prefix}_locate_product_{ean}",
                     disabled=busy or not approved or not preview or not preview["effective"]):
            try:
                pilot.enable()
                _launch(_run_discovery, pilot, False, pairs)
            except (PilotError, ValueError, sqlite3.Error) as exc:
                st.error(f"No se pudo iniciar la localización: {exc}")
            else:
                st.rerun()
        if busy:
            _watch_running_pilot(pilot)

    with store._connect() as connection:
        latest_run = connection.execute("""SELECT id,status,finished_at,error FROM pilot_runs
            WHERE operation='prices' ORDER BY id DESC LIMIT 1""").fetchone()
    if latest_run and latest_run["status"] in ("done", "interrupted", "cancelled"):
        results, _ = pilot.results(int(latest_run["id"]))
        product_results = results[results["ean"] == ean] if not results.empty else results
        if not product_results.empty:
            baseline = st.session_state.get(f"{key_prefix}_refresh_run_before_{ean}")
            is_new_result = baseline is not None and int(latest_run["id"]) > baseline
            updated = int(product_results["priced"].sum())
            total = len(product_results)
            if latest_run["status"] != "done":
                message = "La actualización se interrumpió. Se conserva el último precio comprobado."
                if latest_run["error"]:
                    st.error(f"{message} {latest_run['error']}")
                else:
                    st.warning(message)
            elif is_new_result and updated == total:
                st.success("Precios actualizados.")
            elif is_new_result and updated:
                st.warning(f"Actualizados {updated} de {total} competidores.")
            elif is_new_result:
                st.warning("No se pudo actualizar ningún precio; se conservan las últimas observaciones válidas.")
            else:
                st.caption(f"Últimos resultados guardados: {latest_run['finished_at']} UTC. "
                           "Esta vista no ha realizado una consulta nueva.")
            if is_new_result:
                st.session_state.pop(f"{key_prefix}_refresh_run_before_{ean}", None)
            st.dataframe(product_results[["competitor_id", "outcome", "detail", "checked_at"]].rename(columns={
                "competitor_id": "Competidor", "outcome": "Resultado", "detail": "Detalle",
                "checked_at": "Última consulta",
            }), hide_index=True, width="stretch")
            if product_results["Disponibilidad"].eq("unknown").any():
                st.info("Disponibilidad pendiente de verificar.")


def _render_web_finder(store: PriceComparatorStore) -> None:
    st.subheader("Buscar precios en la competencia")
    st.caption("Busque por EAN, marca + modelo + color, modelo + color o referencia (por ejemplo «BOTTEGA VENETA "
               "BV1274S 005» o «BV1274S 005»). «Almacenados» no consulta ninguna web. «Consultar webs» lee fichas "
               "públicas conocidas o localizadas con métodos permitidos, con pausas y límites; se detiene ante "
               "cualquier 403/429 o CAPTCHA.")
    search = _make_web_search(store)
    configs = search.configs()
    c1, c2 = st.columns([2, 2])
    text = c1.text_input("EAN o referencia", key="web_text", placeholder="8056597875349 · PR 27ZS 16K08Z")
    names = c2.multiselect("Competidores", list(configs["name"]), key="web_competitors",
                           help="Vacío = todos para la búsqueda almacenada. La consulta web exige elegir 1 a "
                                f"{WEB_MAX_COMPETITORS}.")
    f1, f2, f3 = st.columns(3)
    only_stock = f1.checkbox("Solo en stock", key="web_only_stock")
    show_all = f2.checkbox("Mostrar no comparables", value=True, key="web_show_all")
    mode = f3.radio("Modo", ["Almacenados", "Consultar webs"], horizontal=True, key="web_mode")
    ids = [r["competitor_id"] for r in configs.to_dict("records") if r["name"] in names]
    if st.button("Buscar", key="web_search_button", type="primary", disabled=not text.strip()):
        _run_web_search(search, text, ids, mode, only_stock, show_all)
    active = st.session_state.get("web_active")
    if active:
        _render_web_results(search, active)


def _run_web_search(search: WebPriceSearch, text: str, ids: list[str], mode: str, only_stock: bool,
                    show_all: bool) -> None:
    st.session_state.pop("web_active", None)
    try:
        level, products = search.resolve(text)
        if not products:
            st.warning("Sin coincidencias en el catálogo. Pruebe con el EAN o con modelo + color.")
            return
        outcomes: list[dict] = []
        if mode == "Consultar webs":
            if not 1 <= len(ids) <= WEB_MAX_COMPETITORS:
                st.error(f"Elija entre 1 y {WEB_MAX_COMPETITORS} competidores para consultar sus webs.")
                return
            if len(products) > WEB_MAX_PRODUCTS:
                st.error(f"La referencia coincide con {len(products)} productos; concrete (máximo {WEB_MAX_PRODUCTS}).")
                return
            with st.spinner("Consultando fichas con pausas entre peticiones…"):
                outcomes = [{
                    "EAN": o.ean, "Competidor": o.competitor, "Resultado": WEB_STATE_TEXT.get(o.state, o.state),
                    "Precio": o.page.price if o.priced else None, "Stock": o.page.availability if o.page else "",
                    "IVA incluido": {True: "Sí", False: "No", None: "Sin confirmar"}[o.page.includes_tax]
                    if o.page else "", "Ficha": o.url, "Detalle": o.detail} for o in search.run(
                        [p["ean"] for p in products], ids)]
    except (WebSearchError, ValueError) as exc:
        st.error(str(exc))
        return
    st.session_state["web_active"] = {"text": text, "ids": ids, "level": level, "only_stock": only_stock,
                                      "show_all": show_all, "outcomes": outcomes, "products": len(products)}


def _render_web_results(search: WebPriceSearch, active: dict) -> None:
    ids = active["ids"]
    try:
        _, frame = search.stored_results(active["text"], only_in_stock=active["only_stock"],
                                         include_non_comparable=active["show_all"], competitor_ids=ids)
        _, unfiltered = search.stored_results(active["text"], competitor_ids=ids)
    except (WebSearchError, ValueError) as exc:
        st.error(str(exc))
        return
    if active["outcomes"]:
        st.dataframe(pd.DataFrame(active["outcomes"]), width="stretch", hide_index=True,
                     column_config={"Ficha": st.column_config.LinkColumn("Ficha")})
    st.caption(f"Identificación: {WEB_LEVEL_TEXT.get(active['level'], active['level'])} · "
               f"{active['products']} producto(s).")
    _render_registered_pages(search, unfiltered, ids)
    if frame.empty:
        st.info("No hay resultados que mostrar con estos filtros.")
    else:
        st.dataframe(frame, width="stretch", hide_index=True, column_config={
            "Precio": st.column_config.NumberColumn("Precio", format="%.2f €"),
            "Diferencia €": st.column_config.NumberColumn("Diferencia €", format="%.2f"),
            "Diferencia %": st.column_config.NumberColumn("Diferencia %", format="%.1f %%"),
            "Ficha": st.column_config.LinkColumn("Ficha")})
        st.caption("La diferencia se calcula sobre el precio de Vytria; solo «Precio almacenado» entra en las "
                   "comparativas, indicadores y gráficos.")
    _render_url_requests(search, unfiltered, ids)


def _render_registered_pages(search: WebPriceSearch, unfiltered: pd.DataFrame, ids: list[str]) -> None:
    """Product found + known pages, kept apart from priced offers: a registered URL is not a comparable price."""
    if unfiltered.empty:
        return
    for product in unfiltered.drop_duplicates("EAN").to_dict("records"):
        st.success(f"Producto encontrado: {product['Modelo']} {product['Color']} · EAN {product['EAN']}".replace("  ", " "))
    pages = search.registered_pages(list(unfiltered["EAN"].unique()))
    if not pages.empty and ids:
        pages = pages[pages["competitor_id"].isin(ids)]
    comparable = int((unfiltered["Estado"] == "Precio almacenado").sum())
    m1, m2, m3 = st.columns(3)
    m1.metric("URLs conocidas", len(pages))
    m2.metric("Competidores con ficha", int(pages["Competidor"].nunique()) if not pages.empty else 0)
    m3.metric("Ofertas comparables", comparable)
    if pages.empty:
        st.info("Todavía no hay fichas registradas para este producto. Facilite la URL o impórtelas desde la "
                "pestaña «Competidores».")
        return
    st.dataframe(pages[["Competidor", "EAN", "Estado", "Comprobación de precio", "Precio", "Stock", "Ficha"]]
                 .rename(columns={"Precio": "Último precio leído"}), width="stretch", hide_index=True,
                 column_config={"Ficha": st.column_config.LinkColumn("Ficha"),
                                "Último precio leído": st.column_config.NumberColumn("Último precio leído",
                                                                                     format="%.2f €")})
    if comparable == 0:
        st.caption("Hay fichas registradas pero falta la comprobación de precio: una URL registrada no es una "
                   "oferta ni cuenta como competidor con precio comparable.")


def _render_url_requests(search: WebPriceSearch, frame: pd.DataFrame, ids: list[str]) -> None:
    """Ask for the exact page, once, where nothing was found and no page is registered yet."""
    if frame.empty or not ids:
        if not ids:
            st.caption("Elija competidores para poder facilitar la URL de una ficha que no se localice.")
        return
    names = {r["name"]: r["competitor_id"] for r in search.configs().to_dict("records")}
    missing = [(row["EAN"], names[row["Competidor"]], row["Competidor"], row["Modelo"], row["Color"])
               for row in frame.to_dict("records")
               if row["Precio"] is None and row["Competidor"] in names
               and search.needs_url(row["EAN"], names[row["Competidor"]])]
    pending = [(r["EAN"], names[r["Competidor"]], r["Competidor"], r["Modelo"], r["Color"])
               for r in frame.to_dict("records") if r["Competidor"] in names
               and st.session_state.get(f"web_sub_{r['EAN']}_{names[r['Competidor']]}") is not None
               and (r["EAN"], names[r["Competidor"]], r["Competidor"], r["Modelo"], r["Color"]) not in missing]
    for ean, cid, name, model, color in [*missing, *pending][:WEB_MAX_PRODUCTS * WEB_MAX_COMPETITORS]:
        _render_url_prompt(search, ean, cid, name, f"{model} {color}".strip())
    registered = [(r["EAN"], names[r["Competidor"]], r["Competidor"]) for r in frame.to_dict("records")
                  if r["Competidor"] in names and not search.needs_url(r["EAN"], names[r["Competidor"]])]
    if registered:
        _render_url_replacement(search, registered)


def _render_url_prompt(search: WebPriceSearch, ean: str, cid: str, name: str, label: str) -> None:
    box = st.container(border=True)
    box.markdown(f"**No hemos localizado este producto en {name}. ¿Puedes facilitarnos la URL?**  \n"
                 f"{label} · EAN {ean}")
    state = search.url_state(ean, cid)
    if state["key"] in {"pending", "blocked", "relocation", "wrong"}:
        box.caption(f"{state['text']}: {state['url']}" + (f" ({state['note']})" if state["note"] else ""))
    sub_key = f"web_sub_{ean}_{cid}"
    sub = st.session_state.get(sub_key)
    if sub is not None and sub.state == "needs_confirmation":
        box.warning(sub.message)
        c1, c2 = box.columns(2)
        if c1.button("Confirmar: guardar pendiente de revisión", key=f"web_confirm_{ean}_{cid}"):
            st.session_state[sub_key] = search.save_for_review(sub)
            st.rerun()
        if c2.button("Descartar", key=f"web_discard_{ean}_{cid}"):
            st.session_state.pop(sub_key, None)
            st.rerun()
        return
    if sub is not None:
        _show_submission(box, sub)
        if sub.state in {"verified", "saved_for_review"} or sub.action in {"review", "wait", "authorization"}:
            return
    url = box.text_input("URL de la ficha (HTTPS, del sitio del competidor)", key=f"web_url_{ean}_{cid}")
    if box.button("Verificar y guardar", key=f"web_submit_{ean}_{cid}", disabled=not url.strip()):
        with st.spinner("Comprobando condiciones de acceso y verificando la ficha…"):
            st.session_state[sub_key] = search.submit_url(ean, cid, url)
        st.rerun()


def _render_url_replacement(search: WebPriceSearch, registered: list[tuple[str, str, str]]) -> None:
    with st.expander("Sustituir una ficha registrada (cambió o dejó de existir)"):
        options = {f"{name} · EAN {ean}": (ean, cid) for ean, cid, name in registered}
        choice = st.selectbox("Producto y competidor", list(options), key="web_replace_choice")
        ean, cid = options[choice]
        current = search.url_state(ean, cid)
        st.caption(f"{current['text']}: {current['url']}")
        url = st.text_input("Nueva URL", key="web_replace_url")
        if st.button("Verificar y sustituir", key="web_replace_button", disabled=not url.strip()):
            sub = search.submit_url(ean, cid, url, replace=True)
            if sub.state == "needs_confirmation":
                st.session_state[f"web_sub_{ean}_{cid}"] = sub
                st.rerun()
            _show_submission(st, sub)


def _show_submission(target, sub) -> None:
    """A verified page whose offer is not comparable (VAT, market, price) is shown as a warning, not as success."""
    if sub.state == "verified" and not sub.reason:
        target.success(sub.message)
    elif sub.state == "saved_for_review":
        target.info(sub.message)
    else:
        target.warning(sub.message)


def _make_web_search(store: PriceComparatorStore) -> WebPriceSearch:
    return WebPriceSearch(store)


def _render_web_section(search: WebPriceSearch, row: dict) -> None:
    """Web-page configuration of one competitor. Usable with the scheduler disabled; no feed is required."""
    cid = row["competitor_id"]
    status = WEB_STATUS_TEXT.get(row["connector_status"], row["connector_status"])
    st.markdown(f"**Dominio:** {row['domain']} · [{row['site_url']}]({row['site_url']}) · **Estado del "
                f"conector web:** {status}")
    st.markdown("**Estrategia de localización:** " + (", ".join(
        WEB_STRATEGY_TEXT.get(s, s) for s in row["locate_strategy"].split(",")) or "—"))
    market = f"{row['market'] or 'sin confirmar'} / {row['currency'] or 'sin confirmar'}"
    st.markdown(f"**Mercado / moneda verificados:** {market} · **IVA de los precios:** "
                f"{WEB_TAX_TEXT.get(row['tax_policy'], row['tax_policy'])}")
    problems = [WEB_ACCESS_TEXT.get(row["access_status"], row["access_status"])]
    if row["last_error"]:
        problems.append(f"último error: {row['last_error']}")
    if row["error_count"]:
        problems.append(f"{int(row['error_count'])} error(es)")
    if row["blocked_at"]:
        problems.append(f"bloqueado el {row['blocked_at']}: no se reintenta")
    st.markdown("**Acceso:** " + " · ".join(problems))
    st.caption(f"{row['robots_summary'] or 'robots.txt sin comprobar'} · Última consulta: "
               f"{row['last_query_at'] or '—'} · Ofertas web confirmadas: {int(row['confirmed'])}")
    pages = search.registered_pages(competitor_id=cid)
    if pages.empty:
        st.info("Sin URLs conocidas para este competidor. Añada una abajo; no hace falta configurar un feed.")
    else:
        st.markdown(f"**URLs conocidas:** {len(pages)} · **Productos asociados:** {pages['EAN'].nunique()} · "
                    f"**Con precio comprobado:** {int((pages['Comprobación de precio'] == 'Realizada').sum())}")
        st.dataframe(pages[["EAN", "Modelo", "Color", "Estado", "Ficha", "Precio", "Stock", "Última consulta",
                            "Nota"]].rename(columns={"Precio": "Último precio leído"}), width="stretch",
                     hide_index=True, column_config={
                         "Ficha": st.column_config.LinkColumn("Ficha"),
                         "Último precio leído": st.column_config.NumberColumn("Último precio leído", format="%.2f")})
        st.caption("Una URL registrada no es una oferta ni cuenta como precio comparable.")
    try:
        _render_web_settings(search, row)
    except (WebSearchError, ValueError) as exc:
        st.error(str(exc))


def _render_web_settings(search: WebPriceSearch, row: dict) -> None:
    cid, key = row["competitor_id"], f"web_{row['competitor_id']}"
    st.markdown("**Control de acceso**")
    if st.button("Comprobar robots.txt", key=f"{key}_robots", disabled=row["access_status"] == "blocked"):
        result = search.check_access(cid)
        label = {"robots_allowed": "permitido", "robots_restricted": "restringido", "unknown": "no disponible",
                 "blocked": "bloqueado"}[result["access_status"]]
        st.session_state[f"{key}_flash"] = f"Comprobación de acceso realizada. Resultado: {label}. {result['summary']}"
        st.rerun()
    if flash := st.session_state.pop(f"{key}_flash", ""):
        st.info(flash)
    st.caption("La comprobación solo lee robots.txt; no consulta productos ni autoriza automatizaciones.")
    if not row["terms_reviewed"] or not row["terms_note"].strip() or row["access_status"] != "robots_allowed":
        st.info("Falta registrar la revisión de condiciones de uso o completar la comprobación de acceso.")
    st.markdown("**Localización de productos**")
    modes = {"auto": "Automático", "ean": "Buscar por EAN", "model_color": "Buscar por modelo + código de color",
             "known": "Solo URLs conocidas"}
    mode = st.selectbox("Método de localización de productos", list(modes), index=list(modes).index(row["search_mode"]),
                        format_func=modes.get, key=f"{key}_search_mode")
    preferred = st.selectbox("Método preferente (automático)", ["ean", "model_color"],
                             index=["ean", "model_color"].index(row["search_preferred"]),
                             format_func=modes.get, key=f"{key}_search_preferred")
    alternatives = st.multiselect("Métodos alternativos autorizados (automático)", ["ean", "model_color", "sitemap"],
                                   default=[v for v in row["search_alternatives"].split(",") if v],
                                   format_func=lambda v: modes.get(v, "Sitemap público"), key=f"{key}_alternatives")
    c1, c2 = st.columns(2)
    strategies = c1.multiselect("Métodos de localización", list(WEB_STRATEGY_TEXT),
                                default=[s for s in row["locate_strategy"].split(",") if s in WEB_STRATEGY_TEXT],
                                format_func=WEB_STRATEGY_TEXT.get, key=f"{key}_strategy")
    template = c2.text_input("Plantilla HTTPS ({ean}, {model}, {color} o {query})",
                             value=row["search_url_template"], key=f"{key}_template")
    search_verified = st.checkbox("Mecanismo público de búsqueda GET verificado y autorizado",
                                   value=bool(row["search_verified"]), key=f"{key}_search_verified")
    search_evidence = st.text_input("Evidencia del mecanismo: URL, quién/cuándo y autorización",
                                    value=row["search_evidence"], key=f"{key}_search_evidence")
    st.caption("No se implementan AJAX, endpoints internos ni navegador automático. Una sugerencia no confirma "
               "el EAN: la ficha debe publicarlo. Use {query} si permite ambos métodos.")
    s1, s2 = st.columns(2)
    price_selector = s1.text_input("Selector de precio (opcional)", value=row["price_selector"],
                                   key=f"{key}_price_sel")
    stock_selector = s2.text_input("Selector de disponibilidad (opcional)",
                                   value=row["availability_selector"], key=f"{key}_stock_sel")
    st.markdown("**Condiciones comerciales**")
    m1, m2 = st.columns(2)
    markets = ["", "ES", "other"]
    selected_market = m1.selectbox("Mercado de venta", markets,
                                   index=0 if not row["sales_market"] else 1 if row["sales_market"] == "ES" else 2,
                                   format_func=lambda v: {"": "Sin comprobar", "ES": "España (ES)",
                                                          "other": "Otro mercado"}.get(v, v), key=f"{key}_market")
    other_market = m2.text_input("Otro mercado (ISO-2)", value=row["sales_market"] if row["sales_market"] != "ES" else "",
                                 key=f"{key}_other_market")
    market_verified = st.selectbox("Estado de verificación del mercado", [False, True],
                                    index=int(bool(row["market_verified"])),
                                    format_func=lambda v: "Confirmado manualmente" if v else "Pendiente",
                                    key=f"{key}_market_verified")
    market_url = st.text_input("URL de condiciones comerciales o entrega", value=row["market_evidence_url"],
                               key=f"{key}_market_url")
    market_note = st.text_input("Quién y cuándo verificó el mercado", value=row["market_note"],
                                key=f"{key}_market_note")
    st.caption("No se infiere el mercado del dominio, ruta o idioma. La configuración verificada solo se usa "
               "si falta el mercado comercial explícito de la ficha; contradicciones requieren revisión. "
               f"Última confirmación: {row['market_verified_at'] or 'pendiente'}.")
    policies = ["unknown", "included", "excluded"]
    tax = st.selectbox("IVA de los precios publicados", policies, index=policies.index(row["tax_policy"]),
                       format_func=WEB_TAX_TEXT.get, key=f"{key}_tax",
                       help="Solo «incluido» si se ha comprobado en la web. Sin comprobar, el IVA se "
                            "toma de cada ficha y, si no consta, la oferta no es comparable.")
    st.markdown("**Límites HTTP**")
    n1, n2, n3, n4 = st.columns(4)
    pause = n1.number_input("Pausa (s)", 1.0, 3600.0, float(row["pause_seconds"]), 1.0, key=f"{key}_pause")
    budget = n2.number_input("Peticiones máx.", 1, 200, int(row["max_requests"]), key=f"{key}_budget")
    timeout = n3.number_input("Timeout (s)", 1.0, 120.0, float(row["timeout_seconds"]), 1.0, key=f"{key}_timeout")
    retries = n4.number_input("Reintentos", 0, 3, int(row["retries"]), key=f"{key}_retries")
    reviewed = st.checkbox("Condiciones de uso revisadas por una persona", value=bool(row["terms_reviewed"]),
                           key=f"{key}_terms")
    note = st.text_input("Quién y cuándo las revisó", value=row["terms_note"], key=f"{key}_terms_note")
    if st.button("Guardar configuración web", key=f"{key}_save"):
        search.update_config(cid, locate_strategy=",".join(strategies) or "index",
                             search_url_template=template.strip(), price_selector=price_selector.strip(),
                             availability_selector=stock_selector.strip(), tax_policy=tax,
                             pause_seconds=pause, max_requests=int(budget), timeout_seconds=timeout,
                             retries=int(retries), terms_reviewed=reviewed, terms_note=note.strip(),
                             search_mode=mode, search_preferred=preferred,
                             search_alternatives=",".join(alternatives), search_verified=search_verified,
                             search_evidence=search_evidence.strip(), sales_market=(
                                 other_market.strip().upper() if selected_market == "other" else selected_market),
                             market_verified=market_verified, market_evidence_url=market_url.strip(),
                             market_note=market_note.strip())
        st.success("Configuración web guardada.")
        st.rerun()
    with st.form(f"{key}_url_form", clear_on_submit=True):
        u1, u2 = st.columns([1, 3])
        ean = u1.text_input("EAN", key=f"{key}_url_ean")
        url = u2.text_input("URL de ficha conocida", key=f"{key}_url_value")
        if st.form_submit_button("Añadir ficha conocida"):
            search.add_known_url(cid, ean, url.strip())
            st.success("Ficha añadida como candidata; se verificará en la próxima consulta.")
    with st.form(f"{key}_verify_form", clear_on_submit=True):
        v1, v2 = st.columns([1, 3])
        v_ean = v1.text_input("EAN", key=f"{key}_verify_ean")
        v_url = v2.text_input("URL para verificar o sustituir (una consulta, respetando robots.txt)",
                              key=f"{key}_verify_url")
        replace = st.checkbox("Sustituir la ficha registrada (el histórico se conserva)", key=f"{key}_verify_replace")
        if st.form_submit_button("Verificar y guardar"):
            _show_submission(st, search.submit_url(v_ean.strip(), cid, v_url.strip(), replace=replace))


def _render_pilot(store: PriceComparatorStore) -> None:
    st.subheader("Piloto de comparación")
    pilot = PricePilot(store)
    try:
        scope = pilot_scope(store)
        candidates = pilot.candidates()
        if candidates.empty:
            st.info("Importe primero el catálogo y sincronice precios Vytria válidos.")
            return
        st.caption("Solo 20 productos y seis tiendas. Guardar no activa el planificador ni realiza consultas. "
                   "Las consultas de precios reutilizan fichas conocidas; la localización es una operación "
                   "supervisada separada. Prepare y revise las condiciones de acceso "
                   "de cada tienda antes de guardar; robots.txt no es autorización suficiente.")
        st.dataframe(candidates[["ean", "brand", "model", "color", "caliber", "own_price_eur",
                                 "own_price_status", "eligible"]], hide_index=True, width="stretch")
        eligible = candidates.loc[candidates["eligible"]].set_index("ean")
        if st.button("Proponer muestra diversificada de 20", key="pilot_sample"):
            missing = sorted(set(SAMPLE_EANS) - set(eligible.index))
            if missing:
                st.error("La propuesta contiene referencias sin datos/precio válido: " + ", ".join(missing))
            else:
                st.session_state["pilot_eans"] = list(SAMPLE_EANS)
                st.rerun()
        defaults = scope["eans"] if scope else []
        selected = st.multiselect("Productos del piloto (exactamente 20)", list(eligible.index),
                                  default=[ean for ean in defaults if ean in eligible.index],
                                  max_selections=MAX_PRODUCTS, key="pilot_eans",
                                  format_func=lambda ean: f"{ean} · {eligible.loc[ean, 'brand']} · "
                                  f"{eligible.loc[ean, 'model']} · {eligible.loc[ean, 'color']}")
        st.code("\n".join(selected) or "Sin EAN seleccionados")
        st.caption(f"{len(candidates) - len(selected)} productos excluidos de esta selección. "
                   "Al guardar, las colas y actualizaciones generales quedan bloqueadas, incluso al desactivar "
                   "el piloto. Las ocho tiendas restantes no se activan ni se eliminan.")
        if selected and eligible.loc[selected, "caliber"].str.strip().eq("").any():
            st.info("Hay productos sin calibre en el catálogo. Con EAN publicado idéntico y verificado, "
                    "el calibre es informativo; siguen siendo obligatorios marca, modelo, código de color "
                    "y los demás requisitos comerciales.")
        competitors = {row["id"]: row["name"] for row in pilot.competitors()}
        cids = st.multiselect("Seis competidores", list(competitors), default=list(competitors),
                              max_selections=6, format_func=competitors.get, key="pilot_competitors")
        limits = {}
        for cid in cids:
            current = scope["competitors"].get(cid, {}) if scope else {}
            with st.expander(competitors[cid]):
                c1, c2 = st.columns(2)
                pause = c1.number_input("Pausa entre consultas (s)", 1.0, 3600.0,
                                        float(current.get("pause_seconds", 3)), key=f"pilot_pause_{cid}")
                budget = c2.number_input("Máximo HTTP según condiciones de acceso", 1, 200,
                                         int(current.get("max_http_requests", 10)), key=f"pilot_budget_{cid}")
                st.caption("Incluye robots.txt y redirecciones. Concurrencia 1; sin reintentos de 403/429/CAPTCHA.")
                limits[cid] = {"pause_seconds": pause, "max_http_requests": budget}
        if st.button("Guardar configuración del piloto", key="pilot_save"):
            pilot.save(selected, limits)
            st.success("Alcance guardado. Piloto desactivado y planificador automático bloqueado.")
            st.rerun()
        if scope:
            if scope["last_error"]:
                st.error("Último error del piloto: " + scope["last_error"])
            st.write("EAN persistidos:", ", ".join(scope["eans"]))
            plan = pilot.plan()
            st.dataframe(plan, width="stretch", hide_index=True)
            st.caption(f"Plan: {len(plan)} combinaciones, no consultas garantizadas. "
                       "«Ficha no localizada» no provoca búsquedas. La autorización se comprueba de nuevo al ejecutar.")
            confirmed = st.checkbox("He revisado el plan, límites y autorizaciones; iniciar una prueba manual supervisada",
                                     key="pilot_confirm")
            run_col, resume_col, stop_col = st.columns(3)
            busy = pilot.is_running()
            run = run_col.button("Ejecutar prueba manual", type="primary",
                                 disabled=not confirmed or busy, key="pilot_run")
            resume = resume_col.button("Reanudar prueba interrumpida",
                                       disabled=not confirmed or busy, key="pilot_resume")
            if stop_col.button("Desactivar piloto", key="pilot_disable"):
                pilot.disable()
                st.success("Piloto desactivado. Se conservan el alcance y el histórico.")
                st.rerun()
            if run or resume:
                pilot.enable()
                _launch(_run_pilot, pilot, resume)
                st.rerun()
            if busy:
                _watch_running_pilot(pilot)
            results, metrics = pilot.results()
            if metrics:
                st.write(metrics)
                st.dataframe(results.drop(columns=["observation"]), width="stretch", hide_index=True)
                st.download_button("Exportar resultados del piloto", results.to_csv(index=False).encode("utf-8"),
                                   file_name="piloto-precios.csv", mime="text/csv", key="pilot_results_export")
            _render_discovery(pilot, busy)
    except (PilotError, ValueError, sqlite3.Error) as exc:
        st.error(f"Piloto: {exc}")


def _render_discovery(pilot: PricePilot, busy: bool) -> None:
    st.subheader("Localización de fichas del piloto")
    discovery = PilotDiscovery(pilot.store)
    coverage = discovery.coverage()
    if coverage.empty:
        return
    counts = coverage["state"].value_counts()
    st.write({"Fichas conocidas": int(counts.get("known", 0)),
              "Confirmadas": int(counts.get("confirmed", 0)),
              "Candidatas": int(counts.get("candidate_found", 0) + counts.get("pending_validation", 0)),
              "Pendientes": int(counts.get("pending", 0)), "Combinaciones": len(coverage)})
    with st.expander("Cobertura de localización", expanded=False):
        st.dataframe(coverage.pivot(index="ean", columns="competitor", values="Estado"),
                     width="stretch")
    st.caption("La selección de ejecución es independiente de los filtros de la matriz. No se repiten fichas "
               "conocidas ni candidatos pendientes; el descubrimiento no crea ofertas.")
    scope = pilot_scope(pilot.store)
    if scope is None:
        return
    names = {row["id"]: row["name"] for row in pilot.competitors()}
    mode = st.radio("Alcance de ejecución", ["Una referencia y un competidor",
                    "Varios productos de un competidor", "Todos los pendientes"],
                    horizontal=True, key="discovery_execution_mode")
    competitor = st.selectbox("Competidor", list(scope["competitors"]),
                              format_func=lambda cid: names.get(cid, cid),
                              key="discovery_execution_competitor",
                              disabled=mode == "Todos los pendientes")
    available_eans = list(scope["eans"])
    if mode == "Una referencia y un competidor":
        ean = st.selectbox("EAN", available_eans, key="discovery_execution_ean")
        requested = [(competitor, ean)]
    elif mode == "Varios productos de un competidor":
        eans = st.multiselect("EAN o lista de EAN", available_eans, key="discovery_execution_eans")
        requested = [(competitor, selected_ean) for selected_ean in eans]
    else:
        requested = [(cid, ean) for cid in scope["competitors"] for ean in scope["eans"]]
    preview = None
    try:
        if requested:
            preview = discovery.preview(requested)
            st.write({
                "Combinaciones seleccionadas": len(preview["requested"]),
                "Trabajos pendientes reales": len(preview["effective"]),
                "Fichas existentes excluidas": len(preview["known_excluded"]),
                "Candidatos pendientes excluidos": len(preview["candidate_excluded"]),
                "Otras combinaciones excluidas": len(preview["other_excluded"]),
                "Combinaciones sin autorización": len(preview["unauthorized"]),
            })
            if mode == "Todos los pendientes":
                st.warning(f"Se iniciarán como máximo {len(preview['effective'])} trabajos autorizados "
                           "de las combinaciones pendientes.")
            if preview["methods"]:
                st.dataframe(pd.DataFrame([
                    {"Competidor": names.get(cid, cid), "Método configurado": detail["mode"],
                     "Métodos disponibles": ", ".join(detail["methods"]) or "Ninguno",
                     "Presupuesto disponible": preview["budgets"].get(cid, {}).get("available", 0)}
                    for cid, detail in preview["methods"].items()
                ]), hide_index=True, width="stretch")
            if preview["unauthorized"]:
                st.warning(preview["unauthorized"])
            if preview["active_run"]:
                active = preview["active_run"]
                st.warning(f"Hay una ejecución {active['operation']} {active['status']} #{active['id']}; "
                           "debe reanudarse o cerrarse antes de iniciar otra.")
    except PilotError as exc:
        st.error(str(exc))
    review_key = "invalid"
    if preview:
        review_state = json.dumps({
            "requested": preview["requested"], "effective": preview["effective"],
            "methods": preview["methods"], "unauthorized": preview["unauthorized"],
            "budgets": preview["budgets"],
        }, ensure_ascii=True, sort_keys=True)
        review_key = hashlib.sha256(review_state.encode("utf-8")).hexdigest()[:12]
    all_confirmed = True
    if mode == "Todos los pendientes":
        all_confirmed = st.checkbox(
            f"Confirmo iniciar los {len(preview['effective']) if preview else 0} trabajos autorizados pendientes",
            key=f"discovery_all_pending_confirmation_{review_key}")
    approval = st.checkbox("He revisado el alcance seleccionado, los métodos y las autorizaciones",
                           key=f"discovery_approval_{review_key}")
    active = discovery.latest_run()
    can_start = bool(preview and preview["effective"] and not preview["active_run"]
                     and approval and all_confirmed and not busy)
    start = st.button("Iniciar localización supervisada", disabled=not can_start,
                      key="discovery_start", type="primary")
    no_other_operation = not preview or not preview["active_run"] \
        or preview["active_run"]["operation"] == "discovery"
    resume_enabled = bool(active and active["status"] in ("running", "interrupted")
                          and no_other_operation)
    resume = st.button("Reanudar selección original", disabled=not resume_enabled or not approval or busy,
                       key="discovery_resume")
    if start:
        pilot.enable()
        _launch(_run_discovery, pilot, False, requested)
        st.rerun()
    if resume:
        pilot.enable()
        _launch(_run_discovery, pilot, True, None)
        st.rerun()
    if active:
        st.write({"Última ejecución": active["id"], "Estado": active["status"],
                  "Selección inmutable": active["selection"], "Resultados": active["results"],
                  "Presupuesto utilizado": active["budget_used"],
                  "Candidatos pendientes de revisión": active["candidates_pending_review"]})
        if active["status"] in ("running", "interrupted") and no_other_operation:
            with st.form("discovery_close_interrupted"):
                close_note = st.text_input("Motivo para cerrar la cola no finalizada")
                close = st.form_submit_button("Cerrar sin eliminar datos", disabled=busy or not close_note.strip())
                if close:
                    pilot.close_interrupted_discovery(note=close_note)
                    st.rerun()
    f1, f2, f3, f4 = st.columns(4)
    ean_filter = f1.multiselect("Filtrar EAN", list(coverage.ean.unique()), key="discovery_ean_filter")
    cid_filter = f2.multiselect("Filtrar competidor", list(coverage.competitor.unique()), key="discovery_cid_filter")
    method_filter = f3.multiselect("Filtrar método", list(coverage.method.unique()), key="discovery_method_filter")
    state_filter = f4.multiselect("Filtrar estado", list(coverage.Estado.unique()), key="discovery_state_filter")
    filtered = coverage
    for column, selected in (("ean", ean_filter), ("competitor", cid_filter), ("method", method_filter), ("Estado", state_filter)):
        if selected:
            filtered = filtered[filtered[column].isin(selected)]
    st.dataframe(filtered, hide_index=True, width="stretch",
                 column_config={"url": st.column_config.LinkColumn("URL confirmada o pendiente")})
    retryable = coverage[coverage.state.isin(["not_found", "not_allowed", "error", "blocked"])]
    if not retryable.empty:
        with st.form("discovery_retry"):
            cell = st.selectbox("Combinación para un nuevo intento", list(retryable.index),
                                format_func=lambda i: f"{retryable.loc[i, 'ean']} · {retryable.loc[i, 'competitor']}")
            note = st.text_input("Motivo y autorización del nuevo intento")
            if st.form_submit_button("Preparar nuevo intento (sin HTTP)", disabled=busy):
                row = retryable.loc[cell]
                discovery.reset(row["ean"], row["competitor_id"], note=note)
                st.rerun()
    with pilot.store._connect() as connection:
        audit = pd.read_sql_query("""SELECT * FROM pilot_http_log WHERE run_id IN
            (SELECT id FROM pilot_runs WHERE operation='discovery') ORDER BY id DESC LIMIT 500""", connection)
    if not audit.empty:
        with st.expander("Registro de consultas de localización"):
            st.dataframe(audit, hide_index=True, width="stretch")


def _render_pending_reviews(pilot: PricePilot) -> None:
    discovery = PilotDiscovery(pilot.store)
    if flash := st.session_state.pop("discovery_review_flash", ""):
        st.success(flash)
    candidates = discovery.candidates()
    pending = candidates[candidates["state"].isin(["candidate_found", "pending_validation"])] \
        if not candidates.empty else candidates
    st.header("Fichas pendientes de revisión")
    if pending.empty:
        st.info("No hay candidatos pendientes de revisión.")
    else:
        products = pilot.candidates().set_index("ean")
        competitor_names = {row["id"]: row["name"] for row in pilot.competitors()}

        def candidate_label(candidate_id: int) -> str:
            candidate = pending.set_index("id").loc[candidate_id]
            product = products.loc[candidate["ean"]]
            return (f"{product['brand']} {product['model']} · {product['color']} · "
                    f"{competitor_names.get(candidate['competitor_id'], candidate['competitor_id'])}")

        selected_id = st.selectbox("Candidato", pending["id"].tolist(), key="pending_candidate",
                                   format_func=candidate_label)
        row = pending.set_index("id").loc[selected_id]
        product = products.loc[row["ean"]]
        state_text = ("Coincidencia exacta; compruebe la ficha antes de confirmar."
                      if row["state"] == "candidate_found" else
                      "La identidad es ambigua; este candidato solo se puede descartar.")
        with st.container(border=True):
            st.subheader(f"{product['brand']} {product['model']} · {product['color']}")
            st.markdown(f"**Competidor:** {competitor_names.get(row['competitor_id'], row['competitor_id'])}")
            st.write("URL encontrada:", row["url"])
            st.caption(f"Estado: pendiente de revisión. {state_text}")
            try:
                config = WebPriceSearch(pilot.store).config(row["competitor_id"])
                candidate_url = validate_competitor_url(row["url"], config["domain"])
            except (WebSearchError, ValueError) as exc:
                st.error(f"No se puede abrir esta URL como ficha del competidor: {exc}")
            else:
                st.link_button("Ver ficha", candidate_url)
            with st.expander("Detalles técnicos"):
                st.write({"ID interno": int(selected_id), "Estado interno": row["state"],
                          "Ejecución": int(row["run_id"]), "Método": row["method"]})
                try:
                    st.json(json.loads(row["evidence"]))
                except (TypeError, ValueError) as exc:
                    st.error(f"No se pudo leer la evidencia técnica: {exc}")
            with st.form("discovery_review"):
                note = st.text_input("Revisor y motivo", key="discovery_review_note")
                confirm = st.form_submit_button(
                    "Confirmar ficha", type="primary",
                    disabled=pilot.is_running() or row["state"] != "candidate_found",
                    key="discovery_confirm_candidate")
                discard = st.form_submit_button("Descartar candidato", disabled=pilot.is_running(),
                                                key="discovery_discard_candidate")
                if confirm or discard:
                    try:
                        discovery.review(int(selected_id), accept=confirm, note=note)
                    except (PilotError, ValueError, sqlite3.Error) as exc:
                        st.error(f"No se pudo guardar la revisión: {exc}")
                    else:
                        st.session_state["discovery_review_flash"] = (
                            "Ficha confirmada." if confirm else "Candidato descartado. "
                            "Las fichas confirmadas de este producto se conservan.")
                        st.rerun()

    coverage = discovery.coverage()
    issues = coverage[coverage["state"].isin(["not_allowed", "error", "blocked", "rejected"])] \
        if not coverage.empty else coverage
    results, _ = pilot.results()
    if not results.empty:
        failures = results[results["outcome"].isin(["error", "blocked", "not_allowed"])]
    else:
        failures = results
    if not issues.empty or not failures.empty:
        st.header("Incidencias que requieren revisión")
        if not issues.empty:
            st.dataframe(issues[["ean", "competitor", "Estado", "detail"]].rename(columns={
                "ean": "EAN", "competitor": "Competidor", "Estado": "Estado", "detail": "Detalle",
            }), hide_index=True, width="stretch")
        if not failures.empty:
            st.dataframe(failures[["ean", "competitor_id", "outcome", "detail", "checked_at"]].rename(columns={
                "ean": "EAN", "competitor_id": "Competidor", "outcome": "Resultado",
                "detail": "Detalle", "checked_at": "Fecha",
            }), hide_index=True, width="stretch")


def _render_competitors(store: PriceComparatorStore, data: dict) -> None:
    _render_pilot(store)
    coverage = store.coverage_table()
    competitors = store.competitors().set_index("name")
    scheduler = _make_scheduler(store)
    search = _make_web_search(store)
    overview = scheduler.overview().set_index("name")
    pairs = data["pairs"]
    comparable = pairs["competitor"].value_counts().to_dict() if not pairs.empty else {}
    states_frame = search.competitor_states(comparable)
    states = states_frame.set_index("name")
    rows = []
    for item in coverage.to_dict("records"):
        name = item["competitor"]
        state = states.loc[name] if name in states.index else None
        rows.append({
            "Competidor": name,
            "Web": competitors.loc[name, "url"] if name in competitors.index else "",
            "Localización": LOCATION_TEXT[state["location"]] if state is not None else "—",
            "Extracción": EXTRACTION_TEXT[state["extraction"]] if state is not None else "—",
            "Operatividad": OPERABILITY_TEXT[state["operability"]] if state is not None else "—",
            "Ofertas": int(state["offers"]) if state is not None else 0,
            "Comparables": int(state["comparable"]) if state is not None else 0,
            "Estado": STATUS_TEXT.get(item["integration_status"], item["integration_status"]),
            "Método": item["method"] or "—",
            "Restricciones": item["restrictions"] or "—",
            "Referencias localizadas": int(item["references_located"]),
            "Ofertas confirmadas": int(item["confirmed_offers"]),
            "Última comprobación": item["last_checked_at"] or "—",
            "Planificador": overview.loc[name, "state_label"] if name in overview.index else "—",
        })
    if not rows:
        st.info("No hay competidores registrados.")
        return
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True, column_config={
        "Web": st.column_config.LinkColumn("Web", display_text=r"https?://(?:www\.)?([^/]+)")})
    st.caption("Tres estados independientes. **Localización**: si hay una ficha del producto. **Extracción**: si "
               "el extractor obtuvo datos reales. **Operatividad**: solo «Autorizado» cuando una persona revisó las "
               "condiciones de uso y robots.txt lo permite; que la extracción funcione no autoriza consultas "
               "periódicas. «Comparables»: ofertas vigentes con identificación, IVA, mercado, moneda y stock "
               "confirmados. Una URL pública o un robots.txt permisivo no equivalen a autorización.")
    _render_competitor_panels(search, scheduler, states_frame)
    runs = scheduler.recent_runs()
    if not runs.empty:
        st.write("Últimas ejecuciones")
        st.dataframe(runs[["competitor_name", "trigger", "started_at", "finished_at", "status", "requests",
                           "changed", "unchanged", "not_listed", "rejected", "errors", "message"]],
                     width="stretch", hide_index=True)
    st.subheader("Qué necesitamos de Vytria para obtener precios reales")
    for item in coverage.to_dict("records"):
        restrictions, needed = requirement_for(item["competitor"])
        url = competitors.loc[item["competitor"], "url"] if item["competitor"] in competitors.index else ""
        with st.expander(f"{item['competitor']} · {STATUS_TEXT.get(item['integration_status'], '')}"):
            st.markdown(f"**Web:** [{url}]({url})" if url else "")
            st.markdown(f"**Situación:** {restrictions or item['restrictions'] or '—'}")
            st.markdown(f"**Necesitamos:** {needed}")


def _render_data_sources(store: PriceComparatorStore) -> None:
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
    _render_vytria_prices(store)

    st.divider()
    st.subheader("Importar ofertas de competidores")
    st.caption("Importación autorizada por archivo. Registre URL, método, mercado y evidencia. Valores válidos: "
               "disponibilidad `available`, `unavailable` o `unknown`; coincidencia `exact_match`, "
               "`probable_match`, `manual_review`, `not_found` o `error`. El tipo de cambio se expresa en EUR por "
               "unidad de moneda.")
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
                       "plantilla_ofertas_competencia.xlsx", XLSX_MIME)
    expiry = store.price_expiry_days()
    current_offers = store.current_offers(expiry)
    if not current_offers.empty:
        st.write("Ofertas vigentes")
        st.dataframe(current_offers, width="stretch", hide_index=True)
    results = store.current_search_results(expiry)
    if not results.empty:
        st.write("Resultados de búsqueda sin oferta")
        st.dataframe(results, width="stretch", hide_index=True)

    competitors = store.competitors()
    if not competitors.empty:
        st.divider()
        st.subheader("Competidores registrados")
        st.caption("Mercado y moneda requieren confirmación antes de importar o consultar ofertas.")
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


def _render_competitor_panels(search: WebPriceSearch, scheduler: CompetitorScheduler,
                              states: pd.DataFrame) -> None:
    """One panel per competitor with two separate sections. The web section works with the scheduler disabled."""
    st.subheader("Configuración por competidor")
    st.caption("«Configuración web»: fichas de producto, acceso y verificación; no necesita feed ni planificador. "
               "«Actualización automática»: conector, frecuencia, límites y activación; el planificador no está "
               "activado y ninguno se habilita sin autorización. La comprobación de robots.txt hace una sola petición.")
    web = {r["competitor_id"]: r for r in search.configs().to_dict("records")}
    state = {r["competitor_id"]: r for r in states.to_dict("records")}
    scheduled = {r["competitor_id"]: r for r in scheduler.overview().to_dict("records")}
    for cid, web_row in web.items():
        row = scheduled.get(cid)
        mine = state.get(cid)
        label = (f"{web_row['name']} · {LOCATION_TEXT[mine['location']]} · {EXTRACTION_TEXT[mine['extraction']]} · "
                 f"{OPERABILITY_TEXT[mine['operability']]}") if mine else web_row["name"]
        with st.expander(label + (f" · Planificador: {row['state_label']}" if row else "")):
            st.markdown("#### Configuración web")
            _render_web_section(search, web_row)
            st.divider()
            st.markdown("#### Actualización automática")
            if row:
                _render_competitor_config(scheduler, row)


def _render_competitor_config(scheduler: CompetitorScheduler, row: dict) -> None:
    cid = row["competitor_id"]
    key = f"sched_{cid}"
    st.caption(f"Estado del planificador: {row['state_label']}")
    with st.container():
        if row["suspended_at"]:
            st.error(f"Suspendido: {row['suspended_reason']}. Revise la incidencia y reanude manualmente.")
        options = [""] + list(CONNECTORS)
        connector = st.selectbox("Conector", options, index=options.index(row["connector"]) if row["connector"] in options else 0,
                                 format_func=lambda v: CONNECTORS[v].label if v else "Pendiente de configurar",
                                 key=f"{key}_connector")
        source = st.text_input("Fuente (URL HTTPS del feed o nombre de fichero)", value=row["source"],
                               key=f"{key}_source", help="No incluya credenciales ni tokens.")
        note = st.text_input("Autorización de acceso (quién y cómo)", value=row["authorization_note"],
                             key=f"{key}_note")
        c1, c2, c3, c4 = st.columns(4)
        hours = c1.number_input("Cada (horas)", 1.0, 720.0, float(row["interval_hours"]), 1.0, key=f"{key}_hours")
        requests_per_run = c2.number_input("Peticiones por ejecución", 1, 1000, int(row["max_requests_per_run"]),
                                           key=f"{key}_requests")
        pause = c3.number_input("Pausa entre peticiones (s)", 0.0, 3600.0, float(row["pause_seconds"]), 1.0,
                                key=f"{key}_pause")
        attempts = c4.number_input("Reintentos", 1, 10, int(row["max_attempts"]), key=f"{key}_attempts")
        plan = scheduler.plan(cid)
        if plan.get("manual_only"):
            st.info("Excluido del piloto; sin consultas." if plan.get("excluded") else
                    "Piloto configurado: solo ejecuciones manuales supervisadas desde su sección.")
        st.caption(f"Cola: {row['queued']} pendientes · {plan['products']} productos en {plan['batches']} lotes · "
                   f"próxima ejecución {row['next_run_at'] or '—'} · última correcta {row['last_ok_at'] or '—'}")
        if not plan["fits_interval"]:
            st.warning("Con estos límites el ciclo no cabe en el intervalo; aumente las horas o reduzca la pausa.")
        b1, b2, b3, b4 = st.columns(4)
        try:
            if b1.button("Guardar", key=f"{key}_save"):
                scheduler.configure(cid, connector=connector, source=source, authorization_note=note,
                                    interval_hours=hours, max_requests_per_run=int(requests_per_run),
                                    pause_seconds=pause, max_attempts=int(attempts))
                st.rerun()
            if b2.button("Verificar y habilitar", key=f"{key}_enable",
                         disabled=not row["connector"] or plan.get("manual_only", False)):
                scheduler.verify(cid)
                scheduler.enable(cid)
                st.rerun()
            if row["state"] in {"active", "paused"}:
                if b3.button("Reanudar" if row["paused"] else "Pausar", key=f"{key}_pause_btn"):
                    (scheduler.resume if row["paused"] else scheduler.pause)(cid)
                    st.rerun()
            if row["suspended_at"] and b3.button("Reanudar tras incidencia", key=f"{key}_resume"):
                scheduler.resume(cid)
                st.rerun()
            if row["enabled"] and b4.button("Deshabilitar", key=f"{key}_disable"):
                scheduler.disable(cid)
                st.rerun()
        except (SchedulerConfigError, ConnectorError) as exc:
            st.error(str(exc))


def _render_settings(store: PriceComparatorStore) -> None:
    st.subheader("Histórico y caducidad")
    c1, c2, c3 = st.columns(3)
    retention = c1.selectbox("Conservar históricos (meses)", RETENTION_MONTHS_OPTIONS,
                             index=RETENTION_MONTHS_OPTIONS.index(int(store.get_setting("retention_months"))),
                             key="cfg_retention")
    expiry = c2.number_input("Un precio caduca a los (días)", 1, 90, store.price_expiry_days(), key="cfg_expiry")
    threshold = c3.number_input("Cambio importante a partir de (%)", 0.5, 100.0,
                                float(store.get_setting("alert_threshold_pct")), 0.5, key="cfg_threshold")
    sync = _make_sync(store)
    odoo_hours = st.number_input("Actualizar precios de Odoo cada (horas)", 1.0, 720.0,
                                 sync.interval.total_seconds() / 3600, 1.0, key="cfg_odoo_hours",
                                 help="Independiente de la frecuencia de cada competidor. Solo aplica si la "
                                      "sincronización automática está activada.")
    if st.button("Guardar configuración general", key="cfg_save"):
        try:
            store.set_setting("retention_months", str(retention))
            store.set_setting("price_expiry_days", str(int(expiry)))
            store.set_setting("alert_threshold_pct", str(threshold))
            sync.set_interval_hours(odoo_hours)
        except ValueError as exc:
            st.error(str(exc))
        else:
            saved = PriceComparatorStore(store.database_path)  # read back from disk, not from memory
            if (saved.get_setting("retention_months"), saved.price_expiry_days()) != (str(retention), int(expiry)):
                st.error("La configuración no se ha podido confirmar en la base de datos.")
            else:
                st.success(f"Configuración guardada y verificada en {saved.database_path.name}.")
    st.caption("Fechas en UTC. «Vigente»: comprobado dentro del plazo de caducidad. «Último conocido»: hasta el doble "
               "del plazo; se muestra pero no se compara. «Caducado»: más antiguo. La configuración por competidor "
               "(web y actualización automática) está en la pestaña «Competidores».")


def render_price_comparator_page() -> None:
    page_title(
        "Comparador de precios",
        "Comparación trazable por EAN. Las ofertas sólo cuentan como confirmadas "
        "cuando son exactas, disponibles y del mismo mercado.",
    )
    try:
        store = PriceComparatorStore()
        store.seed_defaults()
        pilot_scope(store)
    except (PriceComparatorStorageError, PilotError) as exc:
        st.error(str(exc))
        return
    st.markdown(PAGE_STYLE, unsafe_allow_html=True)
    _show_pending_reports()
    data = _load(store)
    if data["summary"].empty:
        data["filtered_summary"] = data["summary"]
        data["selected_competitors"] = []
        data["dashboard_window"] = st.session_state.get("dashboard_period", "30 días")
    else:
        _render_shared_filters(data)
    summary_tab, products_tab, pending_tab, admin_tab = st.tabs(
        ["Resumen", "Productos", "Pendientes", "Administración"])
    with summary_tab:
        _render_summary(store, data)
    with products_tab:
        _render_products(store, data)
    with pending_tab:
        _render_pending_reviews(PricePilot(store))
    with admin_tab:
        section = st.selectbox(
            "Área administrativa",
            ["Competidores y ejecuciones", "Búsqueda manual", "Importar datos", "Configuración"],
            key="price_admin_section")
        if section == "Competidores y ejecuciones":
            _render_actions(store)
            _render_competitors(store, data)
        elif section == "Búsqueda manual":
            _render_web_finder(store)
        elif section == "Importar datos":
            _render_data_sources(store)
        else:
            _render_settings(store)
