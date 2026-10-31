"""Progressive competitor price scheduler, independent from Streamlit.

Run it from the existing container (nothing is scheduled automatically):

    python price_scheduler.py --status
    python price_scheduler.py --once            # one tick: starts cycles that are due, drains due jobs
    python price_scheduler.py --loop            # long-running tick every --tick-seconds

State lives in the comparator SQLite on the persistent volume: persistent job queue, per-competitor
settings, run log and next run. Each competitor is processed by at most one process (file lock).
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import math
import signal
import sys
import time
from typing import Callable

import pandas as pd
from filelock import FileLock, Timeout

from price_comparator import PriceComparatorStore
from price_connectors import CONNECTORS, Connector, ConnectorBlocked, ConnectorError, Observation, validate_source
from price_web import web_connectors

STATUS_LABELS = {
    "pending_config": "Pendiente de configurar",
    "unverified": "Pendiente de verificar",
    "disabled": "Desactivado",
    "active": "Activo",
    "paused": "En pausa",
    "suspended": "Suspendido",
}
CYCLE_WINDOW_RATIO = 0.8  # queued work is spread over this share of the interval, leaving slack to finish
MAX_BACKOFF_SECONDS = 3600
DEFAULT_TICK_SECONDS = 300


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


class SchedulerConfigError(ValueError):
    """Invalid configuration or an operation not allowed in the competitor's current state."""


@dataclass
class RunSummary:
    competitor_id: str
    status: str = "idle"
    requests: int = 0
    ok: int = 0
    changed: int = 0
    unchanged: int = 0
    not_listed: int = 0
    rejected: int = 0
    errors: int = 0
    message: str = ""
    details: list[str] = field(default_factory=list)


