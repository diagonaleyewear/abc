import re
from concurrent.futures import ThreadPoolExecutor
import threading
import time
from datetime import date, timedelta

import pandas as pd
import pytest

import check_pedidos_vigilados
import graph_mail_downloader
import run_alerta_pedidos as runner
import watchlist_config
from order_alerts import (
    SOURCE_AUTO,
    SOURCE_MANUAL,
    OrderAlertStore,
    alert_recipients,
    dispatch_order_alerts,
    scheduler_state,
)

TODAY = date(2026, 10, 2)
COLUMNS = [
    "pedido_id",
    "pedido",
    "cliente",
    "direccion_entrega",
    "date_order",
    "state",
    "invoice_status",
    "amount_total",
]


def _orders(*order_ids: int) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "pedido_id": order_id,
                "pedido": f"S{order_id:05d}",
                "cliente": "Cliente Vigilado",
                "direccion_entrega": "",
                "date_order": "2026-10-01 10:00:00",
                "state": "sale",
                "invoice_status": "to invoice",
                "amount_total": 100.0,
            }
            for order_id in order_ids
        ],
        columns=COLUMNS,
    )


class FakeSender:
    def __init__(self, fail_times: int = 0, delay: float = 0.0) -> None:
        self.calls: list[dict] = []
        self.fail_times = fail_times
        self.delay = delay
        self._lock = threading.Lock()

    def __call__(self, subject: str, html_body: str, to_address: list[str]) -> None:
        time.sleep(self.delay)
        with self._lock:
            self.calls.append({"subject": subject, "html": html_body, "to": to_address})
            if len(self.calls) <= self.fail_times:
                raise RuntimeError("Graph 503 Service Unavailable")

    def sent_orders(self) -> list[str]:
        return [order for call in self.calls for order in re.findall(r"S\d{5}", call["html"])]


class RecordingEvent:
    def __init__(self) -> None:
        self.waits: list[float] = []

    def is_set(self) -> bool:
        return False

    def wait(self, timeout: float) -> bool:
        self.waits.append(timeout)
        return False


def _forbidden(*_args, **_kwargs):
    raise AssertionError("Los tests no deben acceder a Odoo ni a Microsoft Graph")


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    monkeypatch.setattr(check_pedidos_vigilados, "create_engine", _forbidden)
    monkeypatch.setattr(graph_mail_downloader.requests, "post", _forbidden)
    monkeypatch.setattr(runner, "LEGACY_NOTIFICATIONS_FILE", tmp_path / "legacy.json")
    monkeypatch.setattr(runner, "load_env_file", lambda *_args, **_kwargs: None)
    monkeypatch.delenv("ALERT_RECIPIENT_EMAIL", raising=False)
    monkeypatch.setenv("ORDER_ALERT_STATE_PATH", str(tmp_path / "data" / "order_alerts.sqlite3"))


@pytest.fixture
def store(tmp_path) -> OrderAlertStore:
    return OrderAlertStore(tmp_path / "data" / "order_alerts.sqlite3")


def _auto_check(store, find_orders, send_email, **kwargs):
    return runner.run_check(
        store,
        SOURCE_AUTO,
        find_orders=find_orders,
        send_email=send_email,
        load_clients=lambda: ["Cliente Vigilado"],
        today=TODAY,
        wait=lambda _seconds: None,
        **kwargs,
    )


def test_automatic_check_keeps_pending_filter_window_and_recipients(store) -> None:
    calls = []

    def find_orders(**kwargs):
        calls.append(kwargs)
        return _orders(1)

    sender = FakeSender()
    outcome = _auto_check(store, find_orders, sender)

    assert outcome.result == "enviado"
    assert calls == [
        {
            "clientes": ["Cliente Vigilado"],
            "fecha_desde": TODAY - timedelta(days=180),
            "fecha_hasta": TODAY + timedelta(days=1),
            "solo_pendientes": True,
        }
    ]
    assert sender.calls[0]["to"] == ["roberto@diagonaleyewear.com", "virginia.nunez@diagonaleyewear.com"]


def test_recipients_respect_environment_override(monkeypatch) -> None:
    monkeypatch.setenv("ALERT_RECIPIENT_EMAIL", "ops@example.test")
    assert alert_recipients() == ["ops@example.test", "virginia.nunez@diagonaleyewear.com"]


def test_loop_runs_every_interval_and_records_heartbeat(store) -> None:
    event = RecordingEvent()
    checks = []

    run_check = lambda: checks.append(1) or runner.CheckOutcome("sin_pedidos")  # noqa: E731
    runner.run_forever(store, 300, event, run_check, max_cycles=3)

    status = store.get_status(SOURCE_AUTO)
    assert len(checks) == 3
    assert event.waits == [300, 300]
    assert status["interval_seconds"] == 300
    assert status["heartbeat_at"] is not None
    assert scheduler_state(status) == "stopped"


