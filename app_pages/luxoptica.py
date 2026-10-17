from __future__ import annotations

from typing import Callable

import streamlit as st

from app_pages.image_library_page import render_images_page  # noqa: F401  (re-exported route)


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
