"""Fail-closed, manually supervised price pilot. No network calls on configuration or rendering."""
from __future__ import annotations

from contextlib import ExitStack
from contextvars import ContextVar
from dataclasses import asdict
from datetime import datetime, timezone
import json
import math
import time

import pandas as pd
from filelock import FileLock, Timeout

from price_comparator import PriceComparatorStore, normalize_gtin
from price_connectors import ConnectorError
from price_references import EXACT, classify

MAX_PRODUCTS = 20
MAX_COMPETITORS = 6
COMPETITOR_NAMES = ("Visio Factory", "Óptica Óptima", "Domanci", "Amevista", "Edel-Optics", "ConGafasDeSol")
REQUIRED_EANS = ("8056597875349", "8056597877329", "8056597875226")
SAMPLE_EANS = (*REQUIRED_EANS, "0889652637075", "0843023181451", "0889652543574",
               "0889652494821", "0889652494838", "8056597669016", "8056597669047", "8056262412367",
               "8056262833902", "0805289126577", "0889652429687", "0889652429717", "8056262010730",
               "0889214558619", "0889214558626", "0889214669742", "0889214686374")
_execution: ContextVar[tuple[str, int] | None] = ContextVar("price_pilot_execution", default=None)


class PilotError(ValueError):
    pass


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _limits(settings: dict) -> dict:
    cleaned = {}
    for cid, limits in settings.items():
        if not isinstance(limits, dict):
            raise PilotError("Límites del piloto dañados")
        pause, budget = limits.get("pause_seconds"), limits.get("max_http_requests")
        if isinstance(pause, bool) or not isinstance(pause, (int, float)) or not math.isfinite(pause) \
                or not 1 <= pause <= 3600:
            raise PilotError("Pausa por competidor: entre 1 y 3600 segundos")
        if isinstance(budget, bool) or not isinstance(budget, int) or not 1 <= budget <= 200:
            raise PilotError("Presupuesto HTTP por competidor: entero entre 1 y 200")
        cleaned[cid] = {"pause_seconds": float(pause), "max_http_requests": budget}
    return cleaned


def pilot_scope(store: PriceComparatorStore) -> dict | None:
    with store._connect() as connection:
        row = connection.execute("SELECT * FROM price_pilot WHERE id=1").fetchone()
    if row is None:
        return None
    result = dict(row)
    try:
        result["eans"] = json.loads(result["eans"])
        result["competitors"] = json.loads(result["competitors"])
    except (ValueError, TypeError) as exc:
        raise PilotError("Configuración del piloto dañada; no se permite consultar") from exc
    eans, competitors = result["eans"], result["competitors"]
    if not isinstance(eans, list) or not isinstance(competitors, dict):
        raise PilotError("Alcance del piloto no válido")
    if not all(isinstance(ean, str) for ean in eans):
        raise PilotError("EAN del piloto no válido")
    if len(eans) != MAX_PRODUCTS or len(set(eans)) != MAX_PRODUCTS or len(competitors) != MAX_COMPETITORS:
        raise PilotError("El piloto requiere exactamente 20 EAN y seis competidores")
    try:
        normalized = [normalize_gtin(ean) for ean in eans]
    except ValueError as exc:
        raise PilotError("EAN del piloto no válido") from exc
    if normalized != eans or result["enabled"] not in (0, 1):
        raise PilotError("EAN del piloto no válido")
    result["competitors"] = _limits(competitors)
    with store._connect() as connection:
        allowed = {row["id"] for row in connection.execute("SELECT id,name FROM competitors")
                   if row["name"] in COMPETITOR_NAMES}
    if set(competitors) != allowed or not set(REQUIRED_EANS).issubset(eans):
        raise PilotError("Productos/competidores del piloto no válidos")
    return result


def require_pilot_access(store: PriceComparatorStore, ean: str | None, competitor_id: str) -> None:
    scope = pilot_scope(store)
    if scope is None:
        return
    if not scope["enabled"]:
        raise PilotError("Piloto desactivado: consultas detenidas; el catálogo completo sigue excluido")
    if competitor_id not in scope["competitors"] or (ean is not None and normalize_gtin(ean) not in scope["eans"]):
        raise PilotError("Producto o competidor fuera del piloto")
    context = _execution.get()
    if context is None or context[0] != str(store.database_path):
        raise PilotError("Hay un piloto configurado. Las consultas de productos deben realizarse "
                         "desde la prueba manual supervisada")


