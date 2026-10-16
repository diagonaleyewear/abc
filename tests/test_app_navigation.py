from app_navigation import build_navigation
from streamlit.testing.v1 import AppTest


class FakeStreamlit:
    def __init__(self) -> None:
        self.groups = None
        self.position = None

    def navigation(self, groups, position):
        self.groups = groups
        self.position = position
        return object()


def _pages() -> dict[str, str]:
    return {key: key for key in (
        "home",
        "master_import",
        "master_dictionary",
        "master_dryrun",
        "abc_home",
        "abc_search",
        "abc_reports",
        "abc_history",
        "abc_detail",
        "luxoptica_images",
        "luxoptica_pending",
        "kering_images",
        "alerts",
        "reconciliation",
        "settings",
    )}


def test_masterdata_navigation_exposes_only_authorized_areas() -> None:
    streamlit = FakeStreamlit()

    build_navigation(streamlit, "masterdata", _pages())

    assert set(streamlit.groups) == {"Inicio", "Master Data", "Repositorio de imagenes"}
    assert streamlit.position == "sidebar"
    assert streamlit.groups["Master Data"] == ["master_import", "master_dictionary", "master_dryrun"]
    assert streamlit.groups["Repositorio de imagenes"] == ["kering_images"]


def test_admin_navigation_keeps_all_existing_page_groups() -> None:
    streamlit = FakeStreamlit()

    build_navigation(streamlit, "admin", _pages())

    assert set(streamlit.groups) == {
        "Inicio",
        "Análisis ABC",
        "Master Data",
        "Repositorio de imágenes",
        "Alertas",
        "Conciliación",
        "Configuración",
    }


def test_streamlit_navigation_smoke_for_admin_and_masterdata() -> None:
    script = """
import streamlit as st
from app_navigation import build_navigation

def render_page():
    st.write("navigation smoke")

page_names = (
    "home", "master_import", "master_dictionary", "master_dryrun",
    "abc_home", "abc_search", "abc_reports", "abc_history", "abc_detail",
    "luxoptica_images", "luxoptica_pending", "kering_images", "alerts",
    "reconciliation", "settings",
)
pages = {
    name: st.Page(render_page, title=name, url_path=name)
    for name in page_names
}
navigation = build_navigation(st, ROLE, pages)
navigation.run()
"""
    for role in ("admin", "masterdata"):
        app = AppTest.from_string(script.replace("ROLE", repr(role))).run()
        assert not app.exception