def test_loop_keeps_checking_after_unexpected_errors(store) -> None:
    results = iter([RuntimeError("fallo temporal"), runner.CheckOutcome("sin_pedidos")])

    def check():
        result = next(results)
        if isinstance(result, Exception):
            raise result
        store.record_check(SOURCE_AUTO, result.result)
        return result

    runner.run_forever(store, 60, RecordingEvent(), check, max_cycles=2)

    status = store.get_status(SOURCE_AUTO)
    assert status["last_error"] == "fallo temporal"
    assert status["last_check_result"] == "sin_pedidos"


def test_loop_stops_promptly_when_signalled(store) -> None:
    stop_event = threading.Event()
    started = threading.Event()

    def check():
        started.set()
        return runner.CheckOutcome("sin_pedidos")

    worker = threading.Thread(target=runner.run_forever, args=(store, 3600, stop_event, check))
    worker.start()
    assert started.wait(5)
    stop_event.set()
    worker.join(5)

    assert not worker.is_alive()
    assert scheduler_state(store.get_status(SOURCE_AUTO)) == "stopped"


def test_manual_and_automatic_runs_share_deduplication(store) -> None:
    manual_sender = FakeSender()
    manual = dispatch_order_alerts(_orders(1, 2).to_dict(orient="records"), store, manual_sender, alert_recipients())
    assert manual.sent

    auto_sender = FakeSender()
    outcome = _auto_check(store, lambda **_: _orders(1, 2, 3), auto_sender)
    assert outcome.result == "enviado"
    assert auto_sender.sent_orders() == ["S00003"]

    repeat = dispatch_order_alerts(_orders(1, 2, 3).to_dict(orient="records"), store, manual_sender, alert_recipients())
    assert not repeat.sent
    assert len(manual_sender.calls) == 1


def _simultaneous_alert_runs(database, sender):
    barrier = threading.Barrier(2)

    def manual():
        barrier.wait()
        return dispatch_order_alerts(
            _orders(7).to_dict(orient="records"), OrderAlertStore(database), sender, ["a@example.test"])

    def automatic():
        barrier.wait()
        return _auto_check(OrderAlertStore(database), lambda **_: _orders(7), sender)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(manual), executor.submit(automatic)]
        return [future.result(timeout=10) for future in futures]


def test_simultaneous_manual_and_automatic_runs_send_once(tmp_path) -> None:
    database = tmp_path / "data" / "order_alerts.sqlite3"
    sender = FakeSender(delay=0.2)
    results = _simultaneous_alert_runs(database, sender)

    assert len(results) == 2
    assert sender.sent_orders() == ["S00007"]


def test_concurrent_initialization_error_is_not_success(tmp_path, monkeypatch) -> None:
    import sqlite3
    import sys

    def locked_store(_database):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(sys.modules[__name__], "OrderAlertStore", locked_store)
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        _simultaneous_alert_runs(tmp_path / "alerts.sqlite3", FakeSender())


def test_notifications_status_and_watchlist_survive_restart(tmp_path) -> None:
    database = tmp_path / "data" / "order_alerts.sqlite3"
    watchlist_path = tmp_path / "data" / "watchlist_clientes.json"
    watchlist_config.save_watchlist(["Cliente Vigilado"], watchlist_path)
    first_sender = FakeSender()
    _auto_check(OrderAlertStore(database), lambda **_: _orders(1), first_sender)

    restarted_store = OrderAlertStore(database)
    second_sender = FakeSender()
    outcome = runner.run_check(
        restarted_store,
        SOURCE_AUTO,
        find_orders=lambda **_: _orders(1),
        send_email=second_sender,
        load_clients=lambda: watchlist_config.load_watchlist(watchlist_path),
        today=TODAY,
    )

    status = restarted_store.get_status(SOURCE_AUTO)
    assert outcome.result == "ya_notificados"
    assert second_sender.calls == []
    assert status["last_sent_count"] == 1
    assert status["last_sent_at"] is not None


def test_query_failures_are_retried_a_limited_number_of_times(store) -> None:
    attempts = []
    waits = []

    def flaky(**_kwargs):
        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionError("Odoo no disponible")
        return _orders(1)

    sender = FakeSender()
    outcome = runner.run_check(
        store, SOURCE_AUTO, find_orders=flaky, send_email=sender,
        load_clients=lambda: ["Cliente Vigilado"], today=TODAY, wait=waits.append,
    )
    assert outcome.result == "enviado"
    assert waits == [10, 30]

    attempts.clear()

    def down(**_kwargs):
        attempts.append(1)
        raise ConnectionError("Odoo no disponible")

    always_down = _auto_check(store, down, FakeSender(), query_attempts=2)
    status = store.get_status(SOURCE_AUTO)
    assert always_down.result == "error_consulta"
    assert len(attempts) == 2
    assert "tras 2 intento(s)" in status["last_error"]
    assert status["last_check_result"] == "error_consulta"


