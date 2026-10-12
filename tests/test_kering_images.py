from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from kering_images import ConfigStore, PORTAL_URL, utc_interval, DEFAULT_SETTINGS, configured_orders
from kering_images import ImageStore, VIEWS, execute_run, process_ean, purchase_status, latest_attempts, batch_lock
from kering_images import read_orders, InterventionRequired, UnverifiedPortal, BatchService
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from sqlalchemy import create_engine, text, event
import json
from zipfile import ZipFile
import socket


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch, tmp_path):
    monkeypatch.delenv("KERING_ENCRYPTION_KEY_FILE", raising=False)
    monkeypatch.setenv("KERING_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("KERING_DATA_ROOT", str(tmp_path / "kering-offline"))
    monkeypatch.setenv("IMAGE_REPOSITORY_ROOT", str(tmp_path / "common-images"))
    def reject(*args, **kwargs):
        raise AssertionError("Las pruebas Kering son offline")
    monkeypatch.setattr(socket, "create_connection", reject)
    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr("kering_portal.sync_playwright", reject)
from io import BytesIO
from PIL import Image
from unittest.mock import Mock, MagicMock
from contextlib import nullcontext


def photo(color):
    buffer = BytesIO()
    Image.new("RGB", (600, 600), color).save(buffer, "PNG")
    return buffer.getvalue()


class FakePortal:
    def __init__(self, views=VIEWS):
        self.calls = []
        self.views = views

    def fetch(self, ean, pending):
        self.calls.append((ean, pending))
        return {view: photo((index * 80, 10, 20)) for index, view in enumerate(VIEWS)
                if view in pending and view in self.views}


def order(order_id=1, eans=("0012345678901",)):
    return dict(id=order_id, name=f"PO{order_id}", supplier_id=7, date_order="2026-03-29 12:00:00",
                state="purchase", lines=[dict(id=index, product_id=index + 1, ean=ean)
                                         for index, ean in enumerate(eans)])


def test_full_madrid_day_and_dst():
    assert utc_interval(date(2026, 3, 29), date(2026, 3, 29)) == (
        datetime(2026, 3, 28, 23), datetime(2026, 3, 29, 22)
    )
    with pytest.raises(ValueError):
        utc_interval(date(2026, 2, 2), date(2026, 2, 1))


def test_encrypted_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("KERING_ENCRYPTION_KEY", Fernet.generate_key().decode())
    store = ConfigStore(tmp_path)
    assert store.load()["password"] == ""
    config = dict(url=PORTAL_URL, username="offline-user", password="offline-secret", supplier_id=7)
    store.save(config)
    assert b"offline-secret" not in (tmp_path / "config.enc").read_bytes()
    assert store.load() == {**DEFAULT_SETTINGS, **config}
    with pytest.raises(ValueError):
        store.save(dict(config, url="https://example.org"))


def test_reuse_and_repair_only_pending(tmp_path):
    store = ImageStore(tmp_path)
    portal = FakePortal()
    ean = "0012345678901"
    assert set(process_ean(store, portal, ean)["views"].values()) == {"Descargada"}
    assert set(process_ean(store, portal, ean)["views"].values()) == {"Reutilizada"}
    assert len(portal.calls) == 1
    store.path(ean, "frontal").unlink()
    store.path(ean, "lateral").write_bytes(b"corrupt")
    process_ean(store, portal, ean)
    assert portal.calls[-1] == (ean, ("frontal", "lateral"))
    assert len(store.valid_views(ean)) == 3


def test_cutoff_persists_and_query_includes_full_madrid_day(tmp_path, monkeypatch):
    import kering_images
    config_store = ConfigStore(tmp_path)
    config = {"url": PORTAL_URL, "username": "offline", "password": "offline", "supplier_id": 7}
    config_store.save(config)
    config = config_store.load()
    assert config["cutoff_date"] == "2026-09-01"
    assert not config["auto_enabled"] and config["test_mode"]
    reader = Mock(return_value=[order()])
    monkeypatch.setattr(kering_images, "read_orders", reader)
    configured_orders(None, config, date(2026, 8, 1), date(2026, 9, 1), [1])
    assert reader.call_args.args[2:4] == (date(2026, 9, 1), date(2026, 9, 1))
    assert utc_interval(date(2026, 9, 1), date(2026, 9, 1)) == (
        datetime(2026, 8, 31, 22), datetime(2026, 9, 1, 22))
    assert configured_orders(None, config, date(2026, 8, 1), date(2026, 8, 31)) == []
    assert reader.call_count == 1
    image_store = ImageStore(tmp_path)
    image_store.create_run([order()], "offline", 7, date(2026, 3, 29), date(2026, 3, 29))
    config_store.save({**config, "cutoff_date": "2026-10-01"})
    assert ConfigStore(tmp_path).load()["cutoff_date"] == "2026-10-01"
    assert len(image_store.history()) == 1


def test_purchase_listing_uses_current_lines_and_files(tmp_path):
    store = ImageStore(tmp_path)
    current = order()
    assert purchase_status(store, current)["status"] == "No procesado"
    run = store.create_run([current], "offline", 7, date(2026, 3, 29), date(2026, 3, 29))
    execute_run(store, run, lambda _: current, FakePortal())
    latest = latest_attempts(store)[1]
    assert latest["origin"] == "Manual"
    assert purchase_status(store, current, latest)["status"] == "Procesado"
    assert purchase_status(store, order(eans=("0012345678901", "0002")), latest)["changed"]
    store.path("0012345678901", "frontal").unlink()
    status = purchase_status(store, current, latest)
    assert status["status"] == "Parcial" and status["pending"] == 1
    assert status["needs_processing"]


def test_shared_batch_lock_blocks_a_second_service(tmp_path):
    store = ImageStore(tmp_path)
    service = BatchService(store)
    from filelock import Timeout
    with batch_lock(store):
        with pytest.raises(Timeout):
            service.submit([order()], "offline", 7, date(2026, 3, 29), date(2026, 3, 29), lambda _: order(), FakePortal())
    service.executor.shutdown(wait=True)
    assert not store.history()


def pilot_config(config_store, **changes):
    config_store.save({"url": PORTAL_URL, "username": "offline", "password": "offline",
                       "supplier_id": 7, "test_order_ids": [1, 2], "auto_enabled": True,
                       "auto_interval_hours": 1, **changes})
    return config_store.load()


def pilot_order(order_id=1, eans=("0012345678901",), when="2026-09-01 00:00:00"):
    return {**order(order_id, eans), "date_order": when}


def test_scheduler_cutoff_scope_reuse_and_restart(tmp_path, monkeypatch):
    import kering_jobs as jobs
    from kering_images import configured_orders as query
    store = ImageStore(tmp_path)
    config_store = ConfigStore(tmp_path)
    pilot_config(config_store)
    now = [datetime(2026, 10, 9, 10, tzinfo=timezone.utc).timestamp()]
    orders = [pilot_order(1, when="2026-08-31 21:59:59"), pilot_order(2)]
    requests = []
    def reader(engine, config, start, end, ids=None):
        requests.append((start, end, ids))
        return [row for row in orders if (ids is None or row["id"] in ids) and start <= jobs.order_day(row) <= end]
    monkeypatch.setattr(jobs, "configured_orders", reader)
    portal = FakePortal()
    factory = Mock(return_value=portal)
    scheduler = jobs.KeringScheduler(store, config_store, lambda: None, factory, lambda: now[0])
    assert scheduler.tick()["result"] == "Completado"
    assert [row["order_id"] for row in store.history()] == [2]
    assert store.history()[0]["origin"] == jobs.AUTO_ORIGIN
    assert requests[0][0] == date(2026, 9, 1) and requests[0][2] == [1, 2]
    assert len(portal.calls) == 1
    before = len(requests)
    restarted = jobs.KeringScheduler(ImageStore(tmp_path), ConfigStore(tmp_path), lambda: None, factory, lambda: now[0])
    assert restarted.tick()["next_run"] == now[0] + 3600
    assert len(requests) == before
    now[0] += 3600
    orders[0] = pilot_order(1)
    assert restarted.tick()["result"] == "Completado"
    assert len(portal.calls) == 1
    latest = latest_attempts(store)
    assert purchase_status(store, orders[0], latest[1])["status"] == "Procesado"
    now[0] += 3600
    assert restarted.tick()["result"] == "Sin pendientes"
    assert len(store.history()) == 2 and len(portal.calls) == 1
    config_store.save({**config_store.load(), "cutoff_date": "2026-10-10"})
    now[0] += 3600
    assert restarted.tick()["result"] == "Sin pendientes"
    assert len(store.history()) == 2 and len(store.valid_views("0012345678901")) == 3


def test_scheduler_waits_for_manual_and_recovers_interruption(tmp_path, monkeypatch):
    import kering_jobs as jobs
    store = ImageStore(tmp_path)
    config_store = ConfigStore(tmp_path)
    config = pilot_config(config_store)
    now = datetime(2026, 10, 9, 10, tzinfo=timezone.utc).timestamp()
    reader = Mock(return_value=[pilot_order()])
    monkeypatch.setattr(jobs, "configured_orders", reader)
    scheduler = jobs.KeringScheduler(store, config_store, lambda: None, FakePortal, lambda: now)
    with batch_lock(store):
        assert scheduler.tick()["result"] == "Esperando lote activo"
    reader.assert_not_called()
    run_id = store.create_run([pilot_order()], "programador", 7, date(2026, 9, 1), date(2026, 10, 9), jobs.AUTO_ORIGIN)
    scheduler.write_state(now - 10, None, now + 3500, "En proceso", run_id)
    assert scheduler.tick()["result"] == "Interrumpido"
    assert scheduler.tick()["next_run"] == now + 3600
    reader.assert_not_called()
    assert store.history()[0]["status"] == "Interrumpido"


def test_pilot_blocks_unselected_and_old_manual_orders_and_rechecks_cut(tmp_path, monkeypatch):
    import kering_jobs as jobs
    store = ImageStore(tmp_path)
    config_store = ConfigStore(tmp_path)
    config = pilot_config(config_store, auto_enabled=False, test_order_ids=[1])
    start, end = date(2026, 8, 1), date(2026, 10, 9)
    with pytest.raises(ValueError):
        jobs.prepare_selection(store, config, [pilot_order(2)], start, end)
    with pytest.raises(ValueError):
        jobs.prepare_selection(store, config, [pilot_order(1, when="2026-08-31 21:59:59")], start, end)
    assert jobs.prepare_selection(store, config, [pilot_order()], start, end) == date(2026, 9, 1)
    reader = Mock(return_value=[pilot_order()])
    loader = jobs.make_loader(store, config_store, lambda: None, start, end, 7, reader)
    assert loader(1)["id"] == 1
    config_store.save({**config, "cutoff_date": "2026-10-01"})
    with pytest.raises(ValueError):
        loader(1)
    assert not store.history()


def test_scheduler_error_backoff_and_configuration_gate(tmp_path, monkeypatch):
    import kering_jobs as jobs
    store = ImageStore(tmp_path)
    config_store = ConfigStore(tmp_path)
    config = pilot_config(config_store, auto_interval_hours=0.01, test_order_ids=[1])
    now = [datetime(2026, 10, 9, 10, tzinfo=timezone.utc).timestamp()]
    monkeypatch.setattr(jobs, "configured_orders", Mock(return_value=[pilot_order()]))
    factory = Mock(side_effect=RuntimeError("offline-secret"))
    scheduler = jobs.KeringScheduler(store, config_store, lambda: None, factory, lambda: now[0])
    assert scheduler.tick()["result"] == "Error"
    assert scheduler.tick()["next_run"] == now[0] + 300
    assert factory.call_count == 1
    assert "offline-secret" not in json.dumps(store.history())
    config_store.save({**config, "auto_enabled": False})
    assert scheduler.tick()["next_run"] is None
    for value in (0, -1, float("inf"), float("nan")):
        with pytest.raises(ValueError):
            config_store.save({**config, "auto_interval_hours": value})
    with pytest.raises(ValueError):
        config_store.save({**config, "test_mode": False})


def test_cutoff_sql_boundary_and_manual_loader(tmp_path):
    import kering_jobs as jobs
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE purchase_order(id INTEGER,name TEXT,partner_id INTEGER,date_order TEXT,state TEXT)"))
        connection.execute(text("CREATE TABLE purchase_order_line(id INTEGER,order_id INTEGER,product_id INTEGER,display_type TEXT,name TEXT,product_qty REAL)"))
        connection.execute(text("CREATE TABLE product_product(id INTEGER,barcode TEXT)"))
        connection.execute(text("INSERT INTO purchase_order VALUES(1,'OLD',7,'2026-08-31 21:59:59','purchase'),(2,'CUT',7,'2026-08-31 22:00:00','purchase'),(3,'END',7,'2026-09-01 21:59:59','purchase'),(4,'NEXT',7,'2026-09-01 22:00:00','purchase')"))
    config_store = ConfigStore(tmp_path)
    config = pilot_config(config_store, test_order_ids=[1, 2])
    assert [row["id"] for row in configured_orders(engine, config, date(2026, 8, 1), date(2026, 9, 1))] == [2, 3]
    loader = jobs.make_loader(ImageStore(tmp_path), config_store, lambda: engine, date(2026, 8, 1), date(2026, 9, 1), 7)
    with pytest.raises(ValueError):
        loader(1)
    assert loader(2)["name"] == "CUT"


def test_unlimited_mode_requires_download_reuse_and_history(tmp_path):
    from kering_images import trial_validated
    from kering_jobs import check_permission
    store = ImageStore(tmp_path)
    config_store = ConfigStore(tmp_path)
    config = pilot_config(config_store, test_order_ids=[1])
    assert not trial_validated(store, config)
    portal = FakePortal()
    for _ in range(2):
        run_id = store.create_run([pilot_order()], "offline", 7, date(2026, 9, 1), date(2026, 10, 9))
        execute_run(store, run_id, lambda _: pilot_order(), portal)
    assert len(portal.calls) == 1 and trial_validated(store, config)
    config_store.save({**config, "test_mode": False, "full_lot_validated": True})
    check_permission(store, config_store.load(), [1, 2, 3])
    store.path("0012345678901", "frontal").unlink()
    with pytest.raises(ValueError):
        check_permission(store, config_store.load(), [3])


def test_order_first_ui_processes_one_pilot_and_reuses_images(tmp_path, monkeypatch):
    import kering_images_ui as ui
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("KERING_DATA_ROOT", str(tmp_path))
    config_store = ConfigStore(tmp_path)
    pilot_config(config_store, test_order_ids=[1], auto_enabled=False)
    current = pilot_order()
    portal = FakePortal()
    batch = BatchService(ImageStore(tmp_path))
    batch.executor.shutdown(wait=True)
    batch.executor = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(ui, "service", lambda: batch)
    monkeypatch.setattr(ui, "configured_orders", lambda *args, **kwargs: [current])
    monkeypatch.setattr(ui, "create_portal", lambda _: portal)
    app = AppTest.from_string("import streamlit as st\nfrom kering_images_ui import render_kering_page\nst.session_state['auth_role']='admin'\nrender_kering_page(None)")
    from concurrent.futures import Future
    probe = Future()
    probe.set_result({"ok": True})
    app.session_state["kering_access_probe"] = probe
    try:
        app.run()
        assert not app.exception
        listing = next(frame.value for frame in app.dataframe if "Seleccionar" in frame.value.columns)
        assert listing.iloc[0]["Procesamiento"] == "No procesado"
        button = next(button for button in app.button if button.label == "Procesar pendientes")
        assert not button.disabled
        button.click().run()
        batch.executor.submit(lambda: None).result(timeout=10)
        next(button for button in app.button if button.label == "Actualizar pedidos").click().run()
        assert not app.exception
        listing = next(frame.value for frame in app.dataframe if "Seleccionar" in frame.value.columns)
        assert listing.iloc[0]["Procesamiento"] == "Procesado"
        assert listing.iloc[0]["Completos"] == 1
        next(button for button in app.button if button.label.startswith("Preparar 3 fotos")).click().run()
        assert app.get("download_button") and not app.exception
        next(button for button in app.button if button.label == "Procesar pendientes").click().run()
        batch.executor.submit(lambda: None).result(timeout=10)
        assert len(portal.calls) == 1 and len(batch.store.history()) == 2
        assert all(row["origin"] == "Manual" for row in batch.store.history())
    finally:
        batch.executor.shutdown(wait=True)


def test_schedule_query_failure_has_persistent_execution(tmp_path, monkeypatch):
    import kering_jobs as jobs
    store = ImageStore(tmp_path)
    config_store = ConfigStore(tmp_path)
    pilot_config(config_store, test_order_ids=[1])
    monkeypatch.setattr(jobs, "configured_orders", Mock(side_effect=RuntimeError("offline-secret")))
    now = datetime(2026, 10, 9, 10, tzinfo=timezone.utc).timestamp()
    scheduler = jobs.KeringScheduler(store, config_store, lambda: None, FakePortal, lambda: now)
    status = scheduler.tick()
    assert status["result"] == "Error"
    executions = ImageStore(tmp_path).executions()
    assert len(executions) == 1 and executions[0]["origin"] == jobs.AUTO_ORIGIN
    assert executions[0]["status"] == "Error" and executions[0]["ended"] is not None
    assert "offline-secret" not in json.dumps(executions)


def test_scheduler_rechecks_persisted_due_inside_shared_lock(tmp_path, monkeypatch):
    import kering_jobs as jobs
    from contextlib import contextmanager
    store = ImageStore(tmp_path)
    config_store = ConfigStore(tmp_path)
    pilot_config(config_store, test_order_ids=[1])
    now = datetime(2026, 10, 9, 10, tzinfo=timezone.utc).timestamp()
    scheduler = jobs.KeringScheduler(store, config_store, lambda: None, FakePortal, lambda: now)
    reader = Mock(return_value=[pilot_order()])
    monkeypatch.setattr(jobs, "configured_orders", reader)
    @contextmanager
    def updated_lock(current_store):
        with batch_lock(current_store):
            scheduler.write_state(now - 10, now, now + 3600, "Completado", "other-process")
            yield
    monkeypatch.setattr(jobs, "batch_lock", updated_lock)
    assert scheduler.tick()["next_run"] == now + 3600
    reader.assert_not_called()
    assert not store.history()


def test_order_list_connection_failure_is_specific_and_secret_free(tmp_path, monkeypatch):
    import kering_images_ui as ui
    from sqlalchemy.exc import OperationalError
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("KERING_DATA_ROOT", str(tmp_path))
    pilot_config(ConfigStore(tmp_path), auto_enabled=False)
    monkeypatch.setattr(ui, "service", lambda: Mock(store=ImageStore(tmp_path)))
    failure = OperationalError("private-sql", {}, Exception("offline-secret user host password"))
    monkeypatch.setattr(ui, "configured_orders", Mock(side_effect=failure))
    app = AppTest.from_string("from datetime import date\nfrom kering_images_ui import render_order_list\nrender_order_list(None, date(2026,9,1), date(2026,10,9))")
    app.run()
    assert not app.exception
    assert "ODOO_CONEXION" in app.error[0].value
    assert "offline-secret" not in app.error[0].value
    assert "private-sql" not in app.error[0].value
    assert not app.button


def test_order_list_error_distinguishes_history_and_row_calculation():
    from kering_images_ui import order_list_error
    error = RuntimeError("offline-secret")
    assert "KERING_HISTORIAL" in order_list_error(error, "historial")
    assert "KERING_ESTADO" in order_list_error(error, "estado")
    assert "ODOO_CONSULTA" in order_list_error(error, "odoo")
    assert "offline-secret" not in order_list_error(error, "configuracion")


def test_confirmed_detail_is_not_mislabeled_as_lateral(tmp_path):
    class DetailPortal(FakePortal):
        def fetch(self, ean, pending):
            result = super().fetch(ean, pending)
            if "lateral" in result:
                result["detalle"] = result.pop("lateral")
            return result
    store = ImageStore(tmp_path)
    portal = DetailPortal()
    result = process_ean(store, portal, "0001")
    assert set(result["views"]) == set(VIEWS)
    assert result["views"]["lateral"] == "Pendiente"
    assert "lateral" not in store.valid_views("0001")
    assert process_ean(store, portal, "0001")["tries"] == 1
    assert len(portal.calls) == 2
    store.path("0001", "frontal").unlink()
    process_ean(store, portal, "0001")
    assert portal.calls[-1] == ("0001", ("frontal", "lateral"))


def test_history_reuse_missing_ean_and_changed_lines(tmp_path):
    store = ImageStore(tmp_path)
    portal = FakePortal()
    orders = [order(), order(2, ("0012345678901", ""))]
    run = store.create_run(orders, "offline", 7, date(2026, 3, 29), date(2026, 3, 29))
    execute_run(store, run, lambda order_id: orders[order_id - 1], portal)
    history = store.history()
    assert [row["status"] for row in history] == ["Completo", "Parcial"]
    assert len(portal.calls) == 1
    changed = order(1, ("0012345678901", "0000000000002"))
    run = store.create_run([changed], "offline", 7, date(2026, 3, 29), date(2026, 3, 29))
    execute_run(store, run, lambda _: changed, portal)
    assert portal.calls[-1][0] == "0000000000002"
    history = ImageStore(tmp_path).history()
    assert len(history) == 3
    assert history[0]["attempt"] == 2


def test_odoo_read_only_date_supplier_cancel_and_display_lines():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE purchase_order(id INTEGER, name TEXT, partner_id INTEGER, date_order TEXT, state TEXT)"))
        connection.execute(text("CREATE TABLE purchase_order_line(id INTEGER, order_id INTEGER, product_id INTEGER, display_type TEXT, name TEXT, product_qty REAL)"))
        connection.execute(text("CREATE TABLE product_product(id INTEGER, barcode TEXT)"))
        for order_id, supplier, when, state in [
            (1, 7, "2026-03-28 23:00:00", "purchase"),
            (2, 7, "2026-03-29 21:59:59", "draft"),
            (3, 7, "2026-03-29 22:00:00", "purchase"),
            (4, 8, "2026-03-29 12:00:00", "purchase"),
            (5, 7, "2026-03-29 12:00:00", "cancel"),
            (6, 7, "2026-03-28 22:59:59", "purchase"),
        ]:
            connection.execute(text("INSERT INTO purchase_order VALUES(:id, :name, :supplier, :when, :state)"),
                               dict(id=order_id, name=f"PO{order_id}", supplier=supplier, when=when, state=state))
        connection.execute(text("INSERT INTO product_product VALUES(1, '0012345678901')"))
        connection.execute(text("INSERT INTO purchase_order_line(id,order_id,product_id,display_type) VALUES(1,1,1,NULL),(2,1,NULL,'line_note'),(3,1,NULL,'line_section'),(4,2,2,NULL)"))
    orders = read_orders(engine, 7, date(2026, 3, 29), date(2026, 3, 29))
    assert [item["id"] for item in orders] == [1, 2]
    assert orders[0]["lines"] == [dict(id=1, product_id=1, ean="0012345678901", product_name="", quantity="")]
    assert orders[1]["lines"][0]["ean"] == ""
    calls = []
    event.listen(engine, "before_cursor_execute", lambda conn, cursor, sql, params, context, many: calls.append((sql, params)))
    assert [item["id"] for item in read_orders(engine, 7, date(2026, 3, 29), date(2026, 3, 29), [2, 2])] == [2]
    assert "po.id IN (?)" in calls[-1][0]
    assert calls[-1][1][-1] == 2
    count = len(calls)
    assert read_orders(engine, 7, date(2026, 3, 29), date(2026, 3, 29), []) == []
    assert len(calls) == count
    with pytest.raises(ValueError):
        read_orders(engine, 7, date(2026, 3, 29), date(2026, 3, 29), ["2); DROP TABLE purchase_order;--"])


def test_supplier_commercial_contacts_are_explicit_and_exact():
    from kering_images import read_supplier_contacts
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE res_partner(id INTEGER, name TEXT, commercial_partner_id INTEGER, active BOOLEAN)"))
        connection.execute(text("INSERT INTO res_partner VALUES(7,'Kering',7,1),(8,'Contacto A',7,1),(9,'Kering parecido',9,1),(10,'Contacto archivado',7,0)"))
        connection.execute(text("CREATE TABLE purchase_order(id INTEGER, name TEXT, partner_id INTEGER, date_order TEXT, state TEXT)"))
        connection.execute(text("INSERT INTO purchase_order VALUES(1,'PO1',7,'2026-03-29 10:00:00','purchase'),(2,'PO2',8,'2026-03-29 10:00:00','purchase'),(3,'PO3',9,'2026-03-29 10:00:00','purchase'),(4,'PO4',10,'2026-03-29 10:00:00','cancel')"))
        connection.execute(text("CREATE TABLE purchase_order_line(id INTEGER, order_id INTEGER, product_id INTEGER, display_type TEXT, name TEXT, product_qty REAL)"))
        connection.execute(text("CREATE TABLE product_product(id INTEGER, barcode TEXT)"))
    dates = (date(2026, 3, 29), date(2026, 3, 29))
    assert [row["id"] for row in read_orders(engine, 7, *dates)] == [1]
    assert [row["id"] for row in read_orders(engine, 7, *dates, include_commercial_contacts=True)] == [1, 2]
    assert [row["id"] for row in read_orders(engine, 8, *dates, [2], include_commercial_contacts=True)] == [2]
    assert [row["id"] for row in read_supplier_contacts(engine, 8)] == [7, 8, 10]


def test_partial_duplicate_and_html_are_not_complete(tmp_path):
    store = ImageStore(tmp_path)
    partial = FakePortal(("frontal",))
    result = process_ean(store, partial, "0001")
    assert list(result["views"].values()).count("Pendiente") == 2
    class InvalidPortal:
        def fetch(self, ean, pending):
            return {"frontal": photo((0, 10, 20)), "lateral": photo((0, 10, 20)), "perspectiva": b"<html>login</html>"}
    result = process_ean(store, InvalidPortal(), "0002")
    assert len(store.valid_views("0002")) == 1
    assert result["reason"] == "imagen_invalida"


def test_concurrent_ean_no_duplicate_download(tmp_path):
    store = ImageStore(tmp_path)
    entered, release = Event(), Event()
    class SlowPortal(FakePortal):
        def fetch(self, ean, pending):
            entered.set()
            assert release.wait(10)
            return super().fetch(ean, pending)
    portal = SlowPortal()
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(process_ean, store, portal, "0001")
        assert entered.wait(10)
        result = process_ean(ImageStore(tmp_path), portal, "0001")
        assert result["reason"] == "ean_en_proceso"
        release.set()
        assert future.result(timeout=10)["reason"] == ""
    assert process_ean(store, portal, "0001")["tries"] == 0
    assert len(portal.calls) == 1


def test_interrupt_retry_and_zip(tmp_path):
    store = ImageStore(tmp_path)
    run = store.create_run([order()], "offline", 7, date(2026, 3, 29), date(2026, 3, 29))
    store.recover(now=10**12)
    assert store.history()[0]["status"] == "Interrumpido"
    run = store.create_run([order()], "offline", 7, date(2026, 3, 29), date(2026, 3, 29))
    execute_run(store, run, lambda _: order(), FakePortal())
    assert store.history()[0]["attempt"] == 2
    with ZipFile(BytesIO(store.zip_order(order()))) as archive:
        assert len(archive.namelist()) == 4
        assert all(name.startswith("0012345678901/") or name == "manifest.json" for name in archive.namelist())


def test_bounded_retries_and_intervention_no_secrets(tmp_path):
    store = ImageStore(tmp_path)
    class FailingPortal:
        def __init__(self, exception):
            self.count = 0
            self.exception = exception
        def fetch(self, ean, pending):
            self.count += 1
            raise self.exception("secret-must-not-appear")
    failing = FailingPortal(TimeoutError)
    result = process_ean(store, failing, "0001")
    assert failing.count == 3
    assert "secret" not in json.dumps(result)
    intervention = FailingPortal(InterventionRequired)
    assert process_ean(store, intervention, "0001")["reason"] == "intervencion_captcha_o_mfa"
    assert intervention.count == 1
    assert process_ean(store, UnverifiedPortal(), "0001")["reason"] == "portal_no_verificado"


def test_background_batch_keeps_processing_after_product_error(tmp_path):
    service = BatchService(ImageStore(tmp_path))
    class OneBadPortal(FakePortal):
        def fetch(self, ean, pending):
            if ean == "0001":
                raise TimeoutError("secret")
            return super().fetch(ean, pending)
    orders = [order(1, ("0001",)), order(2, ("0002",))]
    service.submit(orders, "offline", 7, date(2026, 3, 29), date(2026, 3, 29), lambda order_id: orders[order_id-1], OneBadPortal())
    service.executor.shutdown(wait=True)
    assert [row["status"] for row in service.store.history()] == ["Error", "Completo"]


def test_admin_settings_ui_password_not_prefilled(tmp_path, monkeypatch):
    import kering_images_ui as ui
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("KERING_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("KERING_ENCRYPTION_KEY", Fernet.generate_key().decode())
    ConfigStore(tmp_path).save(dict(url=PORTAL_URL, username="offline", password="secret-hidden", supplier_id=7))
    monkeypatch.setattr(ui, "read_suppliers", lambda _: [{"id": 7, "name": "Offline supplier"}])
    app = AppTest.from_string("import streamlit as st\nfrom kering_images_ui import render_kering_settings\nst.session_state['auth_role']='admin'\nrender_kering_settings(None)")
    app.run()
    assert not app.exception
    assert app.text_input[2].value == ""
    assert "secret-hidden" not in str(app)
    restricted = AppTest.from_string("import streamlit as st\nfrom kering_images_ui import render_kering_settings\nst.session_state['auth_role']='masterdata'\nrender_kering_settings(None)")
    restricted.run()
    assert restricted.error
    assert not restricted.text_input


def test_history_filters_and_local_dates():
    from kering_images_ui import filter_history, local_order_time
    row = dict(number="PO001", status="Parcial", order_date="2026-03-28 23:00:00",
               started=datetime(2026, 3, 29, 10, tzinfo=timezone.utc).timestamp())
    assert local_order_time(row["order_date"]).date() == date(2026, 3, 29)
    dates = (date(2026, 3, 29),) * 4
    assert filter_history([row], "po001", ["Parcial"], dates) == [row]
    assert filter_history([row], "PO002", [], dates) == []
    assert filter_history([row], "", ["Completo"], dates) == []


def test_secret_file_configuration_and_fail_closed(tmp_path, monkeypatch):
    key = tmp_path / "server.key"
    key.write_bytes(Fernet.generate_key())
    monkeypatch.delenv("KERING_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("KERING_ENCRYPTION_KEY_FILE", str(key))
    config = ConfigStore(tmp_path / "data")
    config.save(dict(url=PORTAL_URL, username="offline", password="offline-secret", supplier_id=7))
    assert config.load()["password"] == "offline-secret"
    monkeypatch.setenv("KERING_ENCRYPTION_KEY_FILE", str(tmp_path / "absent.key"))
    with pytest.raises(OSError):
        config.load()


def test_duplicates_missing_ean_and_complete_from_existing_are_audited(tmp_path):
    store = ImageStore(tmp_path)
    portal = FakePortal()
    current = order(1, ("0001", "0001", ""))
    run = store.create_run([current], "offline", 7, date(2026, 3, 29), date(2026, 3, 29))
    execute_run(store, run, lambda _: current, portal)
    assert len(portal.calls) == 1
    assert store.history()[0]["status"] == "Parcial"
    current = order(1, ("0001", "0001"))
    run = store.create_run([current], "offline", 7, date(2026, 3, 29), date(2026, 3, 29))
    execute_run(store, run, lambda _: current, UnverifiedPortal())
    latest = store.history()[0]
    assert latest["status"] == "Completo"
    assert latest["attempt"] == 2
    assert set(json.loads(latest["results"])["0001"]["views"].values()) == {"Reutilizada"}


def test_thumbnail_invalid_ean_and_corrupt_history_remain_pending(tmp_path):
    store = ImageStore(tmp_path)
    buffer = BytesIO()
    Image.new("RGB", (100, 100)).save(buffer, "PNG")
    class ThumbnailPortal:
        def fetch(self, ean, pending):
            return {view: buffer.getvalue() for view in pending}
    assert process_ean(store, ThumbnailPortal(), "0001")["reason"] == "imagen_invalida"
    assert not store.valid_views("0001")
    portal = FakePortal()
    assert process_ean(store, portal, "../secret")["reason"] == "ean_invalido"
    assert not portal.calls
    process_ean(store, portal, "0001")
    store.path("0001", "frontal").write_bytes(b"corrupt")
    assert len(ImageStore(tmp_path).valid_views("0001")) == 2


def test_settings_database_failure_does_not_expose_details(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("KERING_DATA_ROOT", str(tmp_path))
    app = AppTest.from_string("import streamlit as st\nfrom kering_images_ui import render_kering_settings\nst.session_state['auth_role']='admin'\ndef failed_engine():\n    raise ValueError('sensitive-connection-value')\nrender_kering_settings(failed_engine)")
    app.run()
    assert not app.exception
    assert app.error
    assert "sensitive-connection-value" not in str(app.error)


def test_file_disappears_between_orders_in_same_batch(tmp_path):
    store = ImageStore(tmp_path)
    portal = FakePortal()
    orders = [order(1, ("0001",)), order(2, ("0001",))]
    def loader(order_id):
        if order_id == 2:
            store.path("0001", "frontal").unlink()
        return orders[order_id - 1]
    run = store.create_run(orders, "offline", 7, date(2026, 3, 29), date(2026, 3, 29))
    execute_run(store, run, loader, portal)
    assert portal.calls == [("0001", VIEWS), ("0001", ("frontal",))]
    assert all(row["status"] == "Completo" for row in store.history())


def test_atomic_write_failure_recovers_without_orphan(tmp_path, monkeypatch):
    import image_repository
    store = ImageStore(tmp_path)
    writer = image_repository.atomic_write
    def interrupted(path, content, **kwargs):
        raise OSError("offline interrupted write")
    monkeypatch.setattr(image_repository, "atomic_write", interrupted)
    assert process_ean(store, FakePortal(), "0001")["reason"] == "imagen_invalida"
    assert not store.valid_views("0001")
    monkeypatch.setattr(image_repository, "atomic_write", writer)
    assert process_ean(store, FakePortal(), "0001")["reason"] == ""
    assert len(store.valid_views("0001")) == 3


def test_orders_and_history_ui_are_offline(tmp_path, monkeypatch):
    import kering_images_ui as ui
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("KERING_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("KERING_ENCRYPTION_KEY", Fernet.generate_key().decode())
    ConfigStore(tmp_path).save(dict(url=PORTAL_URL, username="offline", password="offline", supplier_id=7))
    store = ImageStore(tmp_path)
    run = store.create_run([order()], "offline", 7, date(2026, 3, 29), date(2026, 3, 29))
    execute_run(store, run, lambda _: order(), FakePortal())
    monkeypatch.setattr(ui, "configured_orders", lambda *args, **kwargs: [pilot_order()])
    ui.service.clear()
    app = AppTest.from_string("import streamlit as st\nfrom kering_images_ui import render_kering_page\nst.session_state['auth_role']='admin'\nrender_kering_page(None)")
    app.run()
    assert not app.exception
    assert app.title[0].value == "Im\u00e1genes Kering"
    assert not app.get("download_button")
    next(button for button in app.button if button.label == "Actualizar pedidos").click().run()
    assert not app.exception
    assert any("Seleccionar" in frame.value.columns for frame in app.dataframe)
    ui.service.clear()


def test_ui_injects_configured_adapter_and_sql_scope(monkeypatch):
    import kering_images_ui as ui
    from unittest.mock import Mock
    config = dict(url=PORTAL_URL, username="offline-user", password="offline-secret", supplier_id=7,
                  include_commercial_contacts=True, cutoff_date="2026-03-01", test_order_ids=[1])
    ConfigStore(ui.data_root()).save(config)
    portal = ui.create_portal(config)
    assert "offline-secret" not in repr(portal)
    assert portal._config == config
    factory = Mock(return_value=portal)
    batch = Mock()
    batch.store = ImageStore(ui.data_root())
    batch.submit.return_value = "offline-run"
    reader = Mock(return_value=[order()])
    monkeypatch.setattr(ui, "create_portal", factory)
    monkeypatch.setattr(ui, "service", lambda: batch)
    monkeypatch.setattr(ui, "configured_orders", reader)
    assert ui.submit_orders(None, config, [order()], date(2026, 3, 29), date(2026, 3, 29)) == "offline-run"
    assert batch.submit.call_args.kwargs["portal"] is portal
    loader = batch.submit.call_args.args[-1]
    loader(1)
    assert reader.call_args.args[-1] == [1]
    assert reader.call_args.args[1]["include_commercial_contacts"] is True


def portal_snapshot(ean="0012345678901", image_count=3):
    return {"ean": ean, "upc": "001234567890", "reference": "OFFLINE-001", "size": "TALLA M",
            "images": [{"url": f"https://picture.kecdn.net/OFFLINE__001__{('noshad__qt', 'noshad__fr', 'shad__lt')[index % 3]}.png", "reference": "OFFLINE-001"}
                       for index in range(image_count)]}


def configured_portal():
    from kering_portal import KeringPortal
    return KeringPortal(dict(url=PORTAL_URL, username="offline-user", password="offline-secret"))


def test_portal_resolves_only_exact_code_and_downloads_pending(monkeypatch):
    from kering_portal import exact_product
    portal = configured_portal()
    page = MagicMock()
    portal._page = page
    monkeypatch.setattr(portal, "_check_session", lambda: None)
    page.expect_navigation.return_value = nullcontext()
    page.locator.return_value.evaluate_all.return_value = [
        "https://my.keringeyewear.com/es/p/111", "https://my.keringeyewear.com/es/p/222"]
    page.evaluate.side_effect = [portal_snapshot("9999999999999"), portal_snapshot()]
    snapshot = portal._find_product("0012345678901")
    assert exact_product(snapshot, "0012345678901")
    assert not exact_product(snapshot, "12345678901")
    assert portal.identities["0012345678901"]["color"] == "001"
    assert portal.identities["0012345678901"]["size"] == "TALLA M"
    read_image = Mock(return_value=photo((25, 50, 75)))
    monkeypatch.setattr(portal, "_read_image", read_image)
    collected = {}
    portal._download(snapshot, ("frontal",), collected)
    assert len(collected) == 3
    assert read_image.call_count == 3


def test_real_portal_partial_then_only_pending_and_no_calls_for_complete(tmp_path, monkeypatch):
    portal = configured_portal()
    monkeypatch.setattr(portal, "_start", lambda: None)
    find = Mock(side_effect=[portal_snapshot(image_count=2), portal_snapshot()])
    monkeypatch.setattr(portal, "_find_product", find)
    downloads = []
    def read_image(url):
        downloads.append(url)
        index = ("noshad__qt", "noshad__fr", "shad__lt").index(Path(url).stem.split("__", 2)[2])
        return photo((index * 60, 10, 50))
    monkeypatch.setattr(portal, "_read_image", read_image)
    store = ImageStore(tmp_path)
    first = process_ean(store, portal, "0012345678901")
    assert first["views"]["lateral"] == "Pendiente"
    process_ean(store, portal, "0012345678901")
    assert all(downloads.count(image["url"]) == 1 for image in portal_snapshot()["images"])
    assert process_ean(store, portal, "0012345678901")["tries"] == 0
    assert find.call_count == 2


def test_expired_session_reauth_once_and_bounded_timeouts(monkeypatch, tmp_path):
    from kering_images import SessionExpired
    from playwright.sync_api import TimeoutError as BrowserTimeout
    portal = configured_portal()
    start = Mock()
    close = Mock()
    monkeypatch.setattr(portal, "_start", start)
    monkeypatch.setattr(portal, "close", close)
    monkeypatch.setattr(portal, "_find_product", Mock(side_effect=[SessionExpired(), portal_snapshot()]))
    monkeypatch.setattr(portal, "_download", lambda snapshot, pending, collected: collected.update(frontal=photo((10, 20, 30))))
    assert set(portal.fetch("0012345678901", ("frontal",))) == {"frontal"}
    assert start.call_count == 2 and close.call_count == 1
    start.side_effect = BrowserTimeout("offline-secret")
    start.reset_mock()
    result = process_ean(ImageStore(tmp_path), portal, "0012345678901")
    assert result["reason"] == "timeout_portal"
    assert start.call_count == 3
    assert "offline-secret" not in json.dumps(result)


def test_login_uses_configuration_and_authentication_marker(monkeypatch):
    portal, page, _ = simulated_access(monkeypatch)
    portal._start()
    page.get_by_placeholder.assert_any_call("MAIL", exact=True)
    page.get_by_placeholder.assert_any_call("CONTRASEÑA", exact=True)
    assert page.get_by_placeholder("MAIL", exact=True).fill.call_args.args == ("offline-user",)
    assert page.get_by_placeholder("CONTRASEÑA", exact=True).fill.call_args.args == ("offline-secret",)
    page.expect_navigation.assert_not_called()
    assert page.goto.call_args.kwargs["wait_until"] == "domcontentloaded"
    assert "offline-secret" not in repr(portal)
    portal.close()


def test_login_failure_and_intervention_are_safe_and_not_retried(monkeypatch, tmp_path):
    from kering_images import LoginFailed
    for exception, reason in ((LoginFailed, "login_fallido"), (InterventionRequired, "intervencion_captcha_o_mfa")):
        portal = configured_portal()
        start = Mock(side_effect=exception("offline-secret"))
        monkeypatch.setattr(portal, "_start", start)
        access = portal.check_access()
        assert not access["ok"] and access["code"] == reason
        result = process_ean(ImageStore(tmp_path), portal, "0012345678901")
        assert result["reason"] == reason
        assert start.call_count == 2
        assert "offline-secret" not in json.dumps(result)


def simulated_access(monkeypatch, scenario="success"):
    import kering_portal
    from types import SimpleNamespace
    from playwright.sync_api import TimeoutError as BrowserTimeout, Error as BrowserError
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(kering_portal, "time", SimpleNamespace(
        monotonic=lambda: clock.now, sleep=lambda seconds: setattr(clock, "now", clock.now + seconds)))
    portal = configured_portal()
    manager = MagicMock()
    page = manager.chromium.launch.return_value.new_context.return_value.new_page.return_value
    page.url = PORTAL_URL
    page.goto.return_value.status = 200
    fields = {name: MagicMock() for name in ("MAIL", "CONTRASEÑA")}
    for field in fields.values():
        field.is_visible.return_value = scenario != "carga_formulario"
    page.get_by_placeholder.side_effect = lambda name, **kwargs: fields[name]
    button = MagicMock()
    button.is_visible.return_value = True
    button.is_enabled.return_value = True
    clicked = {"value": False}
    consent = {"visible": False, "dismissed": False}
    def click(**kwargs):
        if scenario == "envio_login":
            raise BrowserTimeout("offline-secret")
        if scenario == "delayed_consent":
            consent["visible"] = True
            locator_handlers["consent"]()
            if not consent["dismissed"]:
                raise BrowserTimeout("offline-secret")
        clicked["value"] = True
        if scenario in {"redirect_network", "subresource_network"}:
            request = Mock(frame=page.main_frame)
            request.is_navigation_request.return_value = scenario == "redirect_network"
            handlers["requestfailed"](request)
        if scenario not in {"redireccion", "captcha", "rejected", "same_url"}:
            page.url = "https://my.keringeyewear.com/es/"
    button.click.side_effect = click
    empty = MagicMock()
    empty.count.return_value = 0
    empty.filter.return_value.count.return_value = 0
    alert = MagicMock()
    alert.filter.return_value.count.side_effect = lambda: int(clicked["value"] and scenario == "rejected")
    alert.filter.return_value.first.is_visible.return_value = True
    page.get_by_role.side_effect = lambda role, **kwargs: (
        alert if role == "alert" else button if kwargs.get("name") == "Iniciar Sesión" else empty)
    logout = MagicMock()
    logout.count.side_effect = lambda: int(clicked["value"] and scenario != "search_only")
    logout.first.is_visible.return_value = False
    search = MagicMock()
    search.filter.return_value.count.side_effect = lambda: int(
        clicked["value"] and scenario in {"success", "same_url", "subresource_network", "delayed_consent", "search_only"}
        and clock.now >= 0.3)
    challenge = MagicMock()
    challenge.filter.return_value.count.side_effect = lambda: int(clicked["value"] and scenario == "captcha")
    consent_banner = MagicMock()
    consent_button = MagicMock()
    def dismiss(**kwargs):
        consent["dismissed"] = True
        consent["visible"] = False
    consent_button.click.side_effect = dismiss
    page.locator.side_effect = lambda selector: {
        'a[href="/es/logout"]': logout, "#onetrust-banner-sdk": consent_banner,
        "#onetrust-reject-all-handler": consent_button, ".showSearchBar": search,
    }.get(selector, challenge)
    locator_handlers = {}
    page.add_locator_handler.side_effect = lambda locator, handler: locator_handlers.update({"consent": handler})
    page.get_by_text.return_value.count.side_effect = lambda: int(clicked["value"] and scenario == "mfa")
    page.get_by_text.return_value.first.is_visible.return_value = True
    handlers = {}
    page.on.side_effect = lambda event, handler: handlers.update({event: handler})
    if scenario == "apertura_portal":
        page.goto.side_effect = BrowserTimeout("offline-secret")
    if scenario == "network":
        page.goto.side_effect = BrowserError("net::ERR_NAME_NOT_RESOLVED offline-secret")
    if scenario == "chromium":
        manager.chromium.launch.side_effect = BrowserError("missing executable offline-secret")
    monkeypatch.setattr(kering_portal, "sync_playwright", lambda: Mock(start=lambda: manager))
    return portal, page, clock


@pytest.mark.parametrize("scenario,phase,code", [
    ("chromium", "chromium", "fallo_chromium"),
    ("apertura_portal", "apertura_portal", "timeout_portal"),
    ("carga_formulario", "carga_formulario", "timeout_portal"),
    ("envio_login", "envio_login", "timeout_portal"),
    ("redireccion", "redireccion", "timeout_portal"),
    ("comprobacion_sesion", "comprobacion_sesion", "timeout_portal"),
    ("network", "apertura_portal", "fallo_red"),
    ("redirect_network", "redireccion", "fallo_red"),
    ("captcha", "redireccion", "intervencion_captcha_o_mfa"),
    ("mfa", "redireccion", "intervencion_captcha_o_mfa"),
    ("rejected", "redireccion", "login_fallido"),
    ("search_only", "comprobacion_sesion", "timeout_portal"),
])
def test_access_reports_exact_failing_phase_without_secrets(monkeypatch, caplog, scenario, phase, code):
    portal, page, clock = simulated_access(monkeypatch, scenario)
    with caplog.at_level("INFO", logger="kering_portal"):
        result = portal.check_access()
    assert not result["ok"] and result["code"] == code
    assert result["phase"] == phase
    assert result["phases"][-1]["code"] == code
    assert result["duration_ms"] <= 60001
    assert set(result["phases"][-1]) == {"phase", "duration_ms", "code"}
    assert "offline-secret" not in json.dumps(result) + caplog.text
    assert "offline-user" not in json.dumps(result) + caplog.text
    assert portal._page is None
    page.expect_navigation.assert_not_called()


@pytest.mark.parametrize("scenario", ["success", "same_url", "subresource_network", "delayed_consent"])
def test_access_waits_for_visible_session_marker_even_without_navigation(monkeypatch, scenario):
    portal, _, clock = simulated_access(monkeypatch, scenario)
    result = portal.check_access()
    assert result["ok"] and result["code"] == "acceso_autenticado"
    assert clock.now >= 0.3
    assert result["phases"][-1]["phase"] == "comprobacion_sesion"
def test_consent_handler_uses_observed_id_and_remaining_budget(monkeypatch):
    portal, page, clock = simulated_access(monkeypatch, "delayed_consent")
    result = portal.check_access()
    assert result["ok"]
    page.add_locator_handler.assert_called_once()
    reject = page.locator("#onetrust-reject-all-handler")
    reject.click.assert_called_once()
    assert 0 < reject.click.call_args.kwargs["timeout"] <= 10000
    assert not page.get_by_role("button", name="Rechazarlas todas", exact=True).count()


def test_access_total_deadline_caps_each_phase(monkeypatch):
    import kering_portal
    monkeypatch.setattr(kering_portal, "ACCESS_TOTAL_MS", 250)
    portal, page, clock = simulated_access(monkeypatch)
    result = portal.check_access()
    assert not result["ok"] and result["code"] == "timeout_portal"
    assert result["duration_ms"] <= 251
    assert clock.now < 0.3
    assert page.goto.call_args.kwargs["timeout"] <= 250


@pytest.mark.parametrize("scenario,code", [
    ("comprobacion_sesion", "timeout_portal"), ("network", "fallo_red"),
    ("captcha", "intervencion_captcha_o_mfa"), ("rejected", "login_fallido"),
])
def test_unconfirmed_access_stops_batch_and_preserves_pending_orders(monkeypatch, tmp_path, scenario, code):
    portal, page, _ = simulated_access(monkeypatch, scenario)
    find = Mock()
    monkeypatch.setattr(portal, "_find_product", find)
    store = ImageStore(tmp_path)
    orders = [pilot_order(1, ("0012345678901", "0002")), pilot_order(2, ("0003",))]
    run_id = store.create_run(orders, "offline", 7, date(2026, 9, 1), date(2026, 9, 1))
    loader = Mock(side_effect=lambda order_id: orders[order_id - 1])
    execute_run(store, run_id, loader, portal)
    find.assert_not_called()
    assert loader.call_count == 1
    with store.connect() as connection:
        run = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        attempts = connection.execute("SELECT * FROM attempts WHERE run_id=? ORDER BY order_id", (run_id,)).fetchall()
    assert run["status"] == "Interrumpido" and run["ended"] is not None
    assert attempts[0]["error"] == code
    result = json.loads(attempts[0]["results"])["0012345678901"]
    assert result["tries"] == 1 and result["access_failure"]["code"] == code
    assert attempts[1]["status"] == "Pendiente"
    assert not store.valid_views("0012345678901")
    assert purchase_status(store, orders[1])["needs_processing"]


def test_scheduler_reports_auth_interruption_and_backs_off(monkeypatch, tmp_path):
    import kering_jobs
    portal, _, _ = simulated_access(monkeypatch, "network")
    store = ImageStore(tmp_path)
    config_store = ConfigStore(tmp_path)
    pilot_config(config_store, auto_interval_hours=0.001)
    orders = [pilot_order(1), pilot_order(2, ("0002",))]
    monkeypatch.setattr(kering_jobs, "configured_orders",
                        lambda engine, config, start, end, ids: [order for order in orders if order["id"] in ids])
    now = datetime(2026, 9, 2, tzinfo=timezone.utc).timestamp()
    scheduler = kering_jobs.KeringScheduler(store, config_store, lambda: None,
                                           portal_factory=lambda _: portal, clock_fn=lambda: now)
    status = scheduler.tick()
    assert status["result"] == "Interrumpido"
    assert status["next_run"] == now + kering_jobs.ERROR_BACKOFF
    assert sorted(row["status"] for row in store.history()) == ["Error", "Pendiente"]


def test_environment_diagnostics_returns_only_sanitized_errors(monkeypatch):
    from scripts import validate_kering_portal as validation
    def reject(*args, **kwargs):
        raise OSError("offline-secret")
    monkeypatch.setattr(validation.subprocess, "run", reject)
    monkeypatch.setattr(validation, "sync_playwright", reject)
    report = validation.diagnose_environment()
    assert not report["passed"]
    assert [event["code"] for event in report["phases"]] == ["fallo_red", "fallo_red", "fallo_chromium"]
    assert "offline-secret" not in json.dumps(report)


@pytest.mark.parametrize("access_ok", [False, True])
def test_server_validation_selects_only_one_authorized_order_and_shared_cutoff(monkeypatch, tmp_path, access_ok):
    from scripts import validate_kering_portal as validation
    import kering_jobs
    config_store = ConfigStore(tmp_path)
    pilot_config(config_store, auto_enabled=False)
    monkeypatch.setattr(validation, "data_root", lambda: tmp_path)
    engine = Mock()
    monkeypatch.setattr(validation, "db_engine", lambda: engine)
    reader = Mock(return_value=[pilot_order(1)])
    monkeypatch.setattr(validation, "configured_orders", reader)
    monkeypatch.setattr(kering_jobs, "configured_orders", reader)
    portals = []
    def factory(config):
        portal = FakePortal()
        original_fetch = portal.fetch
        def fetch(ean, pending):
            images = original_fetch(ean, pending)
            return images
        portal.fetch = fetch
        portal.identities = {"0012345678901": {"ean": "0012345678901", "upc": "",
                            "model": "OFFLINE", "color": "001", "size": "M"}}
        portal.check_access = lambda: {"ok": access_ok, "code": "acceso_autenticado" if access_ok else "timeout_portal"}
        portal.close = Mock()
        portal.lookups = portal.downloads = 0
        portal.access_events = []
        portals.append(portal)
        return portal
    monkeypatch.setattr(validation, "MeasuredPortal", factory)
    report = validation.validate_one_order(config_store, 3)
    assert not report["passed"] and report["code"] == "pedido_no_autorizado"
    reader.assert_not_called()
    report = validation.validate_one_order(config_store, 1)
    if access_ok:
        assert report["status"] == "Completo"
        assert report["images"][0]["identity_verified"]
        assert report["passed"]
    else:
        assert not report["passed"]
    assert reader.call_args.args[2] == date(2026, 9, 1)
    assert reader.call_args.args[-1] == [1]
    history = ImageStore(tmp_path).history()
    if access_ok:
        assert len(history) == 1 and history[0]["order_id"] == 1
        assert len(portals[-1].calls) == 1
    else:
        assert not history
    assert "offline-secret" not in json.dumps(report)


def test_order_image_evidence_requires_exact_identity_and_three_original_views(tmp_path):
    from scripts.validate_kering_portal import order_image_evidence
    store = ImageStore(tmp_path)
    ean = "0012345678901"
    for index, view in enumerate(("frontal", "perspectiva", "lateral")):
        store.save_view(ean, view, photo((index * 70, 20, 30)))
    results = {ean: {"views": dict.fromkeys(("frontal", "perspectiva", "lateral"), "Reutilizada")}}
    prior = [{"results": json.dumps({ean: {"identity": {
        "ean": ean, "upc": "", "model": "OFFLINE", "color": "001", "size": "M"}}})}]
    evidence = order_image_evidence(store, pilot_order(), results, prior)[0]
    assert evidence["valid_three_views"] and evidence["identity_verified"] and evidence["associated_with_order"]
    assert len(evidence["views"]) == 3
    assert not order_image_evidence(store, pilot_order(), results, [])[0]["identity_verified"]


def test_server_order_probe_refuses_enabled_automation(monkeypatch, tmp_path):
    from scripts import validate_kering_portal as validation
    config_store = ConfigStore(tmp_path)
    pilot_config(config_store, auto_enabled=True)
    monkeypatch.setattr(validation, "data_root", lambda: tmp_path)
    monkeypatch.setattr(validation, "db_engine", Mock(return_value=Mock()))
    reader = Mock()
    monkeypatch.setattr(validation, "configured_orders", reader)
    result = validation.validate_one_order(config_store, 1)
    assert result == {"passed": False, "code": "automatizacion_activa"}
    reader.assert_not_called()
    assert not ImageStore(tmp_path).history()


def test_media_rejects_html_redirects_and_untrusted_hosts(monkeypatch):
    from kering_images import SessionExpired
    from kering_portal import allowed_url
    portal = configured_portal()
    context = MagicMock()
    portal._context = context
    response = context.request.get.return_value
    response.status = 200
    response.ok = True
    response.headers = {"content-type": "text/html"}
    assert portal._read_image("https://picture.kecdn.net/offline") is None
    response.body.assert_not_called()
    response.status = 302
    with pytest.raises(SessionExpired):
        portal._read_image("https://picture.kecdn.net/offline")
    assert response.dispose.call_count == 2
    assert not allowed_url("https://attacker.example/p/1")
    assert not allowed_url("https://my.keringeyewear.com@attacker.example")
    assert not allowed_url("http://picture.kecdn.net/offline", media=True)


def test_real_validation_requires_explicit_network_consent():
    from scripts.validate_kering_portal import main
    with pytest.raises(SystemExit):
        main(["--ean", "0001"])


def test_sqlite_connections_are_closed_even_after_error(tmp_path, monkeypatch):
    import sqlite3
    original_connect = sqlite3.connect
    connections = []
    def tracked_connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connections.append(connection)
        return connection
    monkeypatch.setattr(sqlite3, "connect", tracked_connect)
    store = ImageStore(tmp_path)
    store.history()
    with pytest.raises(RuntimeError):
        with store.connect():
            raise RuntimeError("offline")
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
    store.database.unlink()