from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from PIL import Image
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
from sqlalchemy.exc import OperationalError

from kering_images import (
    BatchService, ConfigStore, ImageStore, VIEWS, STATES, data_root, configured_orders, read_suppliers,
    read_supplier_contacts, displayed_views, purchase_status, latest_attempts, trial_validated, batch_busy,
)
from kering_portal import KeringPortal
from kering_jobs import prepare_selection, make_loader, schedule_status
from image_repository import ImageRepository
from image_exports import order_export_plan, prepare_order_zip, repository_signature
from image_thumbnails import ThumbnailCache, validate_visible
from repository_storage import check_image_root


SAVE_ICON = ":material/save:"
DATE_FORMAT = "DD/MM/YYYY"
TIME_FORMAT = "%d/%m/%Y %H:%M"
MADRID = ZoneInfo("Europe/Madrid")


@st.cache_resource
def service() -> BatchService:
    return BatchService(ImageStore(data_root()))


def admin_only() -> None:
    if st.session_state.get("auth_role") != "admin":
        st.error("Acceso reservado a administradores.")
        st.stop()


def resolve_engine(engine):
    return engine() if callable(engine) else engine


def render_kering_settings(engine) -> None:
    admin_only()
    st.subheader("Kering")
    store = ConfigStore(data_root())
    try:
        config = store.load()
        suppliers = read_suppliers(resolve_engine(engine))
    except Exception:
        st.error("No se puede leer la configuracion cifrada o los proveedores Odoo. Revise la clave y la conexion del servidor.")
        return
    options = [0] + [row["id"] for row in suppliers]
    names = {row["id"]: row["name"] for row in suppliers}
    names[0] = "Seleccionar proveedor"
    with st.form("kering_settings"):
        url = st.text_input("URL del portal Kering", config["url"])
        username = st.text_input("Usuario Kering", config["username"])
        password = st.text_input("Contrase\u00f1a Kering", type="password")
        supplier = st.selectbox(
            "Proveedor Odoo Kering", options,
            index=options.index(config["supplier_id"]) if config["supplier_id"] in options else 0,
            format_func=lambda value: names[value],
        )
        include_contacts = st.checkbox(
            "Incluir contactos de la misma entidad comercial Odoo",
            value=config.get("include_commercial_contacts", False),
        )
        clear = st.checkbox("Eliminar credenciales guardadas")
        save = st.form_submit_button("Guardar Kering", icon=SAVE_ICON)
    if save:
        save_settings(store, {**store.load(), "url": url.strip(), "username": "" if clear else username.strip(),
                              "password": "" if clear else password or config["password"], "supplier_id": supplier,
                              "include_commercial_contacts": include_contacts})
    if st.button("Ver contactos asociados al proveedor guardado", disabled=not config["supplier_id"]):
        show_supplier_contacts(engine, config["supplier_id"])
    if st.button("Comprobar configuracion Kering", icon=":material/check_circle:"):
        check_local_configuration(store, names)
    running = st.session_state.get("kering_access_probe")
    if st.button("Probar acceso a Kering", icon=":material/login:", disabled=running is not None and not running.done()):
        try:
            st.session_state["kering_access_probe"] = service().executor.submit(probe_access, store.load())
        except Exception:
            st.error("No se pudo iniciar la prueba de acceso.")
    if st.session_state.get("kering_access_probe"):
        render_access_probe()
    render_job_settings(store)


def render_job_settings(config_store):
    config = config_store.load()
    store = service().store
    validated = trial_validated(store, config)
    st.subheader("Procesamiento Kering")
    with st.form("kering_job_settings"):
        cutoff = st.date_input("Fecha de corte", date.fromisoformat(config["cutoff_date"]), format=DATE_FORMAT)
        automatic = st.toggle("Procesamiento autom\u00e1tico", value=config["auto_enabled"])
        hours = st.number_input("Intervalo (horas)", min_value=0.0, value=float(config["auto_interval_hours"]), step=1.0)
        test_mode = st.toggle("Modo de prueba", value=config["test_mode"] if validated else True, disabled=not validated)
        saved = st.form_submit_button("Guardar procesamiento", icon=SAVE_ICON)
    if saved:
        save_settings(config_store, {**config_store.load(), "cutoff_date": cutoff.isoformat(),
                                   "auto_enabled": automatic, "auto_interval_hours": hours,
                                   "test_mode": test_mode, "full_lot_validated": validated and not test_mode})
    if not validated:
        st.info("Validaci\u00f3n inicial de pedidos pendiente")
    render_scheduler_status()


def format_timestamp(value):
    # pandas turns missing timestamps (e.g. running attempts) into NaN, which is truthy.
    if value is None or pd.isna(value) or not value:
        return "-"
    return datetime.fromtimestamp(value, MADRID).strftime(TIME_FORMAT)


