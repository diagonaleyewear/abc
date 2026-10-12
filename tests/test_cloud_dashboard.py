from datetime import date
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from unittest.mock import Mock

from cryptography.fernet import Fernet
import pytest
from streamlit.testing.v1 import AppTest

import build_info
import cloud_dashboard as dashboard
from kering_images import ConfigStore, ImageStore
from order_alerts import OrderAlertStore
from process_activity import activity_path, record_process, read_state
from scripts.write_build_info import write_build_info


@pytest.fixture(autouse=True)
def offline(tmp_path, monkeypatch):
    original_main = sys.modules.get("__main__")
    monkeypatch.setenv("IMAGE_REPOSITORY_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("KERING_DATA_ROOT", str(tmp_path / "kering"))
    monkeypatch.setenv("ORDER_ALERT_STATE_PATH", str(tmp_path / "alerts.sqlite3"))
    monkeypatch.setenv("KERING_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("KERING_ENCRYPTION_KEY_FILE", raising=False)
    yield
    if original_main is not None:
        sys.modules["__main__"] = original_main


def kering_run(tmp_path, status="Completo", ended=200, heartbeat=200):
    root = tmp_path / "kering"
    config = ConfigStore(root)
    config.save({**config.load(), "supplier_id": 7})
    store = ImageStore(root)
    order = {"id": 1, "name": "SYNTHETIC-A", "date_order": "2026-09-02 12:00:00",
             "lines": [{"id": 1, "product_id": 1, "ean": "0012345678901"}]}
    run = store.create_run([order], "synthetic", 7, date(2026, 9, 1), date(2026, 9, 3))
    results = {"0012345678901": {"reason": "vistas_sin_identificar",
                                "acquisition": {"available_files": 3}}}
    with store.connect() as connection:
        connection.execute("UPDATE runs SET started=100, ended=?, heartbeat=?, status=?",
                           (ended, heartbeat, "Finalizado" if ended else "En proceso"))
        connection.execute("UPDATE attempts SET status=?, results=?", (status, json.dumps(results)))
    return store, run


def test_home_without_state_is_read_only(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Home must not acquire, query Odoo, or scan assets")
    monkeypatch.setattr("kering_images.read_orders", forbidden)
    monkeypatch.setattr("kering_images.ImageStore.__init__", forbidden)
    monkeypatch.setattr("image_repository.ImageRepository.records", forbidden)
    monkeypatch.setattr("kering_portal.KeringPortal._start", forbidden)
    result = dashboard.dashboard_snapshot("admin", now=300)
    assert all(p.state == "Sin información" for p in result["processes"])
    assert not (tmp_path / "images").exists()
    assert not (tmp_path / "kering").exists()
    assert not activity_path().exists()
    with pytest.raises(PermissionError):
        dashboard.dashboard_snapshot("anonymous")


def test_kering_no_runs_but_disabled_config(tmp_path):
    config = ConfigStore(tmp_path / "kering")
    config.save({**config.load(), "supplier_id": 7})
    result = dashboard.read_kering(300)
    assert result.result == "Sin ejecuciones"
    assert result.schedule == "Programación desactivada"


@pytest.mark.parametrize("state,ended,heartbeat,expected", [
    ("Completo", 200, 200, "Correcto"),
    ("Parcial", 200, 200, "Parcial — clasificación pendiente"),
    ("Error", 200, 200, "Error"),
    ("En proceso", None, 290, "En ejecución"),
    ("En proceso", None, 100, "Sin información"),
])
def test_real_schema_statuses(tmp_path, state, ended, heartbeat, expected):
    store, run = kering_run(tmp_path, state, ended, heartbeat)
    now = 1000 if expected == "Sin información" else 300
    result = dashboard.read_kering(now)
    assert result.state == expected
    assert result.schedule == "Programación desactivada"
    assert result.next_run is None
    assert result.counts["Pedidos"] == 1
    assert result.last_success == (200 if state == "Completo" else None)


def test_kering_no_canonical_views_not_success_and_unknown_preserved(tmp_path):
    store, run = kering_run(tmp_path, "Parcial")
    before = store.database.read_bytes()
    result = dashboard.read_kering(300)
    assert result.last_success is None
    assert "V1/V2/V3" in result.attention
    assert store.database.read_bytes() == before


def test_alert_scheduler_not_active_job_and_result_independent(tmp_path):
    store = OrderAlertStore(tmp_path / "alerts.sqlite3")
    store.record_heartbeat("auto", 300, started=True, now=100)
    store.record_check("auto", "enviado", sent_count=2, now=150)
    result = dashboard.read_alerts(200)
    assert result.state == "Correcto"  # A heartbeat never proves a running query.
    assert result.schedule == "Activada"
    assert result.counts == {"Alertas enviadas": 2}
    store.record_stopped("auto", now=200)
    assert dashboard.read_alerts(210).schedule == "Programación desactivada"
    store.record_check("manual", "error_consulta", error="secret@example.invalid", now=220)
    result = dashboard.read_alerts(230)
    assert result.state == "Error"
    assert "secret" not in repr(result)


def test_receipts_active_error_and_counts_without_sensitive_data():
    with record_process("nas-sync") as receipt:
        receipt["counts"] = {"Archivos": 3}
        result = dashboard.read_journal("nas-sync", dashboard.time.time())
        assert result.state == "En ejecución"
    result = dashboard.read_journal("nas-sync", dashboard.time.time())
    assert result.state == "Correcto"
    assert result.counts == {"Archivos": 3}
    assert result.duration is not None
    with pytest.raises(ValueError):
        with record_process("nas-sync"):
            raise ValueError("secret password or path")
    result = dashboard.read_journal("nas-sync", dashboard.time.time())
    assert result.state == "Error"
    assert result.diagnostic == "ValueError"
    with read_state(activity_path()) as connection:
        assert "secret" not in repr([dict(row) for row in connection.execute("SELECT * FROM process_runs")])
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM process_runs")


def test_concurrent_receipt_stays_active_and_stale_is_not_running():
    with record_process("nas-sync"):
        with record_process("nas-sync"):
            pass
        result = dashboard.read_journal("nas-sync", dashboard.time.time())
        assert result.state == "En ejecución" and result.result == "Correcto"
        with sqlite3.connect(activity_path()) as connection:
            connection.execute("UPDATE process_runs SET heartbeat=1 WHERE ended IS NULL")
        assert dashboard.read_journal("nas-sync", dashboard.time.time()).state != "En ejecución"


def test_existing_download_and_upload_operations_record_sanitized_results(monkeypatch):
    import graph_mail_downloader as graph
    import luxoptica_auto_upload as lux
    summary = graph.DownloadSummary(2, 1, 1, ["private/path"], ["private-message-id"])
    monkeypatch.setattr(graph, "_download_luxoptica_mail_attachments", Mock(return_value=summary))
    assert graph.download_luxoptica_mail_attachments() is summary
    monkeypatch.setattr(lux, "_prepare_luxoptica_upload", Mock(return_value=(False, "private reason")))
    assert lux.upload_to_luxoptica() == (False, "private reason")
    assert dashboard.read_journal("luxoptica-upload", dashboard.time.time()).result == "Error"
    assert dashboard.read_journal("luxoptica-mail", dashboard.time.time()).counts == {
        "Correos revisados": 2, "Adjuntos descargados": 1}
    with read_state(activity_path()) as connection:
        assert "private" not in repr([dict(row) for row in connection.execute("SELECT * FROM process_runs")])


def test_corrupt_and_wrong_key_explicit_error(tmp_path, monkeypatch):
    kering_run(tmp_path)
    monkeypatch.setenv("KERING_ENCRYPTION_KEY", Fernet.generate_key().decode())
    activity_path().write_bytes(b"not sqlite")
    result = dashboard.dashboard_snapshot("admin", 300)
    assert next(p for p in result["processes"] if p.key == "kering").diagnostic == "InvalidToken"
    assert next(p for p in result["processes"] if p.key == "nas-sync").diagnostic == "DatabaseError"


def ui_app(role="admin"):
    st = __import__("streamlit")
    from cloud_dashboard import render_dashboard, ProcessStatus
    st.session_state["auth_role"] = role
    st.session_state.setdefault("queries", 0)

    def loader(role):
        st.session_state["queries"] += 1
        return {"role": role, "queried": 300, "processes": [
            ProcessStatus("kering", "Kering · sintético", "kering", state="Parcial — clasificación pendiente",
                          result="Parcial — clasificación pendiente", attention="Vistas pendientes"),
            ProcessStatus("luxoptica-upload", "Luxoptica · sintético", "master", state="Sin información")]}

    render_dashboard({}, loader=loader)


def test_ui_queries_only_entry_and_explicit_refresh():
    app = AppTest.from_function(ui_app).run()
    assert not app.exception
    assert app.session_state["queries"] == 1
    app.run()
    app.selectbox[0].select("Luxoptica · sintético").run()
    assert app.session_state["queries"] == 1
    app.button[0].click().run()
    assert app.session_state["queries"] == 2
    state = {"cloud_home_route": "", "cloud_home_snapshot": app.session_state["cloud_home_snapshot"]}
    dashboard.dashboard_transition(state, "imagenes-kering")
    dashboard.dashboard_transition(state, "")
    assert "cloud_home_snapshot" not in state
    del app.session_state["cloud_home_snapshot"]
    app.run()
    assert app.session_state["queries"] == 3


def test_permission_sources_and_navigation_contract(monkeypatch):
    forbidden = Mock(side_effect=AssertionError("No admin sources for masterdata"))
    monkeypatch.setattr(dashboard, "read_kering", forbidden)
    monkeypatch.setattr(dashboard, "read_alerts", forbidden)
    result = dashboard.dashboard_snapshot("masterdata")
    assert [p.key for p in result["processes"]] == ["luxoptica-upload"]
    source = Path(dashboard.__file__).read_text(encoding="utf-8")
    assert "run_every" not in source
    assert "get_db_engine" not in source
    app = AppTest.from_function(ui_app, args=("anonymous",)).run()
    assert app.error
    assert app.session_state["queries"] == 0


def test_build_matches_exact_artifact_inputs(tmp_path, monkeypatch):
    values = {"BUILD_COMMIT": "a" * 40, "BUILD_PUBLISHED": "2026-10-10T12:00:00Z", "BUILD_ID": "actions-test-1"}
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("BUILD_VERSION", raising=False)
    destination = tmp_path / "build-info.json"
    write_build_info(destination)
    monkeypatch.setattr(build_info, "BUILD_FILE", destination)
    monkeypatch.setenv("BUILD_COMMIT", "b" * 40)
    info = build_info.load_build_info()
    assert info["commit"] == "a" * 40  # runtime env cannot falsify artifact provenance.
    assert info["version"] == "sha-" + "a" * 12
    assert info["published"] == values["BUILD_PUBLISHED"]
    destination.write_text("{}")
    with pytest.raises(ValueError):
        build_info.load_build_info()


def test_supplied_logo_bytes_and_proportions():
    from PIL import Image
    root = Path(dashboard.__file__).parent
    logo = root / "logo" / "logo.png"
    assert hashlib.sha256(logo.read_bytes()).hexdigest() == "fc1f85ef659f698ef404744abdf2965da39ca89b8683a1ecdf3afc896f79ef58"
    with Image.open(logo) as image:
        assert image.size == (1200, 384)
    source = Path(dashboard.__file__).read_text(encoding="utf-8")
    assert "width:240px;max-width:100%;height:auto;border:0" in source
    assert 'alt="Óptica Diagonal"' in source
    assert "height=" not in source


def test_ci_runtime_packaging_and_metadata_no_duplicate_yaml_keys():
    root = Path(dashboard.__file__).parent
    workflow = (root / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
    assert "BUILD_COMMIT=${{ steps.provenance.outputs.commit }}" in workflow
    assert "org.opencontainers.image.revision=${{ steps.provenance.outputs.commit }}" in workflow
    assert "load: ${{ github.event_name == 'pull_request' }}" in workflow
    assert workflow.count("          labels:") == 1
    dockerfile = (root / "Dockerfile").read_text()
    assert "COPY image_repository.py image_naming.py image_exports.py kering_media.py ." in dockerfile
    assert "COPY logo ./logo" in dockerfile
    assert "--network none" in workflow
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    assert compose.count("build: *cloud-build") == 4
    assert "BUILD_COMMIT: ${BUILD_COMMIT:-}" in compose
