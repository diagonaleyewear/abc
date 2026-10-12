"""Cloud home reads bounded persisted status only: no Odoo, providers or asset scans."""
from dataclasses import dataclass, field
import base64
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import time
from zoneinfo import ZoneInfo
from cryptography.fernet import InvalidToken

import pandas as pd
import streamlit as st

from build_info import render_build_footer
from kering_images import ConfigStore, data_root
from order_alerts import default_order_alert_store_path, scheduler_state, RESULT_LABELS
from process_activity import activity_path, read_state


MADRID = ZoneInfo("Europe/Madrid")
LOGO = Path(__file__).parent / "logo" / "logo.png"
JOURNAL_PROCESSES = {
    "luxoptica-monitor": ("Luxoptica · monitor automático", "luxpending"),
    "luxoptica-mail": ("Luxoptica · correo e imágenes", "luxpending"),
    "luxoptica-upload": ("Luxoptica · solicitudes", "master"),
    "nas-sync": ("Sincronización NAS", "images"),
    "abc-snapshot": ("Snapshot ABC semanal", "abchistory"),
}


@dataclass
class ProcessStatus:
    key: str
    name: str
    route: str
    state: str = "Sin información"
    schedule: str = "Sin información"
    result: str = "Sin información"
    last_run: float | None = None
    next_run: float | None = None
    last_success: float | None = None
    duration: float | None = None
    counts: dict = field(default_factory=dict)
    recent: list = field(default_factory=list)
    attention: str = ""
    diagnostic: str = ""


def timestamp(value):
    return datetime.fromtimestamp(value, MADRID).strftime("%d/%m/%Y %H:%M") if value is not None else "—"


def _execution_state(started, ended, result, heartbeat, now):
    if ended is None:
        return "En ejecución" if heartbeat is not None and now - heartbeat <= 300 else "Sin información"
    return result


def read_kering(now: float) -> ProcessStatus:
    status = ProcessStatus("kering", "Kering · imágenes de pedidos", "kering")
    root = data_root()
    if (root / "config.enc").is_file():
        config = ConfigStore(root).load()
        status.schedule = "Activada" if config["auto_enabled"] else "Programación desactivada"
    else:
        config = None
    database = root / "history.sqlite3"
    if not database.is_file():
        if config:
            status.state = status.result = "Sin ejecuciones"
        return status
    with read_state(database) as connection:
        runs = connection.execute("""SELECT r.*,
            (SELECT COUNT(*) FROM attempts a WHERE a.run_id=r.id) AS orders,
            (SELECT COUNT(*) FROM attempts a WHERE a.run_id=r.id AND a.status='Completo') AS complete,
            (SELECT COUNT(*) FROM attempts a WHERE a.run_id=r.id AND a.status='Parcial') AS partial,
            (SELECT COUNT(*) FROM attempts a WHERE a.run_id=r.id AND a.status IN ('Error','Interrumpido')) AS errors
            FROM runs r ORDER BY started DESC LIMIT 30""").fetchall()
        schedule = connection.execute("SELECT * FROM schedule_state WHERE id=1").fetchone()
        if not runs:
            status.state = status.result = "Sin ejecuciones"
            return status
        latest = runs[0]
        acquisition = connection.execute("""SELECT
            COALESCE(SUM(json_extract(j.value, '$.acquisition.downloaded_files')),0) AS downloaded,
            COALESCE(SUM(json_extract(j.value, '$.acquisition.reused_files')),0) AS reused,
            MAX(CASE WHEN json_extract(j.value, '$.reason')='vistas_sin_identificar'
                AND json_extract(j.value, '$.acquisition.available_files')>0 THEN 1 ELSE 0 END) AS unknown
            FROM attempts a, json_each(a.results) j WHERE a.run_id=?""", (latest["id"],)).fetchone()
    last_result = ("Error" if latest["errors"] or latest["status"] in {"Error", "Interrumpido"}
                   else "Parcial" if latest["partial"]
                   else "Correcto" if latest["ended"] is not None else "Sin información")
    classification = bool(acquisition["unknown"])
    if last_result == "Parcial" and classification:
        last_result = "Parcial — clasificación pendiente"
    status.result = last_result if latest["ended"] is not None else "Sin información"
    status.state = _execution_state(latest["started"], latest["ended"], last_result, latest["heartbeat"], now)
    status.last_run = latest["ended"] or latest["started"]
    status.duration = max(0, (latest["ended"] or now) - latest["started"])
    status.counts = {"Pedidos": latest["orders"], "Completos": latest["complete"],
                     "Parciales": latest["partial"], "Errores": latest["errors"],
                     "Fotos descargadas": acquisition["downloaded"], "Fotos reutilizadas": acquisition["reused"]}
    status.next_run = schedule["next_run"] if schedule and config and config["auto_enabled"] else None
    for run in runs:
        result = ("Error" if run["errors"] or run["status"] in {"Error", "Interrumpido"}
                  else "Parcial" if run["partial"] else "Correcto" if run["ended"] is not None else "En ejecución")
        status.recent.append({"process": status.name, "time": run["ended"] or run["started"], "result": result})
    # Query all history for the actual most recent success, not merely the displayed page.
    with read_state(database) as connection:
        success = connection.execute("""SELECT MAX(ended) FROM runs r WHERE ended IS NOT NULL
            AND status='Finalizado' AND NOT EXISTS (
                SELECT 1 FROM attempts a WHERE a.run_id=r.id AND a.status!='Completo')""").fetchone()[0]
    status.last_success = success
    if status.state != "En ejecución" and status.state not in {"Correcto", "Sin ejecuciones"}:
        status.attention = ("Revisar fotos sin clasificación; V1/V2/V3 siguen pendientes." if classification
                            else "Revisar resultados o latido del lote en Kering.")
    return status