@st.fragment(run_every="5s")
def render_scheduler_status():
    try:
        config = ConfigStore(data_root()).load()
        status = schedule_status(service().store, config)
        previous, upcoming = st.columns(2)
        previous.metric("\u00daltima ejecuci\u00f3n autom\u00e1tica", format_timestamp(status["last_finished"] or status["last_started"]))
        upcoming.metric("Pr\u00f3xima ejecuci\u00f3n", format_timestamp(status["next_run"]))
        st.write(f"Resultado: {status['result']}")
    except Exception:
        st.error("No se pudo consultar el estado del programador.")


def show_supplier_contacts(engine, supplier_id):
    try:
        st.dataframe(read_supplier_contacts(resolve_engine(engine), supplier_id), hide_index=True)
    except Exception:
        st.error("No se pudieron consultar los contactos del proveedor.")


def check_local_configuration(store, names):
    try:
        current = store.load()
        store.cipher()
        if not current["username"] or not current["password"] or current["supplier_id"] not in names or not current["supplier_id"]:
            st.warning("Faltan credenciales o proveedor Odoo.")
        else:
            st.success("Cifrado y proveedor disponibles. No se ha comprobado el acceso autenticado a Kering.")
    except Exception:
        st.error("No se pudo comprobar la configuracion del servidor.")


def create_portal(config):
    return KeringPortal(config)


def probe_access(config):
    return create_portal(config).check_access()


@st.fragment(run_every="2s")
def render_access_probe():
    future = st.session_state["kering_access_probe"]
    if not future.done():
        st.info("Comprobando acceso a Kering...")
        return
    try:
        result = future.result()
        if result["ok"]:
            st.success("Acceso autenticado a Kering confirmado. Esta prueba no descarga productos.")
        else:
            st.warning(f"Acceso no confirmado: {result['code']}")
        if result.get("phases"):
            st.dataframe(result["phases"], hide_index=True)
    except Exception:
        st.error("No se pudo comprobar el acceso a Kering.")


def save_settings(store, values):
    try:
        store.save(values)
        st.success("Configuracion Kering guardada cifrada.")
    except Exception:
        st.error("No se pudo guardar. Compruebe URL, proveedor y clave de cifrado del servidor.")


def local_order_time(value):
    instant = datetime.fromisoformat(str(value))
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(MADRID)


def pending_summary(store, order):
    total = len(order["lines"])
    complete = sum(bool(line["ean"]) and set(VIEWS) <= store.valid_views(line["ean"]).keys() for line in order["lines"])
    missing = sum(not line["ean"] for line in order["lines"])
    return f"{complete}/{total} completos; {total - complete} pendientes; {missing} sin EAN"


def submit_orders(engine, config, selected, start, end):
    try:
        store = service().store
        config_store = ConfigStore(data_root())
        current_config = config_store.load()
        if current_config["supplier_id"] != config["supplier_id"]:
            raise ValueError("El proveedor ha cambiado")
        start = prepare_selection(store, current_config, selected, start, end)
        supplier = int(current_config["supplier_id"])
        loader = make_loader(store, config_store, lambda: resolve_engine(engine), start, end, supplier,
                             reader=configured_orders)
        return service().submit(selected, st.session_state.get("auth_user", "admin"), supplier, start, end, loader,
                                portal=create_portal(current_config), origin="Manual")
    except Exception:
        st.error("No se pudo iniciar: revise corte, pedidos autorizados o lotes activos.")
        return None


def render_progress(run_id):
    store = service().store
    with store.connect() as connection:
        run = connection.execute("SELECT ended FROM runs WHERE id=?", (run_id,)).fetchone()
    active = run is not None and run["ended"] is None
    st.fragment(run_every="3s" if active else None)(_render_progress)(run_id)


def _render_progress(run_id):
    rows = service().store.run_history(run_id)
    if not rows:
        return
    finished = sum(row["status"] not in {"Pendiente", "En proceso"} for row in rows)
    st.progress(finished / len(rows), text=f"{finished}/{len(rows)} pedidos")
    st.dataframe(pd.DataFrame(rows)[["number", "status", "error"]], hide_index=True, width="stretch")
    for row in rows:
        results = json.loads(row["results"])
        if results:
            with st.expander(row["number"]):
                st.dataframe([dict(EAN=ean, **result["views"], Motivo=result["reason"], Intentos=result["tries"],
                                   Archivos=result.get("acquisition", {}).get("available_files", "-"),
                                   Descargados=result.get("acquisition", {}).get("downloaded_files", "-"),
                                   Reutilizados=result.get("acquisition", {}).get("reused_files", "-"))
                              for ean, result in results.items()], hide_index=True)
    if rows[0]["ended"] is not None and not st.session_state.get(f"kering_finished_{run_id}"):
        st.session_state[f"kering_finished_{run_id}"] = True
        st.rerun()


