from auth_session import (
    AuthStateStore,
    create_auth_token,
    derive_session_signing_key,
    login_bucket_keys,
    verify_auth_token,
)
from concurrent.futures import ThreadPoolExecutor


def test_signed_token_expires_and_rejects_tampering() -> None:
    signing_key = derive_session_signing_key("s" * 40, "admin", "old-password")
    token = create_auth_token("admin", signing_key, days=1, now=1000)

    claims = verify_auth_token(token, "admin", signing_key, now=1001)

    assert claims is not None
    assert claims.username == "admin"
    assert verify_auth_token(token, "admin", "wrong-secret", now=1001) is None
    assert verify_auth_token(token, "admin", signing_key, now=87401) is None
    changed_password_key = derive_session_signing_key("s" * 40, "admin", "new-password")
    assert verify_auth_token(token, "admin", changed_password_key, now=1001) is None


def test_revocation_is_persistent_across_store_instances(tmp_path) -> None:
    database_path = tmp_path / "auth-state.sqlite3"
    store = AuthStateStore(database_path)
    token = create_auth_token("admin", "s" * 40, days=1, now=1000)
    claims = verify_auth_token(token, "admin", "s" * 40, now=1001)
    assert claims is not None

    store.revoke_session(claims.session_id, claims.expires_at, now=1001)

    reopened_store = AuthStateStore(database_path)
    assert reopened_store.is_revoked(claims.session_id, now=1002)
    assert not reopened_store.is_revoked(claims.session_id, now=claims.expires_at + 1)


def test_login_attempts_are_limited_by_ip_and_identity(tmp_path) -> None:
    store = AuthStateStore(tmp_path / "auth-state.sqlite3")
    bucket_keys = login_bucket_keys("admin", "192.0.2.4")

    assert store.allow_login_attempt(bucket_keys, maximum_attempts=2, window_seconds=60, now=100)
    assert store.allow_login_attempt(bucket_keys, maximum_attempts=2, window_seconds=60, now=101)
    assert not store.allow_login_attempt(bucket_keys, maximum_attempts=2, window_seconds=60, now=102)
    assert store.allow_login_attempt(bucket_keys, maximum_attempts=2, window_seconds=60, now=161)

    store.reset_login_attempts(bucket_keys)
    assert store.allow_login_attempt(bucket_keys, maximum_attempts=2, window_seconds=60, now=162)


def test_concurrent_login_attempts_cannot_exceed_limit(tmp_path) -> None:
    store = AuthStateStore(tmp_path / "auth-state.sqlite3")
    bucket_keys = login_bucket_keys("admin", "192.0.2.8")

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(
            executor.map(
                lambda _: store.allow_login_attempt(
                    bucket_keys,
                    maximum_attempts=3,
                    window_seconds=60,
                    now=100,
                ),
                range(8),
            )
        )

    assert sum(results) == 3


def _load_auth_cookie_remover(controller):
    import ast
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "app_enhanced.py"
    module = ast.parse(source.read_text(encoding="utf-8"))
    function = next(node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == "_remove_auth_cookie")
    namespace = {"_get_cookie_controller": lambda: controller, "AUTH_COOKIE_NAME": "synthetic_auth_cookie"}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)
    return namespace["_remove_auth_cookie"]


def test_remove_auth_cookie_is_idempotent_with_real_controller(monkeypatch):
    import streamlit_cookies_controller.cookie_controller as cookies_module

    commands = []
    monkeypatch.setattr(cookies_module, "_cookie_controller", lambda **kwargs: commands.append(kwargs))
    controller = cookies_module.CookieController.__new__(cookies_module.CookieController)
    controller._CookieController__cookies = {}
    remove = _load_auth_cookie_remover(controller)

    remove()
    controller._CookieController__cookies["synthetic_auth_cookie"] = "synthetic_token"
    remove()
    remove()

    assert controller._CookieController__cookies == {}
    assert len(commands) == 3
    assert all(command["method"] == "remove" and command["name"] == "synthetic_auth_cookie" for command in commands)


def test_remove_auth_cookie_does_not_hide_unrelated_errors():
    import pytest

    class BrokenController:
        def remove(self, name):
            raise KeyError("unrelated_key")

    remove = _load_auth_cookie_remover(BrokenController())
    with pytest.raises(KeyError, match="unrelated_key"):
        remove()
