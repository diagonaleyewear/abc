import pytest


@pytest.fixture(autouse=True)
def isolated_process_receipts(tmp_path, monkeypatch):
    monkeypatch.setenv("PROCESS_ACTIVITY_PATH", str(tmp_path / "activity.sqlite3"))