def render_kering_page(engine) -> None:
    admin_only()
    st.title("Im\u00e1genes Kering")
    try:
        config = ConfigStore(data_root()).load()
        batch = service()
    except Exception:
        st.error("No se pudo abrir el almacenamiento Kering. Revise los permisos y la clave del servidor.")
        return
    if not config["supplier_id"]:
        st.info("Seleccione el proveedor Kering en Configuracion.")
        return
    cutoff = date.fromisoformat(config["cutoff_date"])
    schedule = schedule_status(batch.store, config)
    st.caption(f"Corte: {cutoff:%d/%m/%Y} | Automatizacion: "
               f"{'activa' if config['auto_enabled'] else 'inactiva'} · {config['auto_interval_hours']} h | "
               f"Ultima ejecucion: {format_timestamp(schedule['last_finished'])} | "
               f"Proxima: {format_timestamp(schedule['next_run'])}")
    probe = st.session_state.get("kering_access_probe")
    access = None
    if probe and probe.done():
        try:
            access = probe.result()
        except Exception:
            st.error("Fallo la comprobacion de acceso. Repita la prueba en Configuracion.")
    st.caption("Acceso Kering: " + ("confirmado" if access and access["ok"] else "no confirmado"))
    st.link_button("Configuracion", "/config", icon=":material/settings:")
    orders_tab, search_tab, history_tab = st.tabs(["Procesar pedidos", "Buscar imágenes", "Historial"])
    with orders_tab:
        left, right = st.columns(2)
        start = left.date_input("Fecha desde", cutoff, min_value=cutoff, format=DATE_FORMAT, key="kering_start")
        end = right.date_input("Fecha hasta", max(cutoff, date.today()), min_value=cutoff,
                               format=DATE_FORMAT, key="kering_end")
        number = st.text_input("Numero de pedido", key="kering_order_number")
        state = st.selectbox("Estado de imagenes", ["Todos", "Procesado", "Pendiente", "Error"],
                            key="kering_order_state")
        refresh = st.button("Actualizar pedidos", icon=":material/refresh:", type="primary")
        render_order_list(engine, start, end, number, state, refresh, access)
        if st.session_state.get("kering_run"):
            render_progress(st.session_state["kering_run"])
    with search_tab:
        render_image_search(batch.store)
    with history_tab:
        render_history(engine, config, batch.store)


def page_transition(state, route):
    previous = state.get("image_page_route")
    if route != previous:
        state["image_page_route"] = route
        for key in list(state):
            if key.endswith(("-access", "-records")) or key in {
                    "image_gallery_access", "luxoptica_selected_image", "luxoptica_selected_bytes",
                    "image_gallery_zip", "image_gallery_records"}:
                state.pop(key, None)
        if route == "imagenes-kering":
            state.pop("kering_orders_loaded", None)


def load_order_listing(engine, store, config, start, end, number="", status="Todos"):
    orders = configured_orders(resolve_engine(engine), config, start, end)
    latest = latest_attempts(store)
    cache = {}
    pairs = [(order, purchase_row(store, order, latest.get(order["id"]), cache)) for order in orders]
    pairs = [(order, row) for order, row in pairs if number.casefold() in order["name"].casefold()
             and (status == "Todos" or row["Procesamiento"] == status or
                  status == "Pendiente" and row["Pendientes"] > 0 or
                  status == "Error" and row["Procesamiento"] in {"Error", "Interrumpido"})]
    return {"orders": [order for order, _ in pairs], "rows": [row for _, row in pairs],
            "latest": latest, "filters": (config["supplier_id"], config["cutoff_date"], start, end, number, status),
            "queried": datetime.now(MADRID).timestamp()}


