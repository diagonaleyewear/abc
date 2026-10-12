from datetime import date
from dataclasses import replace
from io import BytesIO
import json
from unittest.mock import Mock
from zipfile import ZipFile

from cryptography.fernet import Fernet
from PIL import Image
import pytest
from streamlit.testing.v1 import AppTest

from image_exports import order_export_plan, prepare_order_zip, repository_signature
from image_naming import classified_view, kering_filename
from image_repository import ImageRepository
from kering_images import ConfigStore, ImageStore, VIEWS, process_ean, order_status
from kering_media import identify_media
from kering_portal import KeringPortal
import kering_images_ui as ui


EAN = "0012345678901"


def photo(index):
    stream = BytesIO()
    Image.new("RGB", (600, 600), (index * 20, 50, 70)).save(stream, "PNG")
    return stream.getvalue()


def order(eans=(EAN,), number=1):
    return {"id": number, "name": f"PO/{number}", "supplier_id": 7, "state": "purchase",
            "date_order": "2026-10-09 12:00:00",
            "lines": [{"id": index, "ean": ean, "product_id": index, "product_name": "Modelo simulado"}
                      for index, ean in enumerate(eans)]}


@pytest.fixture(autouse=True)
def offline(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAGE_REPOSITORY_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("KERING_DATA_ROOT", str(tmp_path / "kering"))
    monkeypatch.setenv("KERING_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("KERING_ENCRYPTION_KEY_FILE", raising=False)
    monkeypatch.setattr("kering_portal.sync_playwright", Mock(side_effect=AssertionError("No portal in UI")))
    service = ui.service
    service.clear()
    config_store = ConfigStore(tmp_path / "kering")
    config_store.save({**config_store.load(), "supplier_id": 7, "cutoff_date": "2026-09-01"})
    yield
    service.clear()


@pytest.mark.parametrize("view", VIEWS)
def test_shared_renaming_requires_original_evidence(view):
    name = kering_filename("MODEL", "001", view, ".png", "opaque.png")
    result = identify_media({"url": f"https://picture.kecdn.net/{name}"})
    assert result["normalized_view"] == classified_view(name) == view
    assert result["rule"] == "luxoptica_original_filename_v1"
    assert result["signals"]["url_filename"] == name


def test_unknown_contradictory_and_nonview_labels():
    assert identify_media({"url": "https://picture.kecdn.net/opaque.png",
                           "reference": "MODEL-001", "title": "MODEL-001"})["normalized_view"] == "unknown"
    result = identify_media({"url": "https://picture.kecdn.net/M__001__noshad__fr.png",
                             "original_name": "M__001__shad__lt.png"})
    assert result["normalized_view"] == "unknown"
    assert result["rule"] == "conflicting_signals"
    assert identify_media({"url": "https://picture.kecdn.net/opaque.png",
                           "title": "front"})["normalized_view"] == "unknown"


def portal_with_images(monkeypatch, names):
    portal = KeringPortal({})
    snapshot = {"ean": EAN, "reference": "MODEL-001",
                "images": [{"url": f"https://picture.kecdn.net/{name}", "reference": "MODEL-001"}
                           for name in names]}
    monkeypatch.setattr(portal, "_start", lambda: None)
    monkeypatch.setattr(portal, "_find_product", lambda ean: snapshot)
    reader = Mock(side_effect=lambda url: photo(names.index(url.rsplit("/", 1)[1]) + 1))
    monkeypatch.setattr(portal, "_read_image", reader)
    return portal, reader


def test_acquire_more_than_three_reordered_and_reuse_unknown(monkeypatch, tmp_path):
    names = ["opaque.png", "M__001__shad__lt.png", "editorial.png",
             "M__001__noshad__qt.png", "M__001__noshad__fr.png", "other__001__noshad__fr.png"]
    portal, reader = portal_with_images(monkeypatch, names)
    store = ImageStore(tmp_path / "kering")
    result = process_ean(store, portal, EAN)
    assert set(store.valid_views(EAN)) == set(VIEWS)
    assert "Pendiente" not in result["views"].values()
    assert len(store.repository.records(EAN)) == 6
    assert reader.call_count == 6
    process_ean(store, portal, EAN)
    assert reader.call_count == 6
    # A missing canonical file triggers discovery, not redownload of five valid URLs.
    store.path(EAN, "lateral").unlink()
    process_ean(store, portal, EAN)
    assert reader.call_count == 7
    assert len(store.repository.records(EAN)) == 6
    assert all(record.metadata["view_detection"]["original_name"] for record in store.repository.records(EAN))


def test_many_same_views_and_unknown_do_not_complete(monkeypatch, tmp_path):
    names = ["M__001__noshad__fr.png", "other__001__noshad__fr.png", "a.png", "b.png"]
    portal, reader = portal_with_images(monkeypatch, names)
    store = ImageStore(tmp_path / "kering")
    result = process_ean(store, portal, EAN)
    assert result["views"]["perspectiva"] == result["views"]["lateral"] == "Pendiente"
    assert len(store.repository.records(EAN)) == 4
    process_ean(store, portal, EAN)
    assert reader.call_count == 4
    assert len(store.valid_views(EAN)) == 1


def test_successful_unknown_acquisition_is_partial_not_download_error(monkeypatch, tmp_path):
    portal, reader = portal_with_images(monkeypatch, ["opaque-a", "opaque-b", "opaque-c"])
    store = ImageStore(tmp_path / "kering")
    first = process_ean(store, portal, EAN)
    assert order_status(order(), {EAN: first}) == "Parcial"
    assert first["acquisition"] == {"available_files": 3, "downloaded_files": 3, "reused_files": 0}
    assert first["reason"] == "vistas_sin_identificar"
    assert set(first["views"].values()) == {"Pendiente"}
    second = process_ean(store, portal, EAN)
    assert order_status(order(), {EAN: second}) == "Parcial"
    assert second["acquisition"] == {"available_files": 3, "downloaded_files": 0, "reused_files": 3}
    assert reader.call_count == 3
    # Files cannot count as acquired once absent or corrupt.
    for record in store.repository.records(EAN):
        record.path.unlink()
    portal._read_image = Mock(return_value=None)
    assert order_status(order(), {EAN: process_ean(store, portal, EAN)}) == "Error"


def test_identical_pixels_cannot_accredit_three_views(monkeypatch, tmp_path):
    names = [kering_filename("M", "001", view, ".png", "") for view in VIEWS]
    portal, reader = portal_with_images(monkeypatch, names)
    reader.side_effect = None
    reader.return_value = photo(1)
    result = process_ean(ImageStore(tmp_path / "kering"), portal, EAN)
    assert list(result["views"].values()).count("Pendiente") == 2


def test_shared_original_and_reclassification_need_no_transfer(monkeypatch, tmp_path):
    name = "M__001__noshad__fr.png"
    portal, reader = portal_with_images(monkeypatch, [name, "opaque.png"])
    store = ImageStore(tmp_path / "kering")
    store.repository.save(EAN, name, photo(1), provider="Luxoptica", origin="archive", view="frontal")
    original, _ = store.repository.save(EAN, "opaque.png", photo(2), provider="Kering",
                                       origin="https://picture.kecdn.net/opaque.png", view="unknown")
    process_ean(store, portal, EAN)
    reader.assert_not_called()
    assert len(store.repository.records(EAN)) == 3
    # Reclassify an existing original from newly provided filename evidence.
    portal._download({"ean": EAN, "reference": "MODEL-001", "images": [{
        "url": "https://picture.kecdn.net/opaque.png", "reference": "MODEL-001",
        "original_name": "M__001__shad__lt.png"}]}, ("lateral",), {})
    reader.assert_not_called()
    assert "lateral" in store.valid_views(EAN)
    assert next(r for r in store.repository.records(EAN) if r.id == original.id).name == "opaque.png"


def test_review_preserves_files_history_and_reuse(tmp_path):
    repository = ImageRepository()
    record, _ = repository.save(EAN, "opaque.png", photo(1), provider="Kering", origin="url", view="unknown")
    before = repository_signature(repository, order())
    repository.review_view(record.id, "frontal", reviewer="operator", reason="Manually checked original")
    revised = repository.records(EAN)[0]
    assert revised.name == "opaque.png" and revised.path == record.path
    assert revised.path.read_bytes() == photo(1)
    assert repository.valid_views(EAN).keys() == {"frontal"}
    assert repository_signature(repository, order()) != before
    repository.review_view(record.id, "unknown", reviewer="operator", reason="Ambiguous")
    with repository.connect() as connection:
        events = connection.execute("SELECT details FROM events WHERE action='view_review'").fetchall()
    assert len(events) == 2
    assert json.loads(events[1][0])["previous_view"] == "frontal"


def test_zip_all_origins_markets_collisions_repeated_ean_and_invalid(tmp_path):
    repository = ImageRepository()
    for index, view in enumerate(VIEWS):
        repository.save(EAN, f"{view}.img", photo(index + 1), provider="Luxoptica", origin="archive", view=view)
    repository.save(EAN, "extra.png", photo(4), provider="Kering", origin="portal")
    repository.save(EAN, "extra.png", photo(5), provider="Other", origin="other")
    repository.save(EAN, "market_V1.jpeg", photo(1), provider="Luxoptica", origin="market",
                    view="frontal", market="Miinto")
    bad, _ = repository.save(EAN, "bad.png", photo(6), provider="Other", origin="bad")
    bad.path.write_bytes(b"invalid")
    current = order((EAN, EAN, "00999", ""))
    plan = order_export_plan(repository, current)
    assert plan.filename == "PO_1.zip" and plan.manifest["partial"]
    assert len(plan.files) == 6
    assert plan.manifest["products"][0]["invalid_files"] == ["bad.png"]
    assert plan.manifest["products"][1]["missing_views"] == list(VIEWS)
    with prepare_order_zip(repository, current, plan) as exported:
        with ZipFile(exported.stream) as archive:
            names = archive.namelist()
            assert len(names) == 7 and "manifest.json" in names
            assert f"{EAN}/market_V1.jpeg" in names
            assert archive.read(f"{EAN}/market_V1.jpeg") == photo(1)
            assert any("__" in name and name.endswith(".png") for name in names)
            manifest = json.loads(archive.read("manifest.json"))
            assert manifest["lines_without_ean"] == 1
            assert manifest["current_images"]


def test_empty_zip_changed_plan_and_size_limits(tmp_path, monkeypatch):
    repository = ImageRepository()
    with pytest.raises(ValueError, match="no tiene fotos"):
        with prepare_order_zip(repository, order()):
            pass
    record, _ = repository.save(EAN, "photo.png", photo(1), provider="Other", origin="test")
    plan = order_export_plan(repository, order())
    record.path.unlink()
    with pytest.raises(ValueError, match="cambiaron"):
        with prepare_order_zip(repository, order(), plan):
            pass
    repository.save(EAN, "photo.png", photo(1), provider="Other", origin="test")
    monkeypatch.setattr("image_exports.MAX_EXPORT_FILES", 0)
    with pytest.raises(ValueError, match="demasiado grande"):
        order_export_plan(repository, order())


def test_one_listing_query_filters_reentry_and_no_export_on_render(monkeypatch, tmp_path):
    reader = Mock(return_value=[order()])
    monkeypatch.setattr(ui, "configured_orders", reader)
    monkeypatch.setattr(ui, "prepare_order_zip", Mock(side_effect=AssertionError("No ZIP on render")))
    app = AppTest.from_string(
        "import streamlit as st\nfrom kering_images_ui import render_kering_page, page_transition\n"
        "st.session_state['auth_role']='admin'\n"
        "page_transition(st.session_state, st.session_state.get('route','imagenes-kering'))\n"
        "render_kering_page(None)")
    app.run()
    assert not app.exception and reader.call_count == 1
    app.run()
    app.text_input(key="kering_order_number").set_value("changed").run()
    assert any("pendientes de aplicar" in warning.value for warning in app.warning)
    app.button(key="kering_indicator_Pendientes").click().run()
    app.selectbox(key="kering_open_order").set_value(1).run()
    assert reader.call_count == 1
    next(button for button in app.button if button.label == "Actualizar pedidos").click().run()
    assert reader.call_count == 2
    app.session_state["route"] = "config"
    app.run()
    app.session_state["route"] = "imagenes-kering"
    app.run()
    assert reader.call_count == 3 and not app.exception


def test_search_leading_zeros_and_zip_cache_invalidation(monkeypatch, tmp_path):
    store = ImageStore(tmp_path / "kering")
    record, _ = store.repository.save(EAN, "extra.png", photo(1), provider="Other", origin="offline")
    monkeypatch.setattr(ui, "configured_orders", Mock(side_effect=AssertionError("No Odoo in search")))
    app = AppTest.from_string(
        "from kering_images import ImageStore, data_root\nfrom kering_images_ui import render_image_search\n"
        "render_image_search(ImageStore(data_root()))")
    app.run()
    app.text_input(key="kering_image_query").set_value(EAN).run()
    assert not app.exception
    assert EAN in app.selectbox[0].value
    app.text_input(key="kering_image_query").set_value(EAN.lstrip('0')).run()
    assert any("Sin imagenes" in item.value for item in app.info)
    # A prepared ZIP is a snapshot; external changes require manual refresh.
    builder = Mock(wraps=ui.prepare_order_zip)
    planner = Mock(wraps=ui.order_export_plan)
    monkeypatch.setattr(ui, "prepare_order_zip", builder)
    monkeypatch.setattr(ui, "order_export_plan", planner)
    app = AppTest.from_string(
        f"from kering_images import ImageStore, data_root\nfrom kering_images_ui import render_order_zip\n"
        f"render_order_zip(ImageStore(data_root()), {order()!r}, 'test')")
    app.run()
    assert builder.call_count == 0
    app.checkbox(key="partial-test").check().run()
    app.button(key="prepare-test").click().run()
    assert builder.call_count == 1 and app.get("download_button")
    app.run()
    assert builder.call_count == planner.call_count == 1
    record.path.write_bytes(b"corrupt")
    app.button(key="refresh-zip-test").click().run()
    assert builder.call_count == 1 and planner.call_count == 1
    assert not app.get("download_button")


def test_pagination_stable_selection_and_queries(monkeypatch, tmp_path):
    orders = [order(number=index) for index in range(1, 24)]
    reader = Mock(return_value=orders)
    monkeypatch.setattr(ui, "configured_orders", reader)
    app = AppTest.from_string(
        "import streamlit as st\nfrom kering_images_ui import render_kering_page\n"
        "st.session_state['auth_role']='admin'\nrender_kering_page(None)")
    app.session_state["kering_selected_ids"] = [1, 22]
    app.run()
    app.number_input(key="kering_orders_page_Todos_2").set_value(2).run()
    assert app.session_state["kering_selected_ids"] == [1, 22]
    assert reader.call_count == 1
    reader.return_value = list(reversed(orders))
    next(button for button in app.button if button.label == "Actualizar pedidos").click().run()
    assert reader.call_count == 2
    assert app.session_state["kering_selected_ids"] == [1, 22]
    assert not app.exception


def test_progress_reads_only_selected_local_run(monkeypatch, tmp_path):
    store = ImageStore(tmp_path / "kering")
    run_id = store.create_run([order()], "offline", 7, date(2026, 9, 1), date(2026, 10, 9))
    monkeypatch.setattr(ui, "service", lambda: Mock(store=store))
    monkeypatch.setattr(store, "history", Mock(side_effect=AssertionError("No full history")))
    monkeypatch.setattr(store, "valid_views", Mock(side_effect=AssertionError("No file validation")))
    monkeypatch.setattr(ui, "configured_orders", Mock(side_effect=AssertionError("No Odoo")))
    app = AppTest.from_string(f"from kering_images_ui import render_progress\nrender_progress({run_id!r})")
    app.run()
    assert not app.exception
    with store.connect() as connection:
        connection.execute("UPDATE attempts SET status='Completo' WHERE run_id=?", (run_id,))
        connection.execute("UPDATE runs SET ended=1 WHERE id=?", (run_id,))
    app.run()
    assert not app.exception and app.session_state[f"kering_finished_{run_id}"]


@pytest.mark.parametrize("size,count", [(1024 * 1024, 256), (None, 2000)])
def test_export_exact_documented_limits(monkeypatch, size, count):
    repository = ImageRepository()
    content = photo(1)
    if size:
        content += b"\0" * (size - len(content))
    record, _ = repository.save(EAN, "large.png", content, provider="Other", origin="synthetic")
    records = [replace(record, id=index, name=f"photo_{index}.png") for index in range(count)]
    monkeypatch.setattr(repository, "records", lambda ean=None: records)
    assert len(order_export_plan(repository, order()).files) == count
    records.append(replace(record, id=count, name=f"photo_{count}.png"))
    with pytest.raises(ValueError, match="demasiado grande"):
        order_export_plan(repository, order())
