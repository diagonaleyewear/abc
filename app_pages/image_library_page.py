"""Biblioteca de imágenes: product-centred browsing of the common image repository.

Browsing reads only the local catalog and thumbnail cache. It never calls Odoo or
providers, never builds a ZIP during a render and never reads more than the visible
covers (grid) or the selected product (detail) from storage.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

import pandas as pd
import streamlit as st

import cloud_design as design
import image_library as library
from kering_images_ui import original_for_display, service, visible_access, visible_previews

TITLE = "Biblioteca de imágenes"
FILTER_KEYS = ("lib_query", "lib_provider", "lib_market", "lib_view", "lib_availability")
COLUMNS = 4
MIME = {".png": "image/png", ".webp": "image/webp", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


def _keep_widget_state() -> None:
    # Streamlit drops widget state when a page is left; re-assigning keeps filters across navigation.
    for key in (*FILTER_KEYS, "lib_size", "lib_page"):
        if key in st.session_state:
            st.session_state[key] = st.session_state[key]


def _clear_filters() -> None:
    for key in FILTER_KEYS:
        st.session_state.pop(key, None)
    st.session_state["lib_page"] = 1


def _goto(page: int) -> None:
    st.session_state["lib_page"] = page


def _open(ean: str) -> None:
    st.session_state["lib_ean"] = ean
    st.session_state.pop("lib_photo", None)


def _close() -> None:
    st.session_state.pop("lib_ean", None)
    st.session_state.pop("lib_photo", None)


def _load(repository, refresh: bool):
    if refresh or "image_gallery_records" not in st.session_state:
        st.session_state["image_gallery_records"] = repository.catalog_records()
        st.session_state.pop("image_gallery_groups", None)
        st.session_state.pop("lib_zip", None)
        st.session_state.pop("lib_original", None)
    if "image_gallery_groups" not in st.session_state:
        st.session_state["image_gallery_groups"] = library.group_records(
            st.session_state["image_gallery_records"]
        )
    return st.session_state["image_gallery_groups"]


def _stats(groups) -> tuple[tuple[str, str], ...]:
    counts = library.summary(groups)
    return (
        (f"{counts['images']:,}".replace(",", "."), "imágenes"),
        (f"{counts['eans']:,}".replace(",", "."), "EAN"),
        (f"{counts['models']:,}".replace(",", "."), "modelos"),
    )


def _header(groups, subtitle: str) -> bool:
    left, right = st.columns([6, 1], vertical_alignment="bottom")
    with left:
        design.render(design.page_header_html(TITLE, subtitle, _stats(groups)))
    with right:
        return st.button("Actualizar", icon=":material/refresh:", key="image_gallery_refresh",
                         help="Vuelve a leer el catálogo local de imágenes.")


def _read_filters(groups) -> library.LibraryFilters:
    options = library.filter_options(groups)
    for key, valid in (("lib_provider", options["providers"]), ("lib_market", options["markets"]),
                       ("lib_view", list(library.VIEW_FILTERS)),
                       ("lib_availability", [library.ALL, *library.AVAILABILITY_FILTERS[1:]])):
        if st.session_state.get(key) not in (None, *valid):
            st.session_state.pop(key)
    query = st.text_input(
        "Buscar", key="lib_query", icon=":material/search:",
        placeholder="EAN (completo o parcial), modelo, referencia, marca o proveedor",
    ).strip()
    columns = st.columns(5)
    with columns[0]:
        provider = st.selectbox("Proveedor", options["providers"], key="lib_provider")
    with columns[1]:
        market = st.selectbox("Mercado", options["markets"], key="lib_market")
    with columns[2]:
        view = st.selectbox("Vista", list(library.VIEW_FILTERS), key="lib_view")
    with columns[3]:
        availability = st.selectbox(
            "Fotografías", [library.ALL, *library.AVAILABILITY_FILTERS[1:]], key="lib_availability")
    with columns[4]:
        st.selectbox("Por página", library.PAGE_SIZES, index=1, key="lib_size")
    return library.LibraryFilters(query, provider, market, view, availability)


def _preview(previews, record) -> bytes | None:
    value = previews.get(record.id)
    return value if isinstance(value, bytes) else None


def _card_views(group) -> tuple[tuple[str, bool], ...]:
    return tuple((label, view in group.present_views) for view, label in library.CORE_VIEWS)


def _render_grid(repository, page, refresh: bool, signature: tuple) -> None:
    covers = [group.cover for group in page.items]
    access = visible_access(repository, covers, "image_gallery_access", refresh=refresh,
                            selection=(signature, page.number))
    previews = visible_previews(repository, covers, access)
    for start in range(0, len(page.items), COLUMNS):
        columns = st.columns(COLUMNS)
        for column, group in zip(columns, page.items[start:start + COLUMNS]):
            with column:
                design.render(design.product_card_html(
                    title=group.title, brand=group.brand or ", ".join(group.providers),
                    reference=group.reference if group.model else "", ean=group.ean,
                    count=len(group.records), views=_card_views(group),
                    preview=_preview(previews, group.cover),
                ))
                st.button("Ver fotografías", key=f"lib_open_{group.ean}", width="stretch",
                          on_click=_open, args=(group.ean,))


def _render_pager(page) -> None:
    previous, status, following = st.columns([1, 4, 1], vertical_alignment="center")
    with previous:
        st.button("Anterior", icon=":material/chevron_left:", key="lib_prev", width="stretch",
                  disabled=page.number <= 1, on_click=_goto, args=(page.number - 1,))
    with status:
        design.render(f'<p class="cd-pager" aria-live="polite">'
                      f'{design.pager_text(page.first, page.last, page.total, page.number, page.pages, "productos")}</p>')
    with following:
        st.button("Siguiente", icon=":material/chevron_right:", key="lib_next", width="stretch",
                  disabled=page.number >= page.pages, on_click=_goto, args=(page.number + 1,))


def _render_browser(repository, groups, refresh: bool) -> None:
    filters = _read_filters(groups)
    size = int(st.session_state.get("lib_size", library.PAGE_SIZES[1]))
    signature = (*filters.signature(), size)
    if st.session_state.get("lib_signature") != signature:
        st.session_state["lib_signature"] = signature
        st.session_state["lib_page"] = 1
    results = library.filter_groups(groups, filters)
    page = library.paginate(results, st.session_state.get("lib_page", 1), size)
    st.session_state["lib_page"] = page.number
    if not groups:
        design.render(design.empty_state_html(
            "La biblioteca aún no tiene imágenes",
            "Las fotografías aparecerán aquí cuando se descarguen o se importen desde los procesos de proveedor."))
        return
    if not results:
        design.render(design.empty_state_html(
            "Sin resultados", "Ningún producto coincide con la búsqueda y los filtros aplicados."))
        st.button("Quitar filtros", key="lib_clear", icon=":material/filter_alt_off:", on_click=_clear_filters)
        return
    if filters != library.LibraryFilters():
        counts = library.summary(results)
        st.caption(f"{page.total:,} productos · {counts['images']:,} imágenes con los filtros actuales".replace(",", "."))
    _render_grid(repository, page, refresh, signature)
    _render_pager(page)


def _photo_caption(record) -> str:
    parts = [library.view_name(record.view), library.market_label(record.market), record.provider]
    return " · ".join(part for part in parts if part)


def _original(repository, record) -> bytes:
    cached = st.session_state.get("lib_original")
    if cached and cached["id"] == record.id and cached["checksum"] == record.checksum:
        return cached["bytes"]
    content = original_for_display(repository, record)
    st.session_state["lib_original"] = {"id": record.id, "checksum": record.checksum, "bytes": content}
    return content


def _step(delta: int, records, current: int) -> None:
    st.session_state["lib_photo"] = records[(current + delta) % len(records)].id


def _select(record_id: int) -> None:
    st.session_state["lib_photo"] = record_id


def _render_viewer(repository, group, records, access, previews) -> None:
    ids = [record.id for record in records]
    selected_id = st.session_state.get("lib_photo")
    index = ids.index(selected_id) if selected_id in ids else 0
    record = records[index]
    image_column, info_column = st.columns([1, 1])
    with image_column:
        if access.get(record.id) is None or not isinstance(previews.get(record.id), bytes):
            st.warning(f"No se puede mostrar esta fotografía: {previews.get(record.id, 'original no accesible')}")
        else:
            try:
                st.image(_original(repository, record), width="stretch")
            except (OSError, ValueError) as error:
                st.error(f"No se puede abrir la imagen: {error}")
    with info_column:
        st.subheader(library.view_name(record.view))
        st.write(f"**Origen:** {record.provider or 'Sin dato'}")
        st.write(f"**Mercado:** {library.market_label(record.market)}")
        st.caption(f"Fotografía {index + 1} de {len(records)}")
        previous, following = st.columns(2)
        previous.button("Anterior", icon=":material/chevron_left:", key="lib_photo_prev", width="stretch",
                        disabled=len(records) < 2, on_click=_step, args=(-1, records, index))
        following.button("Siguiente", icon=":material/chevron_right:", key="lib_photo_next", width="stretch",
                         disabled=len(records) < 2, on_click=_step, args=(1, records, index))
        cached = st.session_state.get("lib_original")
        if cached and cached["id"] == record.id:
            st.download_button("Descargar imagen", cached["bytes"], file_name=record.name,
                               mime=MIME.get(Path(record.name).suffix.lower(), "application/octet-stream"),
                               icon=":material/download:", key="lib_download_one", width="stretch")


def _render_zip(repository, group) -> None:
    key = tuple((r.id, r.checksum) for r in group.records)
    prepared = st.session_state.get("lib_zip")
    if prepared and prepared["key"] != key:
        st.session_state.pop("lib_zip")
        prepared = None
    if st.button("Preparar ZIP del producto", icon=":material/folder_zip:", key="lib_prepare_zip",
                 help="Reúne todas las imágenes disponibles de este EAN. Solo se genera al pulsar."):
        try:
            content = repository.zip_eans_for_user([group.ean], role=st.session_state.get("auth_role"))
        except (OSError, ValueError, PermissionError) as error:
            st.error(f"No se puede preparar el ZIP: {error}")
        else:
            prepared = {"key": key, "bytes": content}
            st.session_state["lib_zip"] = prepared
    if prepared:
        st.download_button("Descargar ZIP", prepared["bytes"], file_name=f"{group.ean}.zip",
                           mime="application/zip", icon=":material/download:", key="lib_zip_download")


def _render_details(group, records) -> None:
    if st.session_state.get("auth_role") != "admin":
        return
    with st.expander("Detalles técnicos y trazabilidad"):
        frame = pd.DataFrame([{
            "Vista": library.view_name(r.view), "Archivo original": r.name, "Checksum": r.checksum,
            "Ruta": str(r.path), "Incorporada": r.date, "Origen": r.origin, "Proveedor": r.provider,
            "Mercado": library.market_label(r.market),
        } for r in records])
        st.dataframe(frame, hide_index=True, width="stretch")


def _render_product(repository, group) -> None:
    st.button("Volver a la biblioteca", icon=":material/arrow_back:", key="lib_back", on_click=_close)
    subtitle = " · ".join(part for part in (group.brand, f"EAN {group.ean}") if part)
    design.render(design.page_header_html(group.title, subtitle, (
        (str(len(group.records)), "imagen" if len(group.records) == 1 else "imágenes"),
        (str(len(group.providers)), "origen" if len(group.providers) == 1 else "orígenes"),
    )))
    if group.missing_core:
        design.render(design.notice_html(
            "Vistas sin fotografía: " + ", ".join(group.missing_core)
            + ". Las vistas no se deducen del aspecto de la imagen.", "warning"))
    else:
        design.render(design.notice_html("Disponibles las vistas V1, V2 y V3.", "success"))
    records = list(group.records)
    access = visible_access(repository, records, "image_gallery_detail_access", selection=group.ean)
    previews = visible_previews(repository, records, access)
    _render_viewer(repository, group, records, access, previews)
    st.subheader("Todas las fotografías")
    for start in range(0, len(records), 6):
        columns = st.columns(6)
        for column, record in zip(columns, records[start:start + 6]):
            with column:
                design.render(design.photo_html(_preview(previews, record), library.view_name(record.view)))
                design.render(design.caption_html(_photo_caption(record)))
                st.button("Ver", key=f"lib_photo_{record.id}", width="stretch",
                          on_click=_select, args=(record.id,))
    _render_zip(repository, group)
    _render_details(group, records)


def render_images_page(
    render_sidebar_shell: Callable[[str], None],
    require_admin_access: Callable[[], None],
    render_footer: Callable[[], None],
) -> None:
    render_sidebar_shell("Repositorio de imágenes")
    require_admin_access()
    design.inject_styles()
    _keep_widget_state()
    try:
        repository = service().store.repository
        refresh = bool(st.session_state.get("image_gallery_refresh"))
        groups = _load(repository, refresh)
    except (OSError, ValueError, RuntimeError) as error:
        design.render(design.page_header_html(TITLE))
        design.render(design.notice_html(f"Repositorio no disponible: {error}", "error"))
        return
    selected = next((g for g in groups if g.ean == st.session_state.get("lib_ean")), None)
    if st.session_state.get("lib_ean") and selected is None:
        _close()
    if selected is not None:
        _render_product(repository, selected)
    else:
        _header(groups, "Fotografías de producto por EAN, vistas y origen.")
        _render_browser(repository, groups, refresh)
    render_footer()