def render_order_list(engine, start, end, number="", status="Todos", refresh=False, access=None):
    stage = "configuracion"
    try:
        config = ConfigStore(data_root()).load()
        stage = "almacenamiento"
        store = service().store
        if refresh or "kering_orders_loaded" not in st.session_state:
            stage = "odoo"
            loaded = load_order_listing(engine, store, config, start, end, number, status)
            st.session_state["kering_orders_loaded"] = loaded
    except Exception as error:
        st.error(order_list_error(error, stage))
        return
    loaded = st.session_state["kering_orders_loaded"]
    active_filters = (config["supplier_id"], config["cutoff_date"], start, end, number, status)
    if active_filters != loaded["filters"]:
        st.warning("Filtros pendientes de aplicar. Los resultados corresponden a la ultima consulta.")
    st.caption(f"Ultima actualizacion: {format_timestamp(loaded['queried'])} | "
               f"Filtros aplicados: {loaded['filters'][2]:%d/%m/%Y} a {loaded['filters'][3]:%d/%m/%Y} · "
               f"{loaded['filters'][4] or 'Todos los pedidos'} · {loaded['filters'][5]}")
    orders, rows, latest = loaded["orders"], loaded["rows"], loaded["latest"]
    filtered_status = st.session_state.get("kering_indicator", "Todos")
    indicators = st.columns(4)
    counts = {"Todos": len(rows), "Procesados": sum(row["Procesamiento"] == "Procesado" for row in rows),
              "Pendientes": sum(row["Pendientes"] > 0 for row in rows),
              "Con error": sum(row["Procesamiento"] in {"Error", "Interrumpido"} for row in rows)}
    for column, (label, count) in zip(indicators, counts.items()):
        if column.button(f"{label}: {count}", key=f"kering_indicator_{label}"):
            st.session_state["kering_indicator"] = label
            filtered_status = label
    st.caption(f"Filtro activo: {filtered_status} (Todos restablece)")
    visible = [(order, row) for order, row in zip(orders, rows) if filtered_status == "Todos"
               or filtered_status == "Procesados" and row["Procesamiento"] == "Procesado"
               or filtered_status == "Pendientes" and row["Pendientes"] > 0
               or filtered_status == "Con error" and row["Procesamiento"] in {"Error", "Interrumpido"}]
    if not orders:
        st.info("Sin pedidos para este corte e intervalo.")
        return
    render_pilot_selection(config, orders)
    if not visible:
        st.info("Sin resultados para el indicador seleccionado.")
        return
    page_count = (len(visible) - 1) // 20 + 1
    page = st.number_input("Pagina de pedidos", min_value=1, max_value=page_count, value=1,
                           key=f"kering_orders_page_{filtered_status}_{page_count}")
    visible = visible[(page - 1) * 20:page * 20]
    remembered = set(st.session_state.get("kering_selected_ids", []))
    display = [{**{key: value for key, value in row.items() if key != "ID"},
                "Seleccionar": order["id"] in remembered} for order, row in visible]
    selection_key = "kering_selection_" + "_".join(str(order["id"]) for order, _ in visible)
    edited = st.data_editor(pd.DataFrame(display), hide_index=True, width="stretch",
                            disabled=[key for key in display[0] if key != "Seleccionar"],
                            key=selection_key)
    visible_ids = {order["id"] for order, _ in visible}
    selected_ids = (remembered - visible_ids) | {
        visible[index][0]["id"] for index, row in edited.iterrows() if row["Seleccionar"]}
    selected_ids &= {order["id"] for order in orders}
    st.session_state["kering_selected_ids"] = sorted(selected_ids)
    selected = [order for order in orders if order["id"] in selected_ids]
    allowed = set(config["test_order_ids"])
    busy = batch_busy(store)
    permitted = bool(selected) and (not config["test_mode"] or len(selected) <= 2 and selected_ids <= allowed)
    confirmed = bool(access and access["ok"])
    blocked = ("Seleccione pedidos" if not selected else "Lote activo" if busy else
               "Modo de prueba: autorice uno o dos pedidos" if not permitted else
               "Acceso no confirmado: pruebe acceso en Configuracion" if not confirmed else "")
    st.caption(blocked or "Procesar obtiene fotos; ZIP exporta las ya guardadas.")
    if st.button("Procesar seleccionados", icon=":material/download:", disabled=bool(blocked)):
        st.session_state["kering_run"] = submit_orders(engine, config, selected,
                                                       loaded["filters"][2], loaded["filters"][3])
        if st.session_state["kering_run"]:
            st.rerun()
    choices = {order["id"]: order for order, _ in visible}
    current_id = st.selectbox("Ver fotos del pedido", list(choices),
                             format_func=lambda value: choices[value]["name"], key="kering_open_order")
    current_order = choices.get(current_id)
    if current_order is not None:
        detail_id = current_order["id"]
        if st.button("Procesar pendientes", disabled=busy or not confirmed or config["test_mode"]
                     and detail_id not in allowed, icon=":material/download:"):
            st.session_state["kering_run"] = submit_orders(engine, config, [current_order],
                                                           loaded["filters"][2], loaded["filters"][3])
        render_current_order(store, current_order, latest.get(detail_id))


def order_list_error(error, stage):
    if stage == "odoo" and isinstance(error, OperationalError):
        return (
            "No se pudo acceder a PostgreSQL de Odoo. Compruebe que el servidor acepta conexiones "
            "y que el acceso de red y la configuracion de base de datos son correctos. "
            "No se han iniciado nuevas descargas. Codigo: ODOO_CONEXION."
        )
    messages = {
        "configuracion": "No se pudo leer la configuracion cifrada de Kering. Codigo: KERING_CONFIG.",
        "almacenamiento": "No se pudo abrir el almacenamiento Kering. Codigo: KERING_ALMACENAMIENTO.",
        "odoo": "No se pudieron leer los pedidos de Odoo. Codigo: ODOO_CONSULTA.",
        "historial": "No se pudo leer el historial Kering. Codigo: KERING_HISTORIAL.",
        "estado": "No se pudo calcular el estado actual de los pedidos. Codigo: KERING_ESTADO.",
    }
    return messages.get(stage, "No se pudo cargar el listado Kering. Codigo: KERING_LISTADO.")