class CompetitorScheduler:
    def __init__(self, store: PriceComparatorStore, connectors: dict[str, Connector] | None = None, *,
                 clock: Callable[[], datetime] = _utc, sleep: Callable[[float], None] = time.sleep) -> None:
        self.store = store
        self.connectors = connectors if connectors is not None else {**CONNECTORS, **web_connectors(store)}
        self._now = clock
        self._sleep = sleep
        self.stop_requested = False  # set by SIGINT/SIGTERM: finish the current request, keep the queue

    def _pause(self, seconds: float) -> None:
        remaining = seconds
        while remaining > 0 and not self.stop_requested:
            step = min(1.0, remaining)
            self._sleep(step)
            remaining -= step

    def _lock(self, competitor_id: str) -> FileLock:
        return FileLock(f"{self.store.database_path}.competitor-{competitor_id}.lock", timeout=0)

    # configuration ----------------------------------------------------------------------------
    def _ensure_rows(self) -> None:
        with self.store._connect() as connection, connection:
            connection.execute("""INSERT OR IGNORE INTO competitor_schedule(competitor_id)
                                  SELECT id FROM competitors""")

    def config(self, competitor_id: str) -> dict:
        self._ensure_rows()
        with self.store._connect() as connection:
            row = connection.execute("SELECT * FROM competitor_schedule WHERE competitor_id=?",
                                     (competitor_id,)).fetchone()
        if row is None:
            raise SchedulerConfigError("Competidor no registrado")
        return dict(row)

    @staticmethod
    def state(config: dict) -> str:
        if not config["connector"]:
            return "pending_config"
        if not config["verified_at"]:
            return "unverified"
        if config["suspended_at"]:
            return "suspended"
        if not config["enabled"]:
            return "disabled"
        return "paused" if config["paused"] else "active"

    def configure(self, competitor_id: str, *, connector: str, source: str = "", authorization_note: str = "",
                  interval_hours: float = 24, max_requests_per_run: int = 20, pause_seconds: float = 2,
                  max_attempts: int = 3) -> None:
        current = self.config(competitor_id)
        if connector:
            if connector not in self.connectors:
                raise SchedulerConfigError(f"Conector no disponible: {connector}")
            try:
                validate_source(source)
            except ConnectorError as exc:
                raise SchedulerConfigError(str(exc)) from exc
        if not 1 <= interval_hours <= 24 * 30:
            raise SchedulerConfigError("La frecuencia debe estar entre 1 hora y 30 días")
        if not 1 <= max_requests_per_run <= 1000:
            raise SchedulerConfigError("El límite de peticiones por ejecución debe estar entre 1 y 1000")
        if not 0 <= pause_seconds <= 3600:
            raise SchedulerConfigError("La pausa entre peticiones debe estar entre 0 y 3600 segundos")
        if not 1 <= max_attempts <= 10:
            raise SchedulerConfigError("Los intentos deben estar entre 1 y 10")
        access_changed = (connector, source.strip()) != (current["connector"], current["source"])
        with self.store._connect() as connection, connection:
            connection.execute("""
                UPDATE competitor_schedule SET connector=?,source=?,authorization_note=?,interval_hours=?,
                    max_requests_per_run=?,pause_seconds=?,max_attempts=?,
                    verified_at=CASE WHEN ? THEN NULL ELSE verified_at END,
                    enabled=CASE WHEN ? THEN 0 ELSE enabled END
                WHERE competitor_id=?
            """, (connector, source.strip(), authorization_note.strip(), interval_hours, max_requests_per_run,
                  pause_seconds, max_attempts, int(access_changed), int(access_changed), competitor_id))

    def verify(self, competitor_id: str) -> None:
        """Prove the authorised access works. Automatic queries stay impossible until this succeeds."""
        from price_pilot import pilot_scope

        if pilot_scope(self.store) is not None:
            raise SchedulerConfigError("Verifique el acceso antes de guardar el piloto; no se descargan feeds generales")
        config = self.config(competitor_id)
        if not config["connector"]:
            raise SchedulerConfigError("Pendiente de configurar: no hay conector ni fuente autorizada")
        if not config["authorization_note"]:
            raise SchedulerConfigError("Indique quién autoriza el acceso (nota de autorización)")
        self.connectors[config["connector"]].verify(config["source"])
        with self.store._connect() as connection, connection:
            connection.execute("UPDATE competitor_schedule SET verified_at=? WHERE competitor_id=?",
                               (_iso(self._now()), competitor_id))

    def enable(self, competitor_id: str) -> None:
        config = self.config(competitor_id)
        if self.state(config) in {"pending_config", "unverified"}:
            raise SchedulerConfigError("Solo se activa un conector con acceso autorizado y verificado")
        if config["suspended_at"]:
            raise SchedulerConfigError("El conector está suspendido; reanúdelo tras revisar la incidencia")
        with self.store._connect() as connection, connection:
            connection.execute("""UPDATE competitor_schedule SET enabled=1,paused=0,
                                  next_run_at=COALESCE(next_run_at,?) WHERE competitor_id=?""",
                               (_iso(self._now()), competitor_id))

    def disable(self, competitor_id: str) -> None:
        self.config(competitor_id)
        with self.store._connect() as connection, connection:
            connection.execute("UPDATE competitor_schedule SET enabled=0 WHERE competitor_id=?", (competitor_id,))

    def pause(self, competitor_id: str) -> None:
        self.config(competitor_id)
        with self.store._connect() as connection, connection:
            connection.execute("UPDATE competitor_schedule SET paused=1 WHERE competitor_id=?", (competitor_id,))

    def resume(self, competitor_id: str) -> None:
        """Clear a manual pause and any suspension; queued jobs continue where they were."""
        self.config(competitor_id)
        with self.store._connect() as connection, connection:
            connection.execute("""UPDATE competitor_schedule SET paused=0,suspended_at=NULL,suspended_reason=''
                                  WHERE competitor_id=?""", (competitor_id,))

    # planning ---------------------------------------------------------------------------------
    def plan(self, competitor_id: str) -> dict:
        from price_pilot import PricePilot, pilot_scope

        if pilot_scope(self.store) is not None:
            plan = PricePilot(self.store).plan()
            rows = plan[plan["competitor_id"] == competitor_id]
            if rows.empty:
                self.config(competitor_id)  # Validate the destination without planning any jobs.
                return {"products": 0, "batches": 0, "spacing_seconds": 0, "min_duration_hours": 0,
                        "fits_interval": True, "manual_only": True, "excluded": True}
            return {"products": len(rows), "batches": len(rows), "spacing_seconds": rows.iloc[0]["pause_seconds"],
                    "min_duration_hours": (len(rows) - 1) * rows.iloc[0]["pause_seconds"] / 3600,
                    "fits_interval": True, "manual_only": True}
        config = self.config(competitor_id)
        with self.store._connect() as connection:
            products = connection.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        batch = self.connectors[config["connector"]].batch_size if config["connector"] in self.connectors else 1
        batches = math.ceil(products / batch) if products else 0
        window = config["interval_hours"] * 3600 * CYCLE_WINDOW_RATIO
        spacing = max(config["pause_seconds"], window / batches) if batches > 1 else 0.0
        minimum = max(batches - 1, 0) * config["pause_seconds"]
        return {"products": products, "batches": batches, "spacing_seconds": spacing,
                "min_duration_hours": minimum / 3600, "fits_interval": minimum <= window}

    def _start_cycle(self, connection, config: dict, plan: dict, now: datetime) -> int:
        from price_pilot import pilot_scope

        if pilot_scope(self.store) is not None:
            raise SchedulerConfigError("La cola general está bloqueada por el piloto")
        eans = [row[0] for row in connection.execute("SELECT ean FROM products ORDER BY ean")]
        batch = self.connectors[config["connector"]].batch_size
        stamp = _iso(now)
        inserted = 0
        for index, ean in enumerate(eans):
            not_before = now + timedelta(seconds=(index // batch) * plan["spacing_seconds"])
            inserted += connection.execute("""
                INSERT OR IGNORE INTO scheduler_jobs(competitor_id,ean,cycle_started_at,not_before,created_at)
                VALUES(?,?,?,?,?)""", (config["competitor_id"], ean, stamp, _iso(not_before), stamp)).rowcount
        connection.execute("""UPDATE competitor_schedule SET cycle_started_at=?,next_run_at=? WHERE competitor_id=?""",
                           (stamp, _iso(now + timedelta(hours=config["interval_hours"])), config["competitor_id"]))
        return inserted

    # execution --------------------------------------------------------------------------------
    def run_once(self, *, competitor_ids: list[str] | None = None, workers: int = 1, force: bool = False,
                 trigger: str = "scheduler") -> list[RunSummary]:
        from price_pilot import PilotError, PricePilot, pilot_scope

        scope = pilot_scope(self.store)
        if scope is not None:
            if competitor_ids is not None and set(competitor_ids) != set(scope["competitors"]):
                raise SchedulerConfigError("La ejecución manual debe usar los seis competidores del piloto")
            if workers != 1:
                raise SchedulerConfigError("Concurrencia del piloto: exactamente 1")
            if trigger not in {"manual", "pilot_manual", "cli"}:
                return []
            try:
                pilot = PricePilot(self.store)
                with self.store._connect() as connection:
                    unfinished = connection.execute("SELECT 1 FROM pilot_runs WHERE status IN ('running','interrupted')").fetchone()
                run_id = pilot.run(resume=bool(unfinished))
                _, metrics = pilot.results(run_id)
                return [RunSummary("pilot", status="ok", message=str(metrics))]
            except PilotError as exc:
                raise SchedulerConfigError(str(exc)) from exc
        self._ensure_rows()
        with self.store._connect() as connection:
            rows = connection.execute("""
                SELECT competitor_id FROM competitor_schedule
                WHERE enabled=1 AND paused=0 AND suspended_at IS NULL AND connector<>'' AND verified_at IS NOT NULL
                ORDER BY competitor_id""").fetchall()
        targets = [row[0] for row in rows if competitor_ids is None or row[0] in competitor_ids]
        self.purge()
        if not targets:
            return []
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            results = list(pool.map(lambda cid: self._run_competitor(cid, force, trigger), targets))
        return [item for item in results if item.status != "idle"]

    def _run_competitor(self, competitor_id: str, force: bool, trigger: str) -> RunSummary:
        summary = RunSummary(competitor_id)
        lock = self._lock(competitor_id)
        try:
            lock.acquire()
        except Timeout:
            summary.status, summary.message = "busy", "Ya hay otra ejecución de este competidor"
            return summary
        try:
            return self._process(competitor_id, force, trigger, summary)
        finally:
            lock.release()

    def _process(self, competitor_id: str, force: bool, trigger: str, summary: RunSummary) -> RunSummary:
        from price_pilot import pilot_scope

        if pilot_scope(self.store) is not None:
            raise SchedulerConfigError("La cola general no puede ejecutarse mientras exista el alcance del piloto")
        now = self._now()
        config = self.config(competitor_id)
        connector = self.connectors[config["connector"]]
        plan = self.plan(competitor_id)
        with self.store._connect() as connection, connection:
            # Holding the lock means jobs still marked running belong to a process that died.
            connection.execute("""UPDATE scheduler_jobs SET status='pending',lease_until=NULL
                                  WHERE competitor_id=? AND status='running'""", (competitor_id,))
            connection.execute("""UPDATE scheduler_runs SET status='error',finished_at=?,
                message='Interrumpida antes de terminar' WHERE competitor_id=? AND status='running'""",
                               (_iso(now), competitor_id))
            open_jobs = connection.execute("""SELECT COUNT(*) FROM scheduler_jobs WHERE competitor_id=?
                AND status='pending'""", (competitor_id,)).fetchone()[0]
            due = config["next_run_at"] is None or config["next_run_at"] <= _iso(now)
            if open_jobs == 0 and (due or force):
                self._start_cycle(connection, config, plan, now)
            pending_now = connection.execute(
                "SELECT COUNT(*) FROM scheduler_jobs WHERE competitor_id=? AND status='pending' AND not_before<=?",
                (competitor_id, _iso(now))).fetchone()[0]
            if pending_now == 0:
                return summary
            run_id = connection.execute("""INSERT INTO scheduler_runs(competitor_id,trigger,started_at,status)
                VALUES(?,?,?,'running')""", (competitor_id, trigger, _iso(now))).lastrowid
        summary.status = "ok"
        try:
            self._drain(config, connector, summary)
        except Exception as exc:  # noqa: BLE001 - always close the run; details are kept out of the log
            summary.status, summary.message = "error", f"Error interno: {type(exc).__name__}"
        self._close_run(run_id, config, summary)
        return summary

    def _drain(self, config: dict, connector: Connector, summary: RunSummary) -> None:
        from price_pilot import pilot_scope

        competitor_id = config["competitor_id"]
        for request in range(config["max_requests_per_run"]):
            if pilot_scope(self.store) is not None:
                raise SchedulerConfigError("El alcance del piloto bloquea la cola general")
            if self.stop_requested:
                summary.message = "Detenida por señal; la cola continúa en la próxima pasada"
                return
            now = self._now()
            with self.store._connect() as connection, connection:
                jobs = connection.execute("""
                    SELECT id,ean,attempts FROM scheduler_jobs WHERE competitor_id=? AND status='pending'
                    AND not_before<=? ORDER BY not_before,id LIMIT ?""",
                                          (competitor_id, _iso(now), connector.batch_size)).fetchall()
                if not jobs:
                    return
                marks = ",".join("?" * len(jobs))
                connection.execute(f"""UPDATE scheduler_jobs SET status='running',attempts=attempts+1,lease_until=?
                    WHERE id IN ({marks})""", (_iso(now + timedelta(minutes=10)), *[job["id"] for job in jobs]))
            summary.requests += 1
            eans = [job["ean"] for job in jobs]
            try:
                observations = {item.ean: item for item in connector.fetch(config["source"], eans)}
            except ConnectorBlocked as exc:
                self._release(jobs, backoff=False)
                self._suspend(competitor_id, str(exc))
                summary.status, summary.message = "blocked", f"Conector suspendido: {exc}"
                return
            except ConnectorError as exc:
                self._fail(jobs, config["max_attempts"], str(exc), summary)
                summary.status, summary.message = "error", str(exc)
                return
            self._apply(config, connector, jobs, observations, summary)
            if request + 1 < config["max_requests_per_run"]:
                self._pause(config["pause_seconds"])

    def _apply(self, config: dict, connector: Connector, jobs, observations: dict[str, Observation],
               summary: RunSummary) -> None:
        competitor = self._competitor(config["competitor_id"])
        for job in jobs:
            observation = observations.get(job["ean"])
            try:
                outcome, detail = ("not_listed", "") if observation is None else self._record(
                    competitor, connector, observation)
            except Exception as exc:  # noqa: BLE001
                outcome, detail = "error", f"Error interno: {type(exc).__name__}"
            if outcome == "error":
                self._fail([job], config["max_attempts"], detail, summary)
                continue
            if outcome == "rejected":
                summary.rejected += 1
                summary.details.append(f"{job['ean']}: {detail}")
            elif outcome == "not_listed":
                summary.not_listed += 1
            else:
                summary.ok += 1
                summary.changed += outcome in {"new", "changed"}
                summary.unchanged += outcome == "unchanged"
            with self.store._connect() as connection, connection:
                connection.execute("""UPDATE scheduler_jobs SET status='done',outcome=?,detail=?,lease_until=NULL,
                    finished_at=? WHERE id=?""", (outcome, detail[:300], _iso(self._now()), job["id"]))
        if summary.rejected or summary.errors:
            summary.status = "partial"

    def _competitor(self, competitor_id: str) -> dict:
        with self.store._connect() as connection:
            return dict(connection.execute("SELECT * FROM competitors WHERE id=?", (competitor_id,)).fetchone())

    def _record(self, competitor: dict, connector: Connector, item: Observation) -> tuple[str, str]:
        market = item.market or competitor["market"]
        stamp = _iso(self._now())
        latest = self._latest_offer(item.ean, competitor["id"], market)
        frame = pd.DataFrame([{
            "EAN": item.ean, "Competidor": competitor["name"], "Precio": item.price,
            "Precio promocional": item.promotional_price, "Precio anterior": item.previous_price,
            "Coste envío": item.shipping_cost, "Moneda": item.currency, "Tipo cambio EUR": "",
            "Mercado": market, "URL": item.url, "Disponibilidad": item.availability,
            "Estado coincidencia": item.match_status, "Método": item.method or f"feed:{connector.name}",
            "Evidencia": item.evidence or "Feed autorizado, identificación por EAN", "Fecha consulta": stamp,
            "Marca": item.brand, "Color": item.color, "Calibre": item.caliber, "EAN publicado": item.ean_published,
            "Título": item.title,
            "Identificadores": list(item.identifiers), "Ambigua": item.ambiguous,
            "IVA incluido": "" if item.tax_included is None else ("sí" if item.tax_included else "no"),
        }])
        report = self.store.import_offers(frame)
        if report.rejected:
            return "rejected", report.rejected[0]
        saved = self._latest_offer(item.ean, competitor["id"], market)
        if not report.updated:
            self._touch_check(item.ean, competitor["id"], market, saved["id"], stamp)
        with self.store._connect() as connection, connection:
            connection.execute("UPDATE competitors SET connectivity=? WHERE id=?",
                               (f"connector:{connector.name}", competitor["id"]))
        return ("unchanged" if report.updated else "new" if latest is None else "changed"), ""

    def _latest_offer(self, ean: str, competitor_id: str, market: str):
        with self.store._connect() as connection:
            return connection.execute("""SELECT * FROM offers WHERE ean=? AND competitor_id=? AND market=?
                ORDER BY captured_at DESC,id DESC LIMIT 1""", (ean, competitor_id, market)).fetchone()

    def _touch_check(self, ean: str, competitor_id: str, market: str, offer_id: int, stamp: str) -> None:
        with self.store._connect() as connection, connection:
            connection.execute("""
                INSERT INTO offer_checks(ean,competitor_id,market,last_offer_id,last_ok_at,checks)
                VALUES(?,?,?,?,?,1)
                ON CONFLICT(ean,competitor_id,market) DO UPDATE SET last_offer_id=excluded.last_offer_id,
                    last_ok_at=excluded.last_ok_at,checks=checks+1""",
                               (ean, competitor_id, market, offer_id, stamp))
            connection.execute("UPDATE competitors SET last_checked_at=? WHERE id=?", (stamp, competitor_id))

    def _release(self, jobs, backoff: bool) -> None:
        with self.store._connect() as connection, connection:
            for job in jobs:
                connection.execute("""UPDATE scheduler_jobs SET status='pending',lease_until=NULL,attempts=attempts-1
                    WHERE id=?""", (job["id"],))

    def _fail(self, jobs, max_attempts: int, detail: str, summary: RunSummary) -> None:
        now = self._now()
        with self.store._connect() as connection, connection:
            for job in jobs:
                attempts = connection.execute("SELECT attempts FROM scheduler_jobs WHERE id=?",
                                              (job["id"],)).fetchone()[0]
                summary.errors += 1
                if attempts >= max_attempts:
                    connection.execute("""UPDATE scheduler_jobs SET status='error',detail=?,lease_until=NULL,
                        finished_at=? WHERE id=?""", (detail[:300], _iso(now), job["id"]))
                else:
                    delay = min(60 * 2 ** attempts, MAX_BACKOFF_SECONDS)
                    connection.execute("""UPDATE scheduler_jobs SET status='pending',detail=?,lease_until=NULL,
                        not_before=? WHERE id=?""", (detail[:300], _iso(now + timedelta(seconds=delay)), job["id"]))

    def _suspend(self, competitor_id: str, reason: str) -> None:
        with self.store._connect() as connection, connection:
            connection.execute("""UPDATE competitor_schedule SET suspended_at=?,suspended_reason=?
                WHERE competitor_id=?""", (_iso(self._now()), reason[:300], competitor_id))

    def _close_run(self, run_id: int, config: dict, summary: RunSummary) -> None:
        now = _iso(self._now())
        with self.store._connect() as connection, connection:
            pending = connection.execute("""SELECT COUNT(*) FROM scheduler_jobs WHERE competitor_id=?
                AND status='pending'""", (summary.competitor_id,)).fetchone()[0]
            if summary.status == "ok" and summary.errors:
                summary.status = "partial"
            connection.execute("""UPDATE scheduler_runs SET finished_at=?,status=?,requests=?,ok=?,changed=?,
                unchanged=?,not_listed=?,rejected=?,errors=?,message=? WHERE id=?""",
                               (now, summary.status, summary.requests, summary.ok, summary.changed,
                                summary.unchanged, summary.not_listed, summary.rejected, summary.errors,
                                (summary.message or "; ".join(summary.details[:3]))[:300], run_id))
            connection.execute("UPDATE competitor_schedule SET last_run_at=? WHERE competitor_id=?",
                               (now, summary.competitor_id))
            if pending == 0 and summary.status in {"ok", "partial"}:
                connection.execute("UPDATE competitor_schedule SET last_ok_at=? WHERE competitor_id=?",
                                   (now, summary.competitor_id))

    # retention and reporting ------------------------------------------------------------------
    def purge(self) -> dict[str, int]:
        """Drop history older than the configured retention. The newest known price per key is kept."""
        months = int(float(self.store.get_setting("retention_months")))
        cutoff = _iso(self._now() - timedelta(days=30.44 * months))
        with self.store._connect() as connection, connection:
            offers = connection.execute("""
                DELETE FROM offers WHERE captured_at<? AND id NOT IN
                    (SELECT last_offer_id FROM offer_checks WHERE last_offer_id IS NOT NULL)
                AND EXISTS (SELECT 1 FROM offers n WHERE n.ean=offers.ean AND n.competitor_id=offers.competitor_id
                    AND n.market=offers.market AND (n.captured_at>offers.captured_at
                        OR (n.captured_at=offers.captured_at AND n.id>offers.id)))""", (cutoff,)).rowcount
            searches = connection.execute("""
                DELETE FROM search_results WHERE captured_at<? AND EXISTS (SELECT 1 FROM search_results n
                    WHERE n.ean=search_results.ean AND n.competitor_id=search_results.competitor_id
                    AND (n.captured_at>search_results.captured_at
                        OR (n.captured_at=search_results.captured_at AND n.id>search_results.id)))""",
                                          (cutoff,)).rowcount
            jobs = connection.execute("""DELETE FROM scheduler_jobs WHERE status IN ('done','error')
                AND created_at<?""", (cutoff,)).rowcount
            runs = connection.execute("DELETE FROM scheduler_runs WHERE started_at<? AND status<>'running'",
                                      (cutoff,)).rowcount
        return {"offers": offers, "search_results": searches, "jobs": jobs, "runs": runs}

    def overview(self) -> pd.DataFrame:
        self._ensure_rows()
        with self.store._connect() as connection:
            frame = pd.read_sql_query("""
                SELECT c.id AS competitor_id,c.name,c.domain,c.market,c.currency,s.connector,s.source,
                    s.authorization_note,s.verified_at,s.enabled,s.paused,s.suspended_at,s.suspended_reason,
                    s.interval_hours,s.max_requests_per_run,s.pause_seconds,s.max_attempts,s.next_run_at,
                    s.last_run_at,s.last_ok_at,
                    (SELECT COUNT(*) FROM scheduler_jobs j WHERE j.competitor_id=c.id AND j.status='pending') AS queued
                FROM competitors c JOIN competitor_schedule s ON s.competitor_id=c.id ORDER BY c.name
            """, connection)
        frame["state"] = [self.state(row) for row in frame.to_dict("records")]
        frame["state_label"] = frame["state"].map(STATUS_LABELS)
        return frame

    def recent_runs(self, limit: int = 30) -> pd.DataFrame:
        with self.store._connect() as connection:
            return pd.read_sql_query("""
                SELECT r.*,c.name AS competitor_name FROM scheduler_runs r
                JOIN competitors c ON c.id=r.competitor_id ORDER BY r.started_at DESC,r.id DESC LIMIT ?
            """, connection, params=(limit,))

    def is_running(self) -> bool:
        for competitor_id in self.overview()["competitor_id"]:
            lock = self._lock(competitor_id)
            try:
                lock.acquire()
            except Timeout:
                return True
            lock.release()
        return False


def _dash(value: object) -> str:
    return value if isinstance(value, str) and value else "—"


def _print_status(scheduler: CompetitorScheduler) -> None:
    last = scheduler.recent_runs(200).drop_duplicates("competitor_id").set_index("competitor_id").to_dict("index")
    for row in scheduler.overview().to_dict("records"):
        extra = f" ({row['suspended_reason']})" if row["suspended_at"] else ""
        run = last.get(row["competitor_id"])
        last_run = f" · último intento {run['status']} {run['started_at']}" if run else ""
        print(f"{row['competitor_id']}: {row['state_label']}{extra} · cola {row['queued']} · "
              f"cada {row['interval_hours']:g} h · próxima {_dash(row['next_run_at'])} · "
              f"última correcta {_dash(row['last_ok_at'])}{last_run}")
    print("En ejecución ahora: " + ("sí" if scheduler.is_running() else "no"))


def _print_runs(scheduler: CompetitorScheduler, limit: int) -> None:
    for run in scheduler.recent_runs(limit).to_dict("records"):
        print(f"{run['started_at']} {run['competitor_id']} [{run['trigger']}] {run['status']} · "
              f"peticiones {run['requests']} · cambios {run['changed']} · sin cambio {run['unchanged']} · "
              f"errores {run['errors']}{' · ' + run['message'] if run['message'] else ''}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Planificador progresivo de precios de competidores.")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--status", action="store_true", help="Muestra el estado de los competidores")
    action.add_argument("--once", action="store_true", help="Una pasada del planificador")
    action.add_argument("--loop", action="store_true", help="Pasadas periódicas hasta SIGINT/SIGTERM")
    action.add_argument("--verify", metavar="ID", help="Prueba el acceso autorizado de un competidor")
    action.add_argument("--enable", metavar="ID")
    action.add_argument("--disable", metavar="ID")
    action.add_argument("--pause", metavar="ID")
    action.add_argument("--resume", metavar="ID", help="Reanuda y levanta una suspensión")
    action.add_argument("--plan", metavar="ID", help="Reparto previsto de la cola")
    action.add_argument("--configure", metavar="ID", help="Cambia conector, fuente, frecuencia y límites")
    action.add_argument("--runs", nargs="?", const=20, type=int, metavar="N", help="Últimas N ejecuciones")
    config = parser.add_argument_group("opciones de --configure (las no indicadas conservan su valor)")
    config.add_argument("--connector")
    config.add_argument("--source")
    config.add_argument("--authorization-note")
    config.add_argument("--interval-hours", type=float)
    config.add_argument("--max-requests", type=int)
    config.add_argument("--pause-seconds", type=float)
    config.add_argument("--max-attempts", type=int)
    parser.add_argument("--competitor", action="append", help="Limita --once/--loop a estos competidores")
    parser.add_argument("--force", action="store_true", help="Inicia un ciclo aunque no toque todavía")
    parser.add_argument("--workers", type=int, default=1, help="Competidores en paralelo (uno por competidor)")
    parser.add_argument("--tick-seconds", type=int, default=DEFAULT_TICK_SECONDS)
    parser.add_argument("--with-odoo", action="store_true",
                        help="Incluye la sincronización de Odoo si está activada y toca por su frecuencia")
    args = parser.parse_args(argv)
    store = PriceComparatorStore()
    store.seed_defaults()
    scheduler = CompetitorScheduler(store)
    from price_pilot import pilot_scope

    if args.loop and pilot_scope(store) is not None:
        print("Error: el piloto solo admite una ejecución manual; --loop está bloqueado", file=sys.stderr)
        return 1
    try:
        for name in ("verify", "enable", "disable", "pause", "resume"):
            target = getattr(args, name)
            if target:
                getattr(scheduler, name)(target)
                print(f"{name}: {target} correcto")
                return 0
        if args.configure:
            current = scheduler.config(args.configure)
            scheduler.configure(
                args.configure,
                connector=current["connector"] if args.connector is None else args.connector,
                source=current["source"] if args.source is None else args.source,
                authorization_note=(current["authorization_note"] if args.authorization_note is None
                                    else args.authorization_note),
                interval_hours=current["interval_hours"] if args.interval_hours is None else args.interval_hours,
                max_requests_per_run=(current["max_requests_per_run"] if args.max_requests is None
                                      else args.max_requests),
                pause_seconds=current["pause_seconds"] if args.pause_seconds is None else args.pause_seconds,
                max_attempts=current["max_attempts"] if args.max_attempts is None else args.max_attempts)
            print(f"configure: {args.configure} correcto (si cambió el conector o la fuente, vuelva a --verify)")
            return 0
        if args.runs is not None:
            _print_runs(scheduler, args.runs)
            return 0
        if args.plan:
            print(scheduler.plan(args.plan))
            return 0
        if args.status:
            _print_status(scheduler)
            return 0
    except (SchedulerConfigError, ConnectorError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    stop = {"now": False}

    def request_stop(*_: object) -> None:
        stop["now"] = True
        scheduler.stop_requested = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    failed = False
    while True:
        if args.with_odoo:
            from vytria_price_sync import VytriaPriceSync

            VytriaPriceSync(scheduler.store).run_scheduled()
        for summary in scheduler.run_once(competitor_ids=args.competitor, workers=args.workers,
                                          force=args.force, trigger="cli"):
            failed |= summary.status in {"error", "blocked"}
            print(f"{summary.competitor_id}: {summary.status} · peticiones {summary.requests} · "
                  f"cambios {summary.changed} · sin cambio {summary.unchanged} · errores {summary.errors}"
                  f"{' · ' + summary.message if summary.message else ''}")
        if args.once or stop["now"]:
            return 1 if failed else 0
        for _ in range(args.tick_seconds):
            if stop["now"]:
                return 0
            time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
