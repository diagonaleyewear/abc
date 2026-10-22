"""Provider requests must live on the persistent volume, not in the image filesystem (#59)."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

import provider_images_ui
from provider_images import ProviderSettings, ProviderStore, StoreMigrationRequired
from provider_support import EANS

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = ProviderSettings("marcolin", max_eans_per_request=50, max_requests_per_day=50)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("PROVIDER_DATA_ROOT", raising=False)
    monkeypatch.delenv("KERING_DATA_ROOT", raising=False)


def test_explicit_provider_root_wins(monkeypatch):
    monkeypatch.setenv("PROVIDER_DATA_ROOT", "/app/data/providers")
    monkeypatch.setenv("KERING_DATA_ROOT", "/elsewhere/kering")
    assert provider_images_ui.data_root() == Path("/app/data/providers")


def test_default_sits_next_to_the_kering_state_on_the_persistent_volume(monkeypatch):
    monkeypatch.setenv("KERING_DATA_ROOT", "/app/data/kering")
    assert provider_images_ui.data_root() == Path("/app/data/providers")


def test_local_default_is_unchanged_without_any_variable():
    assert provider_images_ui.data_root() == ROOT / "masterdata_data" / "providers"


def test_compose_template_sets_the_provider_root_on_the_data_volume():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "PROVIDER_DATA_ROOT: /app/data/providers" in compose
    assert "./masterdata_data:/app/data" in compose


def test_requests_survive_recreating_the_container(tmp_path, monkeypatch):
    volume = tmp_path / "app" / "data"  # the persistent volume survives; the container filesystem does not
    monkeypatch.setenv("PROVIDER_DATA_ROOT", str(volume / "providers"))
    first = ProviderStore(provider_images_ui.data_root())
    plan = first.plan(SETTINGS, EANS[:2])
    rid = plan["created"][0]
    assert (volume / "providers" / "provider_requests.sqlite").is_file()
    del first

    second = ProviderStore(provider_images_ui.data_root())  # a new container, same volume
    assert second.get(rid)["state"] in {"ready", "pending"}
    again = second.plan(SETTINGS, EANS[:2])  # the live request is recognised, never duplicated
    assert not again["created"]


def test_store_never_migrates_a_legacy_database_on_startup(tmp_path):
    import sqlite3
    root = tmp_path / "providers"
    root.mkdir()
    db = root / "provider_requests.sqlite"
    with sqlite3.connect(db) as c:
        c.executescript(
            "CREATE TABLE requests(id INTEGER PRIMARY KEY, provider TEXT, mode TEXT, remote_id TEXT, state TEXT,"
            " attempts INT, polls INT, last_error TEXT, retry_of INT, created_at REAL, updated_at REAL,"
            " next_poll_at REAL);"
            "CREATE TABLE request_eans(request_id INT, provider TEXT, ean TEXT, result TEXT, live INT,"
            " PRIMARY KEY(request_id, ean));")
    before = db.read_bytes()
    with pytest.raises(StoreMigrationRequired):
        ProviderStore(root)
    assert db.read_bytes() == before


def test_rendering_the_page_creates_no_requests_and_contacts_no_portal(tmp_path):
    app = (
        "import socket\n"
        "def blocked(*a, **k): raise AssertionError('network access while rendering')\n"
        "socket.socket.connect = blocked\n"
        "from provider_images_ui import render_provider_page\n"
        "render_provider_page(lambda: (_ for _ in ()).throw(RuntimeError('sin Odoo')))\n"
    )
    code = (
        "from streamlit.testing.v1 import AppTest\n"
        f"at = AppTest.from_string({app!r}, default_timeout=60).run()\n"
        "assert not at.exception, [e.value for e in at.exception]\n"
    )
    root = tmp_path / "providers"
    env = {**os.environ, "PROVIDER_DATA_ROOT": str(root), "PYTHONPATH": str(ROOT),
           "IMAGE_REPOSITORY_ROOT": str(tmp_path / "repo")}
    run = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr[-800:]
    assert not (tmp_path / "repo").exists()  # the image repository stays lazy
    store = root / "provider_requests.sqlite"
    if store.exists():
        import sqlite3
        with sqlite3.connect(store) as c:
            assert c.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0