def purchase_row(store, order, latest, cache):
    status = purchase_status(store, order, latest, cache)
    return {"Seleccionar": False, "ID": order["id"], "Pedido": order["name"],
            "Fecha": local_order_time(order["date_order"]).strftime(TIME_FORMAT),
            "Odoo": {"purchase": "Confirmado", "draft": "Borrador", "sent": "Enviado",
                     "done": "Cerrado", "to approve": "Por aprobar"}.get(order["state"], order["state"]),
            "Procesamiento": status["status"],
            "Completos": status["complete"], "Pendientes": status["pending"],
            "\u00daltimo procesamiento": format_timestamp(status["last_processed"]),
            "Cambios": status["changed"]}


def render_pilot_selection(config, orders):
    if not config["test_mode"]:
        return
    names = {order["id"]: order["name"] for order in orders}
    options = list(dict.fromkeys(list(names) + config["test_order_ids"]))
    ids = st.multiselect("Pedidos autorizados para prueba", options, default=config["test_order_ids"],
                         format_func=lambda value: names.get(value, f"ID {value}"), max_selections=2,
                         key=f"kering_pilot_{config['supplier_id']}_{config['cutoff_date']}")
    if st.button("Guardar selecci\u00f3n de prueba", icon=SAVE_ICON):
        config_store = ConfigStore(data_root())
        save_settings(config_store, {**config_store.load(), "test_order_ids": ids})
        st.rerun()


def render_current_order(store, order, latest):
    st.subheader(order["name"])
    status = purchase_status(store, order, latest)
    st.write(f"{status['status']} | {status['complete']} completos | {status['pending']} pendientes")
    render_order_zip(store, order, f"current-{order['id']}")
    results = json.loads(latest["results"]) if latest else {}
    products = {line["ean"]: line for line in order["lines"] if line["ean"]}
    if products:
        ean = st.selectbox("Producto del pedido", list(products),
                          format_func=lambda value: f"{products[value].get('product_name', '')} · {value}",
                          key=f"current-product-{order['id']}")
        render_product(store, ean, results.get(ean, {}), f"current-{order['id']}")
    if any(not line["ean"] for line in order["lines"]):
        st.warning("El pedido contiene productos sin EAN.")
    if latest and latest["error"]:
        st.warning(latest["error"])


def render_history(engine, config, store):
    st.button("Actualizar historial", icon=":material/refresh:")
    with st.expander("Ejecuciones"):
        st.dataframe(store.executions(), hide_index=True, width="stretch")
    rows = store.history()
    number = st.text_input("Numero de pedido")
    states = st.multiselect("Estado del intento", STATES)
    filter_dates = st.checkbox("Filtrar fechas del historial")
    if filter_dates:
        first, second = st.columns(2)
        order_from = first.date_input("Pedido desde", date.today() - timedelta(days=30))
        order_until = second.date_input("Pedido hasta", date.today())
        execution_from = first.date_input("Ejecucion desde", date.today() - timedelta(days=30))
        execution_until = second.date_input("Ejecucion hasta", date.today())
    dates = (order_from, order_until, execution_from, execution_until) if filter_dates else None
    filtered = filter_history(rows, number, states, dates)
    if not filtered:
        st.info("Sin intentos registrados para estos filtros.")
        return
    display = pd.DataFrame(filtered)[["number", "order_date", "status", "origin", "attempt", "user", "started", "ended", "error"]]
    display["order_date"] = display["order_date"].map(lambda value: local_order_time(value).strftime(TIME_FORMAT))
    for column in ("started", "ended"):
        display[column] = display[column].map(format_timestamp)
    st.dataframe(display, hide_index=True, width="stretch")
    selected = st.selectbox("Detalle de intento", range(len(filtered)),
                            format_func=lambda index: f"{filtered[index]['number']} | intento {filtered[index]['attempt']} | {filtered[index]['status']}")
    render_attempt(engine, config, store, filtered[selected])


def filter_history(rows, number, states, dates):
    filtered = []
    for row in rows:
        if number.casefold() not in row["number"].casefold() or (states and row["status"] not in states):
            continue
        if dates:
            order_from, order_until, execution_from, execution_until = dates
            order_day = local_order_time(row["order_date"]).date()
            execution_day = datetime.fromtimestamp(row["started"], MADRID).date()
            if not order_from <= order_day <= order_until or not execution_from <= execution_day <= execution_until:
                continue
        filtered.append(row)
    return filtered


