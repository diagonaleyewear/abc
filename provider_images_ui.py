"""Admin page for supplier image requests: status, progress, errors, retries and review (#59).

Never contacts portals from a render. Portal calls only happen in the scheduler/CLI, which
stays disabled until the authorised pilot; this page plans requests, imports ZIPs the
operator downloaded manually, reconciles ambiguous requests and resolves review items.
"""
from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path

import streamlit as st

import marcolin_images
import nuorder_images
import safilo_images
from image_repository import ImageRepository
from provider_images import (
    FAILED, NEEDS_REVIEW, PARTIAL, PROVIDERS, ProviderService, ProviderStore, load_settings,
    parse_ean_file, parse_ean_text,
)

from kering_images import AccessNotConfirmed
from provider_odoo_orders import (
    ELIGIBLE, OdooUnavailable, create_requests, demand_preview, load_links, pick_pilot, propose_partners, save_link,
)

TITLE = "Proveedores de imágenes"
MODULES = {"marcolin": marcolin_images, "nuorder": nuorder_images, "safilo": safilo_images}
CONNECTORS = {"marcolin": marcolin_images.MarcolinConnector, "nuorder": nuorder_images.NuorderConnector,
              "safilo": safilo_images.SafiloConnector}
STATE_LABELS = {
    "ready": "Lista", "submitting": "Enviando", "submitted": "Enviada", "pending": "Pendiente en portal",
    "available": "Disponible", "downloaded": "Descargada", "imported": "Importada", "partial": "Parcial",
    "failed": "Fallida", "needs_review": "Requiere revisión", "cancelled": "Cancelada",
}


def data_root() -> Path:
    return Path(os.getenv("PROVIDER_DATA_ROOT", str(Path(__file__).parent / "masterdata_data" / "providers")))


class LazyRepository:
    """Builds the real ImageRepository (which creates directories and tables) only when something needs it.

    Rendering the page, planning EANs and the Odoo preview never touch it.
    """

    def __init__(self, factory=ImageRepository):
        self._factory, self._repository = factory, None

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        if self._repository is None:
            self._repository = self._factory()
        return getattr(self._repository, name)


def build_connector(provider: str, engine=None, *, root: Path | None = None, repository=None):
    root = root or data_root()
    module = MODULES[provider]
    settings = load_settings(provider, root, module.DEFAULTS)
    if provider == "safilo":
        settings = safilo_images.strict_settings(settings)
    service = ProviderService(ProviderStore(root), repository or LazyRepository(), settings)
    return CONNECTORS[provider](service, engine)


def request_rows(store: ProviderStore, provider: str) -> list[dict]:
    rows = []
    for request in store.list_requests(provider):
        detail = store.get(request["id"])["eans"]
        done = sum(1 for e in detail if e["result"] == "imported")
        rows.append({
            "Solicitud": request["id"], "Estado": STATE_LABELS.get(request["state"], request["state"]),
            "Modo": request["mode"], "EAN": request["ean_count"], "Con imagen": done,
            "Intentos": request["attempts"], "Id portal": request["remote_id"] or "",
            "Error": request["last_error"] or "",
        })
    return rows


def _guard() -> None:
    if st.session_state.get("auth_role") != "admin":
        st.error("Acceso reservado a administradores.")
        st.stop()


def render_provider_page(engine_factory) -> None:
    _guard()
    st.title(TITLE)
    provider = st.radio("Proveedor", PROVIDERS, horizontal=True, format_func=str.capitalize)
    try:
        connector = build_connector(provider, None)
    except (OSError, ValueError, RuntimeError) as error:
        st.error(f"No se pudo abrir el almacén de solicitudes: {error}")
        return
    service, store = connector.service, connector.service.store
    flag = "activa" if service.settings.automation_enabled else "desactivada"
    st.caption(f"Automatización {flag}. Sin piloto autorizado, el estado debe ser «desactivada». "
               "La introducción directa de EAN no requiere Odoo.")
    requests_tab, detect_tab, review_tab, library_tab = st.tabs(
        ["Solicitudes", "Planificar por EAN", "Revisión", "Biblioteca"])
    with library_tab:
        _library(connector, provider)
    with requests_tab:
        rows = request_rows(store, provider)
        st.dataframe(rows, use_container_width=True, hide_index=True) if rows else st.info("Sin solicitudes.")
        if rows:
            _request_actions(connector, rows)
    with detect_tab:
        _detect(connector, engine_factory)
    with review_tab:
        _review(connector)