def read_alerts(now: float) -> ProcessStatus:
    status = ProcessStatus("alerts", "Alertas · pedidos vigilados", "alerts")
    path = default_order_alert_store_path()
    if not path.is_file():
        return status
    with read_state(path) as connection:
        rows = connection.execute("SELECT * FROM order_alert_status ORDER BY last_check_at DESC").fetchall()
    if not rows:
        status.state = status.result = "Sin ejecuciones"
        return status
    automatic = next((dict(row) for row in rows if row["source"] == "auto"), None)
    service = scheduler_state(automatic, now=int(now))
    status.schedule = ("Activada" if service == "running" else "Programación desactivada" if service == "stopped"
                       else "Sin información")
    latest = next((row for row in rows if row["last_check_at"]), None)
    if latest is None:
        status.state = status.result = "Sin ejecuciones"
    else:
        code = latest["last_check_result"]
        status.result = "Error" if code.startswith("error") or code == "reintentos_agotados" else (
            "Correcto" if code in RESULT_LABELS else "Sin información")
        status.state = status.result
        status.last_run = latest["last_check_at"]
        if code == "enviado":
            status.counts = {"Alertas enviadas": latest["last_sent_count"] or 0}
        status.recent = [{"process": status.name, "time": row["last_check_at"],
                          "result": RESULT_LABELS.get(row["last_check_result"], "Sin información")}
                         for row in rows if row["last_check_at"]]
    success_times = [row["last_sent_at"] for row in rows if row["last_sent_at"]]
    success_times += [row["last_check_at"] for row in rows if row["last_check_result"] in {
        "sin_clientes", "sin_pedidos", "ya_notificados", "enviado"} and row["last_check_at"]]
    status.last_success = max(success_times, default=None)
    # Heartbeat proves scheduler life, not an active Odoo/email operation.
    # Heartbeat is not the completion time. Only show a due time while still in the future.
    due = automatic["heartbeat_at"] + automatic["interval_seconds"] if service == "running" else None
    status.next_run = due if due and due >= now else None
    if status.result == "Error":
        status.attention = "Revisar la última comprobación en Alertas."
    if service == "stale":
        status.attention = "Sin latido reciente del programador; revisar el servicio de alertas."
    return status