def render_attempt(engine, config, store, row):
    order = json.loads(row["snapshot"])
    results = json.loads(row["results"])
    st.caption("Fotos disponibles actualmente para los EAN del snapshot; no es una copia historica de los archivos.")
    render_order_zip(store, order, f"history-{row['run_id']}-{row['order_id']}")
    st.write(f"Intervalo: {row['start_date']} a {row['end_date']} | Proveedor Odoo: {row['supplier']}")
    for line in order["lines"]:
        if not line["ean"]:
            st.warning(f"Linea {line['id']}: producto sin EAN")
    outcomes = [value for result in results.values() for value in result["views"].values()]
    st.write(f"Vistas requeridas descargadas: {outcomes.count('Descargada')} | "
             f"Reutilizadas: {outcomes.count('Reutilizada')} | Pendientes: {outcomes.count('Pendiente')}")
    acquisitions = [result["acquisition"] for result in results.values() if "acquisition" in result]
    if acquisitions:
        st.caption(f"Archivos validos: {sum(item['available_files'] for item in acquisitions)} | "
                   f"Descargados: {sum(item['downloaded_files'] for item in acquisitions)} | "
                   f"Reutilizados: {sum(item['reused_files'] for item in acquisitions)}")
    products = list(dict.fromkeys(line["ean"] for line in order["lines"] if line["ean"]))
    if products:
        ean = st.selectbox("Producto del intento", products, key=f"history-product-{row['run_id']}-{row['order_id']}")
        render_product(store, ean, results.get(ean, {}), f"history-{row['run_id']}-{row['order_id']}")
    if row["status"] not in {"Pendiente", "En proceso"}:
        if st.button("Reintentar pendientes y errores", icon=":material/replay:",
                     disabled=row["supplier"] != config["supplier_id"]):
            start = date.fromisoformat(row["start_date"])
            end = date.fromisoformat(row["end_date"])
            st.session_state["kering_run"] = submit_orders(engine, config, [order], start, end)
            if st.session_state["kering_run"]:
                st.success("Nuevo intento en segundo plano. Progreso disponible en Pedidos.")


def render_product(store, ean, result, prefix="product"):
    st.subheader(ean)
    valid = {record.view for record in store.repository.catalog_records(ean) if record.pixels}
    reason = result.get("reason", "")
    if reason == "vistas_sin_identificar":
        st.info("Fotos adquiridas; no hay senales suficientes para acreditar las vistas. Revision pendiente.")
    elif reason:
        st.warning("Quedan vistas pendientes. Consulte el diagnostico o revise las fotos.")
    view_review = result.get("view_review")
    if view_review:
        st.warning("Orden del visor Kering pendiente de revision: "
                   + ", ".join(view_review.get("reasons", [])).replace("_", " ")
                   + ". No se aplico la regla posicional.")
        for item in view_review.get("contradictions", []):
            st.caption(f"Posicion {item['viewer_position']}: {item['original_name']} indica "
                       f"{item['explicit_view']} (esperado {item['position_expected']}). Se conserva su vista.")
        if result.get("review_blocking"):
            st.caption("El EAN no se declarara completo hasta revisar manualmente cada imagen contradictoria.")
    acquisition = result.get("acquisition")
    if acquisition:
        st.caption(f"Archivos validos: {acquisition['available_files']} | "
                   f"Descargados: {acquisition['downloaded_files']} | Reutilizados: {acquisition['reused_files']}")
    if reason:
        with st.expander("Diagnostico tecnico"):
            st.code(reason)
    identity = result.get("identity")
    if identity:
        st.write(identity)
    for column, view in zip(st.columns(3), displayed_views(valid)):
        with column:
            if view not in valid:
                st.warning(f"{view}: pendiente")
    render_gallery(store.repository, ean, prefix)
    if result:
        st.write(result["views"])


def gallery_rows() -> list[dict]:
    return ImageRepository().catalog_rows()