def export_caption(connector) -> str:
    if connector.list_separator == "\n":
        return "Lista para pegar en el portal: un código por línea, sin comas ni espacios."
    return "Lista de EAN para pegar en el portal (separada por comas)."


def _request_actions(connector, rows) -> None:
    store, service = connector.service.store, connector.service
    selected = st.selectbox("Solicitud", [r["Solicitud"] for r in rows], key="prov_request")
    request = store.get(selected)
    st.code(connector.export_ean_list(selected), language="text")
    st.caption(export_caption(connector))
    for event in store.events(selected):
        st.text(f"{event['from_state'] or '-'} → {event['to_state']}  {event['detail'] or ''}")
    upload = st.file_uploader("ZIP descargado del portal", type=["zip"], key=f"prov_zip_{selected}")
    if upload is not None and st.button("Importar ZIP", key=f"prov_import_{selected}"):
        try:
            result = connector.import_zip(selected, upload.getvalue())
            st.success(f"Importadas {result['imported']}, reutilizadas {result['reused']}, a revisión {result['review']}.")
        except (ValueError, KeyError) as error:
            st.error(f"ZIP rechazado: {error}")
    if request["state"] in {FAILED, PARTIAL} and st.button("Reintentar", key=f"prov_retry_{selected}"):
        new = store.retry(service.settings, selected)
        st.success(f"Nueva solicitud {new}") if new else st.warning("No se puede reintentar (límite o sin EAN).")
    if request["state"] == NEEDS_REVIEW:
        remote = st.text_input("Id de solicitud visto en el portal (vacío si no existe)", key=f"prov_remote_{selected}")
        if st.button("Reconciliar", key=f"prov_reconcile_{selected}"):
            try:
                store.reconcile(selected, remote_id=remote.strip() or None, sent=bool(remote.strip()))
                st.rerun()
            except ValueError as error:
                st.error(str(error))
    if request["state"] in {"ready", "needs_review"} and st.button("Cancelar", key=f"prov_cancel_{selected}"):
        store.cancel(selected)
        st.rerun()


def library_records(repository, provider: str, ean: str | None = None) -> list:
    return [r for r in repository.catalog_records(ean or None) if r.provider == provider]


def _library(connector, provider: str) -> None:
    repository = connector.service.repository
    ean = st.text_input("Filtrar por EAN (opcional)", key="prov_lib_ean").strip()
    if not st.button("Mostrar imágenes", key="prov_lib_show"):
        st.caption("Pulsa para cargar las miniaturas; no se consulta nada al renderizar.")
        return
    from app_pages.image_library_page import _visible_previews
    from image_thumbnails import validate_visible
    records = library_records(repository, provider, ean)[:60]
    if not records:
        st.info("Sin imágenes de este proveedor en la biblioteca común.")
        return
    try:
        previews = _visible_previews(repository, records, validate_visible(repository, records))
    except (OSError, ValueError, RuntimeError) as error:
        st.error(f"Biblioteca no accesible: {error}")
        return
    columns = st.columns(4)
    for index, record in enumerate(records):
        with columns[index % 4]:
            preview = previews.get(record.id)
            st.image(preview) if isinstance(preview, bytes) else st.warning(str(preview))
            st.caption(f"{record.ean} · {record.view} · {record.origin}")


