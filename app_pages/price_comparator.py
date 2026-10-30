from __future__ import annotations

from io import BytesIO
import sqlite3
import threading
from zipfile import BadZipFile

import pandas as pd
import streamlit as st

from cloud_design import page_title
import price_analytics as analytics
from price_comparator import RETENTION_MONTHS_OPTIONS, PriceComparatorStorageError, PriceComparatorStore
from price_connectors import CONNECTORS, ConnectorError
from price_scheduler import CompetitorScheduler, SchedulerConfigError
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
TABLE_LABELS = {
    "brand": "Marca", "model": "Modelo", "ean": "EAN", "own_price_eur": "Precio Vytria",
    "best_competitor": "Mejor competidor", "competitor_min_eur": "Precio competidor",
    "difference_eur": "Diferencia €", "difference_pct": "% vs Vytria",
    "last_checked_at": "Última comprobación", "position": "Posición", "own_price_status": "Estado precio Vytria",
}


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

    st.caption("El botón «Actualizar precios Vytria» está en la parte superior de la pantalla.")
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
        if st.button("Actualizar precios Vytria", key="sync_vytria_prices", disabled=running, type="primary",
                     help="Lee la tarifa 59 de Odoo (solo lectura)."):
            _launch(_run_sync, sync)
            st.rerun()
    with right:
        busy = scheduler.is_running()
        if st.button("Actualizar competencia", key="update_competitors", disabled=busy or ready.empty,
                     help="Consulta solo los competidores habilitados con acceso autorizado."):
            _launch(_run_competitors, scheduler)
            st.rerun()
        if ready.empty:
            st.caption("Ningún competidor está habilitado todavía. Se activan en la pestaña Configuración.")
    if running:
        _watch_running_sync(sync)
    if busy:
        _watch_running_competitors(scheduler)


def _render_detail(store: PriceComparatorStore, summary: pd.DataFrame, expiry: int) -> None:
    st.subheader("Detalle de referencia")
    selected = st.selectbox("Referencia", options=summary["ean"].tolist(), key="price_detail_ean",
                            format_func=lambda ean: f"{ean} · {summary.loc[summary.ean == ean, 'commercial_name'].iloc[0]}")
    window = st.radio("Periodo", list(analytics.WINDOWS), horizontal=True, key="price_detail_window")
    available = store.competitors()
    names = sorted(available["name"].tolist())
    chosen = st.multiselect("Competidores en el gráfico", names, default=names, key="price_detail_competitors")
    series = analytics.price_series(store, selected, analytics.WINDOWS[window], chosen)
    if series.empty:
        st.info("No hay precios almacenados de esta referencia en el periodo. Los periodos sin dato no se dibujan.")
    else:
        st.plotly_chart(analytics.price_figure(series), width="stretch")
        st.caption("Línea escalonada: cada precio se mantiene hasta el siguiente cambio registrado. "
                   "● cambio detectado · ✕ comprobación correcta sin cambio. Zonas sin línea: sin información.")
    row = summary[summary["ean"] == selected].iloc[0]
    st.write(f"**Precio Vytria:** {row['own_price_eur'] if pd.notna(row['own_price_eur']) else 'sin precio confirmado'}"
             f" · **Estado:** {row['own_price_status'] or '—'}")
    offers = store.offer_history(selected)
    if not offers.empty:
        latest = offers.sort_values(["captured_at", "id"]).groupby(["competitor_id", "market"]).tail(1).copy()
        latest["Estado del precio"] = latest["captured_at"].map(lambda value: analytics.price_state(value, expiry))
        st.dataframe(latest[["competitor_name", "price", "promotional_price", "currency", "availability",
                             "match_status", "captured_at", "Estado del precio", "url"]].rename(columns={
            "competitor_name": "Competidor", "price": "Precio", "promotional_price": "Promoción",
            "currency": "Moneda", "availability": "Disponibilidad", "match_status": "Coincidencia",
            "captured_at": "Último cambio", "url": "URL"}), width="stretch", hide_index=True)
    with st.expander("Histórico completo de ofertas"):
        st.dataframe(offers, width="stretch", hide_index=True)


