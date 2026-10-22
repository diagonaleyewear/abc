import random
from dataclasses import replace

import pytest
from streamlit.testing.v1 import AppTest

import provider_images_ui as ui
from app_navigation import build_navigation
from image_repository import ImageRepository
from provider_images import ProviderService, ProviderStore
from provider_support import EANS, photo


def test_navigation_exposes_provider_page_only_to_full_role():
    class St:
        def navigation(self, groups, position):
            self.groups = groups

    keys = ("home", "master_import", "master_dictionary", "master_dryrun", "abc_home", "abc_search", "abc_reports",
            "abc_history", "abc_detail", "luxoptica_images", "luxoptica_pending", "kering_images", "alerts", "settings")
    pages = {k: k for k in keys}
    st = St()
    build_navigation(st, "admin", {**pages, "provider_images": "provider_images"})
    assert "provider_images" in st.groups["Repositorio de imágenes"]
    build_navigation(st, "masterdata", {**pages, "provider_images": "provider_images"})
    assert "provider_images" not in st.groups["Repositorio de imagenes"]


def test_request_rows_show_state_progress_and_errors(tmp_path):
    store = ProviderStore(tmp_path)
    connector = ui.build_connector("marcolin", None, root=tmp_path,
                                   repository=ImageRepository(tmp_path / "i", state=tmp_path / "r", backups=tmp_path / "b"))
    store.plan(connector.service.settings, EANS[:2])
    (row,) = ui.request_rows(store, "marcolin")
    assert row["Estado"] == "Lista" and row["EAN"] == 2 and row["Con imagen"] == 0


def _page():
    import provider_images_ui as page
    page.render_provider_page(lambda: None)


def test_page_requires_admin(monkeypatch, tmp_path):
    monkeypatch.setenv("PROVIDER_DATA_ROOT", str(tmp_path))
    app = AppTest.from_function(_page)
    app.session_state["auth_role"] = "masterdata"
    app.run()
    assert [e.value for e in app.error] == ["Acceso reservado a administradores."]


def test_page_renders_for_admin_without_contacting_portals(monkeypatch, tmp_path):
    monkeypatch.setenv("PROVIDER_DATA_ROOT", str(tmp_path / "p"))
    monkeypatch.setenv("IMAGE_REPOSITORY_ROOT", str(tmp_path / "img"))
    app = AppTest.from_function(_page)
    app.session_state["auth_role"] = "admin"
    app.run()
    assert not app.exception and app.title[0].value == ui.TITLE
    assert any("desactivada" in c.value for c in app.caption)
    assert [t.label for t in app.tabs] == ["Solicitudes", "Planificar por EAN", "Revisión", "Biblioteca"]


def test_library_records_show_provider_images_with_thumbnail_and_untouched_original(tmp_path, monkeypatch):
    import image_thumbnails as thumbnails
    from image_thumbnails import ThumbnailCache, validate_visible
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    for key in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.setenv(key, str(temporary))
    monkeypatch.setattr(thumbnails.tempfile, "gettempdir", lambda: str(temporary))
    monkeypatch.setenv("IMAGE_THUMBNAIL_CACHE_ROOT", str(tmp_path / "thumbnails"))
    monkeypatch.setenv("IMAGE_THUMBNAIL_CACHE_RESERVE_BYTES", "1")
    monkeypatch.setenv("IMAGE_REPOSITORY_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("IMAGE_REPOSITORY_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("KERING_DATA_ROOT", str(tmp_path / "kering"))
    monkeypatch.setenv("PROCESS_ACTIVITY_PATH", str(tmp_path / "receipts" / "activity.sqlite3"))
    repo = ImageRepository()
    content = photo()
    record, _ = repo.save(EANS[0], "a.jpg", content, provider="marcolin", origin="marcolin:1", view="V1")
    repo.save(EANS[1], "b.jpg", photo(), provider="kering", origin="kering:x", view="V1")
    mine = ui.library_records(repo, "marcolin")
    assert [r.ean for r in mine] == [EANS[0]]
    assert ui.library_records(repo, "marcolin", EANS[1]) == []
    access = validate_visible(repo, mine)
    assert isinstance(ThumbnailCache(repo).get(mine[0], access[mine[0].id]), bytes)
    assert record.path.read_bytes() == content


def test_page_plans_eans_without_odoo_or_portal(monkeypatch, tmp_path):
    monkeypatch.setenv("PROVIDER_DATA_ROOT", str(tmp_path))
    app = AppTest.from_function(_page)
    app.session_state["auth_role"] = "admin"
    app.run()
    assert not app.exception
    app.text_area(key="prov_manual_eans").set_value(f"{EANS[0]}, {EANS[1]} {EANS[1]} 123").run()
    app.button(key="prov_manual_plan").click().run()
    assert not app.exception
    (request,) = ui.ProviderStore(tmp_path).list_requests("marcolin")
    assert request["ean_count"] == 2