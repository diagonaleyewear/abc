import re

from scripts import audit_vytria_pricing_tables as audit

FORBIDDEN = re.compile(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|COPY)\b", re.IGNORECASE)


def test_static_statements_are_select_only():
    for sql in (audit.COLUMNS, audit.FOREIGN_KEYS, audit.PRICELISTS, audit.PRICELIST_ITEM_SUMMARY,
                audit.PRODUCTS, audit.TAXES, audit.PRODUCT_ITEMS):
        assert sql.strip().upper().startswith("SELECT")
        assert not FORBIDDEN.search(sql)


def test_audit_forces_read_only_session(monkeypatch):
    captured = {}
    monkeypatch.setattr(audit, "load_db_config", lambda: type(
        "Config", (), {"user": "u", "password": "p", "host": "h", "port": 5432, "database": "d"})())
    monkeypatch.setattr(audit, "create_engine", lambda url, **kwargs: captured.update(kwargs) or object())

    audit.build_engine()

    assert "default_transaction_read_only=on" in captured["connect_args"]["options"]


def test_barcode_forms_cover_upc_with_and_without_leading_zero():
    assert audit.barcode_forms("889652637075") == ["0889652637075", "889652637075"]
    assert audit.barcode_forms("0889652637075") == ["0889652637075", "889652637075"]
    assert audit.barcode_forms("8056597981361") == ["8056597981361"]


def test_redaction_hides_secrets_and_url_credentials():
    row = {"name": "Tienda", "api_key": "abc", "Password": "x", "url": "https://user:pw@shop.example/api?ws_key=SECRET",
           "nested": {"access_token": "t", "price": 10.5}}

    result = audit.redact(row)

    assert result["api_key"] == "<redacted>" and result["Password"] == "<redacted>"
    assert result["url"] == "https://shop.example/api"
    assert result["nested"] == {"access_token": "<redacted>", "price": 10.5}


def test_identifiers_are_validated_before_being_quoted():
    assert audit.safe_identifier("product_id") == '"product_id"'
    for bad in ('x"; DROP TABLE y;--', "Prod uct", "1abc"):
        try:
            audit.safe_identifier(bad)
        except ValueError:
            continue
        raise AssertionError(bad)
