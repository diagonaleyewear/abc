"""Cooperative stop points, scoped to a service cycle; transactions still drain."""
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Event
import time

from filelock import Timeout


class StopRequested(Exception):
    """Cancellation at an explicitly safe boundary, not an interrupted commit."""


_stop: ContextVar[Event | None] = ContextVar("service_stop", default=None)


@contextmanager
def stopping_with(event: Event):
    token = _stop.set(event)
    try:
        yield
    finally:
        _stop.reset(token)


def checkpoint() -> None:
    event = _stop.get()
    if event is not None and event.is_set():
        raise StopRequested("Parada solicitada; trabajo pendiente conservado")


def request_timeout(default: int) -> int:
    return min(default, 5) if _stop.get() is not None else default


def database_options() -> dict:
    return {"connect_timeout": 5, "options": "-c statement_timeout=5000"} if _stop.get() is not None else {}


@contextmanager
def acquire(lock):
    event = _stop.get()
    if event is None:
        with lock:
            yield
        return
    deadline = time.monotonic() + lock.timeout
    while True:
        checkpoint()
        try:
            lock.acquire(timeout=0)
            break
        except Timeout:
            if time.monotonic() >= deadline:
                raise
            event.wait(min(0.1, max(0, deadline - time.monotonic())))
    try:
        yield
    finally:
        lock.release()
