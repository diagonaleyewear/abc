import re
from pathlib import Path

from scripts import audit_vytria_pricing as audit

FORBIDDEN = re.compile(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|COPY)\b", re.IGNORECASE)


def _statements():
    yield from audit.QUERIES.values()
    yield audit.SAMPLE_SELECTION
    yield audit.SAMPLE_TAXES
    yield audit.SAMPLE_ITEMS
    yield audit.SAMPLE_CHANNELS


def test_audit_statements_are_select_only():
    for sql in _statements():
        assert sql.strip().upper().startswith("SELECT")
        assert not FORBIDDEN.search(sql)


def test_audit_forces_read_only_session(monkeypatch):
    captured = {}
    monkeypatch.setattr(audit, "load_db_config", lambda: type(
        "Config", (), {"user": "u", "password": "p", "host": "h", "port": 5432, "database": "d"})())
    monkeypatch.setattr(audit, "create_engine", lambda url, **kwargs: captured.update(kwargs) or object())

    audit.build_engine()

    assert "default_transaction_read_only=on" in captured["connect_args"]["options"]
    assert "statement_timeout" in captured["connect_args"]["options"]


def test_audit_source_never_prints_credentials():
    source = Path(audit.__file__).read_text(encoding="utf-8")

    assert "password" not in source.split("def main", 1)[1].lower()