def read_journal(key: str, now: float) -> ProcessStatus:
    name, route = JOURNAL_PROCESSES[key]
    status = ProcessStatus(key, name, route)
    path = activity_path()
    if not path.is_file():
        return status
    with read_state(path) as connection:
        rows = connection.execute("SELECT * FROM process_runs WHERE process=? ORDER BY started DESC LIMIT 20",
                                  (key,)).fetchall()
        success = connection.execute("SELECT MAX(ended) FROM process_runs WHERE process=? AND result='Correcto'",
                                     (key,)).fetchone()[0]
        active = connection.execute("SELECT MAX(heartbeat) FROM process_runs WHERE process=? AND ended IS NULL",
                                    (key,)).fetchone()[0]
    if not rows:
        # Journal exists for other processes: this process has no receipts yet.
        return status
    latest = rows[0]
    status.last_run = latest["ended"] or latest["started"]
    status.result = latest["result"] or "Sin información"
    status.state = _execution_state(latest["started"], latest["ended"], status.result, latest["heartbeat"], now)
    if active is not None and 0 <= now - active <= 300:
        status.state = "En ejecución"
    status.schedule = ("Activada (último registro)" if latest["enabled"] == 1 else "Programación desactivada"
                       if latest["enabled"] == 0 else "Manual / programación no registrada")
    status.next_run = latest["next_run"] if latest["next_run"] and latest["next_run"] >= now else None
    status.last_success = success
    status.duration = max(0, latest["ended"] - latest["started"]) if latest["ended"] else None
    status.counts = json.loads(latest["counts"])
    status.recent = [{"process": name, "time": row["ended"] or row["started"],
                      "result": row["result"] or "Ejecución sin finalización registrada"} for row in rows]
    if status.result in {"Error", "Parcial"} or (latest["ended"] is None and status.state != "En ejecución"):
        status.attention = "Revisar el último recibo del proceso; no implica progreso en vivo."
    status.diagnostic = latest["error_code"] or ""
    return status


def dashboard_snapshot(role: str, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    if role not in {"admin", "masterdata"}:
        raise PermissionError("Inicio reservado a usuarios autenticados")
    readers = [("kering", read_kering), ("alerts", read_alerts)] if role == "admin" else []
    keys = list(JOURNAL_PROCESSES) if role == "admin" else ["luxoptica-upload"]
    readers += [(key, lambda instant, key=key: read_journal(key, instant)) for key in keys]
    processes = []
    for key, reader in readers:
        try:
            processes.append(reader(now))
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, InvalidToken) as error:
            name, route = JOURNAL_PROCESSES.get(key, (
                "Kering · imágenes de pedidos", "kering") if key == "kering" else (
                "Alertas · pedidos vigilados", "alerts"))
            processes.append(ProcessStatus(key, name, route,
                attention="No se pudo leer el estado persistido. Revise configuración y almacenamiento.",
                diagnostic=type(error).__name__))
    return {"queried": now, "role": role, "processes": processes}


def dashboard_transition(state, route):
    if state.get("cloud_home_route") != route:
        state["cloud_home_route"] = route
        if route == "":
            state.pop("cloud_home_snapshot", None)