def render_order_zip(store, order, key):
    if st.button("Actualizar fotos para ZIP", key=f"refresh-zip-{key}"):
        st.session_state.pop("kering_prepared_zip", None)
    products = {}
    catalog_records = []
    count = 0
    for ean in dict.fromkeys(line["ean"] for line in order["lines"] if line["ean"]):
        records = store.repository.catalog_records(ean)
        catalog_records.extend(records)
        count += len({(record.name, record.checksum) for record in records})
        views = {record.view for record in records if record.pixels}
        products[ean] = [view for view in VIEWS if view not in views]
    partial = any(products.values()) or any(not line["ean"] for line in order["lines"])
    st.caption(f"{count} fotos registradas; integridad y contenido se comprueban al preparar ZIP.")
    if partial:
        st.warning("Contenido parcial segun catalogo: faltan vistas requeridas o hay lineas sin EAN.")
        st.write({ean: missing for ean, missing in products.items() if missing})
    partial_allowed = st.checkbox("Permitir exportacion parcial", key=f"partial-{key}")
    cached = st.session_state.get("kering_prepared_zip")
    catalog_signature = json.dumps([
        order, [(r.id, r.ean, r.checksum, r.pixels, str(r.path), r.provider, r.origin,
                 r.view, r.market, r.name, r.date, r.metadata) for r in catalog_records]
    ], sort_keys=True)
    if cached and cached["order_id"] == order["id"] and cached["catalog_signature"] != catalog_signature:
        st.session_state.pop("kering_prepared_zip", None)
        cached = None
    if st.button(f"Preparar {count} fotos · ZIP", key=f"prepare-{key}",
                 disabled=not count or (partial and not partial_allowed), icon=":material/folder_zip:"):
        try:
            plan = cached_export_plan(store.repository, order)
            if plan.manifest["partial"] and not partial_allowed:
                st.warning("La verificacion detecto contenido parcial; permita exportacion parcial.")
                return
            with prepare_order_zip(store.repository, order, plan) as export:
                cached = {"signature": plan.signature, "bytes": export.stream.read(), "name": plan.filename,
                          "order_id": order["id"], "count": len(plan.files), "partial": plan.manifest["partial"],
                          "catalog_signature": catalog_signature}
            st.session_state["kering_prepared_zip"] = cached
            st.success("ZIP preparado con fotos actuales y manifiesto.")
        except (OSError, ValueError) as error:
            st.error(f"No se pudo preparar el ZIP: {error}")
    if cached and cached["order_id"] == order["id"] and (not cached["partial"] or partial_allowed):
        st.download_button(f"Descargar {cached['count']} fotos · ZIP", cached["bytes"],
                           file_name=cached["name"], mime="application/zip", key=f"download-{key}")


def cached_export_plan(repository, order):
    signature = repository_signature(repository, order)
    plans = st.session_state.setdefault("kering_export_plans", {})
    if signature not in plans:
        plan = order_export_plan(repository, order)
        if len(plans) >= 4:
            plans.pop(next(iter(plans)))
        plans[signature] = plan
    return plans[signature]


def thumbnail(repository, record, access, cache=None):
    return (cache or ThumbnailCache(repository)).get(record, access)


def original_for_display(repository, record):
    current = repository.catalog_records(record_id=record.id)
    if not current or current[0] != record:
        raise ValueError("Imagen eliminada o modificada en el catalogo")
    return repository.verified_bytes(record)


def visible_previews(repository, records, access):
    previews = {}
    try:
        with ThumbnailCache(repository).batch(records) as cache:
            for record in records:
                try:
                    previews[record.id] = thumbnail(repository, record, access[record.id], cache)
                except (OSError, ValueError, RuntimeError, SyntaxError, Image.DecompressionBombError) as error:
                    previews[record.id] = str(error)
    except (OSError, ValueError, RuntimeError) as error:
        previews = {record.id: str(error) for record in records}
    return previews


def visible_access(repository, records, key, *, refresh=False, selection=()):
    try:
        check_image_root(repository.root)
    except (OSError, ValueError, RuntimeError) as error:
        st.error(f"Repositorio no disponible: {error}")
        return {record.id: None for record in records}
    signature = (tuple((r.id, r.checksum, str(r.path), r.date) for r in records), selection)
    previous = st.session_state.get(key)
    if refresh or previous is None or previous["signature"] != signature:
        previous = {"signature": signature, "access": validate_visible(repository, records)}
        st.session_state[key] = previous
    return previous["access"]


