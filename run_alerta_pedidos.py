"""Comprobación de pedidos pendientes de clientes vigilados y envío de alertas por email.

Uso:
  python run_alerta_pedidos.py                 una comprobación (compatibilidad cron)
  python run_alerta_pedidos.py --loop          servicio continuo (contenedor abcd-order-alerts)
  python run_alerta_pedidos.py --healthcheck   sale con 0 si el servicio continuo tiene latido reciente

El modo automático busca solo pedidos confirmados y pendientes (PENDING_CONDITION de
check_pedidos_vigilados) de los últimos ALERTA_PEDIDOS_DIAS_ATRAS días.
"""

from __future__ import annotations

import argparse
import os
import signal
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable

from check_pedidos_vigilados import find_matching_orders
from db_config import load_env_file
from graph_mail_downloader import send_alert_email
from order_alerts import (
    LEGACY_NOTIFICATIONS_FILE,
    SOURCE_AUTO,
    SOURCE_CRON,
    OrderAlertStore,
    alert_recipients,
    default_order_alert_store_path,
    dispatch_order_alerts,
    import_legacy_notification_file,
    scheduler_state,
)
from watchlist_config import load_watchlist

DIAS_ATRAS_ENV = "ALERTA_PEDIDOS_DIAS_ATRAS"
DIAS_ATRAS_DEFAULT = 180
INTERVALO_ENV = "ALERTA_PEDIDOS_INTERVALO_MINUTOS"
INTERVALO_DEFAULT_MINUTOS = 5
REINTENTOS_CONSULTA_ENV = "ALERTA_PEDIDOS_REINTENTOS_CONSULTA"
REINTENTOS_CONSULTA_DEFAULT = 3
MAX_INTENTOS_ENVIO_ENV = "ALERTA_PEDIDOS_MAX_INTENTOS_ENVIO"
MAX_INTENTOS_ENVIO_DEFAULT = 5
QUERY_RETRY_DELAYS_SECONDS = (10, 30)


