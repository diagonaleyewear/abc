from __future__ import annotations

from dataclasses import dataclass
from contextlib import closing, contextmanager
from html import escape
import json
import logging
import os
from pathlib import Path
import sqlite3
import time
import uuid
from typing import Any, Callable, Iterable, Sequence

from filelock import FileLock


ORDER_ALERT_STATE_PATH_ENV = "ORDER_ALERT_STATE_PATH"
ORDER_ALERT_LEASE_SECONDS = 600
SQLITE_INITIALIZATION_TIMEOUT = 10.0
SQLITE_BUSY_TIMEOUT = 10.0
LOGGER = logging.getLogger(__name__)
LEGACY_NOTIFICATIONS_FILE = Path(__file__).resolve().parent / "alerta_pedidos_notificados.json"

ALERT_RECIPIENT_EMAIL_ENV = "ALERT_RECIPIENT_EMAIL"
ALERT_RECIPIENT_EMAIL_DEFAULT = "roberto@diagonaleyewear.com"
ALERT_RECIPIENT_EMAILS_EXTRA = ("virginia.nunez@diagonaleyewear.com",)

SOURCE_AUTO = "auto"
SOURCE_MANUAL = "manual"
SOURCE_CRON = "cron"

# Margen sobre dos intervalos antes de considerar que el servicio no late.
SCHEDULER_GRACE_SECONDS = 300

RESULT_LABELS = {
    "sin_clientes": "Lista de vigilancia vacía",
    "sin_pedidos": "Sin pedidos pendientes",
    "ya_notificados": "Sin pedidos nuevos (ya notificados)",
    "enviado": "Alerta enviada",
    "error_consulta": "Error al consultar Odoo",
    "error_envio": "Error al enviar el email",
    "reintentos_agotados": "Reintentos de envío agotados",
    "envio_ambiguo": "Envío con resultado desconocido (requiere confirmación)",
    "error": "Error inesperado",
}


class MailDeliveryAmbiguous(RuntimeError):
    """El proveedor pudo haber aceptado el correo (timeout, corte de conexión o 5xx tras enviar la petición).

    Reenviar automáticamente podría duplicar el aviso, por lo que el pedido queda bloqueado hasta que
    una persona confirme el resultado con `OrderAlertStore.resolve_ambiguous`.
    """


@dataclass(frozen=True)
class OrderAlertBatch:
    batch_id: str
    order_ids: tuple[int, ...]


@dataclass(frozen=True)
class AlertDispatchResult:
    new_rows: tuple[dict[str, Any], ...]
    recipients: tuple[str, ...]
    sent: bool
    error: str | None = None
    exhausted_order_ids: tuple[int, ...] = ()
    ambiguous_order_ids: tuple[int, ...] = ()
    ambiguous_batch: bool = False


def alert_recipients() -> list[str]:
    primary = os.getenv(ALERT_RECIPIENT_EMAIL_ENV, "").strip() or ALERT_RECIPIENT_EMAIL_DEFAULT
    return list(dict.fromkeys([primary, *ALERT_RECIPIENT_EMAILS_EXTRA]))


def dispatch_order_alerts(
    rows: Iterable[dict[str, Any]],
    store: "OrderAlertStore",
    send_email: Callable[..., None],
    recipients: Sequence[str],
    max_attempts: int | None = None,
    source: str = SOURCE_MANUAL,
) -> AlertDispatchResult:
    """Reserva los pedidos no notificados, envía un único email y confirma el estado solo tras la aceptación.

    Un fallo definitivo (`Exception`) deja el pedido reintentable. `MailDeliveryAmbiguous` o una interrupción
    tras iniciar el envío lo dejan bloqueado como 'ambiguous' hasta confirmación explícita.
    """
    rows = list(rows)
    order_ids = [int(row["pedido_id"]) for row in rows]
    recipients = tuple(recipients)
    batch = store.claim_orders(order_ids, max_attempts=max_attempts, source=source)
    exhausted = store.exhausted_order_ids(order_ids, max_attempts) if max_attempts else ()
    blocked = store.ambiguous_order_ids(order_ids)
    claimed = set(batch.order_ids)
    new_rows = tuple(row for row in rows if int(row["pedido_id"]) in claimed)
    if not new_rows:
        return AlertDispatchResult((), recipients, False, None, exhausted, blocked)

    if not store.begin_send(batch.batch_id, ", ".join(recipients)):
        return AlertDispatchResult((), recipients, False, "La reserva de envío se perdió; no se envió nada", exhausted, blocked)

    try:
        send_email(
            subject=f"Alerta de pedidos vigilados ({len(new_rows)})",
            html_body=build_alert_email_html(new_rows),
            to_address=list(recipients),
        )
    except MailDeliveryAmbiguous as exc:
        store.mark_ambiguous(batch.batch_id, str(exc))
        blocked = store.ambiguous_order_ids(order_ids)
        return AlertDispatchResult(new_rows, recipients, False, str(exc), exhausted, blocked, True)
    except Exception as exc:  # noqa: BLE001
        store.mark_failed(batch.batch_id, str(exc))
        return AlertDispatchResult(new_rows, recipients, False, str(exc), exhausted, blocked)

    # Si esta escritura falla, el lote sigue 'sending' y pasa a 'ambiguous' al vencer; nunca se reenvía solo.
    store.mark_sent(batch.batch_id, ", ".join(recipients))
    return AlertDispatchResult(new_rows, recipients, True, None, exhausted, blocked)