def _detect(connector, engine_factory) -> None:
    st.subheader("Introducir EAN directamente")
    st.caption("Esta acción solo planifica: no consulta Odoo ni accede al portal.")
    if not connector.service.settings.batch_size_verified:
        st.warning(f"Tamaño máximo de lote ({connector.service.settings.max_eans_per_request}) "
                   "provisional: no verificado en el portal real.")
    upload = st.file_uploader("Importar lista (CSV/TXT)", type=["csv", "txt"], key="prov_ean_file")
    ean_text = st.text_area(
        "EAN (separados por comas, espacios o líneas)",
        key="prov_manual_eans",
        placeholder="Pega una lista de EAN...",
    )
    tokens = parse_ean_text(ean_text)
    if upload is not None:
        tokens += parse_ean_file(upload.getvalue())
    if tokens:
        preview = connector.preview_eans(tokens)
        st.info(f"{preview['total']} introducidos · {len(preview['valid'])} válidos · "
                f"{preview['duplicates']} duplicados · {len(preview['invalid'])} inválidos · "
                f"{len(preview['already_live'])} ya en curso · {len(preview['new'])} nuevos")
        if preview["invalid"]:
            st.warning("EAN inválidos: " + ", ".join(preview["invalid"][:25]))
    limit_manual = st.number_input("Máximo de EAN por planificación", 1, 5000, 50,
                                   key="prov_manual_limit")
    if st.button("Validar y crear solicitudes", key="prov_manual_plan"):
        if not tokens:
            st.warning("Introduce al menos un EAN.")
        else:
            result = connector.plan_eans(tokens, limit=int(limit_manual))
            st.success(
                f"Solicitudes creadas: {len(result['created'])}; "
                f"EAN ya en curso: {len(result['already_live'])}; "
                f"EAN inválidos: {len(result['invalid'])}."
            )
    st.divider()
    with st.expander("Secundario: consultar EAN desde Odoo (opcional)"):
        st.caption("Solo se ejecuta al pulsar el botón. Cloud funciona sin Odoo.")
        _detect_from_odoo(connector, engine_factory)


def _odoo_engine(engine_factory):
    return engine_factory() if callable(engine_factory) else engine_factory


def _detect_from_odoo(connector, engine_factory) -> None:
    """Optional, read-only. Odoo only supplies EAN candidates; it proves nothing about downloaded images."""
    provider = connector.service.settings.provider
    root = data_root()
    st.markdown("**Obtener EAN desde pedidos de compra de Odoo**")
    st.caption("Solo lectura. No envía nada a ningún portal; las solicitudes se crean con el planificador común.")
    _odoo_association(provider, root, engine_factory)
    today = date.today()
    start = st.date_input("Desde", today - timedelta(days=90), key=f"prov_from_{provider}")
    finish = st.date_input("Hasta", today, key=f"prov_to_{provider}")
    max_refs = st.number_input("Máximo de referencias a mostrar", 1, 500, 50, key=f"prov_limit_{provider}")
    rows_key = f"prov_odoo_rows_{provider}"
    if st.button("Consultar pedidos de compra", key=f"prov_odoo_query_{provider}"):
        try:
            preview = demand_preview(_odoo_engine(engine_factory), connector, root, start, finish,
                                     max_refs=int(max_refs))
        except AccessNotConfirmed as error:
            st.warning("Asociación proveedor Odoo ↔ portal sin confirmar: defínela y guárdala arriba "
                       f"({getattr(error, 'code', error)}).")
            return
        except (OdooUnavailable, ValueError) as error:
            st.error(f"Odoo no disponible o consulta rechazada: {getattr(error, 'code', error)}. "
                     "El resto de Cloud no se ve afectado.")
            return
        except Exception as error:  # configuration problems must not break the page
            st.error(f"No se pudo consultar Odoo: {error.__class__.__name__}")
            return
        st.session_state[rows_key] = preview
        st.session_state.pop(f"prov_odoo_picks_{provider}", None)
    preview = st.session_state.get(rows_key)
    if not preview:
        return
    note = f"{preview['lines_read']} líneas leídas · {preview['eligible']} elegibles · {preview['summary']}"
    if preview["truncated"] or preview["shown_truncated"]:
        note += " · resultado limitado: acota el período o sube el máximo"
    st.caption(note)
    catalog = preview.get("catalog") or {}
    if catalog.get("status") == "inaccesible":
        st.warning("Catálogo de imágenes inaccesible (" + str(catalog.get("blocker")) + "): la cobertura es "
                   "desconocida, no equivale a imágenes ausentes.")
    else:
        st.caption("Cobertura según metadatos del catálogo existente, abierto en solo lectura; "
                   "los archivos no se han verificado.")
    rows = preview["rows"]
    picks_key = f"prov_odoo_picks_{provider}"
    if st.button("Proponer lote piloto (5–10 EAN)", key=f"prov_odoo_pilot_{provider}"):
        st.session_state[picks_key] = pick_pilot(rows, 8)
    proposal = set(st.session_state.get(picks_key, ()))
    table = [{"Seleccionar": r["EAN"] in proposal and r["Estado"] == ELIGIBLE, **r} for r in rows]
    edited = st.data_editor(
        table, key=f"prov_odoo_editor_{provider}_{len(proposal)}", hide_index=True, use_container_width=True,
        disabled=[k for k in (table[0] if table else {}) if k != "Seleccionar"])
    chosen = [r["EAN"] for r in edited if r["Seleccionar"] and r["Estado"] == ELIGIBLE]
    st.write(f"Seleccionados: {len(chosen)}")
    approved = st.checkbox("Apruebo la selección definitiva", key=f"prov_odoo_ok_{provider}")
    if st.button("Crear solicitudes de imágenes para los EAN seleccionados", key=f"prov_odoo_create_{provider}",
                 disabled=not (chosen and approved)):
        who = st.session_state.get("auth_user") or st.session_state.get("auth_role") or "admin"
        result = create_requests(connector, root, rows, chosen, approved_by=str(who))
        st.success(f"Solicitudes creadas: {len(result['created'])}; EAN ya en curso: {len(result['already_live'])}. "
                   "No se ha contactado con ningún portal.")


