"""Independent-process alert smoke using synthetic rows and a local email ledger."""
import multiprocessing
import os
from pathlib import Path

from order_alerts import OrderAlertStore, dispatch_order_alerts


def alert_actor(database: str, ledger: str, ready, start, results, fail_send=False):
    ready.put(os.getpid())
    if not start.wait(10):
        raise TimeoutError("Synthetic alert actor start timeout")
    store = OrderAlertStore(database)

    def sender(**_kwargs):
        if fail_send:
            raise RuntimeError("synthetic email failure")
        with Path(ledger).open("a", encoding="utf-8") as stream:
            stream.write("synthetic-order\n")

    outcome = dispatch_order_alerts([{"pedido_id": 7, "pedido": "synthetic-order"}],
                                   store, sender, ["synthetic@example.test"])
    results.put((outcome.sent, outcome.error))


def concurrent_alerts(directory: Path, actors=4, *, fail_send=False):
    context = multiprocessing.get_context("spawn")
    ready, results, start = context.Queue(), context.Queue(), context.Event()
    workers = [context.Process(target=alert_actor, args=(
        str(directory / "alerts.sqlite3"), str(directory / "email-ledger.txt"),
        ready, start, results, fail_send)) for _ in range(actors)]
    try:
        for worker in workers:
            worker.start()
        for _ in workers:
            ready.get(timeout=15)
        start.set()
        for worker in workers:
            worker.join(20)
            if worker.is_alive() or worker.exitcode != 0:
                raise RuntimeError(f"Synthetic alert actor failed: pid={worker.pid}, exit={worker.exitcode}")
        return [results.get(timeout=2) for _ in workers]
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(5)
        ready.close()
        results.close()
