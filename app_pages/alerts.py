from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Callable

import pandas as pd
import streamlit as st

from check_pedidos_vigilados import find_matching_orders
from db_config import load_env_file
from graph_mail_downloader import send_alert_email
from order_alerts import (
    LEGACY_NOTIFICATIONS_FILE,
    RESULT_LABELS,
    SOURCE_AUTO,
    SOURCE_MANUAL,
    OrderAlertStore,
    alert_recipients,
    dispatch_order_alerts,
    import_legacy_notification_file,
    scheduler_state,
)
from watchlist_config import add_customer, load_watchlist, remove_customer, save_watchlist


def _format_alert_timestamp(value: int | None) -> str:
    return datetime.fromtimestamp(int(value)).strftime("%d/%m/%Y %H:%M:%S") if value else "—"


def _render_order_alert_service_status() -> None:
    st.subheader("Envío automático")
    try:
        status = OrderAlertStore().get_status(SOURCE_AUTO)
    except Exception as exc:  # noqa: BLE001
        st.error(f"No se puede leer el registro de alertas: {exc}")
        return

    state = scheduler_state(status)
    if state == "never":
        st.warning(
            "El servicio automático (contenedor `abcd-order-alerts`) no ha registrado ninguna ejecución. "
            "Hasta que arranque, las alertas solo se envían con «Comprobar ahora»."
        )
        return

    interval_minutes = max(1, int(status.get("interval_seconds") or 300) // 60)
    result_label = RESULT_LABELS.get(status.get("last_check_result") or "", "—")
    last_error_at = status.get("last_error_at")
    last_check_failed = bool(last_error_at) and int(last_error_at) >= int(status.get("last_check_at") or 0)
    if state == "stopped":
        st.error(
            f"Servicio automático DETENIDO desde {_format_alert_timestamp(status.get('stopped_at'))}. "
            "No se están comprobando pedidos."
        )
    elif state == "stale":
        st.error(
            f"Servicio automático sin actividad desde {_format_alert_timestamp(status.get('heartbeat_at'))}: "
            "proceso detenido o bloqueado. No se están comprobando pedidos."
        )
    elif last_check_failed:
        st.warning(f"Servicio activo (cada {interval_minutes} min), pero la última comprobación falló: {result_label}.")
    else:
        st.success(f"Servicio activo (cada {interval_minutes} min). Último resultado: {result_label}.")

    col_check, col_sent, col_error = st.columns(3)
    col_check.markdown(
        f"**Última comprobación**  \n{_format_alert_timestamp(status.get('last_check_at'))}  \n"
        f"{result_label}. {status.get('last_check_detail') or ''}"
    )
    col_sent.markdown(
        f"**Último envío**  \n{_format_alert_timestamp(status.get('last_sent_at'))}  \n"
        + (f"{status.get('last_sent_count')} pedido(s)" if status.get("last_sent_at") else "Sin envíos")
    )
    col_error.markdown(
        f"**Último error**  \n{_format_alert_timestamp(last_error_at)}  \n"
        f"{status.get('last_error') or 'Sin errores'}"
    )
    st.caption(
        "El envío automático busca solo pedidos confirmados y pendientes (estado 'sale', sin facturar del todo "
        "o con líneas sin entregar) de la ventana ALERTA_PEDIDOS_DIAS_ATRAS (180 días por defecto). "
        "Comparte con «Comprobar ahora» la lista de clientes y el registro de pedidos ya notificados."
    )


def _render_watchlist_editor(watchlist: list[str]) -> None:
    with st.form("watchlist_add_form", clear_on_submit=True):
        st.subheader("Añadir cliente a vigilar")
        new_name = st.text_input(
            "Nombre exacto del cliente (tal cual figura en Odoo)",
            placeholder="Ej: Óptica Ejemplo S.L.",
        )
        added = st.form_submit_button("Añadir", type="primary")

    if added:
        if new_name.strip():
            add_customer(new_name)
            st.success(f"Cliente '{new_name.strip()}' añadido a la lista de vigilancia.")
        else:
            st.warning("Introduce un nombre antes de añadir.")

    st.divider()
    st.subheader("Clientes vigilados")
    if not watchlist:
        st.info("No hay clientes en la lista de vigilancia todavía.")
        return

    edited_customers = st.data_editor(
        pd.DataFrame({"Cliente": watchlist}),
        column_config={"Cliente": st.column_config.TextColumn("Cliente vigilado", required=True)},
        hide_index=True,
        num_rows="dynamic",
        key="watchlist_editor",
    )
    if st.button("Guardar lista de clientes", type="secondary"):
        customers = [
            str(name).strip()
            for name in edited_customers["Cliente"].dropna().tolist()
            if str(name).strip()
        ]
        customers = list(dict.fromkeys(customers))
        save_watchlist(customers)
        st.success(f"Lista guardada: {len(customers)} cliente(s) vigilado(s).")
        st.rerun()

    customer_to_remove = st.selectbox("Cliente a quitar", options=watchlist)
    if st.button("Quitar cliente", type="secondary"):
        remove_customer(customer_to_remove)
        st.success(f"Cliente '{customer_to_remove}' eliminado de la lista.")
        st.rerun()


def _render_order_check(watchlist: list[str]) -> None:
    st.divider()
    st.subheader("Comprobar pedidos")

    start_column, end_column, state_column = st.columns([1, 1, 1])
    today = date.today()
    date_from = start_column.date_input("Desde", value=today - timedelta(days=30))
    date_to = end_column.date_input("Hasta", value=today)
    status_filter = state_column.radio("Estado", options=["Pendientes", "Todos"], horizontal=True)

    if not st.button("Comprobar ahora", type="primary"):
        return
    if not watchlist:
        st.warning("Añade al menos un cliente a la lista de vigilancia antes de comprobar.")
        return
    if date_from > date_to:
        st.warning("La fecha 'Desde' no puede ser posterior a la fecha 'Hasta'.")
        return

    with st.spinner("Consultando pedidos en Odoo..."):
        try:
            results = find_matching_orders(
                clientes=watchlist,
                fecha_desde=date_from,
                fecha_hasta=date_to + timedelta(days=1),
                solo_pendientes=(status_filter == "Pendientes"),
            )
        except Exception as exc:  # noqa: BLE001
            st.error(f"Error al consultar Odoo: {exc}")
            return

    if results.empty:
        st.info("No se han encontrado pedidos para los clientes vigilados en el rango indicado.")
        return

    st.success(f"Se han encontrado {len(results)} pedido(s).")
    st.dataframe(results, use_container_width=True, hide_index=True)

    alert_store = OrderAlertStore()
    import_legacy_notification_file(alert_store, LEGACY_NOTIFICATIONS_FILE)
    load_env_file()
    recipients = alert_recipients()
    with st.spinner(f"Comprobando registro y enviando alerta a {', '.join(recipients)}..."):
        dispatch = dispatch_order_alerts(
            results.to_dict(orient="records"),
            alert_store,
            send_alert_email,
            recipients,
        )
    if dispatch.error:
        alert_store.record_check(SOURCE_MANUAL, "error_envio", error=dispatch.error)
        st.error(f"No se pudo enviar el email de alerta: {dispatch.error}")
    elif dispatch.sent:
        alert_store.record_check(SOURCE_MANUAL, "enviado", sent_count=len(dispatch.new_rows))
        st.success(f"Email de alerta enviado a {', '.join(recipients)}.")
    else:
        alert_store.record_check(SOURCE_MANUAL, "ya_notificados")
        st.info("Los pedidos ya están notificados o hay otra ejecución procesándolos.")


def render_alerts_page(
    render_sidebar_shell: Callable[[str], None],
    require_admin_access: Callable[[], None],
    render_footer: Callable[[], None],
) -> None:
    render_sidebar_shell("Alerta Pedidos")
    require_admin_access()
    st.title("Alerta de Pedidos de Clientes Vigilados")
    st.caption(
        "Mantén aquí la lista de clientes a vigilar. Se busca coincidencia en el nombre del cliente "
        "o en la dirección de entrega. Cuando se detecte un pedido, se enviará un email de alerta."
    )

    watchlist = load_watchlist()
    _render_watchlist_editor(watchlist)
    _render_order_alert_service_status()
    _render_order_check(watchlist)
    render_footer()