class PricePilot:
    def __init__(self, store: PriceComparatorStore):
        self.store = store

    def lock(self):
        return FileLock(f"{self.store.database_path}.pilot.lock", timeout=0)

    def is_running(self) -> bool:
        try:
            with self.lock():
                return False
        except Timeout:
            return True

    def report_error(self, error: Exception) -> None:
        with self.store._connect() as connection, connection:
            connection.execute("UPDATE price_pilot SET last_error=? WHERE id=1", (str(error)[:500],))

    def candidates(self) -> pd.DataFrame:
        summary, _ = self.store.dashboard()
        products = self.store.products()
        if products.empty:
            return products
        prices = summary[["ean", "own_price_eur", "own_price_status"]]
        result = products.merge(prices, on="ean", how="left")
        result["eligible"] = (
            result["brand"].str.strip().ne("") & result["model"].str.strip().ne("")
            & result["color"].str.strip().ne("") & result["own_price_eur"].gt(0)
            & result["own_price_status"].eq("validated")
        )
        return result

    def competitors(self) -> list[dict]:
        with self.store._connect() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM competitors ORDER BY name")
                    if row["name"] in COMPETITOR_NAMES]

    def save(self, eans: list[str], settings: dict[str, dict]) -> None:
        normalized = [normalize_gtin(ean) for ean in eans]
        if len(normalized) != MAX_PRODUCTS or len(set(normalized)) != MAX_PRODUCTS:
            raise PilotError("Seleccione exactamente 20 EAN distintos")
        if not set(REQUIRED_EANS).issubset(normalized):
            raise PilotError("Deben incluirse las tres referencias Prada requeridas")
        allowed = {row["id"] for row in self.competitors()}
        if len(settings) != MAX_COMPETITORS or set(settings) != allowed:
            raise PilotError("Seleccione exclusivamente los seis competidores del piloto")
        eligible = self.candidates()
        valid = set(eligible.loc[eligible["eligible"], "ean"]) if not eligible.empty else set()
        invalid = sorted(set(normalized) - valid)
        if invalid:
            raise PilotError("Sin marca/modelo/color/precio Vytria válido: " + ", ".join(invalid))
        cleaned = _limits(settings)
        try:
            with self.lock(), ExitStack() as locks:
                # A scope change cannot race an old manual/web/scheduler request.
                with self.store._connect() as connection:
                    ids = [row[0] for row in connection.execute("SELECT id FROM competitors ORDER BY id")]
                for cid in ids:
                    locks.enter_context(FileLock(f"{self.store.database_path}.competitor-{cid}.lock", timeout=0))
                with self.store._connect() as connection, connection:
                    connection.execute("""INSERT INTO price_pilot(id,eans,competitors,enabled,updated_at)
                        VALUES(1,?,?,0,?) ON CONFLICT(id) DO UPDATE SET eans=excluded.eans,
                        competitors=excluded.competitors,enabled=0,last_error='',updated_at=excluded.updated_at""",
                                       (json.dumps(normalized), json.dumps(cleaned), stamp()))
                    connection.execute("""UPDATE pilot_runs SET status='cancelled',finished_at=?
                        WHERE status IN ('running','interrupted')""", (stamp(),))
                    connection.execute("""UPDATE pilot_jobs SET status='cancelled'
                        WHERE status IN ('pending','running')""")
                    # Retain legacy queue evidence but never consume it as pilot work.
                    connection.execute("""UPDATE scheduler_jobs SET status='error',lease_until=NULL,
                        outcome='pilot_excluded',detail='Sustituida por la cola acotada del piloto',
                        finished_at=? WHERE status IN ('pending','running')""", (stamp(),))
        except Timeout as exc:
            raise PilotError("Hay una consulta en curso; espere antes de cambiar el alcance") from exc

    def enable(self) -> None:
        try:
            with self.lock():
                if pilot_scope(self.store) is None:
                    raise PilotError("Guarde primero los 20 EAN y seis competidores")
                with self.store._connect() as connection, connection:
                    connection.execute("UPDATE price_pilot SET enabled=1,last_error='' WHERE id=1")
        except Timeout as exc:
            raise PilotError("El piloto está en ejecución") from exc

    def disable(self) -> None:
        # The next HTTP boundary observes this flag, including a currently running pilot.
        with self.store._connect() as connection, connection:
            connection.execute("UPDATE price_pilot SET enabled=0 WHERE id=1")

    def plan(self) -> pd.DataFrame:
        from price_web import WebPriceSearch

        scope = pilot_scope(self.store)
        if scope is None:
            raise PilotError("Piloto sin configurar")
        search = WebPriceSearch(self.store)
        rows = []
        for cid, limits in scope["competitors"].items():
            config = search.config(cid)
            permission = bool(config["terms_reviewed"] and config["terms_note"].strip())
            for ean in scope["eans"]:
                urls = search._indexed(cid, ean)
                state = "Ficha conocida" if urls else "Ficha no localizada"
                if not permission or config["access_status"] != "robots_allowed":
                    state = "Sin acceso autorizado/verificado"
                rows.append({"ean": ean, "competitor_id": cid, "competitor": config["name"],
                             "plan": state, "known_urls": len(urls), **limits})
        return pd.DataFrame(rows)

    def price_refresh_plan(self, ean: str) -> pd.DataFrame:
        """Return confirmed, authorized URL refresh options without performing HTTP."""
        from price_web import WebPriceSearch

        scope = pilot_scope(self.store)
        if scope is None:
            raise PilotError("Piloto sin configurar")
        try:
            normalized = normalize_gtin(ean)
        except ValueError as exc:
            raise PilotError("EAN no válido") from exc
        if normalized != ean or ean not in scope["eans"]:
            raise PilotError("El producto está fuera del alcance del piloto")
        search = WebPriceSearch(self.store)
        names = {row["id"]: row["name"] for row in self.competitors()}
        rows = []
        for cid in scope["competitors"]:
            config = search.config(cid)
            verified = [row for row in search._indexed(cid, ean) if row["status"] == "verified"]
            if not verified:
                status, reason = "No disponible", "No hay una ficha confirmada"
            elif not config["terms_reviewed"] or not config["terms_note"].strip():
                status, reason = "No consultable", "Falta autorización comercial"
            elif config["access_status"] != "robots_allowed":
                status, reason = "No consultable", "Acceso web no autorizado o no verificado"
            else:
                status, reason = "Consultable", ""
            rows.append({"competitor_id": cid, "competitor": names[cid], "status": status,
                         "url": verified[0]["url"] if verified else "", "reason": reason})
        return pd.DataFrame(rows, columns=["competitor_id", "competitor", "status", "url", "reason"])

    @staticmethod
    def _price_selection(selection, scope: dict) -> list[list[str]]:
        if selection is None:
            return [[cid, ean] for cid in scope["competitors"] for ean in scope["eans"]]
        selected = []
        for item in selection:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise PilotError("Cada selección debe contener un competidor y un EAN")
            cid, ean = item
            if not isinstance(cid, str) or not isinstance(ean, str):
                raise PilotError("Competidor/EAN de selección no válido")
            try:
                normalized = normalize_gtin(ean)
            except ValueError as exc:
                raise PilotError("EAN de selección no válido") from exc
            if normalized != ean or cid not in scope["competitors"] or ean not in scope["eans"]:
                raise PilotError("La selección contiene un EAN o competidor fuera del piloto")
            selected.append([cid, ean])
        if not selected:
            raise PilotError("Seleccione al menos una combinación EAN × competidor")
        if len(set(map(tuple, selected))) != len(selected):
            raise PilotError("La selección contiene combinaciones duplicadas")
        ean_order = {ean: index for index, ean in enumerate(scope["eans"])}
        competitor_order = {cid: index for index, cid in enumerate(scope["competitors"])}
        return sorted(selected, key=lambda pair: (competitor_order[pair[0]], ean_order[pair[1]]))

    def run(self, *, resume: bool = False, search=None, operation: str = "prices",
            selection=None) -> int:
        from price_web import WebPriceSearch
        from price_discovery import PilotDiscovery

        if operation not in ("prices", "discovery"):
            raise PilotError("Operación del piloto no válida")

        try:
            with self.lock():
                scope = pilot_scope(self.store)
                if scope is None or not scope["enabled"]:
                    raise PilotError("Piloto no preparado para ejecución manual")
                eligible = self.candidates()
                if not set(scope["eans"]).issubset(set(eligible.loc[eligible["eligible"], "ean"])):
                    raise PilotError("Uno o más productos ya no tienen datos/precio Vytria válidos")
                discovery_preview = None
                if operation == "discovery" and not resume:
                    if selection is None:
                        raise PilotError("La localización requiere una selección explícita")
                    discovery_preview = PilotDiscovery(self.store).preview(selection)
                    if not discovery_preview["effective"]:
                        raise PilotError("No hay trabajos pendientes autorizados en la selección")
                price_selection = self._price_selection(selection, scope) if operation == "prices" else None
                with self.store._connect() as connection, connection:
                    last = connection.execute("""SELECT * FROM pilot_runs
                        WHERE status IN ('running','interrupted') ORDER BY id DESC LIMIT 1""").fetchone()
                    if last and not resume:
                        raise PilotError("Existe una cola interrumpida; reanúdela o desactive y guarde el piloto")
                    if resume and last is None:
                        raise PilotError("No hay una ejecución pendiente para reanudar")
                    if last:
                        if last["operation"] != operation:
                            raise PilotError("Reanude primero la operación interrumpida del piloto")
                        if last["scope"] != json.dumps(scope["eans"]) or last["competitors"] != json.dumps(scope["competitors"]):
                            raise PilotError("El alcance cambió; la cola anterior no se puede reanudar")
                        run_id = last["id"]
                        if operation == "discovery":
                            original = json.loads(last["selection_request"] or "[]")
                            saved = json.loads(last["selection"] or "[]")
                            if not saved:
                                saved = [[r["competitor_id"], r["ean"]] for r in connection.execute(
                                    "SELECT DISTINCT competitor_id,ean FROM pilot_jobs WHERE run_id=? ORDER BY id",
                                    (run_id,))]
                                connection.execute("UPDATE pilot_runs SET selection=? WHERE id=?",
                                                   (json.dumps(saved), run_id))
                            if selection is not None:
                                if not original:
                                    raise PilotError("La selección antigua está fijada por sus trabajos; reanude sin cambios")
                                requested = PilotDiscovery(self.store).preview(selection)["requested"]
                                if requested != original:
                                    raise PilotError("La selección original de la localización es inmutable")
                            job_pairs = [[r["competitor_id"], r["ean"]] for r in connection.execute(
                                "SELECT DISTINCT competitor_id,ean FROM pilot_jobs WHERE run_id=? ORDER BY id",
                                (run_id,))]
                            if set(map(tuple, saved)) != set(map(tuple, job_pairs)):
                                raise PilotError("La cola de localización no coincide con su selección persistida")
                            if any(cid not in scope["competitors"] or ean not in scope["eans"]
                                   for cid, ean in saved):
                                raise PilotError("La cola contiene trabajos fuera del alcance persistido")
                        else:
                            saved = json.loads(last["selection"] or "[]")
                            if not saved:
                                saved = [[r["competitor_id"], r["ean"]] for r in connection.execute(
                                    "SELECT DISTINCT competitor_id,ean FROM pilot_jobs WHERE run_id=? ORDER BY id",
                                    (run_id,))]
                                connection.execute("UPDATE pilot_runs SET selection=? WHERE id=?",
                                                   (json.dumps(saved), run_id))
                            if selection is not None and price_selection != saved:
                                raise PilotError("La selección original de la actualización es inmutable")
                            job_pairs = [[r["competitor_id"], r["ean"]] for r in connection.execute(
                                "SELECT DISTINCT competitor_id,ean FROM pilot_jobs WHERE run_id=? ORDER BY id",
                                (run_id,))]
                            in_scope_pairs = {(cid, ean) for cid, ean in map(tuple, job_pairs)
                                              if cid in scope["competitors"] and ean in scope["eans"]}
                            if set(map(tuple, saved)) != in_scope_pairs:
                                raise PilotError("La cola de actualización no coincide con su selección persistida")
                            if any(cid not in scope["competitors"] or ean not in scope["eans"]
                                   for cid, ean in saved):
                                raise PilotError("La cola contiene trabajos fuera del alcance persistido")
                        connection.execute("UPDATE pilot_jobs SET status='pending' WHERE run_id=? AND status='running'",
                                           (run_id,))
                        connection.execute("UPDATE pilot_runs SET status='running' WHERE id=?", (run_id,))
                    else:
                        if operation == "discovery":
                            effective = discovery_preview["effective"]
                            original = discovery_preview["requested"]
                            selected_competitors = list(dict.fromkeys(cid for cid, _ in effective))
                            run_id = connection.execute("""INSERT INTO pilot_runs(
                                started_at,status,scope,competitors,operation,selection,selection_request)
                                VALUES(?,'running',?,?,?,?,?)""",
                                (stamp(), json.dumps(scope["eans"]), json.dumps(scope["competitors"]),
                                 operation, json.dumps(effective), json.dumps(original))).lastrowid
                            for cid in selected_competitors:
                                connection.execute("INSERT INTO pilot_budgets(run_id,competitor_id) VALUES(?,?)",
                                                   (run_id, cid))
                            for cid, ean in effective:
                                connection.execute("INSERT INTO pilot_jobs(run_id,competitor_id,ean) VALUES(?,?,?)",
                                                   (run_id, cid, ean))
                        else:
                            selected_competitors = list(dict.fromkeys(cid for cid, _ in price_selection))
                            run_id = connection.execute("""INSERT INTO pilot_runs(
                                started_at,status,scope,competitors,operation,selection,selection_request)
                                VALUES(?,'running',?,?,?,?,?)""",
                                (stamp(), json.dumps(scope["eans"]), json.dumps(scope["competitors"]), operation,
                                 json.dumps(price_selection), json.dumps(price_selection))).lastrowid
                            for cid in selected_competitors:
                                connection.execute("INSERT INTO pilot_budgets(run_id,competitor_id) VALUES(?,?)",
                                                   (run_id, cid))
                            for cid, ean in price_selection:
                                connection.execute("INSERT INTO pilot_jobs(run_id,competitor_id,ean) VALUES(?,?,?)",
                                                   (run_id, cid, ean))
                    ean_placeholders = ",".join("?" for _ in scope["eans"])
                    cid_placeholders = ",".join("?" for _ in scope["competitors"])
                    connection.execute(f"""UPDATE pilot_jobs SET status='cancelled',outcome='pilot_excluded',
                        detail='Trabajo fuera del alcance persistido' WHERE run_id=? AND
                        (ean NOT IN ({ean_placeholders}) OR competitor_id NOT IN ({cid_placeholders}))""",
                                       (run_id, *scope["eans"], *scope["competitors"]))
                token = _execution.set((str(self.store.database_path), run_id))
                try:
                    verified_only = False
                    if operation == "prices":
                        with self.store._connect() as connection:
                            saved = connection.execute(
                                "SELECT selection FROM pilot_runs WHERE id=?", (run_id,)).fetchone()
                        saved_pairs = set(map(tuple, json.loads(saved["selection"] or "[]")))
                        full_scope_pairs = {(cid, ean) for cid in scope["competitors"] for ean in scope["eans"]}
                        verified_only = bool(saved_pairs and saved_pairs != full_scope_pairs)
                    self._execute(run_id, scope, search or WebPriceSearch(self.store), operation=operation,
                                  verified_only=verified_only)
                except BaseException as exc:
                    # Persist interruption even on cancellation; never hide or downgrade the exception.
                    with self.store._connect() as connection, connection:
                        connection.execute("UPDATE pilot_runs SET status='interrupted',error=? WHERE id=?",
                                           (str(exc)[:500], run_id))
                    raise
                finally:
                    _execution.reset(token)
                with self.store._connect() as connection, connection:
                    connection.execute("UPDATE pilot_runs SET status='done',finished_at=? WHERE id=?", (stamp(), run_id))
                return run_id
        except Timeout as exc:
            raise PilotError("Ya hay otra ejecución del piloto") from exc

    def _execute(self, run_id: int, scope: dict, search, *, operation: str = "prices",
                 verified_only: bool = False) -> None:
        from price_web_fetch import WebFetcher
        from price_discovery import PilotDiscovery

        pilot = self

        class BudgetFetcher(WebFetcher):
            def _request(self, url: str, *, max_bytes: int):
                require_pilot_access(pilot.store, job["ean"], cid)
                with pilot.store._connect() as connection:
                    previous = connection.execute("""SELECT last_http_at FROM pilot_budgets
                        WHERE run_id=? AND competitor_id=?""", (run_id, cid)).fetchone()[0]
                if previous:
                    self._sleep(max(0, limits["pause_seconds"] - (time.time() - previous)))
                require_pilot_access(pilot.store, job["ean"], cid)
                current = search.config(cid)
                from price_web import validate_competitor_url
                validate_competitor_url(url, current["domain"])
                if not current["terms_reviewed"] or not current["terms_note"].strip() \
                        or current["access_status"] != "robots_allowed":
                    raise PilotError("Autorización o comprobación de acceso retirada durante el piloto")
                # Reserve before I/O: a process death cannot refund an HTTP request.
                with pilot.store._connect() as connection, connection:
                    changed = connection.execute("""UPDATE pilot_budgets SET requests=requests+1,last_http_at=?
                        WHERE run_id=? AND competitor_id=? AND requests<?""",
                                                 (time.time(), run_id, cid, limits["max_http_requests"])).rowcount
                if not changed:
                    raise ConnectorError("Presupuesto HTTP del competidor agotado")
                try:
                    result = super()._request(url, max_bytes=max_bytes)
                except ConnectorError as exc:
                    with pilot.store._connect() as connection, connection:
                        connection.execute("""INSERT INTO pilot_http_log(run_id,competitor_id,ean,url,result,requested_at)
                            VALUES(?,?,?,?,?,?)""", (run_id, cid, job["ean"], url, str(exc)[:500], stamp()))
                    if self.blocked:
                        with pilot.store._connect() as connection, connection:
                            connection.execute("UPDATE pilot_budgets SET blocked=1 WHERE run_id=? AND competitor_id=?",
                                               (run_id, cid))
                    raise
                with pilot.store._connect() as connection, connection:
                    connection.execute("""INSERT INTO pilot_http_log(run_id,competitor_id,ean,url,result,requested_at)
                        VALUES(?,?,?,?,?,?)""", (run_id, cid, job["ean"], url, f"HTTP {result[0]}", stamp()))
                return result

        with self.store._connect() as connection:
            run_competitors = {row[0] for row in connection.execute(
                "SELECT competitor_id FROM pilot_budgets WHERE run_id=?", (run_id,))}
        for cid, limits in scope["competitors"].items():
            if cid not in run_competitors:
                continue
            config = search.config(cid)
            with FileLock(f"{self.store.database_path}.competitor-{cid}.lock", timeout=0):
                with self.store._connect() as connection:
                    budget = connection.execute("SELECT * FROM pilot_budgets WHERE run_id=? AND competitor_id=?",
                                                (run_id, cid)).fetchone()
                fetcher = BudgetFetcher(pause_seconds=limits["pause_seconds"],
                                        max_requests=limits["max_http_requests"], retries=0,
                                        timeout=config["timeout_seconds"])
                # Keep injectable transport for offline tests, with the same production budget guard.
                source = search._factory(config)
                fetcher._transport, fetcher._sleep, fetcher._clock = source._transport, source._sleep, source._clock
                while True:
                    with self.store._connect() as connection:
                        job = connection.execute("""SELECT * FROM pilot_jobs WHERE run_id=? AND competitor_id=?
                            AND status='pending' ORDER BY id LIMIT 1""", (run_id, cid)).fetchone()
                    if job is None:
                        break
                    require_pilot_access(self.store, job["ean"], cid)
                    if budget["blocked"]:
                        self._finish(job, "blocked", "Competidor bloqueado; no se reintenta")
                        continue
                    if not config["terms_reviewed"] or not config["terms_note"].strip() \
                            or config["access_status"] != "robots_allowed":
                        self._finish(job, "not_allowed", "Sin autorización y comprobación de acceso")
                        continue
                    if operation == "prices":
                        indexed = search._indexed(cid, job["ean"])
                        if not indexed:
                            self._finish(job, "not_found", "Ficha no localizada")
                            continue
                        if verified_only and not any(row["status"] == "verified" for row in indexed):
                            self._finish(job, "not_found", "No hay ficha confirmada para actualizar")
                            continue
                    with self.store._connect() as connection, connection:
                        connection.execute("UPDATE pilot_jobs SET status='running' WHERE id=?", (job["id"],))
                    if operation == "discovery":
                        discovery = PilotDiscovery(self.store)
                        state, detail, evidence = discovery.locate(job["ean"], cid, run_id, search, fetcher)
                        self._finish(job, state, detail, observation=evidence)
                        if state == "blocked":
                            with self.store._connect() as connection, connection:
                                connection.execute("UPDATE pilot_budgets SET blocked=1 WHERE run_id=? AND competitor_id=?",
                                                   (run_id, cid))
                                budget = connection.execute("SELECT * FROM pilot_budgets WHERE run_id=? AND competitor_id=?",
                                                            (run_id, cid)).fetchone()
                        continue
                    outcome = search.locate(job["ean"], cid, fetcher=fetcher,
                                            verified_only=verified_only)
                    validation: dict = {}
                    reasons = self._record(search, search.config(cid), outcome, validation=validation)
                    observation = asdict(outcome)
                    observation["validation"] = validation
                    self._finish(job, outcome.state, "; ".join(reasons) or outcome.detail,
                                 observation=observation, comparable=not reasons and outcome.priced,
                                 priced=bool(outcome.page and outcome.page.has_price))
                    if outcome.state == "blocked":
                        with self.store._connect() as connection, connection:
                            connection.execute("UPDATE pilot_budgets SET blocked=1 WHERE run_id=? AND competitor_id=?",
                                               (run_id, cid))
                            budget = connection.execute("SELECT * FROM pilot_budgets WHERE run_id=? AND competitor_id=?",
                                                        (run_id, cid)).fetchone()

    def close_interrupted_discovery(self, *, note: str) -> int:
        if not note.strip():
            raise PilotError("Indique el motivo para cerrar la localización interrumpida")
        try:
            with self.lock():
                with self.store._connect() as connection, connection:
                    run = connection.execute("""SELECT id,operation FROM pilot_runs
                        WHERE status IN ('running','interrupted')
                        ORDER BY id DESC LIMIT 1""").fetchone()
                    if run is None:
                        raise PilotError("No hay una localización interrumpida que cerrar")
                    if run["operation"] != "discovery":
                        raise PilotError("Reanude o cierre primero la operación interrumpida del piloto")
                    run_id = run["id"]
                    detail = f"Cerrado manualmente: {note.strip()[:400]}"
                    connection.execute("""UPDATE pilot_jobs SET status='cancelled',outcome='cancelled',
                        detail=? WHERE run_id=? AND status IN ('pending','running')""", (detail, run_id))
                    connection.execute("""UPDATE pilot_runs SET status='cancelled',finished_at=?,error=?
                        WHERE id=?""", (stamp(), detail, run_id))
                    return run_id
        except Timeout as exc:
            raise PilotError("Ya hay otra ejecución del piloto") from exc

    def _record(self, search, config: dict, outcome, *, validation: dict | None = None) -> list[str]:
        page = outcome.page
        if page is None:
            return [outcome.reason or outcome.state]
        product = search._product(outcome.ean)
        match = classify(product, page.candidate())
        normalized = match.normalized
        reasons = list(match.reasons) if match.status != EXACT else []
        if outcome.state != "exact" or page.ambiguous:
            reasons.append("Coincidencia no exacta")
        identity_valid = not reasons
        if page.availability != "available":
            reasons.append("Sin stock" if page.availability == "unavailable" else "Stock desconocido")
        if page.includes_tax is not True:
            reasons.append("IVA sin confirmar" if page.includes_tax is None else "Precio sin IVA")
        from price_web_extract import resolve_market
        market, market_note, conflict = resolve_market(page, config)
        market_valid = market == "ES" and not conflict
        currency_valid = page.currency == "EUR" and config["currency"] in ("", "EUR")
        if not market_valid:
            reasons.append("Mercado sin confirmar o no comparable")
        if not currency_valid:
            reasons.append("Moneda no comparable")
        if not page.has_price or page.price <= 0:
            reasons.append("Precio final no válido")
        if page.has_price and outcome.state in {"exact", "probable"}:
            frame = search.frame_row(product, config, outcome, stamp())
            listing = normalized["listing"]
            frame["Modelo"] = listing["models"][0] if len(listing["models"]) == 1 else ""
            frame["Color"] = listing["colors"][0] if len(listing["colors"]) == 1 else ""
            # Unknown/incorrect identity must never enter the global comparable set.
            if not identity_valid or not market_valid or not currency_valid or page.price <= 0:
                frame["Estado coincidencia"] = "manual_review"
            report = self.store.import_offers(pd.DataFrame([frame]))
            reasons.extend(report.rejected)
        elif not reasons:
            reasons.append("Observación no registrada")
        if validation is not None:
            validation.update(normalized)
            validation.update({"accepted": not reasons and outcome.priced, "observations": match.observations,
                               "market": market, "market_evidence": market_note, "market_conflict": conflict,
                               "reasons": reasons or [*match.reasons, "Precio EUR, IVA incluido, stock y mercado ES verificados"]})
        return reasons

    def _finish(self, job, outcome: str, detail: str, *, observation: dict | None = None,
                comparable: bool = False, priced: bool = False) -> None:
        with self.store._connect() as connection, connection:
            operation = connection.execute("SELECT operation FROM pilot_runs WHERE id=?", (job["run_id"],)).fetchone()[0]
            if operation == "discovery":
                methods = [attempt["method"] for attempt in (observation or {}).get("attempts", [])]
                connection.execute("""INSERT INTO pilot_discovery(competitor_id,ean,run_id,state,method,detail,updated_at)
                    VALUES(?,?,?,?,?,?,?) ON CONFLICT(competitor_id,ean) DO UPDATE SET run_id=excluded.run_id,
                    state=excluded.state,method=excluded.method,detail=excluded.detail,updated_at=excluded.updated_at""",
                                   (job["competitor_id"], job["ean"], job["run_id"], outcome,
                                    ",".join(methods), detail, stamp()))
            connection.execute("""UPDATE pilot_jobs SET status='done',outcome=?,detail=?,observation=?,
                comparable=?,priced=?,checked_at=? WHERE id=?""",
                               (outcome, detail, json.dumps(observation or {}, ensure_ascii=False),
                                int(comparable), int(priced), stamp(), job["id"]))

    def results(self, run_id: int | None = None) -> tuple[pd.DataFrame, dict]:
        with self.store._connect() as connection:
            if run_id is None:
                row = connection.execute("SELECT MAX(id) FROM pilot_runs WHERE operation='prices'").fetchone()
                run_id = row[0]
            frame = pd.read_sql_query("SELECT * FROM pilot_jobs WHERE run_id=? ORDER BY competitor_id,ean",
                                      connection, params=(run_id,))
            budgets = pd.read_sql_query("SELECT * FROM pilot_budgets WHERE run_id=?", connection, params=(run_id,))
        if frame.empty:
            return frame, {}
        done = frame[frame["status"] == "done"]
        metrics = {"Productos consultados": done.loc[done["observation"] != "{}", "ean"].nunique(),
                   "Competidores consultados": int((budgets["requests"] > 0).sum()),
                   "Peticiones HTTP": int(budgets["requests"].sum()),
                   "Fichas encontradas": int(done["outcome"].isin(["exact", "probable", "rejected"]).sum()),
                   "Precios extraídos": int(done["priced"].sum()),
                   "Ofertas comparables": int(done["comparable"].sum()),
                   "Sin stock": int(done["detail"].str.contains("Sin stock").sum()),
                   "IVA sin confirmar": int(done["detail"].str.contains("IVA sin confirmar").sum()),
                   "Errores y bloqueos": int(done["outcome"].isin(["error", "blocked", "not_allowed"]).sum()),
                   "Última consulta correcta": done.loc[done["priced"] == 1, "checked_at"].max()}
        summary, _ = self.store.dashboard()
        own = dict(zip(summary["ean"], summary["own_price_eur"]))
        frame["Precio Vytria"] = frame["ean"].map(own)
        observations = frame["observation"].map(json.loads)
        frame["Validación normalizada"] = observations.map(
            lambda row: json.dumps(row.get("validation", {}), ensure_ascii=False))
        frame["Motivo de validación"] = observations.map(
            lambda row: "; ".join(row.get("validation", {}).get("reasons", [])))
        frame["Observaciones de calibre"] = observations.map(
            lambda row: "; ".join(row.get("validation", {}).get("observations", [])))
        frame["Precio final"] = observations.map(lambda row: (row.get("page") or {}).get("price"))
        for label, field in {"EAN publicado": "ean_published", "Marca ficha": "brand", "Título ficha": "title",
                             "Color ficha": "color", "Calibre ficha": "caliber", "Moneda": "currency",
                             "IVA incluido": "includes_tax", "Tipo IVA": "vat_rate",
                             "Disponibilidad": "availability", "Mercado ficha": "market", "URL": "url"}.items():
            frame[label] = observations.map(lambda row: (row.get("page") or {}).get(field))
        frame["Diferencia frente a Vytria €"] = frame["Precio final"] - frame["Precio Vytria"]
        frame.loc[frame["comparable"] != 1, "Diferencia frente a Vytria €"] = float("nan")
        frame["Diferencia frente a Vytria %"] = (
            frame["Diferencia frente a Vytria €"] / frame["Precio Vytria"].where(frame["Precio Vytria"] > 0) * 100)
        return frame, metrics