def _render_comparison(store: PriceComparatorStore) -> None:
    expiry = store.price_expiry_days()
    summary, counts = store.dashboard(expiry)
    if summary.empty:
        st.info("Importe primero el catálogo piloto en la pestaña «Datos y fuentes».")
        return
    offers = store.current_offers(expiry)
    changes = analytics.recent_changes(store, 7)
    indicators = analytics.kpis(summary, len(changes))
    cols = st.columns(4)
    cols[0].metric("Con precio Vytria confirmado", f"{indicators['vytria_confirmed']}/{counts['products']}")
    cols[1].metric("Con competencia comparable", indicators["comparable"])
    cols[2].metric("Vytria con el mejor precio", f"{indicators['best_price_pct']:.0f}%",
                   help=f"Sobre {indicators['compared']} referencias comparadas")
    cols[3].metric("Cambios importantes (7 días)", indicators["recent_changes"])
    st.caption(
        "Se comparan los últimos datos válidos almacenados: precio Vytria validado desde Odoo (IVA incluido) frente a "
        f"ofertas exactas y disponibles del mismo mercado comprobadas en los últimos {expiry} días. "
        f"Última oferta comprobada: {offers['checked_at'].max() if not offers.empty else '—'}.")
    if offers.empty:
        st.warning("No hay datos de competencia suficientes. Importe ofertas en «Datos y fuentes» o habilite "
                   "un conector autorizado en «Configuración». Mientras tanto solo se muestran los precios de Vytria.")

    table = analytics.commercial_table(summary)
    f1, f2, f3, f4 = st.columns(4)
    brands = f1.multiselect("Marca", sorted(v for v in table["brand"].dropna().unique() if v), key="cmp_brand")
    competitors = f2.multiselect("Competidor", sorted(offers["competitor_name"].unique()) if not offers.empty else [],
                                 key="cmp_competitor")
    availability = f3.multiselect("Disponibilidad", sorted(offers["availability"].unique()) if not offers.empty else [],
                                  key="cmp_availability")
    positions = f4.multiselect("Posición de precio", list(analytics.POSITIONS), key="cmp_position")
    filtered = analytics.filter_table(table, offers, brands=brands, competitors=competitors,
                                      availability=availability, positions=positions)
    st.dataframe(filtered.rename(columns=TABLE_LABELS), width="stretch", hide_index=True)
    st.download_button("Descargar comparación Excel", _excel_bytes({"Comparación": filtered.rename(columns=TABLE_LABELS)}),
                       "comparador_precios.xlsx", XLSX_MIME)

    attention = st.expander(f"Requieren atención ({int(summary['own_price_eur'].isna().sum())} sin precio Vytria · "
                            f"{len(changes)} cambios recientes)")
    with attention:
        missing = summary[summary["own_price_eur"].isna()]
        if not missing.empty:
            st.write("Sin precio Vytria validado (no participan en la comparación):")
            st.dataframe(missing[["brand", "commercial_name", "ean", "own_price_status"]], width="stretch",
                         hide_index=True)
        if not changes.empty:
            st.write("Cambios de precio importantes:")
            st.dataframe(changes, width="stretch", hide_index=True)

    st.subheader("Evolución global")
    window = st.radio("Periodo", list(analytics.WINDOWS), horizontal=True, key="price_global_window")
    series = analytics.competitiveness_series(store, analytics.WINDOWS[window])
    if series.empty:
        st.info("Todavía no hay histórico suficiente de Vytria y competencia para calcular la competitividad.")
    else:
        st.plotly_chart(analytics.competitiveness_figure(series), width="stretch")
    top = analytics.top_differences(summary)
    if not top.empty:
        st.write("Mayores diferencias")
        st.dataframe(top.rename(columns=TABLE_LABELS), width="stretch", hide_index=True)

    _render_detail(store, summary, expiry)


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


def _render_competitor_config(scheduler: CompetitorScheduler, row: dict) -> None:
    cid = row["competitor_id"]
    key = f"sched_{cid}"
    with st.expander(f"{row['name']} · {row['state_label']}"):
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
            if b2.button("Verificar y habilitar", key=f"{key}_enable", disabled=not row["connector"]):
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
            st.success("Configuración guardada.")
    st.caption("Fechas en UTC. «Vigente»: comprobado dentro del plazo de caducidad. «Último conocido»: hasta el doble "
               "del plazo; se muestra pero no se compara. «Caducado»: más antiguo.")

    scheduler = _make_scheduler(store)
    st.subheader("Competidores y planificador")
    st.caption("Cada competidor se habilita por separado y solo con un conector de acceso autorizado y verificado. "
               "Los demás figuran como «Pendiente de configurar»; no se simulan consultas. El planificador "
               "automático no está activado: se ejecuta como comando (`python price_scheduler.py --once`).")
    overview = scheduler.overview()
    for row in overview.to_dict("records"):
        _render_competitor_config(scheduler, row)
    runs = scheduler.recent_runs()
    if not runs.empty:
        st.write("Últimas ejecuciones")
        st.dataframe(runs[["competitor_name", "trigger", "started_at", "finished_at", "status", "requests",
                           "changed", "unchanged", "not_listed", "rejected", "errors", "message"]],
                     width="stretch", hide_index=True)


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
    _render_actions(store)
    comparison_tab, data_tab, settings_tab = st.tabs(["Comparación", "Datos y fuentes", "Configuración"])
    with comparison_tab:
        _render_comparison(store)
    with data_tab:
        _render_data_sources(store)
    with settings_tab:
        _render_settings(store)