def scheduler_state(
    status: dict[str, Any] | None,
    now: int | None = None,
    grace_seconds: int = SCHEDULER_GRACE_SECONDS,
) -> str:
    """Devuelve 'never', 'stopped', 'stale' o 'running' a partir del latido del servicio automático."""
    if not status or not status.get("heartbeat_at"):
        return "never"
    timestamp = int(time.time()) if now is None else int(now)
    heartbeat_at = int(status["heartbeat_at"])
    stopped_at = status.get("stopped_at")
    if stopped_at and int(stopped_at) >= heartbeat_at:
        return "stopped"
    interval = int(status.get("interval_seconds") or 300)
    if timestamp - heartbeat_at > 2 * interval + grace_seconds:
        return "stale"
    return "running"


def default_order_alert_store_path() -> Path:
    default_path = Path(__file__).resolve().parent / "masterdata_data" / "order_alerts.sqlite3"
    return Path(os.getenv(ORDER_ALERT_STATE_PATH_ENV, str(default_path)))


def build_alert_email_html(rows: Iterable[dict[str, Any]]) -> str:
    table_rows = "".join(
        "<tr>"
        + "".join(
            f"<td>{escape(str(row.get(column, '') or ''))}</td>"
            for column in (
                "pedido",
                "cliente",
                "direccion_entrega",
                "date_order",
                "state",
                "invoice_status",
                "amount_total",
            )
        )
        + "</tr>"
        for row in rows
    )
    return (
        "<p>Se han detectado los siguientes pedidos NUEVOS de clientes vigilados en Odoo:</p>"
        "<table border='1' cellpadding='4' cellspacing='0'>"
        "<tr><th>Pedido</th><th>Cliente</th><th>Dirección entrega</th><th>Fecha</th><th>Estado</th>"
        "<th>Estado factura</th><th>Total</th></tr>"
        f"{table_rows}"
        "</table>"
    )


def import_legacy_notification_file(store: "OrderAlertStore", legacy_path: Path | str) -> None:
    source = Path(legacy_path)
    if not source.exists():
        return
    try:
        payload = json.loads(source.read_text(encoding="utf-8") or "[]")
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"No se pudo migrar el registro legacy de alertas: {source}") from exc
    if not isinstance(payload, list):
        raise RuntimeError(f"El registro legacy de alertas tiene un formato inválido: {source}")
    store.import_legacy_sent(payload)


