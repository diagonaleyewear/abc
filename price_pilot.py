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
        raise PilotError("Use la prueba manual del piloto; las consultas generales están bloqueadas")


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

    def run(self, *, resume: bool = False, search=None) -> int:
        from price_web import WebPriceSearch

        try:
            with self.lock():
                scope = pilot_scope(self.store)
                if scope is None or not scope["enabled"]:
                    raise PilotError("Piloto no preparado para ejecución manual")
                eligible = self.candidates()
                if not set(scope["eans"]).issubset(set(eligible.loc[eligible["eligible"], "ean"])):
                    raise PilotError("Uno o más productos ya no tienen datos/precio Vytria válidos")
                with self.store._connect() as connection, connection:
                    last = connection.execute("""SELECT * FROM pilot_runs
                        WHERE status IN ('running','interrupted') ORDER BY id DESC LIMIT 1""").fetchone()
                    if last and not resume:
                        raise PilotError("Existe una cola interrumpida; reanúdela o desactive y guarde el piloto")
                    if resume and last is None:
                        raise PilotError("No hay una ejecución pendiente para reanudar")
                    if last:
                        if last["scope"] != json.dumps(scope["eans"]) or last["competitors"] != json.dumps(scope["competitors"]):
                            raise PilotError("El alcance cambió; la cola anterior no se puede reanudar")
                        run_id = last["id"]
                        connection.execute("UPDATE pilot_jobs SET status='pending' WHERE run_id=? AND status='running'",
                                           (run_id,))
                        connection.execute("UPDATE pilot_runs SET status='running' WHERE id=?", (run_id,))
                    else:
                        run_id = connection.execute("""INSERT INTO pilot_runs(started_at,status,scope,competitors)
                            VALUES(?,'running',?,?)""",
                                                    (stamp(), json.dumps(scope["eans"]),
                                                     json.dumps(scope["competitors"]))).lastrowid
                        for cid in scope["competitors"]:
                            connection.execute("INSERT INTO pilot_budgets(run_id,competitor_id) VALUES(?,?)", (run_id, cid))
                            for ean in scope["eans"]:
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
                    self._execute(run_id, scope, search or WebPriceSearch(self.store))
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

    def _execute(self, run_id: int, scope: dict, search) -> None:
        from price_web_fetch import WebFetcher

        pilot = self

        class BudgetFetcher(WebFetcher):
            def _request(self, url: str, *, max_bytes: int):
                require_pilot_access(pilot.store, None, cid)
                with pilot.store._connect() as connection:
                    previous = connection.execute("""SELECT last_http_at FROM pilot_budgets
                        WHERE run_id=? AND competitor_id=?""", (run_id, cid)).fetchone()[0]
                if previous:
                    self._sleep(max(0, limits["pause_seconds"] - (time.time() - previous)))
                require_pilot_access(pilot.store, None, cid)
                current = search.config(cid)
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
                    return super()._request(url, max_bytes=max_bytes)
                except ConnectorError:
                    if self.blocked:
                        with pilot.store._connect() as connection, connection:
                            connection.execute("UPDATE pilot_budgets SET blocked=1 WHERE run_id=? AND competitor_id=?",
                                               (run_id, cid))
                    raise

        for cid, limits in scope["competitors"].items():
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
                    if not search._indexed(cid, job["ean"]):
                        self._finish(job, "not_found", "Ficha no localizada")
                        continue
                    with self.store._connect() as connection, connection:
                        connection.execute("UPDATE pilot_jobs SET status='running' WHERE id=?", (job["id"],))
                    outcome = search.locate(job["ean"], cid, fetcher=fetcher)
                    validation: dict = {}
                    reasons = self._record(search, config, outcome, validation=validation)
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
        if not normalized["verified_ean"]:
            reasons.append("EAN no confirmado")
        identity_valid = not reasons
        if page.availability != "available":
            reasons.append("Sin stock" if page.availability == "unavailable" else "Stock desconocido")
        if page.includes_tax is not True:
            reasons.append("IVA sin confirmar" if page.includes_tax is None else "Precio sin IVA")
        market_valid = page.market == "ES" and config["market"] in ("", "ES")
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
            frame["Mercado"] = page.market or "UNKNOWN"
            report = self.store.import_offers(pd.DataFrame([frame]))
            reasons.extend(report.rejected)
        elif not reasons:
            reasons.append("Observación no registrada")
        if validation is not None:
            validation.update(normalized)
            validation.update({"accepted": not reasons and outcome.priced, "observations": match.observations,
                               "reasons": reasons or [*match.reasons, "Precio EUR, IVA incluido, stock y mercado ES verificados"]})
        return reasons

    def _finish(self, job, outcome: str, detail: str, *, observation: dict | None = None,
                comparable: bool = False, priced: bool = False) -> None:
        with self.store._connect() as connection, connection:
            connection.execute("""UPDATE pilot_jobs SET status='done',outcome=?,detail=?,observation=?,
                comparable=?,priced=?,checked_at=? WHERE id=?""",
                               (outcome, detail, json.dumps(observation or {}, ensure_ascii=False),
                                int(comparable), int(priced), stamp(), job["id"]))

    def results(self, run_id: int | None = None) -> tuple[pd.DataFrame, dict]:
        with self.store._connect() as connection:
            if run_id is None:
                row = connection.execute("SELECT MAX(id) FROM pilot_runs").fetchone()
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