@dataclass(frozen=True)
class CheckOutcome:
    result: str
    found: int = 0
    sent: int = 0
    detail: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _log(mensaje: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {mensaje}", flush=True)


def _int_env(name: str, default: int, minimum: int) -> int:
    raw = os.getenv(name, "").strip()
    value = int(raw) if raw else default
    if value < minimum:
        raise ValueError(f"{name} debe ser >= {minimum} (valor: {value})")
    return value


def _query_with_retries(
    query: Callable[[], Any],
    attempts: int,
    wait: Callable[[float], Any],
) -> Any:
    for attempt in range(1, attempts + 1):
        try:
            return query()
        except Exception as exc:  # noqa: BLE001
            if attempt >= attempts:
                raise
            delay = QUERY_RETRY_DELAYS_SECONDS[min(attempt - 1, len(QUERY_RETRY_DELAYS_SECONDS) - 1)]
            _log(f"Consulta a Odoo fallida (intento {attempt}/{attempts}): {exc}. Reintento en {delay}s.")
            wait(delay)
    raise AssertionError("unreachable")


def run_check(
    store: OrderAlertStore,
    source: str,
    find_orders: Callable[..., Any] = find_matching_orders,
    send_email: Callable[..., None] = send_alert_email,
    load_clients: Callable[[], list[str]] = load_watchlist,
    today: date | None = None,
    dias_atras: int = DIAS_ATRAS_DEFAULT,
    query_attempts: int = REINTENTOS_CONSULTA_DEFAULT,
    max_send_attempts: int | None = MAX_INTENTOS_ENVIO_DEFAULT,
    wait: Callable[[float], Any] = time.sleep,
) -> CheckOutcome:
    """Una comprobación completa: watchlist -> Odoo (pendientes) -> deduplicación -> email -> estado."""
    watchlist = load_clients()
    if not watchlist:
        outcome = CheckOutcome("sin_clientes", detail="Lista de vigilancia vacía")
        store.record_check(source, outcome.result, detail=outcome.detail)
        return outcome

    hoy = today or date.today()
    fecha_desde = hoy - timedelta(days=dias_atras)
    try:
        resultados = _query_with_retries(
            lambda: find_orders(
                clientes=watchlist,
                fecha_desde=fecha_desde,
                fecha_hasta=hoy + timedelta(days=1),
                solo_pendientes=True,
            ),
            query_attempts,
            wait,
        )
    except Exception as exc:  # noqa: BLE001
        outcome = CheckOutcome(
            "error_consulta",
            detail=f"{len(watchlist)} cliente(s) vigilado(s)",
            error=f"Consulta Odoo tras {query_attempts} intento(s): {exc}",
        )
        store.record_check(source, outcome.result, detail=outcome.detail, error=outcome.error)
        return outcome

    ventana = f"últimos {dias_atras} días"
    if resultados.empty:
        outcome = CheckOutcome("sin_pedidos", detail=f"0 pedidos pendientes ({ventana})")
        store.record_check(source, outcome.result, detail=outcome.detail)
        return outcome

    import_legacy_notification_file(store, LEGACY_NOTIFICATIONS_FILE)
    dispatch = dispatch_order_alerts(
        resultados.to_dict(orient="records"),
        store,
        send_email,
        alert_recipients(),
        max_attempts=max_send_attempts,
    )
    found = len(resultados)
    nuevos = len(dispatch.new_rows)
    detail = f"{found} pendiente(s) ({ventana}), {nuevos} nuevo(s)"
    errors: list[str] = []
    if dispatch.error:
        errors.append(f"Envío a {', '.join(dispatch.recipients)}: {dispatch.error}")
    if dispatch.exhausted_order_ids:
        errors.append(
            f"Pedido(s) {', '.join(map(str, dispatch.exhausted_order_ids))} sin enviar tras "
            f"{max_send_attempts} intento(s); usa «Comprobar ahora» para reintentar"
        )

    if dispatch.error:
        result = "error_envio"
    elif dispatch.sent:
        result = "enviado"
    elif dispatch.exhausted_order_ids:
        result = "reintentos_agotados"
    else:
        result = "ya_notificados"
    outcome = CheckOutcome(
        result,
        found=found,
        sent=nuevos if dispatch.sent else 0,
        detail=detail,
        error="; ".join(errors) or None,
    )
    store.record_check(source, result, detail=detail, error=outcome.error, sent_count=outcome.sent)
    return outcome


def run_forever(
    store: OrderAlertStore,
    interval_seconds: int,
    stop_event: threading.Event,
    check: Callable[[], CheckOutcome],
    max_cycles: int | None = None,
) -> None:
    """Ejecuta `check` cada `interval_seconds` hasta `stop_event`; un fallo no detiene el bucle."""
    store.record_heartbeat(SOURCE_AUTO, interval_seconds, started=True)
    _log(f"Servicio de alertas iniciado. Intervalo: {interval_seconds}s.")
    cycles = 0
    while not stop_event.is_set():
        try:
            store.record_heartbeat(SOURCE_AUTO, interval_seconds)
            outcome = check()
            message = f"Comprobación: {outcome.result}. {outcome.detail}"
            _log(f"{message}. ERROR: {outcome.error}" if outcome.error else message)
            store.record_heartbeat(SOURCE_AUTO, interval_seconds)
        except Exception as exc:  # noqa: BLE001
            _log(f"ERROR inesperado en la comprobación: {exc}")
            try:
                store.record_check(SOURCE_AUTO, "error", error=str(exc))
            except Exception as status_exc:  # noqa: BLE001
                _log(f"ERROR al registrar el estado del servicio: {status_exc}")
        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            break
        stop_event.wait(interval_seconds)
    store.record_stopped(SOURCE_AUTO)
    _log("Servicio de alertas detenido.")


def healthcheck(store: OrderAlertStore) -> int:
    state = scheduler_state(store.get_status(SOURCE_AUTO))
    print(state)
    return 0 if state == "running" else 1


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--loop", action="store_true", help="Comprobar periódicamente hasta recibir SIGTERM.")
    mode.add_argument("--healthcheck", action="store_true", help="Comprobar el latido del servicio continuo.")
    parser.add_argument("--interval-minutes", type=int, help=f"Intervalo del modo --loop (env {INTERVALO_ENV}).")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ejecutar la comprobación puntual aunque el servicio continuo esté activo.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    load_env_file()

    try:
        store = OrderAlertStore()
        if args.healthcheck:
            return healthcheck(store)

        dias_atras = _int_env(DIAS_ATRAS_ENV, DIAS_ATRAS_DEFAULT, minimum=1)
        query_attempts = _int_env(REINTENTOS_CONSULTA_ENV, REINTENTOS_CONSULTA_DEFAULT, minimum=1)
        max_send_attempts = _int_env(MAX_INTENTOS_ENVIO_ENV, MAX_INTENTOS_ENVIO_DEFAULT, minimum=1)

        if args.loop:
            interval_minutes = args.interval_minutes or _int_env(INTERVALO_ENV, INTERVALO_DEFAULT_MINUTOS, minimum=1)
            if interval_minutes < 1:
                raise ValueError("--interval-minutes debe ser >= 1")
            stop_event = threading.Event()
            for signum in (signal.SIGTERM, signal.SIGINT):
                signal.signal(signum, lambda *_: stop_event.set())
            run_forever(
                store,
                interval_minutes * 60,
                stop_event,
                lambda: run_check(
                    store,
                    SOURCE_AUTO,
                    dias_atras=dias_atras,
                    query_attempts=query_attempts,
                    max_send_attempts=max_send_attempts,
                    wait=stop_event.wait,
                ),
            )
            return 0

        if not args.force and scheduler_state(store.get_status(SOURCE_AUTO)) == "running":
            _log("El servicio automático abcd-order-alerts está activo; se omite la ejecución puntual (usa --force).")
            return 0
        outcome = run_check(
            store,
            SOURCE_CRON,
            dias_atras=dias_atras,
            query_attempts=query_attempts,
            max_send_attempts=max_send_attempts,
        )
        message = f"Comprobación: {outcome.result}. {outcome.detail}"
        _log(f"{message}. ERROR: {outcome.error}" if outcome.error else message)
        return 0 if outcome.ok else 1
    except ValueError as exc:
        _log(f"ERROR de configuración: {exc}")
        return 2
    except (OSError, sqlite3.Error) as exc:
        _log(
            f"ERROR: no se puede usar el registro persistente {default_order_alert_store_path()}: {exc}. "
            "Revisa que el volumen /app/data exista y sea escribible por el usuario del contenedor."
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