class OrderAlertStore:
    """Shared durable claims for manual and scheduled order-alert email sends."""

    def __init__(self, database_path: Path | str | None = None) -> None:
        self.database_path = Path(database_path or default_order_alert_store_path()).resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + SQLITE_INITIALIZATION_TIMEOUT
        last_busy: sqlite3.OperationalError | None = None
        # Initialization lock is separate from transactional claims; never hold it during email sends.
        with FileLock(str(self.database_path) + ".init.lock", timeout=SQLITE_INITIALIZATION_TIMEOUT):
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if last_busy is not None:
                        raise last_busy
                    raise TimeoutError("Order alert SQLite initialization deadline exceeded")
                try:
                    with self._connect(timeout=min(0.1, remaining)) as connection:
                        if connection.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                            mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                            if mode != "wal":
                                raise RuntimeError(f"Order alert SQLite journal mode is {mode}, not WAL")
                        self._initialize_schema(connection)
                    break
                except sqlite3.OperationalError as error:
                    code = getattr(error, "sqlite_errorcode", 0) & 0xff
                    if code not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                        raise
                    last_busy = error
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise
                    LOGGER.warning("Order alert SQLite initialization busy; bounded retry: %s", error)
                    time.sleep(min(0.05, remaining))

    @contextmanager
    def _connect(self, timeout: float = SQLITE_BUSY_TIMEOUT):
        with closing(sqlite3.connect(self.database_path, timeout=timeout)) as connection:
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                yield connection

    @staticmethod
    def _initialize_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS order_notifications (
                order_id INTEGER PRIMARY KEY,
                status TEXT NOT NULL,
                batch_id TEXT,
                claimed_until INTEGER,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                recipient TEXT,
                updated_at INTEGER NOT NULL,
                sent_at INTEGER
            );
            CREATE INDEX IF NOT EXISTS order_notifications_batch_idx
                ON order_notifications (batch_id, status);
            CREATE TABLE IF NOT EXISTS order_alert_deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT NOT NULL,
                source TEXT,
                order_ids TEXT NOT NULL,
                recipients TEXT,
                outcome TEXT NOT NULL,
                error TEXT,
                started_at INTEGER NOT NULL,
                finished_at INTEGER
            );
            CREATE INDEX IF NOT EXISTS order_alert_deliveries_batch_idx
                ON order_alert_deliveries (batch_id);
            CREATE TABLE IF NOT EXISTS order_alert_status (
                source TEXT PRIMARY KEY,
                started_at INTEGER,
                stopped_at INTEGER,
                heartbeat_at INTEGER,
                interval_seconds INTEGER,
                last_check_at INTEGER,
                last_check_result TEXT,
                last_check_detail TEXT,
                last_sent_at INTEGER,
                last_sent_count INTEGER,
                last_error_at INTEGER,
                last_error TEXT,
                updated_at INTEGER NOT NULL
            );
            """
        )

    def import_legacy_sent(self, order_ids: Iterable[int], now: int | None = None) -> None:
        timestamp = int(time.time()) if now is None else int(now)
        values = [(int(order_id), timestamp) for order_id in order_ids]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                """
                INSERT OR IGNORE INTO order_notifications (
                    order_id, status, updated_at, sent_at
                ) VALUES (?, 'sent', ?, ?)
                """,
                ((order_id, timestamp, timestamp) for order_id, timestamp in values),
            )

    def claim_orders(
        self,
        order_ids: Iterable[int],
        now: int | None = None,
        lease_seconds: int = ORDER_ALERT_LEASE_SECONDS,
        max_attempts: int | None = None,
        source: str = SOURCE_MANUAL,
    ) -> OrderAlertBatch:
        timestamp = int(time.time()) if now is None else int(now)
        batch_id = uuid.uuid4().hex
        claimed: list[int] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for order_id in dict.fromkeys(int(value) for value in order_ids):
                row = connection.execute(
                    "SELECT status, claimed_until, attempts, batch_id FROM order_notifications WHERE order_id = ?",
                    (order_id,),
                ).fetchone()
                if row and row[0] in ("sent", "ambiguous"):
                    continue
                if row and row[0] == "sending":
                    if int(row[1] or 0) > timestamp:
                        continue
                    # La reserva venció durante el envío: no se sabe si el proveedor lo aceptó.
                    self._set_ambiguous(
                        connection, "WHERE order_id = ?", (order_id,), row[3],
                        "Envío interrumpido: no hay confirmación de aceptación ni de rechazo", timestamp,
                    )
                    continue
                if row and row[0] == "pending" and int(row[1] or 0) > timestamp:
                    continue
                if row and max_attempts is not None and int(row[2]) >= max_attempts:
                    continue

                attempts = int(row[2]) + 1 if row else 1
                connection.execute(
                    """
                    INSERT INTO order_notifications (
                        order_id, status, batch_id, claimed_until, attempts,
                        last_error, updated_at, sent_at
                    ) VALUES (?, 'pending', ?, ?, ?, NULL, ?, NULL)
                    ON CONFLICT (order_id) DO UPDATE SET
                        status = 'pending',
                        batch_id = excluded.batch_id,
                        claimed_until = excluded.claimed_until,
                        attempts = excluded.attempts,
                        last_error = NULL,
                        updated_at = excluded.updated_at,
                        sent_at = NULL
                    """,
                    (order_id, batch_id, timestamp + lease_seconds, attempts, timestamp),
                )
                claimed.append(order_id)
            if claimed:
                connection.execute(
                    """
                    INSERT INTO order_alert_deliveries (batch_id, source, order_ids, outcome, started_at)
                    VALUES (?, ?, ?, 'claimed', ?)
                    """,
                    (batch_id, source, json.dumps(claimed), timestamp),
                )
        return OrderAlertBatch(batch_id, tuple(claimed))

    @staticmethod
    def _set_ambiguous(
        connection: sqlite3.Connection,
        where: str,
        params: tuple[Any, ...],
        batch_id: str | None,
        error: str,
        timestamp: int,
    ) -> None:
        connection.execute(
            f"""
            UPDATE order_notifications
            SET status = 'ambiguous', last_error = ?, updated_at = ?, claimed_until = NULL
            {where} AND status = 'sending'
            """,
            (error[:2000], timestamp, *params),
        )
        if batch_id:
            connection.execute(
                """
                UPDATE order_alert_deliveries SET outcome = 'ambiguous', error = ?, finished_at = ?
                WHERE batch_id = ? AND outcome IN ('claimed', 'sending')
                """,
                (error[:2000], timestamp, batch_id),
            )

    def begin_send(
        self,
        batch_id: str,
        recipient: str,
        now: int | None = None,
        lease_seconds: int = ORDER_ALERT_LEASE_SECONDS,
    ) -> bool:
        """Persiste (con fsync) que el envío va a empezar. Devuelve False si la reserva ya no es válida."""
        timestamp = int(time.time()) if now is None else int(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            expected = connection.execute(
                "SELECT order_ids FROM order_alert_deliveries WHERE batch_id = ? AND outcome = 'claimed'",
                (batch_id,),
            ).fetchone()
            if expected is None:
                return False
            updated = connection.execute(
                """
                UPDATE order_notifications
                SET status = 'sending', claimed_until = ?, updated_at = ?
                WHERE batch_id = ? AND status = 'pending' AND claimed_until > ?
                """,
                (timestamp + lease_seconds, timestamp, batch_id, timestamp),
            ).rowcount
            if updated != len(json.loads(expected[0])):
                connection.rollback()
                return False
            connection.execute(
                "UPDATE order_alert_deliveries SET outcome = 'sending', recipients = ? WHERE batch_id = ?",
                (recipient, batch_id),
            )
        return True

    def mark_sent(self, batch_id: str, recipient: str, now: int | None = None) -> None:
        timestamp = int(time.time()) if now is None else int(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE order_notifications
                SET status = 'sent', recipient = ?, sent_at = ?, updated_at = ?,
                    claimed_until = NULL, last_error = NULL
                WHERE batch_id = ? AND status IN ('pending', 'sending')
                """,
                (recipient, timestamp, timestamp, batch_id),
            )
            connection.execute(
                """
                UPDATE order_alert_deliveries SET outcome = 'sent', recipients = ?, error = NULL, finished_at = ?
                WHERE batch_id = ? AND outcome IN ('claimed', 'sending')
                """,
                (recipient, timestamp, batch_id),
            )

    def mark_failed(self, batch_id: str, error: str, now: int | None = None) -> None:
        timestamp = int(time.time()) if now is None else int(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE order_notifications
                SET status = 'failed', last_error = ?, updated_at = ?, claimed_until = NULL
                WHERE batch_id = ? AND status IN ('pending', 'sending')
                """,
                (str(error)[:2000], timestamp, batch_id),
            )
            connection.execute(
                """
                UPDATE order_alert_deliveries SET outcome = 'failed', error = ?, finished_at = ?
                WHERE batch_id = ? AND outcome IN ('claimed', 'sending')
                """,
                (str(error)[:2000], timestamp, batch_id),
            )

    def mark_ambiguous(self, batch_id: str, error: str, now: int | None = None) -> None:
        timestamp = int(time.time()) if now is None else int(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._set_ambiguous(connection, "WHERE batch_id = ?", (batch_id,), batch_id, str(error), timestamp)

    def ambiguous_order_ids(self, order_ids: Iterable[int] | None = None) -> tuple[int, ...]:
        """Pedidos bloqueados por un envío de resultado desconocido (todos, o los indicados)."""
        ids = None if order_ids is None else list(dict.fromkeys(int(value) for value in order_ids))
        with self._connect() as connection:
            if ids is None:
                rows = connection.execute(
                    "SELECT order_id FROM order_notifications WHERE status = 'ambiguous' ORDER BY order_id"
                ).fetchall()
            elif not ids:
                return ()
            else:
                placeholders = ",".join("?" for _ in ids)
                rows = connection.execute(
                    f"SELECT order_id FROM order_notifications WHERE status = 'ambiguous' "
                    f"AND order_id IN ({placeholders}) ORDER BY order_id",
                    ids,
                ).fetchall()
        return tuple(int(row[0]) for row in rows)

    def resolve_ambiguous(
        self,
        order_ids: Iterable[int],
        resolution: str,
        actor: str,
        now: int | None = None,
    ) -> tuple[int, ...]:
        """Acción explícita tras comprobar el buzón: 'sent' lo da por notificado, 'retry' lo habilita de nuevo."""
        if resolution not in ("sent", "retry"):
            raise ValueError("resolution debe ser 'sent' o 'retry'")
        if not actor.strip():
            raise ValueError("Se requiere identificar quién resuelve el envío ambiguo")
        timestamp = int(time.time()) if now is None else int(now)
        resolved: list[int] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for order_id in dict.fromkeys(int(value) for value in order_ids):
                if resolution == "sent":
                    updated = connection.execute(
                        """
                        UPDATE order_notifications
                        SET status = 'sent', sent_at = ?, updated_at = ?, last_error = NULL,
                            recipient = 'confirmado manualmente'
                        WHERE order_id = ? AND status = 'ambiguous'
                        """,
                        (timestamp, timestamp, order_id),
                    ).rowcount
                else:
                    updated = connection.execute(
                        """
                        UPDATE order_notifications
                        SET status = 'failed', attempts = 0, updated_at = ?
                        WHERE order_id = ? AND status = 'ambiguous'
                        """,
                        (timestamp, order_id),
                    ).rowcount
                if updated:
                    resolved.append(order_id)
            if resolved:
                connection.execute(
                    """
                    INSERT INTO order_alert_deliveries
                        (batch_id, source, order_ids, outcome, error, started_at, finished_at)
                    VALUES (?, 'resolution', ?, ?, ?, ?, ?)
                    """,
                    (uuid.uuid4().hex, json.dumps(resolved), f"resolved_{resolution}", f"actor={actor.strip()}",
                     timestamp, timestamp),
                )
        return tuple(resolved)

    def recent_deliveries(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM order_alert_deliveries ORDER BY id DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [dict(row) for row in rows]

    def exhausted_order_ids(
        self,
        order_ids: Iterable[int],
        max_attempts: int,
        now: int | None = None,
    ) -> tuple[int, ...]:
        timestamp = int(time.time()) if now is None else int(now)
        ids = list(dict.fromkeys(int(value) for value in order_ids))
        if not ids:
            return ()
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT order_id FROM order_notifications
                WHERE order_id IN ({placeholders})
                  AND status NOT IN ('sent', 'ambiguous')
                  AND attempts >= ?
                  AND COALESCE(claimed_until, 0) <= ?
                ORDER BY order_id
                """,
                (*ids, int(max_attempts), timestamp),
            ).fetchall()
        return tuple(int(row[0]) for row in rows)

    def _update_status(self, source: str, values: dict[str, Any], now: int) -> None:
        assignments = ", ".join(f"{column} = ?" for column in values)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT OR IGNORE INTO order_alert_status (source, updated_at) VALUES (?, ?)",
                (source, now),
            )
            connection.execute(
                f"UPDATE order_alert_status SET {assignments}, updated_at = ? WHERE source = ?",
                (*values.values(), now, source),
            )

    def record_heartbeat(
        self,
        source: str,
        interval_seconds: int,
        started: bool = False,
        now: int | None = None,
    ) -> None:
        timestamp = int(time.time()) if now is None else int(now)
        values: dict[str, Any] = {"heartbeat_at": timestamp, "interval_seconds": int(interval_seconds)}
        if started:
            values.update(started_at=timestamp, stopped_at=None)
        self._update_status(source, values, timestamp)

    def record_stopped(self, source: str, now: int | None = None) -> None:
        timestamp = int(time.time()) if now is None else int(now)
        self._update_status(source, {"stopped_at": timestamp}, timestamp)

    def record_check(
        self,
        source: str,
        result: str,
        detail: str = "",
        error: str | None = None,
        sent_count: int = 0,
        now: int | None = None,
    ) -> None:
        timestamp = int(time.time()) if now is None else int(now)
        values: dict[str, Any] = {
            "last_check_at": timestamp,
            "last_check_result": result,
            "last_check_detail": detail[:2000],
        }
        if sent_count:
            values.update(last_sent_at=timestamp, last_sent_count=int(sent_count))
        if error:
            values.update(last_error_at=timestamp, last_error=str(error)[:2000])
        self._update_status(source, values, timestamp)

    def get_status(self, source: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM order_alert_status WHERE source = ?",
                (source,),
            ).fetchone()
        return dict(row) if row else None