def render_gallery(repository, ean, prefix="product"):
    refresh = st.button("Actualizar fotos", key=f"{prefix}-{ean}-refresh")
    if refresh:
        st.session_state.pop("kering_prepared_zip", None)
    records_key = f"{prefix}-{ean}-records"
    try:
        if refresh or records_key not in st.session_state:
            st.session_state[records_key] = repository.catalog_records(ean)
        records = st.session_state[records_key]
    except (OSError, ValueError, RuntimeError) as error:
        st.error(f"Repositorio no disponible: {error}")
        return
    origins = st.multiselect("Origen", sorted({r.provider for r in records}), key=f"{prefix}-{ean}-origins")
    views = st.multiselect("Vista", sorted({r.view for r in records}), key=f"{prefix}-{ean}-views")
    markets = st.multiselect("Mercado", sorted({r.market or "Original" for r in records}),
                             key=f"{prefix}-{ean}-markets")
    filtered = [r for r in records if (not origins or r.provider in origins)
                and (not views or r.view in views) and (not markets or (r.market or "Original") in markets)]
    covered = {r.view for r in records if r.pixels}
    missing = [view for view in VIEWS if view not in covered]
    st.caption(f"{len(filtered)} fotos | Vistas sin registrar: {', '.join(missing) or 'ninguna'}")
    if not filtered:
        st.info("Sin fotos para estos filtros.")
        return
    pages = (len(filtered) - 1) // 12 + 1
    page = st.number_input("Pagina de fotos", min_value=1, max_value=pages, value=1,
                           key=f"{prefix}-{ean}-page-{pages}")
    columns = st.columns(3)
    visible = filtered[(page - 1) * 12:page * 12]
    access = visible_access(repository, visible, f"{prefix}-{ean}-access", refresh=refresh,
                            selection=(tuple(origins), tuple(views), tuple(markets), page))
    previews = visible_previews(repository, visible, access)
    for index, record in enumerate(visible):
        with columns[index % 3]:
            preview = previews[record.id]
            if isinstance(preview, bytes):
                st.image(preview,
                         caption=f"{record.name} · {record.provider} · {record.view} · {record.market or 'Original'}",
                         width="stretch")
            else:
                st.warning(f"No se puede mostrar {record.name}: {preview}")
    selected = st.selectbox("Ampliar foto", filtered, format_func=lambda r: f"{r.name} · {r.provider} · {r.market}",
                            key=f"{prefix}-{ean}-enlarge")
    if st.button("Ampliar", key=f"{prefix}-{ean}-show"):
        try:
            st.image(original_for_display(repository, selected), caption=selected.name, width="stretch")
            st.caption(f"Origen: {selected.origin} | Vista: {selected.view} | Mercado: {selected.market or 'Original'}")
        except (OSError, ValueError):
            st.error("No se puede abrir esta imagen.")
    if selected.provider == "Kering":
        with st.expander("Revisar clasificacion de la vista (sin renombrar)"):
            st.json(selected.metadata.get("view_detection", {}))
            if selected.metadata.get("view_review"):
                st.json(selected.metadata["view_review"])
            review = st.selectbox("Vista comprobada", ["unknown", *VIEWS, "detalle"],
                                  key=f"{prefix}-{ean}-review-view")
            reason = st.text_input("Motivo y evidencia de la revision", key=f"{prefix}-{ean}-review-reason")
            if st.button("Confirmar revision", disabled=not reason.strip(), key=f"{prefix}-{ean}-review-save"):
                try:
                    repository.review_view(selected.id, review, reviewer=st.session_state.get("auth_user", "admin"),
                                           reason=reason)
                    st.session_state.pop(records_key, None)
                    st.success("Clasificacion revisada; archivo y nombre conservados. Historial registrado.")
                    st.rerun()
                except (OSError, ValueError):
                    st.error("No se pudo guardar la revision de la vista.")


def render_image_search(store):
    mode = st.radio("Buscar por", ["EAN exacto", "Pedido", "Producto / modelo"], horizontal=True)
    query = st.text_input("Busqueda de imagenes", key="kering_image_query").strip()
    if not query:
        st.info("Buscar y exportar usa solo fotos existentes; no accede al portal.")
        return
    if mode == "Pedido":
        loaded = st.session_state.get("kering_orders_loaded", {})
        orders = {order["id"]: order for order in loaded.get("orders", [])}
        for row in store.history():
            orders.setdefault(row["order_id"], json.loads(row["snapshot"]))
        matches = [order for order in orders.values() if query.casefold() in order["name"].casefold()
                   and local_order_time(order["date_order"]).date() >= date.fromisoformat(
                       ConfigStore(data_root()).load()["cutoff_date"])]
        if not matches:
            st.info("Sin pedidos cargados para ese numero y corte; use Actualizar pedidos.")
            return
        order = st.selectbox("Pedido encontrado", matches, format_func=lambda item: item["name"])
        render_order_zip(store, order, f"search-{order['id']}")
        eans = list(dict.fromkeys(line["ean"] for line in order["lines"] if line["ean"]))
        names = {line["ean"]: line.get("product_name", "") for line in order["lines"]}
    else:
        try:
            if mode == "EAN exacto" and query.isascii() and query.isdigit() and len(query) <= 20:
                records = store.repository.catalog_records(query)
            else:
                records = store.repository.catalog_records()
        except (OSError, ValueError, RuntimeError) as error:
            st.error(f"Repositorio no disponible: {error}")
            return
        eans = sorted({record.ean for record in records if record.ean == query}) \
            if mode == "EAN exacto" else sorted({record.ean for record in records
            if query.casefold() in str(record.metadata.get("modelo", "")).casefold() or
            query.casefold() in str(record.metadata.get("product_name", "")).casefold()})
        names = {}
    if not eans:
        st.info("Sin imagenes disponibles.")
        return
    ean = st.selectbox("Producto / EAN", eans, format_func=lambda value: f"{names.get(value, '')} · {value}")
    st.caption("La consulta directa del repositorio no restringe la antiguedad de las fotos.")
    render_gallery(store.repository, ean, "search")