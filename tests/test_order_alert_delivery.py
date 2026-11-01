"""Pruebas sintéticas de entrega de alertas: concurrencia, resultados ambiguos y registro durable.

Ningún test llama a Microsoft Graph, Odoo ni envía correo real.
"""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading
import time

import pandas as pd
import pytest
import requests

import graph_mail_downloader
import run_alerta_pedidos as runner
from order_alerts import (
    MailDeliveryAmbiguous,
    OrderAlertStore,
    SOURCE_CRON,
    SOURCE_MANUAL,
    dispatch_order_alerts,
)

RECIPIENTS = ["alertas@example.invalid"]


def rows(*order_ids):
    return [{"pedido_id": order_id, "pedido": f"S{order_id}", "cliente": "Cliente sintético"} for order_id in order_ids]


@pytest.fixture
def store(tmp_path):
    return OrderAlertStore(tmp_path / "alerts.sqlite3")


class Sender:
    def __init__(self, error=None, delay=0.0):
        self.calls = 0
        self.error = error
        self.delay = delay
        self._lock = threading.Lock()

    def __call__(self, **kwargs):
        with self._lock:
            self.calls += 1
        time.sleep(self.delay)
        if self.error:
            raise self.error


def outcomes(store):
    return [row["outcome"] for row in reversed(store.recent_deliveries())]


def test_concurrent_manual_and_cron_dispatch_send_each_order_once(tmp_path) -> None:
    path = tmp_path / "alerts.sqlite3"
    sender = Sender(delay=0.2)
    stores = [OrderAlertStore(path), OrderAlertStore(path)]

    def run(index):
        source = SOURCE_MANUAL if index % 2 else SOURCE_CRON
        return dispatch_order_alerts(rows(1, 2), stores[index % 2], sender, RECIPIENTS, source=source)

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(run, range(6)))

    assert sender.calls == 1
    assert sum(result.sent for result in results) == 1
    assert outcomes(stores[0]) == ["sent"]


def test_manual_send_suppresses_cron_and_vice_versa(store) -> None:
    sender = Sender()
    assert dispatch_order_alerts(rows(7), store, sender, RECIPIENTS, source=SOURCE_MANUAL).sent
    repeat = dispatch_order_alerts(rows(7), store, sender, RECIPIENTS, max_attempts=5, source=SOURCE_CRON)
    assert not repeat.sent and not repeat.new_rows
    assert sender.calls == 1


def test_definitive_failure_is_logged_and_retryable(store) -> None:
    failing = Sender(error=RuntimeError("HTTP 400"))
    result = dispatch_order_alerts(rows(5), store, failing, RECIPIENTS)
    assert not result.sent and result.error == "HTTP 400" and not result.ambiguous_batch

    assert dispatch_order_alerts(rows(5), store, Sender(), RECIPIENTS).sent
    assert outcomes(store) == ["failed", "sent"]
    assert store.recent_deliveries()[1]["error"] == "HTTP 400"


def test_ambiguous_timeout_blocks_automatic_resend(store) -> None:
    result = dispatch_order_alerts(rows(5), store, Sender(error=MailDeliveryAmbiguous("timeout")), RECIPIENTS)
    assert result.ambiguous_batch and result.ambiguous_order_ids == (5,) and not result.sent

    later = Sender()
    for source in (SOURCE_CRON, SOURCE_MANUAL):
        blocked = dispatch_order_alerts(rows(5), store, later, RECIPIENTS, max_attempts=5, source=source)
        assert not blocked.sent and blocked.ambiguous_order_ids == (5,)
    assert later.calls == 0
    assert outcomes(store) == ["ambiguous"]


def test_ambiguous_resolution_requires_actor_and_only_touches_ambiguous(store) -> None:
    dispatch_order_alerts(rows(1), store, Sender(), RECIPIENTS)
    dispatch_order_alerts(rows(2), store, Sender(error=MailDeliveryAmbiguous("timeout")), RECIPIENTS)

    with pytest.raises(ValueError):
        store.resolve_ambiguous([2], "retry", " ")
    with pytest.raises(ValueError):
        store.resolve_ambiguous([2], "delete", "ruben")
    assert store.resolve_ambiguous([1, 2, 3], "retry", "ruben") == (2,)

    sender = Sender()
    result = dispatch_order_alerts(rows(1, 2), store, sender, RECIPIENTS)
    assert result.sent and [row["pedido_id"] for row in result.new_rows] == [2]
    assert "resolved_retry" in outcomes(store)


def test_ambiguous_confirmed_as_sent_is_never_resent(store) -> None:
    dispatch_order_alerts(rows(2), store, Sender(error=MailDeliveryAmbiguous("timeout")), RECIPIENTS)
    assert store.resolve_ambiguous([2], "sent", "ruben") == (2,)
    sender = Sender()
    assert not dispatch_order_alerts(rows(2), store, sender, RECIPIENTS).sent
    assert sender.calls == 0


