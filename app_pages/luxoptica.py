from __future__ import annotations

from typing import Callable

import pandas as pd
import streamlit as st

from kering_images_ui import original_for_display, service, visible_access, visible_previews


def render_images_page(
    render_sidebar_shell: Callable[[str], None],
    require_admin_access: Callable[[], None],
    render_footer: Callable[[], None],
) -> None:
    render_sidebar_shell("Repositorio de imágenes")
    require_admin_access()
    st.title("Imágenes")
    st.caption("Explora las imágenes descargadas por modelo, EAN y mercado.")

    refresh = st.button("Actualizar imágenes", key="image_gallery_refresh")
    try:
        repository = service().store.repository
        if refresh or "image_gallery_records" not in st.session_state:
            st.session_state["image_gallery_records"] = repository.catalog_records()
        records = st.session_state["image_gallery_records"]
        rows = repository.catalog_rows(records)
    except (OSError, ValueError, RuntimeError) as error:
        st.error(f"Repositorio no disponible: {error}")
        return

    if refresh:
        st.session_state.pop("image_gallery_zip", None)
        st.session_state.pop("luxoptica_selected_image", None)
        st.session_state.pop("luxoptica_selected_bytes", None)

    image_catalog = pd.DataFrame(
        rows,
        columns=[
            "Modelo",
            "EAN",
            "Mercado",
            "Archivo",
            "Ruta",
            "Descargada",
            "Fecha",
            "Origen",
            "Proveedor",
            "Vista",
            "Checksum",
            "RecordId",
        ],
    ).sort_values("Descargada", ascending=False).reset_index(drop=True)

    st.subheader("Visor de imágenes")
    search_ean = st.text_input(
        "Buscar por EAN",
        placeholder="Introduce el EAN completo o una parte",
        key="luxoptica_search_ean",
    ).strip()
    market_filter = st.selectbox(
        "Mercado",
        options=["Todos", "Original", "Farfetch", "Miinto"],
        key="luxoptica_market_filter",
    )

    filtered_catalog = image_catalog
    if search_ean:
        filtered_catalog = filtered_catalog[
            filtered_catalog["EAN"].str.contains(search_ean, case=False, na=False)
        ]
    if market_filter != "Todos":
        filtered_catalog = filtered_catalog[filtered_catalog["Mercado"] == market_filter]

    if filtered_catalog.empty:
        st.info("No hay imágenes que coincidan con la búsqueda.")
    else:
        zip_key = (
            tuple(filtered_catalog["RecordId"]),
            tuple(filtered_catalog["Checksum"]),
            market_filter,
        )
        prepared = st.session_state.get("image_gallery_zip")
        if prepared and prepared["key"] != zip_key:
            st.session_state.pop("image_gallery_zip", None)
            prepared = None
        if st.button("Preparar ZIP de imágenes", key="image_repository_prepare_zip"):
            try:
                selected_zip = repository.zip_eans(
                    filtered_catalog["EAN"].unique().tolist(),
                    market=(
                        None if market_filter == "Todos"
                        else "" if market_filter == "Original"
                        else market_filter
                    ),
                )
                prepared = {"key": zip_key, "bytes": selected_zip}
                st.session_state["image_gallery_zip"] = prepared
            except (OSError, ValueError) as error:
                st.error(f"No se puede preparar el ZIP: {error}")
        if prepared:
            st.download_button(
                "ZIP de imágenes",
                prepared["bytes"],
                file_name="imagenes-ean.zip",
                mime="application/zip",
                key="image_repository_zip",
            )

        st.caption(
            f"{len(filtered_catalog):,} imágenes | "
            f"{filtered_catalog['EAN'].nunique():,} EAN | "
            f"{filtered_catalog['Modelo'].nunique():,} modelos"
        )
        page_size = 24
        total_pages = max(1, (len(filtered_catalog) + page_size - 1) // page_size)
        current_page = min(st.session_state.get("luxoptica_gallery_page", 1), total_pages)
        page_column, size_column = st.columns([3, 1])
        with page_column:
            current_page = st.number_input(
                "Página",
                min_value=1,
                max_value=total_pages,
                value=current_page,
                step=1,
                key="luxoptica_gallery_page_input",
            )
        with size_column:
            st.caption(f"{total_pages} página(s) de {page_size} miniaturas")
        st.session_state["luxoptica_gallery_page"] = int(current_page)

        start = (int(current_page) - 1) * page_size
        page_catalog = filtered_catalog.iloc[start : start + page_size]
        by_id = {record.id: record for record in records}
        visible = [by_id[int(row["RecordId"])] for _, row in page_catalog.iterrows()]
        access = visible_access(
            repository,
            visible,
            "image_gallery_access",
            refresh=refresh,
            selection=(search_ean, market_filter, current_page),
        )
        previews = visible_previews(repository, visible, access)
        thumbnail_columns = st.columns(4)
        for position, (row_index, image_row) in enumerate(page_catalog.iterrows()):
            with thumbnail_columns[position % 4]:
                record = by_id[int(image_row["RecordId"])]
                preview = previews[record.id]
                if isinstance(preview, bytes):
                    st.image(preview, width="stretch")
                else:
                    st.warning(f"No se puede mostrar {record.name}: {preview}")
                st.caption(
                    f"{image_row['EAN']} · {image_row['Mercado']}\n"
                    f"{image_row['Archivo']} · {image_row['Fecha']}"
                )
                if st.button(
                    "Ver imagen",
                    key=f"luxoptica_view_{row_index}",
                    width="stretch",
                    disabled=not isinstance(preview, bytes),
                ):
                    try:
                        st.session_state["luxoptica_selected_bytes"] = original_for_display(
                            repository,
                            record,
                        )
                        st.session_state["luxoptica_selected_image"] = str(image_row["Ruta"])
                        st.session_state["luxoptica_selected_checksum"] = record.checksum
                    except (OSError, ValueError) as error:
                        st.error(f"No se puede abrir la imagen: {error}")
                    st.rerun()

        selected_path = st.session_state.get("luxoptica_selected_image")
        if selected_path and selected_path in set(page_catalog["Ruta"]):
            selected_row = filtered_catalog[filtered_catalog["Ruta"] == selected_path].iloc[0]
            selected_record = by_id[int(selected_row["RecordId"])]
            if (
                not isinstance(previews[selected_record.id], bytes)
                or access[selected_record.id] is None
                or st.session_state.get("luxoptica_selected_checksum") != selected_record.checksum
            ):
                st.session_state.pop("luxoptica_selected_image", None)
                st.session_state.pop("luxoptica_selected_bytes", None)
                st.warning("Original cambiado o inaccesible; vuelva a abrir la imagen.")
                return
            st.markdown("#### Imagen ampliada")
            viewer_column, details_column = st.columns([2, 1])
            with viewer_column:
                st.image(
                    st.session_state["luxoptica_selected_bytes"],
                    caption=selected_row["Archivo"],
                    width="stretch",
                )
            with details_column:
                st.write(f"**Modelo:** {selected_row['Modelo']}")
                st.write(f"**EAN:** {selected_row['EAN']}")
                st.write(f"**Mercado:** {selected_row['Mercado']}")
                st.write(f"**Fecha:** {selected_row['Fecha']}")
                st.write(f"**Archivo:** {selected_row['Archivo']}")

        st.markdown("#### Estructura encontrada")
        st.dataframe(
            filtered_catalog[["Modelo", "EAN", "Mercado", "Archivo", "Fecha", "Proveedor", "Vista"]],
            hide_index=True,
            width="stretch",
        )

    render_footer()


def render_pending_page(
    render_sidebar_shell: Callable[[str], None],
    require_admin_access: Callable[[], None],
    render_footer: Callable[[], None],
    render_pending_panel: Callable[[], None],
) -> None:
    render_sidebar_shell("Repositorio de imágenes")
    require_admin_access()
    st.title("Pendiente Luxoptica")
    st.caption("Revisa y procesa manualmente las copias pendientes para Farfetch y Miinto.")
    render_pending_panel()
    render_footer()
