from __future__ import annotations

from typing import Any


def build_navigation(st_module: Any, auth_role: str | None, pages: dict[str, Any]):
    """Compose Streamlit navigation groups from already-created Page objects."""
    if auth_role == "masterdata":
        groups = {
            "Inicio": [pages["home"]],
            "Master Data": [pages["master_import"], pages["master_dictionary"], pages["master_dryrun"]],
            "Repositorio de imagenes": [
                pages.get("masterdata_kering_images", pages["kering_images"])
            ],
        }
    else:
        provider_pages = (
            [pages["provider_images"]] if "provider_images" in pages else []
        )
        groups = {
            "Inicio": [pages["home"]],
            "Análisis ABCD": [
                pages["abc_home"],
                pages["abc_search"],
                pages["abc_reports"],
                pages["abc_history"],
                pages["abc_detail"],
            ],
            "Master Data": [pages["master_import"], pages["master_dictionary"], pages["master_dryrun"]],
            "Repositorio de imágenes": [
                pages["luxoptica_images"],
                *provider_pages,
                pages["luxoptica_pending"],
                pages["kering_images"],
            ],
            "Alertas": [pages["alerts"]],
        }
        if "reconciliation" in pages:
            groups["Conciliación"] = [pages["reconciliation"]]
        if "price_comparator" in pages:
            groups["Análisis comercial"] = [pages["price_comparator"]]
        groups["Configuración"] = [pages["settings"]]
    return st_module.navigation(groups, position="sidebar")