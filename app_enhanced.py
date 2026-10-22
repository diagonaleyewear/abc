from __future__ import annotations

from functools import partial
from pathlib import Path
from image_repository import IMAGE_REPOSITORY_ROOT_ENV, repository_root
from datetime import date, datetime, timedelta
import hmac
import json
import os
import time

import numpy as np
import pandas as pd
import streamlit as st
import plotly.express as px
import plotly.graph_objects as go
from sqlalchemy import create_engine, text, URL
from streamlit_cookies_controller import CookieController

from db_loader import load_odoo_dataframe
from adyen_reconciliation_ui import render_reconciliation_page
from kering_images_ui import render_kering_page, render_kering_settings
from engine import run_abcd_engine
from db_config import load_db_config, load_env_file
from transform_luxottica_masterdata import (
    build_brand_dictionary_from_db,
    build_color_dictionary_from_db,
    build_shape_dictionary_from_db,
    EXPECTED_OUTPUT_COLUMNS,
    build_executive_summary_markdown,
    transform_masterdata,
)
from validate_masterdata_odoo_dryrun import (
    analyze_masterdata_against_odoo,
    build_summary_markdown,
    load_odoo_snapshot,
)
from graph_mail_downloader import (
    download_luxoptica_mail_attachments,
    load_m365_config,
    _refresh_pending_market_images,
    _model_from_image_name,
    send_alert_email,
    validate_m365_config,
)
from watchlist_config import add_customer, load_watchlist, remove_customer, save_watchlist
from check_pedidos_vigilados import find_matching_orders
from order_alerts import (
    LEGACY_NOTIFICATIONS_FILE,
    RESULT_LABELS,
    SOURCE_AUTO,
    SOURCE_MANUAL,
    OrderAlertStore,
    alert_recipients,
    dispatch_order_alerts,
    import_legacy_notification_file,
    scheduler_state,
)
from auth_session import (
    AuthStateStore,
    create_auth_token,
    derive_session_signing_key,
    login_bucket_keys,
    verify_auth_token,
)
from app_exports import (
    dataframe_to_excel_bytes,
    extract_clean_eans as _extract_clean_eans,
    generate_luxoptica_request_files,
    load_masterdata_file,
    normalize_result_export_schema,
    split_report_warnings,
)
from app_navigation import build_navigation
from app_pages.alerts import render_alerts_page
from app_pages.luxoptica import render_images_page, render_pending_page
from abcd_snapshots import (
    ABCD_SNAPSHOT_TABLE,
    build_abcd_report_excel_df,
    build_abcd_report_export_df,
    build_snapshot_parameters,
    current_week_start as _current_week_start,
    engine_source_version,
    ensure_abcd_snapshot_table as _ensure_abcd_snapshot_table,
    list_abcd_snapshot_refs,
    list_abcd_snapshot_dates,
    load_abcd_snapshot_by_id,
    load_abcd_weekly_snapshot,
    save_abcd_weekly_snapshot,
)

# ==============================================================================
# CONFIG
# ==============================================================================

APP_TITLE = "Cloud Diagonal"
APP_ICON = Path(__file__).resolve().parent / "logo" / "favicon.svg"
AUTH_USERNAME_ENV = "APP_USERNAME"
AUTH_COOKIE_SECRET_ENV = "AUTH_COOKIE_SECRET"
AUTH_REMEMBER_DAYS_ENV = "AUTH_REMEMBER_DAYS"
AUTH_REMEMBER_DAYS_DEFAULT = 30
AUTH_COOKIE_NAME = "abc_auth_session"
AUTH_PASSWORD_ENV = "APP_PASSWORD"
AUTH_STATE_PATH_ENV = "AUTH_STATE_PATH"
AUTH_LOGIN_MAX_ATTEMPTS_ENV = "AUTH_LOGIN_MAX_ATTEMPTS"
AUTH_LOGIN_WINDOW_SECONDS_ENV = "AUTH_LOGIN_WINDOW_SECONDS"
AUTH_COOKIE_SECRET_MIN_LENGTH = 32
AUTH_LOGIN_MAX_ATTEMPTS_DEFAULT = 8
AUTH_LOGIN_WINDOW_SECONDS_DEFAULT = 900
MASTERDATA_USERNAME_ENV = "MASTERDATA_USERNAME"
MASTERDATA_PASSWORD_ENV = "MASTERDATA_PASSWORD"
LUXOPTICA_URL_ENV = "LUXOPTICA_URL"
LUXOPTICA_USER_ENV = "LUXOPTICA_USERNAME"
LUXOPTICA_PASSWORD_ENV = "LUXOPTICA_PASSWORD"
LUXOPTICA_REQUEST_EMAIL_ENV = "LUXOPTICA_REQUEST_EMAIL"
LEGACY_ESSILOR_URL_ENV = "ESSILOR_URL"
LEGACY_ESSILOR_USER_ENV = "ESSILOR_USERNAME"
LEGACY_ESSILOR_PASSWORD_ENV = "ESSILOR_PASSWORD"
M365_TENANT_ID_ENV = "M365_TENANT_ID"
M365_CLIENT_ID_ENV = "M365_CLIENT_ID"
M365_CLIENT_SECRET_ENV = "M365_CLIENT_SECRET"
M365_MAILBOX_ENV = "M365_MAILBOX"
M365_DOWNLOAD_ROOT_ENV = IMAGE_REPOSITORY_ROOT_ENV
MASTERDATA_DICTIONARY_FILE = Path(
    os.getenv(
        "MASTERDATA_DICTIONARY_PATH",
        str(Path(__file__).resolve().parent / "docs" / "MasterData" / "masterdata_dictionary.json"),
    )
)
MASTERDATA_DOWNLOAD_COLUMNS = [
    "Barcode",
    "Categoría",
    "Marca",
    "Colección",
    "Modelo",
    "Color",
    "Calibre",
    "Ancho Puente",
    "Longitud Varilla",
    "Género",
    "Color Frontal",
    "Color Lente",
    "Forma",
    "Material Principal",
    "Fotocromático",
    "Polarizado",
    "PVO",
    "PVP",
]

st.set_page_config(
    page_title=APP_TITLE,
    page_icon=APP_ICON,
    layout="wide",
    initial_sidebar_state="expanded",
)

# ==============================================================================
# FUNCIONES AUXILIARES
# ==============================================================================

@st.cache_resource
def get_db_engine():
    """Obtiene conexión a BD (cacheada)"""
    config = load_db_config()
    url = URL.create(
        "postgresql+psycopg2",
        username=config.user,
        password=config.password,
        host=config.host,
        port=config.port,
        database=config.database,
    )
    return create_engine(url)

@st.cache_data(ttl=300)  # Cache por 5 minutos
def load_data_cached():
    """Carga datos con cache"""
    return load_odoo_dataframe()


@st.cache_data(ttl=300)
def load_odoo_snapshot_cached():
    """Carga snapshot de Odoo para dry-run de importacion."""
    return load_odoo_snapshot()


@st.cache_data(ttl=300)
def load_masterdata_reference_maps():
    """Carga diccionarios de apoyo para transformacion MASTERDATA."""
    return (
        build_color_dictionary_from_db(),
        build_brand_dictionary_from_db(),
        build_shape_dictionary_from_db(),
    )