def _odoo_association(provider: str, root: Path, engine_factory) -> None:
    links = load_links(root).get(provider) or {}
    if links.get("confirmed"):
        st.success(f"Asociación confirmada por {links.get('reviewer')}: proveedores Odoo {links['partner_ids']} → {provider}.")
    else:
        st.warning("Sin asociación confirmada entre proveedores de Odoo y este portal.")
    with st.expander("Asociación proveedor Odoo ↔ portal (revisar antes de guardar)"):
        fragment = st.text_input("Buscar proveedor en Odoo (nombre)", key=f"prov_assoc_q_{provider}")
        if st.button("Buscar candidatos", key=f"prov_assoc_find_{provider}"):
            try:
                st.session_state[f"prov_assoc_c_{provider}"] = propose_partners(_odoo_engine(engine_factory), fragment)
            except (OdooUnavailable, ValueError) as error:
                st.error(f"No se pudo buscar: {getattr(error, 'code', error)}")
            except Exception as error:
                st.error(f"No se pudo consultar Odoo: {error.__class__.__name__}")
        candidates = st.session_state.get(f"prov_assoc_c_{provider}", [])
        if candidates:
            st.caption("Candidatos por nombre: son solo propuestas, no se asocian solos.")
            st.dataframe(candidates, hide_index=True, use_container_width=True)
        ids_text = st.text_input("Ids de proveedor Odoo a asociar (separados por comas)",
                                 ", ".join(map(str, links.get("partner_ids", []))), key=f"prov_assoc_ids_{provider}")
        brands = st.text_input("Marcas/catálogos incluidos (informativo)", ", ".join(links.get("brands", [])),
                               key=f"prov_assoc_brands_{provider}")
        if st.button("Confirmar y guardar asociación", key=f"prov_assoc_save_{provider}"):
            who = st.session_state.get("auth_user") or st.session_state.get("auth_role") or "admin"
            try:
                save_link(root, provider, [int(p) for p in ids_text.replace(";", ",").split(",") if p.strip()],
                          reviewer=str(who), brands=brands.split(","))
                st.rerun()
            except ValueError as error:
                st.error(f"No se guardó: {error}")


def _review(connector) -> None:
    service = connector.service
    items = service.store.review_items(service.settings.provider)
    if not items:
        st.info("Nada pendiente de revisión.")
        return
    for item in items:
        with st.expander(f"{item['name']} — {item['reason']} (solicitud {item['request_id']})"):
            reviewer = st.session_state.get("auth_user") or st.session_state.get("auth_role") or "admin"
            reason = st.text_input("Motivo de la decisión", key=f"rv_reason_{item['id']}")
            views = ["frontal", "lateral", "perspectiva", "detalle"]
            view = st.selectbox("Vista", views, key=f"rv_view_{item['id']}")
            ean = item["ean"]
            if item["record_id"] is None:
                request = service.store.get(item["request_id"])
                ean = st.selectbox("EAN", [e["ean"] for e in request["eans"]], key=f"rv_ean_{item['id']}")
            accept, reject = st.columns(2)
            try:
                if accept.button("Aceptar", key=f"rv_ok_{item['id']}"):
                    service.resolve_review(item["id"], reviewer=str(reviewer), reason=reason, ean=ean, view=view)
                    st.rerun()
                if reject.button("Rechazar", key=f"rv_no_{item['id']}"):
                    service.resolve_review(item["id"], reviewer=str(reviewer), reason=reason, reject=True)
                    st.rerun()
            except (ValueError, OSError) as error:
                st.error(str(error))
