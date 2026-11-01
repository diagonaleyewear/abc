"""Supervised pilot URL discovery. Never records prices or grants general search permission."""
from __future__ import annotations

from dataclasses import asdict
import json
from collections.abc import Iterable

import pandas as pd
from filelock import Timeout

from price_comparator import PriceComparatorStore, normalize_gtin
from price_connectors import ConnectorBlocked, ConnectorError
from price_pilot import PilotError, PricePilot, _execution, pilot_scope, require_pilot_access, stamp
from price_references import EXACT, classify
from price_web import MAX_CANDIDATES, WebPriceSearch, WebSearchError, validate_competitor_url
from price_web_extract import ProductPage
from price_web_fetch import RobotsDenied, WebFetcher

STATE_TEXT = {
    "known": "Ficha conocida", "pending": "Pendiente de localizar",
    "candidate_found": "Candidato encontrado", "pending_validation": "Pendiente de validar",
    "confirmed": "Ficha confirmada", "not_found": "Producto no encontrado",
    "not_allowed": "Consulta no autorizada", "error": "Error o bloqueo", "blocked": "Error o bloqueo",
}


class PilotDiscovery:
    def __init__(self, store: PriceComparatorStore):
        self.store = store

    def coverage(self) -> pd.DataFrame:
        scope = pilot_scope(self.store)
        if scope is None:
            return pd.DataFrame()
        with self.store._connect() as connection:
            saved = {(r["competitor_id"], r["ean"]): dict(r) for r in connection.execute("SELECT * FROM pilot_discovery")}
            urls: dict[tuple[str, str], list[str]] = {}
            for row in connection.execute("""SELECT * FROM competitor_product_urls
                WHERE status IN ('verified','probable','pending_verification') ORDER BY verified_at DESC"""):
                urls.setdefault((row["competitor_id"], row["ean"]), []).append(row["url"])
            candidates: dict[tuple[str, str], list[dict]] = {}
            for row in connection.execute("SELECT * FROM pilot_url_candidates ORDER BY id"):
                candidates.setdefault((row["competitor_id"], row["ean"]), []).append(dict(row))
            configs = {r["competitor_id"]: dict(r) for r in connection.execute(
                "SELECT * FROM competitor_web_config")}
            names = {r["id"]: r["name"] for r in connection.execute("SELECT id,name FROM competitors")}
        result = []
        for cid in scope["competitors"]:
            for ean in scope["eans"]:
                cell = saved.get((cid, ean), {})
                known = urls.get((cid, ean), [])
                found = candidates.get((cid, ean), [])
                unreviewed = [r for r in found if r["state"] in ("candidate_found", "pending_validation")]
                state = cell.get("state", "pending")
                if known:
                    state = "confirmed" if cell.get("state") == "confirmed" else "known"
                elif unreviewed:
                    state = "candidate_found" if any(r["state"] == "candidate_found" for r in unreviewed) else "pending_validation"
                result.append({"ean": ean, "competitor_id": cid, "competitor": names[cid], "state": state,
                               "Estado": STATE_TEXT[state], "method": cell.get("method") or configs.get(cid, {}).get("search_mode", "auto"),
                               "url": known[0] if known else (unreviewed[0]["url"] if unreviewed else ""),
                               "detail": cell.get("detail", ""), "candidates": len(unreviewed),
                               "updated_at": cell.get("updated_at", "")})
        return pd.DataFrame(result)

    def pending(self) -> set[tuple[str, str]]:
        return {(r["competitor_id"], r["ean"]) for r in self.coverage().to_dict("records") if r["state"] == "pending"}

    @staticmethod
    def _canonical(pairs: Iterable[tuple[str, str]], scope: dict) -> list[list[str]]:
        order_ean = {ean: index for index, ean in enumerate(scope["eans"])}
        order_cid = {cid: index for index, cid in enumerate(scope["competitors"])}
        return [[cid, ean] for cid, ean in sorted(
            set(pairs), key=lambda pair: (order_cid[pair[0]], order_ean[pair[1]]))]

    def preview(self, requested: Iterable[tuple[str, str]]) -> dict:
        """Read-only execution preview; validates scope and never performs HTTP or writes."""
        scope = pilot_scope(self.store)
        if scope is None:
            raise PilotError("Piloto sin configurar")
        requested = list(requested)
        if not requested:
            raise PilotError("Seleccione al menos una combinación EAN × competidor")
        pairs: set[tuple[str, str]] = set()
        for item in requested:
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
            pairs.add((cid, ean))
        if len(pairs) != len(requested):
            raise PilotError("La selección contiene combinaciones duplicadas")

        coverage = self.coverage().set_index(["competitor_id", "ean"])
        pending, known, candidates, other, unauthorized = [], [], [], [], []
        methods: dict[str, dict] = {}
        budgets: dict[str, dict] = {}
        config_ids = tuple(dict.fromkeys(cid for cid, _ in pairs))
        with self.store._connect() as connection:
            config_rows = connection.execute(f"""SELECT w.*,c.name,c.url AS site_url,c.domain,c.market,c.currency
                FROM competitor_web_config w JOIN competitors c ON c.id=w.competitor_id
                WHERE w.competitor_id IN ({','.join('?' for _ in config_ids)})""", config_ids).fetchall()
            configs = {row["competitor_id"]: dict(row) for row in config_rows}
            missing = {cid for cid, _ in pairs} - set(configs)
            if missing:
                raise PilotError("Falta configuración web para: " + ", ".join(sorted(missing)))
            active = connection.execute("""SELECT id,status,operation FROM pilot_runs
                WHERE status IN ('running','interrupted') ORDER BY id DESC LIMIT 1""").fetchone()
            active_budgets = {}
            if active:
                active_budgets = {r["competitor_id"]: dict(r) for r in connection.execute(
                    "SELECT * FROM pilot_budgets WHERE run_id=?", (active["id"],))}
        for cid, ean in self._canonical(pairs, scope):
            cell = coverage.loc[(cid, ean)].to_dict()
            config = configs[cid]
            state = cell["state"]
            if state in ("known", "confirmed"):
                known.append([cid, ean])
                continue
            if state in ("candidate_found", "pending_validation"):
                candidates.append([cid, ean])
                continue
            if state != "pending":
                other.append({"competitor_id": cid, "ean": ean, "state": state})
                continue
            pending.append([cid, ean])
            auth_reasons = []
            if not config["terms_reviewed"] or not config["terms_note"].strip():
                auth_reasons.append("Condiciones comerciales no verificadas")
            if config["access_status"] != "robots_allowed":
                auth_reasons.append("robots.txt no permitido/verificado")
            allowed = set(config["locate_strategy"].split(","))
            mode = config["search_mode"]
            search_methods = []
            if mode == "known":
                auth_reasons.append("Configurado para solo URLs conocidas")
            elif mode == "auto":
                search_methods = []
                if config["search_verified"] and "site_search" in allowed:
                    search_methods.extend([config["search_preferred"], *config["search_alternatives"].split(",")])
                if "sitemap" in allowed and "sitemap" in config["search_alternatives"].split(","):
                    search_methods.append("sitemap")
                search_methods = list(dict.fromkeys(m for m in search_methods if m))
                if not search_methods:
                    auth_reasons.append("Sin método automático verificado y autorizado")
            elif not config["search_verified"] or "site_search" not in allowed:
                search_methods = []
                auth_reasons.append("Búsqueda GET no verificada/autorizada")
            else:
                search_methods = [mode]
            methods[cid] = {"mode": mode, "methods": search_methods}
            used = active_budgets.get(cid, {}).get("requests", 0)
            limit = scope["competitors"][cid]["max_http_requests"]
            budgets[cid] = {"used": used, "limit": limit, "available": max(0, limit - used)}
            if auth_reasons:
                unauthorized.append({"competitor_id": cid, "ean": ean, "reasons": auth_reasons})
        auth_keys = {(r["competitor_id"], r["ean"]) for r in unauthorized}
        effective = [pair for pair in pending if tuple(pair) not in auth_keys]
        if active and active["operation"] == "discovery":
            with self.store._connect() as connection:
                previous = connection.execute("SELECT selection FROM pilot_runs WHERE id=?", (active["id"],)).fetchone()[0]
            saved = json.loads(previous or "[]")
        else:
            saved = []
        return {"requested": self._canonical(pairs, scope), "pending": pending, "effective": effective,
                "known_excluded": known, "candidate_excluded": candidates, "other_excluded": other,
                "unauthorized": unauthorized, "methods": methods, "budgets": budgets,
                "active_run": dict(active) if active else None, "saved_selection": saved}

    def candidates(self) -> pd.DataFrame:
        scope = pilot_scope(self.store)
        with self.store._connect() as connection:
            frame = pd.read_sql_query("SELECT * FROM pilot_url_candidates ORDER BY id DESC", connection)
        if scope is None or frame.empty:
            return frame.iloc[:0]
        return frame[frame.ean.isin(scope["eans"]) & frame.competitor_id.isin(scope["competitors"])]

    def run(self, *, selection: Iterable[tuple[str, str]] | None = None, resume: bool = False,
            search: WebPriceSearch | None = None) -> int:
        return PricePilot(self.store).run(resume=resume, search=search, operation="discovery", selection=selection)

    def latest_run(self) -> dict | None:
        with self.store._connect() as connection:
            row = connection.execute("""SELECT * FROM pilot_runs WHERE operation='discovery'
                ORDER BY id DESC LIMIT 1""").fetchone()
        if row is None:
            return None
        result = dict(row)
        result["selection"] = json.loads(result["selection"])
        result["selection_request"] = json.loads(result["selection_request"])
        with self.store._connect() as connection:
            jobs = connection.execute("""SELECT outcome,status,COUNT(*) AS count FROM pilot_jobs
                WHERE run_id=? GROUP BY outcome,status""", (row["id"],)).fetchall()
            result["jobs"] = {f"{item['outcome'] or 'pending'}:{item['status']}": item["count"] for item in jobs}
            totals = {}
            for item in jobs:
                key = item["outcome"] or "pending"
                totals[key] = totals.get(key, 0) + item["count"]
            result["results"] = {
                "Trabajos ejecutados": sum(item["count"] for item in jobs if item["status"] == "done"),
                "Candidatos encontrados": totals.get("candidate_found", 0) + totals.get("pending_validation", 0),
                "Productos no encontrados": totals.get("not_found", 0),
                "Errores": totals.get("error", 0),
                "Bloqueos": totals.get("blocked", 0),
                "No autorizados": totals.get("not_allowed", 0),
                "Pendientes o en curso": sum(item["count"] for item in jobs
                                               if item["status"] in ("pending", "running")),
            }
            result["budgets"] = [dict(item) for item in connection.execute(
                "SELECT * FROM pilot_budgets WHERE run_id=? ORDER BY competitor_id", (row["id"],))]
            result["budget_used"] = sum(item["requests"] for item in result["budgets"])
            result["candidates_pending_review"] = connection.execute("""SELECT COUNT(*) FROM pilot_url_candidates
                WHERE run_id=? AND state IN ('candidate_found','pending_validation')""", (row["id"],)).fetchone()[0]
        return result

    def _guard(self, ean: str, cid: str, run_id: int) -> None:
        require_pilot_access(self.store, ean, cid)
        context = _execution.get()
        with self.store._connect() as connection:
            run = connection.execute("SELECT * FROM pilot_runs WHERE id=?", (run_id,)).fetchone()
            job = connection.execute("""SELECT id FROM pilot_jobs
                WHERE run_id=? AND ean=? AND competitor_id=? AND status='running'""", (run_id, ean, cid)).fetchone()
        if context != (str(self.store.database_path), run_id) or not run or not job \
                or run["operation"] != "discovery" or run["status"] != "running":
            raise PilotError("La localización requiere la cola supervisada del piloto; no es una búsqueda general")

    def locate(self, ean: str, cid: str, run_id: int, search: WebPriceSearch, fetcher: WebFetcher) -> tuple[str, str, dict]:
        self._guard(ean, cid, run_id)
        # Re-check coverage on resume: a URL or candidate saved before interruption is never queried again.
        cell = self.coverage()
        state = cell.loc[(cell.ean == ean) & (cell.competitor_id == cid), "state"].iloc[0]
        if state in ("known", "confirmed", "candidate_found", "pending_validation"):
            return state, "URL ya registrada; no se repite la consulta", {}
        config, product = search.config(cid), search._product(ean)
        mode = config["search_mode"]
        if mode == "known":
            return "not_allowed", "Solo URLs conocidas; esta combinación no tiene ficha", {}
        if not config["terms_reviewed"] or not config["terms_note"].strip() or config["access_status"] != "robots_allowed":
            return "not_allowed", "Sin autorización comercial y robots.txt verificado", {}
        methods = [mode] if mode != "auto" else [
            config["search_preferred"], *config["search_alternatives"].split(",")]
        evidence: dict = {"attempts": [], "candidates": []}
        seen: set[str] = set()
        best_state = "not_found"
        try:
            def save_candidate(outcome, method: str) -> None:
                nonlocal best_state
                data = asdict(outcome)
                evidence["candidates"].append(data)
                if outcome.page is None:
                    return
                match = classify(product, outcome.page.candidate())
                exact = outcome.state == "exact" and match.status == EXACT and not outcome.page.ambiguous
                normalized = match.normalized
                contradiction = any(set(normalized["listing"][field]) - {normalized["catalogue"][expected]}
                                    for field, expected in (("brands", "brand"), ("models", "model"),
                                                            ("colors", "color")))
                candidate_state = "rejected" if outcome.state == "rejected" or contradiction else (
                    "candidate_found" if exact else "pending_validation")
                with self.store._connect() as connection, connection:
                    connection.execute("""INSERT INTO pilot_url_candidates(
                        competitor_id,ean,url,run_id,state,method,evidence,identity,checked_at)
                        VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id,competitor_id,ean,url) DO NOTHING""",
                                       (cid, ean, outcome.url, run_id, candidate_state, method,
                                        json.dumps(data, ensure_ascii=False),
                                        json.dumps(match.normalized, ensure_ascii=False), stamp()))
                if candidate_state != "rejected":
                    best_state = "candidate_found" if exact else (
                        best_state if best_state == "candidate_found" else "pending_validation")

            for method in dict.fromkeys(methods):
                if not method:
                    continue
                self._guard(ean, cid, run_id)
                if method == "sitemap":
                    if "sitemap" not in config["locate_strategy"].split(","):
                        evidence["attempts"].append({"method": method, "result": "Método no autorizado"})
                        continue
                    urls = search._sitemap_candidates(product, config, fetcher)
                else:
                    if not config["search_verified"] or "site_search" not in config["locate_strategy"].split(","):
                        evidence["attempts"].append({"method": method, "result": "Búsqueda GET no verificada/autorizada"})
                        continue
                    single = {**config, "search_mode": method}
                    if config["domain"].removeprefix("www.") == "edel-optics.es":
                        recognized, cards, source_url, total, fallback_urls = search._edel_search_cards(
                            product, single, fetcher, method)
                        if recognized:
                            evidence["attempts"].append({"method": method, "search_url": source_url,
                                                        "cards_examined": total,
                                                        "candidate_urls": [card.url for card in cards]})
                            for outcome in cards:
                                self._guard(ean, cid, run_id)
                                save_candidate(outcome, method)
                            if best_state == "candidate_found":
                                break
                            continue
                        if source_url:
                            evidence["attempts"].append({"method": method, "search_url": source_url,
                                                        "urls": fallback_urls,
                                                        "result": "Sin tarjetas estructuradas reconocibles"})
                            urls = fallback_urls
                        else:
                            evidence["attempts"].append({"method": method,
                                                        "result": "No se pudo interpretar la respuesta de búsqueda"})
                            continue
                    else:
                        urls = search._site_search_candidates(product, single, fetcher)
                evidence["attempts"].append({"method": method, "urls": urls})
                for url in urls:
                    if url in seen or len(seen) >= MAX_CANDIDATES:
                        continue
                    seen.add(url)
                    self._guard(ean, cid, run_id)
                    url = validate_competitor_url(url, config["domain"])
                    current = search.config(cid)
                    if current["search_mode"] == "known" or (
                            method != "sitemap" and not current["search_verified"]):
                        raise PilotError("Autorización del método de localización retirada")
                    outcome = search._visit(product, current, fetcher, url, method, False)
                    save_candidate(outcome, method)
                if best_state == "candidate_found" or len(seen) >= MAX_CANDIDATES:
                    break
        except ConnectorBlocked as exc:
            search._note_failure(cid, str(exc), blocked=True)
            return "blocked", str(exc), evidence
        except RobotsDenied as exc:
            return "not_allowed", str(exc), evidence
        except (ConnectorError, WebSearchError) as exc:
            search._note_failure(cid, str(exc))
            return "error", str(exc), evidence
        if not any("urls" in attempt or "search_url" in attempt for attempt in evidence["attempts"]):
            return "not_allowed", "No hay un método autorizado y verificado de localización", evidence
        return best_state, json.dumps(evidence["attempts"], ensure_ascii=False), evidence

    def review(self, candidate_id: int, *, accept: bool, note: str) -> None:
        if not note.strip():
            raise PilotError("Indique quién revisa la ficha y el motivo de confirmar o descartar")
        pilot, search = PricePilot(self.store), WebPriceSearch(self.store)
        try:
            with pilot.lock():
                scope = pilot_scope(self.store)
                with self.store._connect() as connection:
                    row = connection.execute("SELECT * FROM pilot_url_candidates WHERE id=?", (candidate_id,)).fetchone()
                if scope is None or row is None or row["ean"] not in scope["eans"] \
                        or row["competitor_id"] not in scope["competitors"]:
                    raise PilotError("Candidato fuera del alcance actual del piloto")
                if row["state"] not in ("candidate_found", "pending_validation"):
                    raise PilotError("El candidato ya se revisó o fue rechazado por identidad")
                cid, ean = row["competitor_id"], row["ean"]
                with search._lock(cid):
                    config, product = search.config(cid), search._product(ean)
                    data = json.loads(row["evidence"])
                    page = ProductPage(**data["page"])
                    match = classify(product, page.candidate())
                    if accept:
                        if match.status != EXACT or page.ambiguous:
                            raise PilotError("La identidad exacta no está confirmada; no puede aprobarse")
                        url = validate_competitor_url(row["url"], config["domain"])
                        # Save URL/identity only; the future price run revalidates commercial facts.
                        with self.store._connect() as connection, connection:
                            connection.execute("""INSERT INTO competitor_product_urls(
                                competitor_id,ean,url,model_code,color_code,caliber,ean_published,match_status,status,
                                verified_at,source,note) VALUES(?,?,?,?,?,?,?,'exact_match','verified',?,'user',?)
                                ON CONFLICT(competitor_id,ean,url) DO UPDATE SET status='verified',
                                match_status='exact_match',ean_published=excluded.ean_published,
                                verified_at=excluded.verified_at,note=excluded.note""",
                                               (cid, ean, url, product["model"], product["color"], page.caliber,
                                                page.ean_published, stamp(), note.strip()))
                            self._review_row(connection, row, "confirmed", note)
                    else:
                        with self.store._connect() as connection, connection:
                            self._review_row(connection, row, "discarded", note)
        except Timeout as exc:
            raise PilotError("Hay una ejecución incompatible; espere antes de revisar candidatos") from exc

    @staticmethod
    def _review_row(connection, row, state: str, note: str) -> None:
        connection.execute("UPDATE pilot_url_candidates SET state=?,reviewed_at=?,review_note=? WHERE id=?",
                           (state, stamp(), note.strip(), row["id"]))
        connection.execute("""INSERT INTO pilot_discovery(competitor_id,ean,state,method,detail,updated_at)
            VALUES(?,?,?,?,?,?) ON CONFLICT(competitor_id,ean) DO UPDATE SET state=excluded.state,
            detail=excluded.detail,updated_at=excluded.updated_at""",
                           (row["competitor_id"], row["ean"], "confirmed" if state == "confirmed" else "not_found",
                            row["method"], note.strip(), stamp()))

    def reset(self, ean: str, cid: str, *, note: str) -> None:
        if not note.strip():
            raise PilotError("Indique el motivo de reintentar la localización")
        try:
            with PricePilot(self.store).lock():
                scope = pilot_scope(self.store)
                if scope is None or ean not in scope["eans"] or cid not in scope["competitors"]:
                    raise PilotError("Fuera del piloto")
                with self.store._connect() as connection:
                    interrupted = connection.execute("""SELECT id FROM pilot_runs
                        WHERE status IN ('running','interrupted')""").fetchone()
                if interrupted:
                    raise PilotError("Reanude o cierre la operación interrumpida antes de preparar otro intento")
                coverage = self.coverage()
                row = coverage[(coverage.ean == ean) & (coverage.competitor_id == cid)].iloc[0]
                if row["state"] not in ("not_found", "not_allowed", "error", "blocked"):
                    raise PilotError("Solo se reintentan combinaciones sin ficha ni candidatos pendientes")
                with self.store._connect() as connection, connection:
                    connection.execute("""UPDATE pilot_discovery SET state='pending',detail=?,updated_at=?
                        WHERE competitor_id=? AND ean=?""", (note.strip(), stamp(), cid, ean))
        except Timeout as exc:
            raise PilotError("Hay otra ejecución del piloto") from exc