def test_query_failure_does_not_claim_orders(store) -> None:
    def down(**_kwargs):
        raise ConnectionError("Odoo no disponible")

    _auto_check(store, down, FakeSender(), query_attempts=1)
    sender = FakeSender()
    outcome = _auto_check(store, lambda **_: _orders(1), sender)

    assert outcome.result == "enviado"
    assert len(sender.calls) == 1


def test_send_failure_is_not_marked_sent_and_is_retried(store) -> None:
    sender = FakeSender(fail_times=1)

    failed = _auto_check(store, lambda **_: _orders(1), sender)
    status = store.get_status(SOURCE_AUTO)
    assert failed.result == "error_envio"
    assert "Graph 503" in status["last_error"]
    assert status["last_sent_at"] is None

    retried = _auto_check(store, lambda **_: _orders(1), sender)
    assert retried.result == "enviado"
    assert len(sender.calls) == 2


def test_automatic_send_attempts_are_limited_and_manual_can_retry(store) -> None:
    sender = FakeSender(fail_times=99)
    results = [
        _auto_check(store, lambda **_: _orders(1), sender, max_send_attempts=2).result
        for _ in range(3)
    ]

    assert results == ["error_envio", "error_envio", "reintentos_agotados"]
    assert len(sender.calls) == 2
    assert "Pedido(s) 1 sin enviar tras 2" in store.get_status(SOURCE_AUTO)["last_error"]

    manual_sender = FakeSender()
    manual = dispatch_order_alerts(_orders(1).to_dict(orient="records"), store, manual_sender, ["a@example.test"])
    store.record_check(SOURCE_MANUAL, "enviado", sent_count=1)
    assert manual.sent
    assert _auto_check(store, lambda **_: _orders(1), sender).result == "ya_notificados"


def test_scheduler_state_distinguishes_stopped_from_idle(store) -> None:
    assert scheduler_state(None) == "never"
    store.record_heartbeat(SOURCE_AUTO, 300, started=True, now=1_000)
    store.record_check(SOURCE_AUTO, "sin_pedidos", now=1_000)
    status = store.get_status(SOURCE_AUTO)

    assert scheduler_state(status, now=1_000 + 600) == "running"
    assert scheduler_state(status, now=1_000 + 2 * 300 + 301) == "stale"
    store.record_stopped(SOURCE_AUTO, now=1_100)
    assert scheduler_state(store.get_status(SOURCE_AUTO), now=1_100) == "stopped"


def test_one_shot_cron_is_skipped_while_service_runs(monkeypatch, store) -> None:
    monkeypatch.setattr(runner, "run_check", _forbidden)
    store.record_heartbeat(SOURCE_AUTO, 300, started=True)

    assert runner.main([]) == 0
    assert runner.main(["--healthcheck"]) == 0


def test_healthcheck_fails_when_service_is_stale(store) -> None:
    store.record_heartbeat(SOURCE_AUTO, 60, started=True, now=int(time.time()) - 3_600)

    assert runner.main(["--healthcheck"]) == 1


def test_unwritable_state_path_exits_with_clear_error(monkeypatch, tmp_path, capsys) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setenv("ORDER_ALERT_STATE_PATH", str(blocker / "order_alerts.sqlite3"))

    assert runner.main(["--loop"]) == 2
    assert "registro persistente" in capsys.readouterr().out


def test_store_only_writes_inside_data_directory(tmp_path) -> None:
    data_dir = tmp_path / "data"
    store = OrderAlertStore(data_dir / "order_alerts.sqlite3")
    store.record_heartbeat(SOURCE_AUTO, 300, started=True)
    _auto_check(store, lambda **_: _orders(1), FakeSender())

    assert {path.parent for path in tmp_path.rglob("*") if path.is_file()} == {data_dir}


def test_watchlist_uses_shared_path_with_legacy_fallback(monkeypatch, tmp_path) -> None:
    legacy = tmp_path / "legacy_watchlist.json"
    legacy.write_text('{"clientes": ["Cliente Legacy"]}', encoding="utf-8")
    shared = tmp_path / "data" / "watchlist_clientes.json"
    monkeypatch.setattr(watchlist_config, "WATCHLIST_FILE", legacy)
    monkeypatch.setenv("WATCHLIST_PATH", str(shared))

    assert watchlist_config.load_watchlist() == ["Cliente Legacy"]
    assert watchlist_config.add_customer("Cliente Nuevo") == ["Cliente Legacy", "Cliente Nuevo"]
    assert watchlist_config.load_watchlist() == ["Cliente Legacy", "Cliente Nuevo"]
    assert legacy.read_text(encoding="utf-8") == '{"clientes": ["Cliente Legacy"]}'
    assert [path.name for path in shared.parent.iterdir()] == ["watchlist_clientes.json"]