def load_masterdata_dictionary() -> list[dict[str, str]]:
    if not MASTERDATA_DICTIONARY_FILE.exists():
        return []
    try:
        payload = json.loads(MASTERDATA_DICTIONARY_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return payload if isinstance(payload, list) else payload.get("rules", [])


def save_masterdata_dictionary(rules: list[dict[str, str]]) -> None:
    MASTERDATA_DICTIONARY_FILE.parent.mkdir(parents=True, exist_ok=True)
    MASTERDATA_DICTIONARY_FILE.write_text(
        json.dumps({"rules": rules}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def render_master_dictionary_page() -> None:
    render_sidebar_shell("Master Data")
    cloud_page_title("Diccionario Masterdata")
    st.caption("Define transformaciones personalizadas por columna y valor exacto.")
    st.info("Las columnas del Excel origen son fijas y se cargan automáticamente.")
    source_columns = sorted(EXPECTED_OUTPUT_COLUMNS, key=str.casefold)

    rules = load_masterdata_dictionary()
    editor_df = pd.DataFrame(rules, columns=["Columna", "Valor", "Transformado"])
    edited_df = st.data_editor(
        editor_df,
        column_config={
            "Columna": st.column_config.SelectboxColumn(
                "Columna",
                options=source_columns,
                required=True,
            ),
            "Valor": st.column_config.TextColumn("Valor", required=True),
            "Transformado": st.column_config.TextColumn("Transformado", required=False),
        },
        num_rows="dynamic",
        hide_index=True,
        key="masterdata_dictionary_editor",
    )
    if st.button("Guardar diccionario", type="primary"):
        clean_rules = []
        for row in edited_df.fillna("").to_dict("records"):
            rule = {key: str(row.get(key, "")).strip() for key in ["Columna", "Valor", "Transformado"]}
            if any(rule.values()):
                if not rule["Columna"] or not rule["Valor"]:
                    st.error("Cada regla debe tener Columna y Valor. Transformado puede quedar vacío.")
                    return
                clean_rules.append(rule)
        save_masterdata_dictionary(clean_rules)
        st.success(f"Diccionario guardado: {len(clean_rules)} regla(s).")
        st.rerun()

    if rules:
        st.info(f"Reglas activas: {len(rules)}. Se aplican antes de los diccionarios de Odoo.")
    else:
        st.info("No hay reglas personalizadas todavía.")


def _load_market_pending_rows(images_root: Path) -> list[dict[str, object]]:
    from repository_storage import state_root
    pending_path = state_root(images_root) / ".market_pending.json"
    if not pending_path.exists():
        return []

    try:
        payload = json.loads(pending_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"No se pueden leer los pendientes {pending_path}") from error

    if not isinstance(payload, dict):
        return []

    rows: list[dict[str, object]] = []
    for source_name, entry in payload.items():
        if not isinstance(entry, dict):
            continue
        source = Path(source_name)
        rows.append(
            {
                "Modelo": _model_from_image_name(source.name),
                "EAN": str(entry.get("ean", "")),
                "Archivo": source.name,
                "Mercados pendientes": ", ".join(entry.get("missing_markets", [])),
                "Ruta": source_name,
            }
        )
    return rows


def _read_env_setting(key: str, default: str = "") -> str:
    load_env_file()
    return os.getenv(key, "").strip() or default


def _read_env_setting_any(keys: list[str], default: str = "") -> str:
    load_env_file()
    for key in keys:
        value = os.getenv(key, "").strip()
        if value:
            return value
    return default


def _upsert_env_settings(updates: dict[str, str], env_path: str | Path = ".env") -> None:
    path = Path(env_path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path

    lines: list[str] = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()

    applied: set[str] = set()
    out_lines: list[str] = []

    for line in lines:
        item = line.strip()
        if not item or item.startswith("#") or "=" not in line:
            out_lines.append(line)
            continue

        key, _ = line.split("=", 1)
        key = key.strip()
        if key in updates:
            out_lines.append(f"{key}={updates[key]}")
            applied.add(key)
        else:
            out_lines.append(line)

    for key, value in updates.items():
        if key not in applied:
            out_lines.append(f"{key}={value}")

    path.write_text("\n".join(out_lines).rstrip() + "\n", encoding="utf-8")


def _first_available_selector(page, selectors: list[str]) -> str:
    for selector in selectors:
        locator = page.locator(selector)
        if locator.count() > 0 and locator.first.is_visible():
            return selector
    return ""


def _attempt_essilor_auto_login(url: str, username: str, password: str) -> tuple[bool, str]:
    if not url.strip() or not username.strip() or not password.strip():
        return False, "Faltan URL, usuario o contraseña en la configuración."

    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except Exception:
        return False, (
            "Playwright no está disponible. Instala dependencias y ejecuta: "
            "python -m playwright install chromium"
        )

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)

            user_selector = _first_available_selector(
                page,
                [
                    "input#signInName",
                    "input[name='signInName']",
                    "input[aria-label='Sign in name']",
                    "input[type='email']",
                    "input[type='text']",
                ],
            )
            if not user_selector:
                browser.close()
                return False, "No se encontró el campo de usuario en la página de login."

            page.fill(user_selector, username)

            continue_selector = _first_available_selector(
                page,
                [
                    "button:has-text('Continue')",
                    "button:has-text('Continuar')",
                    "button#continue",
                    "#continue",
                ],
            )
            if continue_selector:
                page.click(continue_selector)

            password_selector = ""
            for _ in range(20):
                if page.locator("text=LOGIN_USER_NOT_FOUND").count() > 0:
                    browser.close()
                    return False, "Usuario no encontrado en Essilor (LOGIN_USER_NOT_FOUND)."

                password_selector = _first_available_selector(
                    page,
                    [
                        "input#password",
                        "input[name='password']",
                        "input[type='password']",
                    ],
                )
                if password_selector:
                    break
                page.wait_for_timeout(500)

            if not password_selector:
                browser.close()
                return False, "No se encontró el campo de contraseña tras enviar el usuario."

            page.fill(password_selector, password)

            sign_in_selector = _first_available_selector(
                page,
                [
                    "button:has-text('Sign in')",
                    "button:has-text('Iniciar sesión')",
                    "button:has-text('Acceder')",
                    "button:has-text('Continue')",
                    "button:has-text('Continuar')",
                    "button#next",
                    "#next",
                    "button#continue",
                    "#continue",
                ],
            )
            if sign_in_selector:
                page.click(sign_in_selector)

            page.wait_for_timeout(3500)
            current_url = page.url
            browser.close()

            if "b2clogin.com" in current_url.lower():
                return False, (
                    "El login no se completó automáticamente (posible validación adicional/MFA o credenciales inválidas)."
                )

            return True, f"Login automático completado. URL actual: {current_url}"

    except PlaywrightTimeoutError:
        return False, "Timeout durante el login automático."
    except Exception as exc:
        return False, f"Error en login automático: {exc}"

def _get_auth_credentials() -> tuple[str, str]:
    """Obtiene credenciales desde variables de entorno."""
    load_env_file()
    username = os.getenv(AUTH_USERNAME_ENV, "")
    password = os.getenv(AUTH_PASSWORD_ENV, "")
    return username, password


def _get_auth_users() -> dict[str, dict[str, str]]:
    load_env_file()
    users: dict[str, dict[str, str]] = {}
    admin_user = os.getenv(AUTH_USERNAME_ENV, "").strip()
    admin_password = os.getenv(AUTH_PASSWORD_ENV, "")
    if admin_user and admin_password:
        users[admin_user] = {"password": admin_password, "role": "admin"}

    masterdata_user = os.getenv(MASTERDATA_USERNAME_ENV, "").strip()
    masterdata_password = os.getenv(MASTERDATA_PASSWORD_ENV, "")
    if masterdata_user and masterdata_password and masterdata_user not in users:
        users[masterdata_user] = {"password": masterdata_password, "role": "masterdata"}
    return users


def _get_auth_cookie_secret() -> str:
    return os.getenv(AUTH_COOKIE_SECRET_ENV, "").strip()


def _get_auth_state_store() -> AuthStateStore:
    if "_auth_state_store" not in st.session_state:
        default_path = Path(__file__).resolve().parent / "masterdata_data" / "auth_state.sqlite3"
        state_path = Path(os.getenv(AUTH_STATE_PATH_ENV, str(default_path)))
        st.session_state["_auth_state_store"] = AuthStateStore(state_path)
    return st.session_state["_auth_state_store"]


def _get_auth_login_limit(env_name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(env_name, default)))
    except ValueError:
        return default


def _get_remember_days() -> int:
    try:
        return int(os.getenv(AUTH_REMEMBER_DAYS_ENV, AUTH_REMEMBER_DAYS_DEFAULT))
    except ValueError:
        return AUTH_REMEMBER_DAYS_DEFAULT


def _make_auth_token(username: str, secret: str, password: str, days: int) -> str:
    """Genera un token firmado con expiración para recordar la sesión."""
    if len(secret) < AUTH_COOKIE_SECRET_MIN_LENGTH:
        raise ValueError(
            f"{AUTH_COOKIE_SECRET_ENV} debe tener al menos "
            f"{AUTH_COOKIE_SECRET_MIN_LENGTH} caracteres."
        )
    signing_key = derive_session_signing_key(secret, username, password)
    return create_auth_token(username, signing_key, days)


def _verify_auth_token(
    token: str,
    expected_user: str,
    password: str,
    secret: str,
) -> bool:
    """Valida firma, usuario y expiración de un token de sesión recordada."""
    signing_key = derive_session_signing_key(secret, expected_user, password)
    claims = verify_auth_token(token, expected_user, signing_key)
    return bool(claims and not _get_auth_state_store().is_revoked(claims.session_id))


def _get_cookie_controller() -> CookieController:
    if "_cookie_controller" not in st.session_state:
        st.session_state["_cookie_controller"] = CookieController()
    return st.session_state["_cookie_controller"]


def _remove_auth_cookie() -> None:
    try:
        _get_cookie_controller().remove(AUTH_COOKIE_NAME)
    except KeyError as exc:
        if exc.args != (AUTH_COOKIE_NAME,):
            raise


def _render_login() -> None:
    st.title(APP_TITLE)
    st.subheader("Acceso restringido")

    users = _get_auth_users()
    if not users:
        st.error(
            f"Autenticación no configurada. Define {AUTH_USERNAME_ENV}/{AUTH_PASSWORD_ENV}."
        )
        st.stop()

    remember_days = _get_remember_days()
    cookie_secret = _get_auth_cookie_secret()
    can_remember = len(cookie_secret) >= AUTH_COOKIE_SECRET_MIN_LENGTH
    if not can_remember:
        st.info(
            f"Las sesiones recordadas requieren {AUTH_COOKIE_SECRET_ENV} "
            f"con al menos {AUTH_COOKIE_SECRET_MIN_LENGTH} caracteres; no se usa la contraseña como alternativa."
        )
    with st.form("login_form", clear_on_submit=False):
        username = st.text_input("Usuario")
        password = st.text_input("Contraseña", type="password")
        recordarme = st.checkbox(
            f"Recordarme durante {remember_days} días",
            value=can_remember,
            disabled=not can_remember,
        )
        submitted = st.form_submit_button("Entrar")

    if submitted:
        remote_ip = getattr(st.context, "ip_address", None)
        attempt_keys = login_bucket_keys(username, remote_ip)
        max_attempts = _get_auth_login_limit(
            AUTH_LOGIN_MAX_ATTEMPTS_ENV,
            AUTH_LOGIN_MAX_ATTEMPTS_DEFAULT,
        )
        window_seconds = _get_auth_login_limit(
            AUTH_LOGIN_WINDOW_SECONDS_ENV,
            AUTH_LOGIN_WINDOW_SECONDS_DEFAULT,
        )
        if not _get_auth_state_store().allow_login_attempt(
            attempt_keys,
            max_attempts,
            window_seconds,
        ):
            st.error("Demasiados intentos. Espera antes de volver a intentarlo.")
            st.stop()

        user_config = users.get(username)
        if user_config and hmac.compare_digest(password, user_config["password"]):
            _get_auth_state_store().reset_login_attempts(attempt_keys)
            st.session_state["authenticated"] = True
            st.session_state["auth_user"] = username
            st.session_state["auth_role"] = user_config["role"]
            st.session_state.pop("auth_session_id", None)
            st.session_state.pop("auth_session_expires_at", None)
            signing_key = derive_session_signing_key(
                cookie_secret,
                username,
                user_config["password"],
            )
            st.session_state["auth_credential_fingerprint"] = signing_key
            existing_token = _get_cookie_controller().get(AUTH_COOKIE_NAME)
            existing_claims = (
                verify_auth_token(existing_token, username, signing_key)
                if existing_token
                else None
            )
            if existing_claims:
                _get_auth_state_store().revoke_session(
                    existing_claims.session_id,
                    existing_claims.expires_at,
                )
            if recordarme and can_remember:
                token = _make_auth_token(
                    username,
                    cookie_secret,
                    user_config["password"],
                    remember_days,
                )
                claims = verify_auth_token(token, username, signing_key)
                assert claims is not None
                st.session_state["auth_session_id"] = claims.session_id
                st.session_state["auth_session_expires_at"] = claims.expires_at
                _get_cookie_controller().set(
                    AUTH_COOKIE_NAME, token, max_age=remember_days * 86400
                )
            else:
                _remove_auth_cookie()
            st.rerun()
        st.error("Usuario o contraseña incorrectos")

    st.stop()


def require_authentication() -> None:
    """Bloquea la app hasta que el usuario se autentique (incluye sesión recordada por cookie)."""
    users = _get_auth_users()
    if st.session_state.get("authenticated", False):
        expected_user = st.session_state.get("auth_user", "")
        user_config = users.get(expected_user)
        secret = _get_auth_cookie_secret()
        current_fingerprint = (
            derive_session_signing_key(secret, expected_user, user_config["password"])
            if user_config
            else ""
        )
        session_id = st.session_state.get("auth_session_id")
        expires_at = st.session_state.get("auth_session_expires_at")
        session_expired = expires_at is not None and int(expires_at) < int(time.time())
        credentials_changed = (
            not user_config
            or st.session_state.get("auth_role") != user_config["role"]
            or not hmac.compare_digest(
                st.session_state.get("auth_credential_fingerprint", ""),
                current_fingerprint,
            )
        )
        session_revoked = bool(session_id and _get_auth_state_store().is_revoked(session_id))
        if not credentials_changed and not session_expired and not session_revoked:
            return
        st.session_state["authenticated"] = False
        st.session_state.pop("auth_user", None)
        st.session_state.pop("auth_role", None)
        st.session_state.pop("auth_session_id", None)
        st.session_state.pop("auth_session_expires_at", None)
        st.session_state.pop("auth_credential_fingerprint", None)
        _remove_auth_cookie()

    if users:
        token = _get_cookie_controller().get(AUTH_COOKIE_NAME)
        secret = _get_auth_cookie_secret()
        for expected_user, user_config in users.items():
            if token and len(secret) >= AUTH_COOKIE_SECRET_MIN_LENGTH and _verify_auth_token(
                token,
                expected_user,
                user_config["password"],
                secret,
            ):
                signing_key = derive_session_signing_key(
                    secret,
                    expected_user,
                    user_config["password"],
                )
                claims = verify_auth_token(token, expected_user, signing_key)
                assert claims is not None
                st.session_state["authenticated"] = True
                st.session_state["auth_user"] = expected_user
                st.session_state["auth_role"] = user_config["role"]
                st.session_state["auth_session_id"] = claims.session_id
                st.session_state["auth_session_expires_at"] = claims.expires_at
                st.session_state["auth_credential_fingerprint"] = signing_key
                return

    _render_login()


def require_admin_access() -> None:
    if st.session_state.get("auth_role") != "admin":
        st.error("Este apartado requiere un usuario administrador.")
        st.stop()

def get_product_stockout_periods(product_id: int, engine) -> list:
    """Obtiene períodos de agotamiento de un producto"""
    query = text("""
        WITH stock_moves AS (
            SELECT 
                sm.date,
                sm.product_qty,
                CASE 
                    WHEN sl_src.usage = 'supplier' AND sl_dst.usage = 'internal' THEN 'ENTRADA'
                    WHEN sl_src.usage = 'internal' AND sl_dst.usage = 'customer' THEN 'SALIDA'
                    ELSE 'OTRO'
                END as tipo
            FROM stock_move sm
            JOIN stock_location sl_src ON sm.location_id = sl_src.id
            JOIN stock_location sl_dst ON sm.location_dest_id = sl_dst.id
            WHERE sm.product_id = :product_id AND sm.state = 'done'
              AND COALESCE(sm.scrapped, FALSE) = FALSE
              AND COALESCE(sm.is_inventory, FALSE) = FALSE
            ORDER BY sm.date ASC
        )
        SELECT date, tipo, product_qty FROM stock_moves
    """)
    
    with engine.connect() as conn:
        result = conn.execute(query, {"product_id": int(product_id)})
        rows = result.fetchall()
    
    if not rows:
        return []
    
    # Calcular períodos de agotamiento
    stock = 0
    periods = []
    stockout_start = None
    
    for row in rows:
        fecha, tipo, cantidad = row
        
        if tipo == 'ENTRADA':
            stock += cantidad
            if stockout_start:
                periods.append({
                    'inicio': stockout_start,
                    'fin': fecha,
                    'dias': (fecha - stockout_start).days
                })
                stockout_start = None
        elif tipo == 'SALIDA':
            stock -= cantidad
        
        if stock <= 0 and not stockout_start:
            stockout_start = fecha
    
    if stockout_start:
        periods.append({
            'inicio': stockout_start,
            'fin': None,
            'dias': (datetime.now() - stockout_start.replace(tzinfo=None)).days
        })
    
    return periods

def render_sidebar_shell(section_name: str) -> None:
    with st.sidebar:
        st.title("⚙️ Configuración")
        st.caption(f"Sesión: {st.session_state.get('auth_user', 'usuario')}")
        st.caption(f"Área: {section_name}")
        if st.button("Cerrar sesión"):
            session_id = st.session_state.get("auth_session_id")
            expires_at = st.session_state.get("auth_session_expires_at")
            if session_id and expires_at:
                _get_auth_state_store().revoke_session(session_id, int(expires_at))
            st.session_state["authenticated"] = False
            st.session_state.pop("auth_user", None)
            st.session_state.pop("auth_role", None)
            st.session_state.pop("auth_session_id", None)
            st.session_state.pop("auth_session_expires_at", None)
            _remove_auth_cookie()
            st.rerun()
        st.divider()
        st.markdown("### Información")
        st.info(
            """
            **Diagonal Eyewear**

            - Área ABC para análisis comercial.
            - Área Masterdata para transformación y validación previa a Odoo.
            """
        )
        st.caption("Cloud Diagonal · Catálogo, imágenes y automatizaciones")


def render_footer() -> None:
    from build_info import render_build_footer
    render_build_footer()


def render_home_page() -> None:
    from cloud_dashboard import render_dashboard
    render_sidebar_shell("Inicio")
    pages = {"master": MASTER_IMPORT_PAGE, "dictionary": MASTER_DICTIONARY_PAGE}
    if st.session_state.get("auth_role") == "masterdata":
        pages["kering"] = KERING_IMAGES_PAGE
    elif st.session_state.get("auth_role") == "admin":
        pages.update(images=LUXOPTICA_IMAGES_PAGE, luxpending=LUXOPTICA_PENDING_PAGE,
                     kering=KERING_IMAGES_PAGE, alerts=ALERTA_PEDIDOS_PAGE,
                     abchistory=ABC_HISTORY_PAGE, config=SETTINGS_PAGE)
    render_dashboard(pages)


def render_odoo_reconciliation_page() -> None:
    render_sidebar_shell("Conciliación Odoo")
    require_admin_access()
    render_reconciliation_page()
    render_footer()


def render_settings_page() -> None:
    render_sidebar_shell("Configuración")
    require_admin_access()
    cloud_page_title("Configuración")
    render_kering_settings(get_db_engine)
    st.divider()
    st.caption("Guarda credenciales de integración en el archivo .env local del proyecto.")

    current_url = _read_env_setting_any([LUXOPTICA_URL_ENV, LEGACY_ESSILOR_URL_ENV])
    current_user = _read_env_setting_any([LUXOPTICA_USER_ENV, LEGACY_ESSILOR_USER_ENV])
    current_password = _read_env_setting_any([LUXOPTICA_PASSWORD_ENV, LEGACY_ESSILOR_PASSWORD_ENV])
    current_request_email = _read_env_setting(LUXOPTICA_REQUEST_EMAIL_ENV, "images@diagonaleyewear.com")
    current_tenant_id = _read_env_setting(M365_TENANT_ID_ENV)
    current_client_id = _read_env_setting(M365_CLIENT_ID_ENV)
    current_client_secret = _read_env_setting(M365_CLIENT_SECRET_ENV)
    current_mailbox = _read_env_setting(M365_MAILBOX_ENV, "ruben.cebreiros@diagonaleyewear.com")
    current_download_root = str(repository_root())

    with st.form("settings_form", clear_on_submit=False):
        st.subheader("Essilor Luxottica")
        url_value = st.text_input("URL", value=current_url, placeholder="https://...")
        user_value = st.text_input("Usuario", value=current_user)
        password_value = st.text_input(
            "Contraseña",
            type="password",
            value=current_password,
            placeholder="Deja vacío para conservar la actual",
        )
        request_email_value = st.text_input("Email solicitud imágenes", value=current_request_email)

        st.subheader("Microsoft 365 (Graph)")
        tenant_id_value = st.text_input("Tenant ID", value=current_tenant_id)
        client_id_value = st.text_input("Client ID", value=current_client_id)
        client_secret_value = st.text_input(
            "Client Secret",
            type="password",
            placeholder="Deja vacío para conservar el actual",
        )
        mailbox_value = st.text_input("Mailbox objetivo", value=current_mailbox)
        st.text_input("Repositorio comun (IMAGE_REPOSITORY_ROOT)", value=current_download_root, disabled=True)
        st.caption("Cambiar en el entorno de todos los servicios tras verificar la migración y reiniciarlos.")

        submitted = st.form_submit_button("Guardar configuración", type="primary")

    if submitted:
        updates = {
            LUXOPTICA_URL_ENV: url_value.strip(),
            LUXOPTICA_USER_ENV: user_value.strip(),
            LUXOPTICA_REQUEST_EMAIL_ENV: request_email_value.strip(),
            M365_TENANT_ID_ENV: tenant_id_value.strip(),
            M365_CLIENT_ID_ENV: client_id_value.strip(),
            M365_MAILBOX_ENV: mailbox_value.strip(),
        }
        keep_existing_password = not password_value.strip() and bool(current_password)
        keep_existing_client_secret = not client_secret_value.strip() and bool(current_client_secret)
        if password_value.strip():
            updates[LUXOPTICA_PASSWORD_ENV] = password_value.strip()
        if client_secret_value.strip():
            updates[M365_CLIENT_SECRET_ENV] = client_secret_value.strip()

        _upsert_env_settings(updates)
        for key, value in updates.items():
            os.environ[key] = value

        st.success("Configuración guardada en .env")
        if keep_existing_password:
            st.info("Se conservó la contraseña existente.")
        if keep_existing_client_secret:
            st.info("Se conservó el client secret existente.")

    st.markdown("### Variables gestionadas")
    st.write(f"- {LUXOPTICA_URL_ENV}")
    st.write(f"- {LUXOPTICA_USER_ENV}")
    st.write(f"- {LUXOPTICA_PASSWORD_ENV}")
    st.write(f"- {LUXOPTICA_REQUEST_EMAIL_ENV}")
    st.write(f"- {M365_TENANT_ID_ENV}")
    st.write(f"- {M365_CLIENT_ID_ENV}")
    st.write(f"- {M365_CLIENT_SECRET_ENV}")
    st.write(f"- {M365_MAILBOX_ENV}")
    st.write(f"- {M365_DOWNLOAD_ROOT_ENV}")

    st.divider()
    st.subheader("Login automático")
    st.caption("Usa las credenciales guardadas en .env para probar el acceso automático a la web.")
    if st.button("Probar login automático", type="secondary"):
        auto_url = _read_env_setting_any([LUXOPTICA_URL_ENV, LEGACY_ESSILOR_URL_ENV])
        auto_user = _read_env_setting_any([LUXOPTICA_USER_ENV, LEGACY_ESSILOR_USER_ENV])
        auto_password = _read_env_setting_any([LUXOPTICA_PASSWORD_ENV, LEGACY_ESSILOR_PASSWORD_ENV])
        with st.spinner("Ejecutando login automático..."):
            ok, message = _attempt_essilor_auto_login(auto_url, auto_user, auto_password)
        if ok:
            st.success(message)
        else:
            st.error(message)

    render_footer()


def render_abc_page(abc_page: str | None = None) -> None:
    render_sidebar_shell("Análisis ABCD")
    require_admin_access()
    if abc_page is None:
        abc_page = "📊 Inicio"

    if abc_page == "📊 Inicio":
        cloud_page_title("ABC")
        st.markdown(
            """
            ### Bienvenido al sistema ABCD

            Este sistema clasifica productos según su demanda y valor, considerando:
            - **Historial de ventas** (últimos 180 días)
            - **Períodos de agotamiento** (stock = 0)
            - **Valor unitario** (PVO)
            - **Capital bloqueado** en inventario
            """
        )

        with st.spinner("Cargando datos..."):
            df = load_data_cached()
            df_classified = run_abcd_engine(df.copy())

        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric("Total Productos", f"{len(df_classified):,}", delta="Con stock actual")
        with col2:
            capital_total = df_classified["Capital_Bloqueado (€)"].sum()
            st.metric("Capital Bloqueado", f"€{capital_total:,.0f}", delta="Total en inventario")
        with col3:
            productos_a = len(df_classified[df_classified['ABCD'] == 'A'])
            st.metric("Categoría A", productos_a, delta="Alta prioridad")
        with col4:
            productos_d = len(df_classified[df_classified['ABCD'] == 'D'])
            st.metric("Categoría D", productos_d, delta="Para liquidar")

        st.subheader("Distribución por Categoría")
        col1, col2 = st.columns(2)
        with col1:
            abcd_counts = df_classified['ABCD'].value_counts().sort_index()
            fig = px.pie(
                values=abcd_counts.values,
                names=abcd_counts.index,
                title="Productos por Categoría",
                color_discrete_map={'A': '#00cc96', 'B': '#636EFA', 'C': '#FFA15A', 'D': '#EF553B'},
            )
            st.plotly_chart(fig, use_container_width=True)
        with col2:
            capital_by_abcd = df_classified.groupby('ABCD')['Capital_Bloqueado (€)'].sum().sort_index()
            fig = px.bar(
                x=capital_by_abcd.index,
                y=capital_by_abcd.values,
                title="Capital Bloqueado por Categoría",
                labels={'x': 'Categoría', 'y': 'Capital (€)'},
                color=capital_by_abcd.index,
                color_discrete_map={'A': '#00cc96', 'B': '#636EFA', 'C': '#FFA15A', 'D': '#EF553B'},
            )
            st.plotly_chart(fig, use_container_width=True)

        summary = df_classified.groupby('ABCD').agg({
            'product_id': 'count',
            'Stock': 'sum',
            'Capital_Bloqueado (€)': 'sum',
            'PVO': 'mean',
        }).round(2)
        summary.columns = ['Productos', 'Stock Total', 'Capital (€)', 'PVO Promedio']
        summary['Stock Total'] = summary['Stock Total'].astype(int)
        summary['Capital (€)'] = summary['Capital (€)'].apply(lambda x: f"€{x:,.0f}")
        summary['PVO Promedio'] = summary['PVO Promedio'].apply(lambda x: f"€{x:,.2f}")
        st.subheader("Resumen por Categoría")
        st.dataframe(summary, use_container_width=True)

    elif abc_page == "🔍 Buscar Producto":
        cloud_page_title("Buscar Producto")
        with st.spinner("Cargando datos..."):
            df = load_data_cached()
            df_classified = run_abcd_engine(df.copy())

        col1, col2 = st.columns([3, 1])
        with col1:
            search_term = st.text_input("Busca por nombre, código o barcode:", placeholder="Ej: DIOR, 192337232893")
        with col2:
            search_btn = st.button("🔍 Buscar", use_container_width=True)

        if search_term and search_btn:
            mask = (
                df_classified['Marca'].str.contains(search_term, case=False, na=False)
                | df_classified['Cód Barras'].astype(str).str.contains(search_term, case=False, na=False)
                | df_classified['product_id'].astype(str).str.contains(search_term, case=False, na=False)
            )
            results = df_classified[mask]
            if len(results) == 0:
                st.warning("No se encontraron productos")
            elif len(results) == 1:
                product = results.iloc[0]
                st.subheader(f"📦 {product['Marca']}")
                col1, col2, col3, col4 = st.columns(4)
                with col1:
                    st.metric("ID", product['product_id'])
                with col2:
                    st.metric("Clasificación", product['ABCD'], delta=product['Motivo'][:30])
                with col3:
                    st.metric("Stock", f"{product['Stock']:.0f} ud")
                with col4:
                    st.metric("Capital", f"€{product['Capital_Bloqueado (€)']:,.0f}")
                st.divider()
                col1, col2 = st.columns(2)
                with col1:
                    st.markdown("**Información Comercial**")
                    st.write(f"- **Código**: {product['Cód Barras']}")
                    st.write(f"- **PVO**: €{product['PVO']:.2f}")
                    st.write(f"- **Primera Compra**: {product['Primera Compra']}")
                    st.write(f"- **Última Compra**: {product['Última Compra']}")
                with col2:
                    st.markdown("**Actividad Reciente**")
                    st.write(f"- **Última Venta**: {product['Última Venta']}")
                    st.write(f"- **Ventas 180 días**: {product['Num_Ventas_180D']:.0f}")
                    st.write(f"- **Unidades (180d)**: {product['Ventas_180_Dias']:.0f}")
                    st.write(f"- **Última Reposición**: {product.get('Última Reposicion', 'N/A')}")
                st.divider()
                st.subheader("Períodos de Agotamiento")
                engine = get_db_engine()
                periods = get_product_stockout_periods(int(product['product_id']), engine)
                if periods:
                    for i, period in enumerate(periods, 1):
                        col1, col2, col3 = st.columns(3)
                        with col1:
                            st.write(f"**Período {i}**")
                            st.write(f"Inicio: {period['inicio']}")
                        with col2:
                            st.write(f"Fin: {period['fin'] if period['fin'] else 'Aún agotado'}")
                        with col3:
                            st.write(f"**Duración: {period['dias']} días**")
                else:
                    st.info("No hay períodos de agotamiento registrados")
            else:
                st.info(f"Se encontraron {len(results)} productos. Mostrando los primeros 10:")
                display_cols = ['Marca', 'Cód Barras', 'ABCD', 'Stock', 'PVO', 'Capital_Bloqueado (€)']
                st.dataframe(results[display_cols].head(10), use_container_width=True, hide_index=True)

    elif abc_page == "📈 Reportes ABCD":
        cloud_page_title("Reportes ABCD")
        with st.spinner("Cargando datos..."):
            df = load_data_cached()
            df_classified = run_abcd_engine(df.copy())

        col1, col2, col3 = st.columns(3)
        with col1:
            selected_abcd = st.multiselect("Categoría ABCD:", ['A', 'B', 'C', 'D'], default=['A', 'B', 'C', 'D'])
        with col2:
            min_capital = st.number_input("Capital mínimo (€):", min_value=0, value=0, step=100)
        with col3:
            max_products = st.number_input("Mostrar máximo:", min_value=10, value=50, step=10)

        filtered = df_classified[
            (df_classified['ABCD'].isin(selected_abcd))
            & (df_classified['Capital_Bloqueado (€)'] >= min_capital)
        ]
        filtered_report = filtered.copy()
        if ('EAN' not in filtered_report.columns or filtered_report['EAN'].isna().all()) and 'Cód Barras' in filtered_report.columns:
            filtered_report['EAN'] = filtered_report['Cód Barras']
        filtered_report = build_abcd_report_export_df(filtered_report)
        st.subheader(f"Resultados: {len(filtered)} productos")
        tab1, tab2, tab3 = st.tabs(["📊 Tabla", "📈 Gráficos", "💾 Descargar"])
        with tab1:
            display_filtered = filtered_report.sort_values('Capital_Bloqueado (€)', ascending=False).head(max_products)
            st.dataframe(
                display_filtered[[
                    'Marca', 'Modelo', 'EAN', 'ABCD', 'Stock', 'PVO', 'Capital_Bloqueado (€)',
                    'Num_Ventas_180D', 'Última Venta', 'Accion_Recomendada',
                ]],
                use_container_width=True,
                hide_index=True,
            )
        with tab2:
            col1, col2 = st.columns(2)
            with col1:
                stock_by_abcd = filtered.groupby('ABCD')['Stock'].sum().sort_index()
                fig = px.bar(x=stock_by_abcd.index, y=stock_by_abcd.values, title="Stock por Categoría", labels={'x': 'Categoría', 'y': 'Stock (ud)'})
                st.plotly_chart(fig, use_container_width=True)
            with col2:
                top10 = filtered.nlargest(10, 'Capital_Bloqueado (€)')
                fig = px.bar(top10, x='Capital_Bloqueado (€)', y='Marca', orientation='h', title="Top 10 Capital Bloqueado", labels={'Capital_Bloqueado (€)': 'Capital (€)'})
                st.plotly_chart(fig, use_container_width=True)
        with tab3:
            csv = filtered_report.to_csv(index=False)
            excel = dataframe_to_excel_bytes(build_abcd_report_excel_df(filtered_report))
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            col_csv, col_excel = st.columns(2)
            with col_csv:
                st.download_button("📥 Descargar CSV", data=csv, file_name=f"abcd_report_{timestamp}.csv", mime="text/csv")
            with col_excel:
                st.download_button(
                    "📥 Descargar Excel",
                    data=excel,
                    file_name=f"abcd_report_{timestamp}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )

    elif abc_page == "📅 Histórico Semanal":
        cloud_page_title("Histórico semanal ABCD")
        st.caption("Guarda una foto semanal del análisis ABCD y consulta snapshots anteriores.")
        engine = get_db_engine()

        current_snapshot_date = _current_week_start()
        st.info(f"Semana actual: {current_snapshot_date.strftime('%d/%m/%Y')}")

        if st.button("Guardar nueva foto de esta semana", type="primary"):
            with st.spinner("Calculando ABCD y guardando foto semanal..."):
                df = load_data_cached()
                reference_date = pd.Timestamp.today().normalize()
                snapshot_parameters = build_snapshot_parameters(reference_date)
                df_classified = run_abcd_engine(df.copy(), reference_date=reference_date)
                if ('EAN' not in df_classified.columns or df_classified['EAN'].isna().all()) and 'Cód Barras' in df_classified.columns:
                    df_classified['EAN'] = df_classified['Cód Barras']
                saved_date = save_abcd_weekly_snapshot(
                    engine,
                    df_classified,
                    run_at=datetime.now().astimezone(),
                    engine_version=engine_source_version(),
                    parameters=snapshot_parameters,
                )
            st.success(f"Nueva foto semanal guardada: {saved_date.strftime('%d/%m/%Y')}")

        try:
            snapshot_refs = list_abcd_snapshot_refs(engine)
        except Exception as exc:
            st.error(f"No se pudo cargar el histórico ABCD: {exc}")
            render_footer()
            return

        if not snapshot_refs:
            st.warning("Todavía no hay fotos semanales guardadas.")
            render_footer()
            return

        selected_snapshot = st.selectbox(
            "Captura:",
            snapshot_refs,
            format_func=lambda item: (
                f"{item.snapshot_date:%d/%m/%Y} · {item.run_at:%H:%M:%S %Z} · #{item.snapshot_id}"
            ),
        )
        try:
            snapshot_df, snapshot_metadata = load_abcd_snapshot_by_id(
                engine,
                selected_snapshot.snapshot_id,
            )
        except Exception as exc:
            st.error(f"No se pudo verificar la integridad de la captura: {exc}")
            render_footer()
            return
        if snapshot_df.empty:
            st.warning("La foto seleccionada no contiene datos.")
            render_footer()
            return

        if snapshot_metadata:
            integrity_label = "verificada" if snapshot_metadata.data_sha256 else "legacy, no verificable"
            st.caption(
                f"Capturada: {snapshot_metadata.run_at:%d/%m/%Y %H:%M:%S %Z} · "
                f"Motor: {snapshot_metadata.engine_version} · Integridad: {integrity_label}"
            )
            st.json(snapshot_metadata.parameters)

        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric("Productos", f"{len(snapshot_df):,}")
        with col2:
            st.metric("Stock", f"{snapshot_df['Stock'].sum():,.0f}")
        with col3:
            st.metric("Capital", f"€{snapshot_df['Capital_Bloqueado (€)'].sum():,.0f}")
        with col4:
            st.metric("Productos D", f"{len(snapshot_df[snapshot_df['ABCD'] == 'D']):,}")

        st.subheader("Resumen por categoría")
        summary = snapshot_df.groupby('ABCD').agg({
            'product_id': 'count',
            'Stock': 'sum',
            'Capital_Bloqueado (€)': 'sum',
            'PVO': 'mean',
        }).round(2)
        summary.columns = ['Productos', 'Stock Total', 'Capital (€)', 'PVO Promedio']
        st.dataframe(summary, use_container_width=True)

        st.subheader("Detalle de la foto")
        st.dataframe(snapshot_df, use_container_width=True, hide_index=True)

        timestamp = selected_snapshot.snapshot_date.strftime('%Y%m%d')
        col_csv, col_excel = st.columns(2)
        with col_csv:
            st.download_button(
                "📥 Descargar histórico CSV",
                data=snapshot_df.to_csv(index=False),
                file_name=f"abcd_historico_{timestamp}.csv",
                mime="text/csv",
            )
        with col_excel:
            st.download_button(
                "📥 Descargar histórico Excel",
                data=dataframe_to_excel_bytes(build_abcd_report_excel_df(snapshot_df)),
                file_name=f"abcd_historico_{timestamp}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

    else:
        cloud_page_title("Análisis Detallado de Productos")
        with st.spinner("Cargando datos..."):
            df = load_data_cached()
            df_classified = run_abcd_engine(df.copy())
        st.markdown("### Selecciona un Producto")
        selected_product = st.selectbox("Busca por nombre:", df_classified.sort_values('Marca')['Marca'].values, label_visibility="collapsed")
        product = df_classified[df_classified['Marca'] == selected_product].iloc[0]
        st.subheader(selected_product)
        col1, col2, col3, col4, col5 = st.columns(5)
        with col1:
            st.metric("ABCD", product['ABCD'])
        with col2:
            st.metric("Stock", f"{product['Stock']:.0f}")
        with col3:
            st.metric("PVO", f"€{product['PVO']:.2f}")
        with col4:
            st.metric("Capital", f"€{product['Capital_Bloqueado (€)']:,.0f}")
        with col5:
            st.metric("Ventas 180d", f"{product['Num_Ventas_180D']:.0f}")
        st.divider()
        col1, col2 = st.columns(2)
        with col1:
            st.markdown("**Histórico**")
            st.write(f"- Primera Compra: {product['Primera Compra']}")
            st.write(f"- Última Compra: {product['Última Compra']}")
            st.write(f"- Última Venta: {product['Última Venta']}")
            st.write(f"- Última Reposición: {product.get('Última Reposicion', 'N/A')}")
        with col2:
            st.markdown("**Clasificación**")
            st.write(f"- Motivo: {product['Motivo']}")
            st.write(f"- Alerta: {product['Alerta']}")
            st.write(f"- Acción: {product['Accion_Recomendada']}")
        st.divider()
        st.subheader("Períodos de Agotamiento")
        engine = get_db_engine()
        periods = get_product_stockout_periods(int(product['product_id']), engine)
        if periods:
            cols = st.columns(len(periods))
            for i, (col, period) in enumerate(zip(cols, periods)):
                with col:
                    st.info(
                        f"""
                        **Período {i+1}**

                        Inicio: {period['inicio'].strftime('%d/%m/%Y')}
                        Fin: {period['fin'].strftime('%d/%m/%Y') if period['fin'] else 'Aún agotado'}
                        Duración: **{period['dias']} días**
                        """
                    )
        else:
            st.success("✅ Sin agotamientos registrados")

    render_footer()


def render_luxoptica_pending_panel() -> None:
    st.subheader("Imágenes pendientes de procesar")
    images_root = repository_root()
    try:
        pending_rows = _load_market_pending_rows(images_root)
    except RuntimeError as error:
        st.error(str(error))
        return
    if not pending_rows:
        st.success("No hay imágenes pendientes de procesar.")
        return

    pending_df = pd.DataFrame(pending_rows)
    metric_col, action_col = st.columns([3, 2])
    with metric_col:
        st.metric("Imágenes pendientes", len(pending_df))
        st.caption(f"{pending_df['EAN'].nunique()} EAN con copias de mercado pendientes.")
    with action_col:
        st.write("")
        if st.button(
            "Procesar pendientes ahora",
            type="primary",
            icon=":material/refresh:",
            key="process_market_pending",
        ):
            with st.spinner("Consultando Odoo y creando copias de mercado..."):
                try:
                    completed, created = _refresh_pending_market_images(images_root)
                except Exception as exc:
                    st.error(f"No se pudieron procesar las imágenes pendientes: {exc}")
                else:
                    st.success(
                        f"Proceso completado: {created} copias creadas y "
                        f"{completed} imágenes resueltas."
                    )
                    st.rerun()

    st.dataframe(
        pending_df,
        hide_index=True,
        width="stretch",
        column_config={
            "Ruta": st.column_config.TextColumn("Ruta", width="large"),
        },
    )

    st.markdown("#### Visor de imágenes pendientes")
    selected_image_name = st.selectbox(
        "Selecciona una imagen",
        options=pending_df["Archivo"].tolist(),
        key="market_pending_image_selector",
    )
    selected_row = pending_df.loc[pending_df["Archivo"] == selected_image_name].iloc[0]
    selected_path = Path(str(selected_row["Ruta"]))
    if selected_path.is_file():
        st.image(
            str(selected_path),
            caption=(
                f"{selected_row['Modelo']} | EAN {selected_row['EAN']} | "
                f"Pendiente: {selected_row['Mercados pendientes']}"
            ),
            width="stretch",
        )
    else:
        st.warning(f"No se encuentra el archivo en el servidor: {selected_path}")


def render_luxoptica_pending_page() -> None:
    render_sidebar_shell("Repositorio de imágenes")
    require_admin_access()
    cloud_page_title("Pendiente Luxoptica")
    st.caption("Revisa y procesa manualmente las copias pendientes para Farfetch y Miinto.")
    render_luxoptica_pending_panel()
    render_footer()


def render_master_page(master_page: str | None = None) -> None:
    render_sidebar_shell("Master Data")
    if master_page is None:
        master_page = "📥 Importador Masterdata"

    masterdata_dir = Path(__file__).resolve().parent / "docs" / "MasterData"
    available_files = sorted(masterdata_dir.glob("*transformado.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True)

    if master_page == "📥 Importador Masterdata":
        cloud_page_title("Importador Masterdata")
        st.caption("Flujo web: cargar Excel origen, transformar, revisar advertencias y descargar el MASTERDATA.")
        dictionary_rules = load_masterdata_dictionary()
        if dictionary_rules:
            st.caption(f"Diccionario personalizado cargado: {len(dictionary_rules)} reglas")
        else:
            st.warning("No hay reglas de diccionario cargadas. Revisa la imagen Docker y masterdata_dictionary.json.")
        st.subheader("1. Cargar Excel origen Luxottica")
        uploaded_source = st.file_uploader("Sube el Excel origen", type=["xlsx"], key="masterdata_source_upload")
        use_whitelist = st.checkbox("Aplicar whitelist de accesorios local", value=True)

        if uploaded_source is not None and st.button("Transformar fichero", type="primary"):
            with st.spinner("Transformando MASTERDATA..."):
                source_df = load_masterdata_file(uploaded_source)
                st.session_state["masterdata_source_columns"] = list(source_df.columns)
                color_map, brand_map, shape_map = load_masterdata_reference_maps()
                whitelist_codes: set[str] = set()
                whitelist_path = masterdata_dir / "accessory_whitelist_codes.txt"
                if use_whitelist and whitelist_path.exists():
                    whitelist_codes = {
                        line.strip().upper()
                        for line in whitelist_path.read_text(encoding="utf-8").splitlines()
                        if line.strip() and not line.strip().startswith("#")
                    }
                output_df, transform_report, discarded_audit, zero_audit_df, brand_audit_df = transform_masterdata(
                    source_df,
                    color_map,
                    accessory_whitelist=whitelist_codes,
                    brand_map=brand_map,
                    shape_map=shape_map,
                    dictionary_rules=dictionary_rules,
                )
                st.session_state["masterdata_transform_output_df"] = output_df
                st.session_state["masterdata_transform_report"] = transform_report.to_dict()
                st.session_state["masterdata_transform_discarded_df"] = discarded_audit
                st.session_state["masterdata_transform_zero_df"] = zero_audit_df
                st.session_state["masterdata_transform_brand_df"] = brand_audit_df
                st.session_state["masterdata_transform_source_name"] = uploaded_source.name

        output_df = st.session_state.get("masterdata_transform_output_df")
        report_dict = st.session_state.get("masterdata_transform_report")
        discarded_audit = st.session_state.get("masterdata_transform_discarded_df")
        zero_audit_df = st.session_state.get("masterdata_transform_zero_df")
        brand_audit_df = st.session_state.get("masterdata_transform_brand_df")
        source_name = st.session_state.get("masterdata_transform_source_name", "origen.xlsx")

        if output_df is not None and report_dict is not None:
            st.subheader("2. Advertencias y resultado")
            col1, col2, col3 = st.columns(3)
            with col1:
                st.metric("Procesados", report_dict["total_input"])
            with col2:
                st.metric("Exportados", report_dict["total_output"])
            with col3:
                st.metric("Descartados", report_dict["discarded"])
            col1, col2, col3 = st.columns(3)
            with col1:
                st.metric("Marcas no resueltas", len(report_dict["unmatched_brand_names"]))
            with col2:
                st.metric("Filas inválidas SI/NO", report_dict["invalid_yes_no_rows"])
            with col3:
                st.metric("Pérdida real de ceros", report_dict["leading_zero_real_loss_columns"])

            blocking_warnings, medium_warnings, info_warnings = split_report_warnings(type("ReportProxy", (), report_dict)())
            if blocking_warnings:
                st.error("Avisos bloqueantes")
                for line in blocking_warnings:
                    st.error(line)
            if medium_warnings:
                st.warning("Avisos de riesgo medio")
                for line in medium_warnings:
                    st.warning(line)
            if info_warnings:
                st.info("Avisos informativos")
                for line in info_warnings:
                    st.info(line)
            if report_dict.get("category_review_rows"):
                st.dataframe(
                    pd.DataFrame(report_dict["category_review_rows"]),
                    hide_index=True,
                    use_container_width=True,
                )
            if report_dict.get("dictionary_values_not_resolved"):
                st.warning("Hay valores del XLS sin relación en Odoo ni en el diccionario:")
                st.dataframe(
                    pd.DataFrame(report_dict["dictionary_values_not_resolved"]),
                    hide_index=True,
                    use_container_width=True,
                )
            if not blocking_warnings and not medium_warnings and not info_warnings:
                st.success("Transformación completada sin incidencias críticas ni advertencias abiertas.")
            elif not blocking_warnings:
                st.success("Transformación completada sin incidencias bloqueantes.")

            export_df = normalize_result_export_schema(output_df).reindex(columns=MASTERDATA_DOWNLOAD_COLUMNS)

            st.subheader("Vista previa del MASTERDATA")
            st.caption(f"Filas en vista previa: {len(export_df):,}")
            preview_df = export_df.copy()
            for column in preview_df.columns:
                preview_df[column] = preview_df[column].map(
                    lambda value: str(value).upper() if pd.notna(value) else ""
                )
            st.dataframe(preview_df, use_container_width=True, hide_index=True)

            executive_summary = build_executive_summary_markdown(
                type("ReportProxy", (), {**report_dict, "to_dict": lambda self=None: report_dict})(),
                Path(source_name),
                Path(source_name).with_name(f"{Path(source_name).stem}_transformado.xlsx"),
                None,
            )
            download_export_df = export_df.reindex(columns=MASTERDATA_DOWNLOAD_COLUMNS)
            transformed_excel = dataframe_to_excel_bytes(download_export_df)
            transformed_csv = download_export_df.to_csv(index=False, sep=";").encode("utf-8-sig")
            discarded_csv = discarded_audit.to_csv(index=False) if discarded_audit is not None else ""
            zero_csv = zero_audit_df.to_csv(index=False) if zero_audit_df is not None else ""
            brand_csv = brand_audit_df.to_csv(index=False) if brand_audit_df is not None else ""
            report_json = json.dumps(report_dict, ensure_ascii=False, indent=2)

            st.subheader("3. Descargas")
            col1, col2 = st.columns(2)
            with col1:
                st.download_button("📥 Descargar fichero transformado (.xlsx)", data=transformed_excel, file_name=f"{Path(source_name).stem}_transformado.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                st.download_button("📥 Descargar fichero transformado (.csv)", data=transformed_csv, file_name=f"{Path(source_name).stem}_transformado.csv", mime="text/csv")
                st.download_button("📥 Descargar reporte JSON", data=report_json, file_name=f"{Path(source_name).stem}_validacion.json", mime="application/json")
                st.download_button("📥 Descargar resumen ejecutivo (.md)", data=executive_summary, file_name=f"{Path(source_name).stem}_resumen_ejecutivo.md", mime="text/markdown")
            with col2:
                st.download_button("📥 Descargar auditoría descartes (.csv)", data=discarded_csv, file_name=f"{Path(source_name).stem}_descartes_auditoria.csv", mime="text/csv")
                st.download_button("📥 Descargar auditoría ceros (.csv)", data=zero_csv, file_name=f"{Path(source_name).stem}_ceros_iniciales_resumen.csv", mime="text/csv")
                st.download_button("📥 Descargar auditoría marcas (.csv)", data=brand_csv, file_name=f"{Path(source_name).stem}_marcas_auditoria.csv", mime="text/csv")

            st.subheader("4. Solicitud de imágenes (Luxoptica)")
            configured_request_email = _read_env_setting(LUXOPTICA_REQUEST_EMAIL_ENV, "images@diagonaleyewear.com")
            default_email = st.session_state.get("luxoptica_request_email", configured_request_email)
            request_email = st.text_input("Email para la solicitud", value=default_email, key="luxoptica_request_email_input")
            st.caption("Se generan lotes de 250 EAN máximos por archivo, sin prefijo.")

            col_gen_a, col_gen_b = st.columns(2)

            with col_gen_a:
                generate_test_50 = st.button(
                    "Generar lote de prueba (50 EAN)",
                    key="generate_luxoptica_request_files_50",
                    use_container_width=True,
                )
            with col_gen_b:
                generate_full = st.button(
                    "Generar lotes completos",
                    key="generate_luxoptica_request_files_full",
                    use_container_width=True,
                )

            if generate_test_50 or generate_full:
                luxoptica_dir = Path(__file__).resolve().parent / "docs" / "Luxoptica"
                limit = 50 if generate_test_50 else None
                generated_files = generate_luxoptica_request_files(
                    export_df,
                    luxoptica_dir,
                    batch_size=250,
                    max_total_eans=limit,
                )
                st.session_state["luxoptica_request_email"] = request_email.strip() or default_email
                st.session_state["luxoptica_generated_files"] = [str(p) for p in generated_files]
                st.session_state["luxoptica_generated_total_eans"] = sum(
                    len(path.read_text(encoding="utf-8").splitlines()) for path in generated_files
                )
                st.session_state["luxoptica_processed_files"] = []

                if generated_files:
                    if limit:
                        st.success(
                            f"Generado lote de prueba con {st.session_state['luxoptica_generated_total_eans']} EAN en docs/Luxoptica."
                        )
                    else:
                        st.success(f"Generados {len(generated_files)} archivo(s) en docs/Luxoptica.")
                else:
                    st.info("No hay EAN con vistas pendientes para generar archivos.")

            generated_files_raw = st.session_state.get("luxoptica_generated_files", [])
            if generated_files_raw:
                generated_paths = [Path(p) for p in generated_files_raw if Path(p).exists()]
                total_eans = st.session_state.get("luxoptica_generated_total_eans", 0)
                processed_files = set(st.session_state.get("luxoptica_processed_files", []))
                st.info(
                    f"Email de solicitud: {st.session_state.get('luxoptica_request_email', request_email)} | "
                    f"EAN totales: {total_eans} | Archivos: {len(generated_paths)}"
                )
                
                # Botón de subida automática a Luxoptica
                st.subheader("Subida Automática a Luxoptica")
                col_auto_a, col_auto_b = st.columns([2, 1])
                with col_auto_a:
                    st.caption("Requiere credenciales en .env: LUXOPTICA_USERNAME, LUXOPTICA_PASSWORD")
                with col_auto_b:
                    if st.button("🚀 Subir a Luxoptica Automáticamente", use_container_width=True, key="auto_upload_luxoptica"):
                        import subprocess
                        try:
                            result = subprocess.run(
                                ["python", "luxoptica_auto_upload.py"],
                                cwd=Path(__file__).parent,
                                capture_output=True,
                                text=True,
                                timeout=300,
                            )
                            if result.returncode == 0:
                                st.success("✅ Solicitud enviada a Luxoptica automáticamente!")
                                st.session_state["luxoptica_processed_files"] = [p.name for p in generated_paths]
                                st.rerun()
                            else:
                                st.error(f"❌ Error en la subida:\n{result.stdout}\n{result.stderr}")
                        except subprocess.TimeoutExpired:
                            st.error("❌ Timeout (5 minutos) en la subida")
                        except Exception as e:
                            st.error(f"❌ Error: {e}")
                
                st.caption("Sube los archivos uno por uno en Luxoptica. Cada archivo corresponde a una solicitud.")

                for idx, file_path in enumerate(generated_paths, start=1):
                    content = file_path.read_text(encoding="utf-8")
                    num_lines = len([line for line in content.splitlines() if line.strip()])
                    col_a, col_b = st.columns([3, 2])
                    with col_a:
                        st.download_button(
                            label=f"📄 Descargar lote {idx} ({num_lines} EAN)",
                            data=content,
                            file_name=file_path.name,
                            mime="text/plain",
                            key=f"luxoptica_download_{idx}_{file_path.name}",
                        )
                    with col_b:
                        file_key = file_path.name
                        if file_key in processed_files:
                            st.success("✅ Procesado")
                        else:
                            if st.button("Marcar como procesado", key=f"luxoptica_mark_{idx}_{file_key}"):
                                updated = list(processed_files)
                                updated.append(file_key)
                                st.session_state["luxoptica_processed_files"] = updated
                                st.rerun()

                processed_count = len([p for p in generated_paths if p.name in processed_files])
                total_batches = len(generated_paths)
                if total_batches > 0:
                    st.progress(processed_count / total_batches)
                    st.caption(f"Lotes procesados: {processed_count}/{total_batches}")

                if total_batches > 0 and processed_count == total_batches:
                    st.success(
                        "Todos los lotes están procesados. Solicitudes de imágenes completadas para este lote de MASTERDATA."
                    )

            st.subheader("5. Descarga automática de imágenes (Microsoft 365)")
            m365_cfg = load_m365_config()
            missing_m365 = validate_m365_config(m365_cfg)
            sender_hint = st.text_input(
                "Filtro remitente (contains)",
                value="luxottica",
                key="m365_sender_hint",
            )
            subject_hint = st.text_input(
                "Filtro asunto (contains)",
                value="image",
                key="m365_subject_hint",
            )
            lookback_days = st.number_input(
                "Ventana de búsqueda (días)",
                min_value=1,
                max_value=60,
                value=7,
                step=1,
                key="m365_lookback_days",
            )
            top_messages = st.number_input(
                "Máximo correos a revisar",
                min_value=10,
                max_value=500,
                value=100,
                step=10,
                key="m365_top_messages",
            )

            if missing_m365:
                st.warning(
                    "Faltan variables M365 en .env: " + ", ".join(missing_m365)
                )
            else:
                st.caption(
                    f"Mailbox: {m365_cfg.mailbox} | Destino: {m365_cfg.download_root}"
                )

            if st.button("Procesar buzón y descargar adjuntos", key="m365_download_attachments"):
                if missing_m365:
                    st.error("Configura primero las variables de Microsoft 365 en Configuración.")
                else:
                    with st.spinner("Leyendo buzón y descargando adjuntos..."):
                        try:
                            summary = download_luxoptica_mail_attachments(
                                sender_hint=sender_hint,
                                subject_hint=subject_hint,
                                lookback_days=int(lookback_days),
                                top_messages=int(top_messages),
                            )
                            st.session_state["m365_last_download_summary"] = {
                                "messages_scanned": summary.messages_scanned,
                                "messages_with_attachments": summary.messages_with_attachments,
                                "attachments_downloaded": summary.attachments_downloaded,
                                "saved_paths": summary.saved_paths,
                                "processed_message_ids": summary.processed_message_ids,
                            }
                        except Exception as exc:
                            st.error(f"Error en descarga automática: {exc}")

            m365_summary = st.session_state.get("m365_last_download_summary")
            if m365_summary:
                st.success(
                    "Descarga automática finalizada: "
                    f"{m365_summary['attachments_downloaded']} adjunto(s) en "
                    f"{m365_summary['messages_with_attachments']} correo(s)."
                )
                st.caption(
                    f"Correos revisados: {m365_summary['messages_scanned']} | "
                    f"Correos marcados como procesados: {len(m365_summary['processed_message_ids'])}"
                )
                if m365_summary["saved_paths"]:
                    with st.expander("Ver archivos descargados"):
                        for file_path in m365_summary["saved_paths"]:
                            st.write(f"- {file_path}")

            render_luxoptica_pending_panel()

    else:
        cloud_page_title("Dry-run Odoo")
        st.caption("Usa un MASTERDATA ya transformado para clasificar altas y actualizaciones sin grabar nada.")
        output_df = st.session_state.get("masterdata_transform_output_df")
        source_name = st.session_state.get("masterdata_transform_source_name", "origen.xlsx")
        source_mode = st.radio(
            "Origen del MASTERDATA:",
            ["Usar fichero generado", "Usar último transformado en esta sesión", "Subir Excel"],
            horizontal=True,
        )
        selected_label = ""
        masterdata_df = None
        if source_mode == "Usar fichero generado":
            if not available_files:
                st.warning("No hay ficheros MASTERDATA transformados disponibles en docs/MasterData.")
            else:
                selected_path = st.selectbox("Selecciona un fichero:", available_files, format_func=lambda p: p.name)
                selected_label = str(selected_path)
                if st.button("Ejecutar dry-run", type="primary", key="dryrun_generated"):
                    with st.spinner("Leyendo MASTERDATA y snapshot de Odoo..."):
                        masterdata_df = load_masterdata_file(selected_path)
        elif source_mode == "Usar último transformado en esta sesión":
            if output_df is None:
                st.info("Todavía no has transformado ningún fichero en esta sesión.")
            else:
                selected_label = f"sesion::{source_name}"
                if st.button("Ejecutar dry-run", type="primary", key="dryrun_session"):
                    masterdata_df = output_df.copy()
        else:
            uploaded_file = st.file_uploader("Sube un Excel MASTERDATA transformado", type=["xlsx"], key="dryrun_upload")
            if uploaded_file is not None and st.button("Ejecutar dry-run", type="primary", key="dryrun_upload_btn"):
                selected_label = uploaded_file.name
                with st.spinner("Leyendo MASTERDATA subido y snapshot de Odoo..."):
                    masterdata_df = load_masterdata_file(uploaded_file)

        if masterdata_df is not None:
            required_cols_alternatives = {
                "UPC": ["UPC", "Barcode"],
                "Nombre de la marca": ["Nombre de la marca", "Marca"],
                "Código del modelo": ["Código del modelo", "Modelo"],
            }
            missing_cols = sorted(
                required
                for required, alternatives in required_cols_alternatives.items()
                if not any(col in masterdata_df.columns for col in alternatives)
            )
            if missing_cols:
                st.error(f"El fichero no parece un MASTERDATA válido. Faltan columnas: {', '.join(missing_cols)}")
            else:
                with st.spinner("Comparando contra Odoo..."):
                    odoo_barcodes_df, brand_map = load_odoo_snapshot_cached()
                    detail_df, report = analyze_masterdata_against_odoo(masterdata_df, odoo_barcodes_df, brand_map)
                    summary_md = build_summary_markdown(report, Path(selected_label), None)
                col1, col2, col3 = st.columns(3)
                with col1:
                    st.metric("Altas potenciales", report.creates)
                with col2:
                    st.metric("Actualizaciones potenciales", report.updates)
                with col3:
                    st.metric("Conflictos", report.conflicts)
                col1, col2, col3 = st.columns(3)
                with col1:
                    st.metric("Marcas no resueltas", report.unresolved_brands)
                with col2:
                    st.metric("Duplicados en fichero", report.duplicate_barcodes_in_file)
                with col3:
                    st.metric("Registros evaluados", report.total_rows)
                if report.conflicts == 0 and report.duplicate_barcodes_in_file == 0:
                    st.success("Dry-run técnico correcto: no hay conflictos de barcode ni duplicados internos.")
                else:
                    st.warning("El lote requiere revisión antes de importar en Odoo.")
                st.subheader("Resumen")
                st.markdown(summary_md)
                st.subheader("Detalle")
                action_filter = st.multiselect("Filtrar acciones:", ["create", "update", "conflict"], default=["create", "update", "conflict"], key="dryrun_action_filter")
                filtered_detail = detail_df[detail_df["dry_run_action"].isin(action_filter)].copy()
                st.dataframe(filtered_detail, use_container_width=True, hide_index=True)
                st.subheader("Descargas")
                col1, col2, col3 = st.columns(3)
                with col1:
                    st.download_button("📥 Descargar detalle CSV", data=filtered_detail.to_csv(index=False), file_name=f"dryrun_odoo_detalle_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv", mime="text/csv")
                with col2:
                    st.download_button("📥 Descargar reporte JSON", data=json.dumps(report.to_dict(), ensure_ascii=False, indent=2), file_name=f"dryrun_odoo_reporte_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json", mime="application/json")
                with col3:
                    st.download_button("📥 Descargar resumen MD", data=summary_md, file_name=f"dryrun_odoo_resumen_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md", mime="text/markdown")

    render_footer()


require_authentication()


def render_abc_home_page() -> None:
    render_abc_page("📊 Inicio")


def render_abc_search_page() -> None:
    render_abc_page("🔍 Buscar Producto")


def render_abc_reports_page() -> None:
    render_abc_page("📈 Reportes ABCD")


def render_abc_history_page() -> None:
    render_abc_page("📅 Histórico Semanal")


def render_abc_detail_page() -> None:
    render_abc_page("📉 Análisis Detallado")


def render_master_import_page() -> None:
    render_master_page("📥 Importador Masterdata")


def render_master_dryrun_page() -> None:
    render_master_page("🧪 Dry-run Odoo")


def render_master_dictionary_page_route() -> None:
    render_master_dictionary_page()


def _format_alert_timestamp(value: int | None) -> str:
    return datetime.fromtimestamp(int(value)).strftime("%d/%m/%Y %H:%M:%S") if value else "—"


def _render_order_alert_service_status() -> None:
    st.subheader("Envío automático")
    try:
        status = OrderAlertStore().get_status(SOURCE_AUTO)
    except Exception as exc:  # noqa: BLE001
        st.error(f"No se puede leer el registro de alertas: {exc}")
        return

    state = scheduler_state(status)
    if state == "never":
        st.warning(
            "El servicio automático (contenedor `abcd-order-alerts`) no ha registrado ninguna ejecución. "
            "Hasta que arranque, las alertas solo se envían con «Comprobar ahora»."
        )
        return

    interval_minutes = max(1, int(status.get("interval_seconds") or 300) // 60)
    result_label = RESULT_LABELS.get(status.get("last_check_result") or "", "—")
    last_error_at = status.get("last_error_at")
    last_check_failed = bool(last_error_at) and int(last_error_at) >= int(status.get("last_check_at") or 0)
    if state == "stopped":
        st.error(
            f"Servicio automático DETENIDO desde {_format_alert_timestamp(status.get('stopped_at'))}. "
            "No se están comprobando pedidos."
        )
    elif state == "stale":
        st.error(
            f"Servicio automático sin actividad desde {_format_alert_timestamp(status.get('heartbeat_at'))}: "
            "proceso detenido o bloqueado. No se están comprobando pedidos."
        )
    elif last_check_failed:
        st.warning(f"Servicio activo (cada {interval_minutes} min), pero la última comprobación falló: {result_label}.")
    else:
        st.success(f"Servicio activo (cada {interval_minutes} min). Último resultado: {result_label}.")

    col_check, col_sent, col_error = st.columns(3)
    col_check.markdown(
        f"**Última comprobación**  \n{_format_alert_timestamp(status.get('last_check_at'))}  \n"
        f"{result_label}. {status.get('last_check_detail') or ''}"
    )
    col_sent.markdown(
        f"**Último envío**  \n{_format_alert_timestamp(status.get('last_sent_at'))}  \n"
        + (f"{status.get('last_sent_count')} pedido(s)" if status.get("last_sent_at") else "Sin envíos")
    )
    col_error.markdown(
        f"**Último error**  \n{_format_alert_timestamp(last_error_at)}  \n"
        f"{status.get('last_error') or 'Sin errores'}"
    )
    st.caption(
        "El envío automático busca solo pedidos confirmados y pendientes (estado 'sale', sin facturar del todo "
        "o con líneas sin entregar) de la ventana ALERTA_PEDIDOS_DIAS_ATRAS (180 días por defecto). "
        "Comparte con «Comprobar ahora» la lista de clientes y el registro de pedidos ya notificados."
    )


def render_alerta_pedidos_page() -> None:
    render_sidebar_shell("Alerta Pedidos")
    require_admin_access()
    cloud_page_title("Alerta de Pedidos de Clientes Vigilados")
    st.caption(
        "Mantén aquí la lista de clientes a vigilar. Se busca coincidencia en el nombre del cliente "
        "o en la dirección de entrega. Cuando se detecte un pedido, se enviará un email de alerta."
    )

    watchlist = load_watchlist()

    with st.form("watchlist_add_form", clear_on_submit=True):
        st.subheader("Añadir cliente a vigilar")
        new_name = st.text_input(
            "Nombre exacto del cliente (tal cual figura en Odoo)",
            placeholder="Ej: Óptica Ejemplo S.L.",
        )
        added = st.form_submit_button("Añadir", type="primary")

    if added:
        if new_name.strip():
            watchlist = add_customer(new_name)
            st.success(f"Cliente '{new_name.strip()}' añadido a la lista de vigilancia.")
        else:
            st.warning("Introduce un nombre antes de añadir.")

    st.divider()
    st.subheader("Clientes vigilados")
    if not watchlist:
        st.info("No hay clientes en la lista de vigilancia todavía.")
    else:
        clientes_editados = st.data_editor(
            pd.DataFrame({"Cliente": watchlist}),
            column_config={"Cliente": st.column_config.TextColumn("Cliente vigilado", required=True)},
            hide_index=True,
            num_rows="dynamic",
            key="watchlist_editor",
        )
        if st.button("Guardar lista de clientes", type="secondary"):
            clientes = [
                str(nombre).strip()
                for nombre in clientes_editados["Cliente"].dropna().tolist()
                if str(nombre).strip()
            ]
            clientes = list(dict.fromkeys(clientes))
            save_watchlist(clientes)
            st.success(f"Lista guardada: {len(clientes)} cliente(s) vigilado(s).")
            st.rerun()

            cliente_a_quitar = st.selectbox("Cliente a quitar", options=watchlist)
            if st.button("Quitar cliente", type="secondary"):
                remove_customer(cliente_a_quitar)
                st.success(f"Cliente '{cliente_a_quitar}' eliminado de la lista.")
                st.rerun()

    st.divider()
    _render_order_alert_service_status()

    st.divider()
    st.subheader("Comprobar pedidos")

    col_desde, col_hasta, col_estado = st.columns([1, 1, 1])
    hoy = date.today()
    fecha_desde = col_desde.date_input("Desde", value=hoy - timedelta(days=30))
    fecha_hasta = col_hasta.date_input("Hasta", value=hoy)
    filtro_estado = col_estado.radio("Estado", options=["Pendientes", "Todos"], horizontal=True)

    if st.button("Comprobar ahora", type="primary"):
        if not watchlist:
            st.warning("Añade al menos un cliente a la lista de vigilancia antes de comprobar.")
        elif fecha_desde > fecha_hasta:
            st.warning("La fecha 'Desde' no puede ser posterior a la fecha 'Hasta'.")
        else:
            with st.spinner("Consultando pedidos en Odoo..."):
                try:
                    resultados = find_matching_orders(
                        clientes=watchlist,
                        fecha_desde=fecha_desde,
                        fecha_hasta=fecha_hasta + timedelta(days=1),
                        solo_pendientes=(filtro_estado == "Pendientes"),
                    )
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Error al consultar Odoo: {exc}")
                    resultados = None

            if resultados is not None:
                if resultados.empty:
                    st.info("No se han encontrado pedidos para los clientes vigilados en el rango indicado.")
                else:
                    st.success(f"Se han encontrado {len(resultados)} pedido(s).")
                    st.dataframe(resultados, use_container_width=True, hide_index=True)

                    alert_store = OrderAlertStore()
                    import_legacy_notification_file(alert_store, LEGACY_NOTIFICATIONS_FILE)
                    load_env_file()
                    destinatarios = alert_recipients()
                    with st.spinner(f"Comprobando registro y enviando alerta a {', '.join(destinatarios)}..."):
                        # Acción explícita: sin límite de intentos para poder reintentar envíos agotados.
                        dispatch = dispatch_order_alerts(
                            resultados.to_dict(orient="records"),
                            alert_store,
                            send_alert_email,
                            destinatarios,
                        )
                    if dispatch.error:
                        alert_store.record_check(
                            SOURCE_MANUAL, "error_envio", error=dispatch.error
                        )
                        st.error(f"No se pudo enviar el email de alerta: {dispatch.error}")
                    elif dispatch.sent:
                        alert_store.record_check(
                            SOURCE_MANUAL, "enviado", sent_count=len(dispatch.new_rows)
                        )
                        st.success(f"Email de alerta enviado a {', '.join(destinatarios)}.")
                    else:
                        alert_store.record_check(SOURCE_MANUAL, "ya_notificados")
                        st.info("Los pedidos ya están notificados o hay otra ejecución procesándolos.")

    render_footer()


def render_kering_images_page() -> None:
    render_sidebar_shell("Imagenes Kering")
    render_kering_page(get_db_engine)
    render_footer()


KERING_IMAGES_PAGE = st.Page(render_kering_images_page, title="Imágenes Kering", url_path="imagenes-kering")


def render_provider_images_page() -> None:
    render_sidebar_shell("Proveedores de imágenes")
    from provider_images_ui import render_provider_page
    render_provider_page(get_db_engine)
    render_footer()


PROVIDER_IMAGES_PAGE = st.Page(render_provider_images_page, title="Proveedores de imágenes",
                               url_path="proveedores-imagenes")
HOME_PAGE = st.Page(render_home_page, title="Inicio", icon="🏠", url_path="", default=True)
ABC_HOME_PAGE = st.Page(render_abc_home_page, title="Inicio", icon="📊", url_path="abc")
ABC_SEARCH_PAGE = st.Page(render_abc_search_page, title="Buscar Producto", icon="🔍", url_path="abc-buscar")
ABC_REPORTS_PAGE = st.Page(render_abc_reports_page, title="Reportes ABCD", icon="📈", url_path="abc-reportes")
ABC_HISTORY_PAGE = st.Page(render_abc_history_page, title="Histórico Semanal", icon="📅", url_path="abc-historico")
ABC_DETAIL_PAGE = st.Page(render_abc_detail_page, title="Análisis Detallado", icon="📉", url_path="abc-analisis")
MASTER_IMPORT_PAGE = st.Page(render_master_import_page, title="Importador Masterdata", icon="📥", url_path="master")
MASTER_DRYRUN_PAGE = st.Page(render_master_dryrun_page, title="Dry-run Odoo", icon="🧪", url_path="master-dry-run")
MASTER_DICTIONARY_PAGE = st.Page(render_master_dictionary_page_route, title="Diccionario", icon="📖", url_path="master-diccionario")
LUXOPTICA_IMAGES_PAGE = st.Page(
    partial(
        render_images_page,
        render_sidebar_shell=render_sidebar_shell,
        require_admin_access=require_admin_access,
        render_footer=render_footer,
    ),
    title="Biblioteca de imágenes",
    icon="🖼️",
    url_path="repositorio-imagenes",
)
LUXOPTICA_PENDING_PAGE = st.Page(
    partial(
        render_pending_page,
        render_sidebar_shell=render_sidebar_shell,
        require_admin_access=require_admin_access,
        render_footer=render_footer,
        render_pending_panel=render_luxoptica_pending_panel,
    ),
    title="Pendiente Luxoptica",
    icon="⏳",
    url_path="pendiente-luxoptica",
)
ALERTA_PEDIDOS_PAGE = st.Page(
    partial(
        render_alerts_page,
        render_sidebar_shell=render_sidebar_shell,
        require_admin_access=require_admin_access,
        render_footer=render_footer,
    ),
    title="Alerta Pedidos",
    icon="🔔",
    url_path="alerta-pedidos",
)
SETTINGS_PAGE = st.Page(render_settings_page, title="Configuración", icon="⚙️", url_path="config")
ODOO_RECONCILIATION_PAGE = st.Page(render_odoo_reconciliation_page, title="Conciliación Odoo", url_path="conciliacion-odoo")

navigation = build_navigation(
    st,
    st.session_state.get("auth_role"),
    {
        "home": HOME_PAGE,
        "abc_home": ABC_HOME_PAGE,
        "abc_search": ABC_SEARCH_PAGE,
        "abc_reports": ABC_REPORTS_PAGE,
        "abc_history": ABC_HISTORY_PAGE,
        "abc_detail": ABC_DETAIL_PAGE,
        "master_import": MASTER_IMPORT_PAGE,
        "master_dryrun": MASTER_DRYRUN_PAGE,
        "master_dictionary": MASTER_DICTIONARY_PAGE,
        "luxoptica_images": LUXOPTICA_IMAGES_PAGE,
        "luxoptica_pending": LUXOPTICA_PENDING_PAGE,
        "kering_images": KERING_IMAGES_PAGE,
        "provider_images": PROVIDER_IMAGES_PAGE,
        "alerts": ALERTA_PEDIDOS_PAGE,
        "reconciliation": ODOO_RECONCILIATION_PAGE,
        "settings": SETTINGS_PAGE,
    },
)

from kering_images_ui import page_transition
page_transition(st.session_state, navigation.url_path)
from cloud_dashboard import dashboard_transition
from cloud_design import page_title as cloud_page_title
dashboard_transition(st.session_state, navigation.url_path)
navigation.run()