def render_dashboard(pages: dict, loader=dashboard_snapshot):
    role = st.session_state.get("auth_role")
    if role not in {"admin", "masterdata"}:
        st.error("Acceso reservado a usuarios autenticados.")
        return
    header = st.columns([2, 5])
    with header[0]:
        if LOGO.is_file():
            logo = base64.b64encode(LOGO.read_bytes()).decode("ascii")
            st.html(f'<img src="data:image/png;base64,{logo}" alt="Óptica Diagonal" '
                    'style="width:240px;max-width:100%;height:auto;border:0;display:block">')
        else:
            st.caption("Logo Diagonal pendiente: logo/logo.png")
    with header[1]:
        st.title("Cloud")
        st.caption("Catálogo, imágenes y automatizaciones")
    refresh = st.button("Actualizar", icon=":material/refresh:", type="primary")
    snapshot = st.session_state.get("cloud_home_snapshot")
    if refresh or not snapshot or snapshot["role"] != role:
        snapshot = loader(role)
        st.session_state["cloud_home_snapshot"] = snapshot
    st.caption(f"Última consulta: {timestamp(snapshot['queried'])} · Estado persistido; sin acceso a proveedores.")
    processes = snapshot["processes"]
    running, attention, success = st.columns(3)
    running.metric("En ejecución", sum(p.state == "En ejecución" for p in processes))
    attention.metric("Pendientes de atención", sum(bool(p.attention) for p in processes))
    correct = max((p.last_success for p in processes if p.last_success is not None), default=None)
    success.metric("Última correcta", timestamp(correct).split()[0] if correct else "Sin información")
    if correct:
        success.caption(timestamp(correct))
    st.subheader("Procesos")
    st.dataframe(pd.DataFrame([{
        "Proceso": p.name, "Estado": p.state, "Programación": p.schedule,
        "Última ejecución": timestamp(p.last_run), "Próxima ejecución": timestamp(p.next_run),
        "Resultado": p.result, "Duración": f"{p.duration:.1f} s" if p.duration is not None else "Sin información",
        "Cantidades": " · ".join(f"{key}: {value}" for key, value in p.counts.items()) or "Sin información",
    } for p in processes]), hide_index=True, width="stretch")
    st.caption("Programación y resultado son independientes. Sin información no equivale a correcto ni a desactivado.")
    selected = st.selectbox("Ver detalle del proceso", processes, format_func=lambda p: p.name)
    if selected:
        with st.expander("Ver detalle", expanded=True):
            st.write(f"{selected.state} · {selected.schedule}")
            if selected.attention:
                st.warning(selected.attention)
            if selected.diagnostic:
                with st.expander("Diagnóstico técnico"):
                    st.code(selected.diagnostic)
            if selected.route in pages:
                st.page_link(pages[selected.route], label="Ver detalle en su pantalla", icon=":material/open_in_new:")
        with st.expander("Ver historial"):
            if selected.recent:
                st.dataframe([{"Proceso": row["process"], "Fecha": timestamp(row["time"]),
                               "Resultado": row["result"]} for row in selected.recent], hide_index=True)
            else:
                st.info("Sin información histórica persistida.")
            if selected.route in pages:
                st.page_link(pages[selected.route], label="Ver historial en su pantalla", icon=":material/history:")
    st.subheader("Pendientes de atención")
    pending = [p for p in processes if p.attention]
    for process in pending:
        st.warning(f"{process.name}: {process.attention}")
        if process.route in pages:
            st.page_link(pages[process.route], label=f"Revisar {process.name}")
    if not pending:
        st.info("Sin incidencias registradas en las fuentes disponibles; los procesos sin información no están verificados.")
    st.subheader("Actividad reciente")
    recent = sorted([row for p in processes for row in p.recent], key=lambda row: row["time"], reverse=True)[:10]
    if recent:
        st.dataframe([{"Proceso": row["process"], "Fecha": timestamp(row["time"]), "Resultado": row["result"]}
                      for row in recent], hide_index=True, width="stretch")
    else:
        st.info("Sin información de actividad reciente.")
    st.subheader("Accesos rápidos")
    quick = [("images", "Buscar imágenes"), ("kering", "Pedidos Kering"), ("config", "Configuración")] \
        if role == "admin" else [("master", "Master Data"), ("dictionary", "Diccionario")]
    for column, (route, label) in zip(st.columns(len(quick)), quick):
        if route in pages:
            with column:
                st.page_link(pages[route], label=label, use_container_width=True)
    render_build_footer()