def test_interrupted_send_becomes_ambiguous_not_resent(store) -> None:
    batch = store.claim_orders([9], now=100, lease_seconds=10)
    assert store.begin_send(batch.batch_id, "dest", now=101, lease_seconds=10)
    # El proceso muere aquí: no hay mark_sent ni mark_failed.
    takeover = store.claim_orders([9], now=500, lease_seconds=10)
    assert takeover.order_ids == ()
    assert store.ambiguous_order_ids([9]) == (9,)
    assert outcomes(store) == ["ambiguous"]


def test_claim_interrupted_before_send_is_safely_reclaimed(store) -> None:
    store.claim_orders([9], now=100, lease_seconds=10)
    assert store.claim_orders([9], now=500, lease_seconds=10).order_ids == (9,)


def test_failure_persisting_acceptance_never_causes_resend(tmp_path) -> None:
    class BrokenStore(OrderAlertStore):
        def mark_sent(self, *args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

    broken = BrokenStore(tmp_path / "alerts.sqlite3")
    sender = Sender()
    with pytest.raises(sqlite3.OperationalError):
        dispatch_order_alerts(rows(3), broken, sender, RECIPIENTS)

    clean = OrderAlertStore(tmp_path / "alerts.sqlite3")
    assert clean.claim_orders([3], now=int(time.time()) + 10_000).order_ids == ()
    assert clean.ambiguous_order_ids([3]) == (3,)
    assert sender.calls == 1


def test_lost_reservation_does_not_send(store) -> None:
    batch = store.claim_orders([4], now=100, lease_seconds=10)
    store.claim_orders([4], now=200, lease_seconds=10)  # otro worker toma la reserva vencida
    assert not store.begin_send(batch.batch_id, "dest", now=201)


def test_corrupt_database_is_not_replaced(tmp_path) -> None:
    path = tmp_path / "alerts.sqlite3"
    path.write_bytes(b"this is not a sqlite database" * 100)
    before = path.read_bytes()
    with pytest.raises(sqlite3.DatabaseError):
        OrderAlertStore(path)
    assert path.read_bytes() == before


def test_run_check_reports_ambiguous_and_stays_blocked(store) -> None:
    orders = pd.DataFrame(rows(11))
    kwargs = dict(
        find_orders=lambda **_: orders,
        load_clients=lambda: ["Cliente sintético"],
        wait=lambda _: None,
    )
    first = runner.run_check(store, SOURCE_CRON, send_email=Sender(error=MailDeliveryAmbiguous("t")), **kwargs)
    assert first.result == "envio_ambiguo" and not first.ok
    assert store.get_status(SOURCE_CRON)["last_check_result"] == "envio_ambiguo"

    sender = Sender()
    second = runner.run_check(store, SOURCE_CRON, send_email=sender, **kwargs)
    assert second.result == "envio_ambiguo" and sender.calls == 0


def test_cli_resolve_ambiguous(store, monkeypatch) -> None:
    dispatch_order_alerts(rows(21), store, Sender(error=MailDeliveryAmbiguous("t")), RECIPIENTS)
    assert runner.resolve_ambiguous_orders(store, "retry", "21", "ruben") == 0
    assert runner.resolve_ambiguous_orders(store, "retry", "21", "ruben") == 1
    with pytest.raises(ValueError):
        runner.resolve_ambiguous_orders(store, "retry", "21", None)


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code

    def close(self):
        pass


@pytest.fixture
def graph(monkeypatch):
    class Config:
        mailbox = "buzon@example.invalid"

    monkeypatch.setattr(graph_mail_downloader, "load_m365_config", lambda: Config())
    monkeypatch.setattr(graph_mail_downloader, "validate_m365_config", lambda config: [])
    monkeypatch.setattr(graph_mail_downloader, "_get_access_token", lambda config: "token")
    monkeypatch.setattr(graph_mail_downloader, "_graph_headers", lambda token: {})

    def install(outcome):
        def post(*args, **kwargs):
            if isinstance(outcome, BaseException):
                raise outcome
            return FakeResponse(outcome)

        monkeypatch.setattr(graph_mail_downloader.requests, "post", post)

    return install


def send():
    graph_mail_downloader.send_alert_email("asunto", "<p>x</p>", RECIPIENTS)


def test_graph_202_is_the_only_acceptance(graph) -> None:
    graph(202)
    send()


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429])
def test_graph_client_errors_and_throttling_are_definitive(graph, status) -> None:
    graph(status)
    with pytest.raises(RuntimeError) as info:
        send()
    assert not isinstance(info.value, MailDeliveryAmbiguous)


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_graph_server_errors_are_ambiguous(graph, status) -> None:
    graph(status)
    with pytest.raises(MailDeliveryAmbiguous):
        send()


@pytest.mark.parametrize("error", [requests.ReadTimeout("slow"), requests.ConnectionError("reset by peer")])
def test_graph_timeout_after_request_is_ambiguous(graph, error) -> None:
    graph(error)
    with pytest.raises(MailDeliveryAmbiguous):
        send()


def test_graph_connect_failures_are_definitive(graph) -> None:
    from urllib3.exceptions import NewConnectionError

    graph(requests.ConnectTimeout("no connect"))
    with pytest.raises(requests.ConnectTimeout):
        send()
    graph(requests.ConnectionError(NewConnectionError(None, "refused")))
    with pytest.raises(requests.ConnectionError):
        send()
