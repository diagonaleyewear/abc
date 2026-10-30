"""Competitor web price search: locate the product page, verify it is the right product, record the price.

Reuses the existing comparator: reference matching (``price_references``), the offer importer
(``PriceComparatorStore.import_offers``), the history and the scheduler lock. Public access is never
treated as authorisation for periodic queries: that needs ``terms_reviewed`` and a verified connector.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import re
from typing import Any, Callable, Iterable
from urllib.parse import quote, urljoin, urlparse

from filelock import FileLock, Timeout
import pandas as pd

from price_comparator import PriceComparatorStore, normalize_gtin
from price_connectors import Connector, ConnectorBlocked, ConnectorError, Observation
from price_references import EXACT, PROBABLE, REJECTED, ReferenceIndex, classify, key, search_plan
from price_web_extract import ProductPage, extract_product, parse_html
from price_web_fetch import MAX_SITEMAP_BYTES, PageGone, RobotsDenied, WebFetcher

ACCESS_STATES = ("unknown", "robots_allowed", "robots_restricted", "blocked", "pending_authorization")
TAX_POLICIES = ("unknown", "included", "excluded")
STRATEGIES = ("index", "sitemap", "site_search", "external")
MAX_CANDIDATES = 3
MAX_SITEMAPS = 4
MAX_CLI_EANS = 5
MAX_CLI_COMPETITORS = 3
_PRODUCT_SITEMAP = re.compile(r"product|producto|gafas|glasses|sunglass|eyewear|catalog", re.I)
_LOC = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.I | re.S)

FetcherFactory = Callable[[dict], WebFetcher]


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Outcome:
    ean: str
    competitor_id: str
    competitor: str
    state: str  # exact | probable | rejected | not_found | blocked | not_allowed | error
    url: str = ""
    detail: str = ""
    method: str = ""
    page: ProductPage | None = None
    match_status: str = ""
    requests: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def priced(self) -> bool:
        return self.state == "exact" and self.page is not None and self.page.has_price


class WebSearchError(ValueError):
    pass


class WebPriceSearch:
    def __init__(self, store: PriceComparatorStore, *, fetcher_factory: FetcherFactory | None = None,
                 external_search: Callable[[str, str], list[str]] | None = None,
                 clock: Callable[[], datetime] = _utc) -> None:
        self.store = store
        self._factory = fetcher_factory or self._default_factory
        self.external_search = external_search  # (query, domain) -> URLs; needs a provider and key: pending
        self._now = clock

    @staticmethod
    def _default_factory(config: dict) -> WebFetcher:
        return WebFetcher(pause_seconds=config["pause_seconds"], max_requests=config["max_requests"],
                          timeout=config["timeout_seconds"], retries=config["retries"])

    # configuration ----------------------------------------------------------------------------
    def ensure_config(self) -> None:
        with self.store._connect() as connection, connection:
            connection.execute("INSERT OR IGNORE INTO competitor_web_config(competitor_id) SELECT id FROM competitors")

    def config(self, competitor_id: str) -> dict:
        self.ensure_config()
        with self.store._connect() as connection:
            row = connection.execute("""
                SELECT w.*,c.name,c.url AS site_url,c.domain,c.market,c.currency FROM competitor_web_config w
                JOIN competitors c ON c.id=w.competitor_id WHERE w.competitor_id=?""", (competitor_id,)).fetchone()
        if row is None:
            raise WebSearchError("Competidor no registrado")
        return dict(row)

    def configs(self) -> pd.DataFrame:
        self.ensure_config()
        with self.store._connect() as connection:
            frame = pd.read_sql_query("""
                SELECT w.*,c.name,c.url AS site_url,c.domain,c.market,c.currency,s.interval_hours,s.next_run_at,
                       s.enabled AS scheduled,
                       (SELECT COUNT(*) FROM competitor_product_urls u WHERE u.competitor_id=w.competitor_id
                          AND u.status IN ('verified','probable')) AS located,
                       (SELECT COUNT(DISTINCT o.ean) FROM offers o WHERE o.competitor_id=w.competitor_id
                          AND o.match_status='exact_match' AND o.method LIKE 'web:%') AS confirmed
                FROM competitor_web_config w JOIN competitors c ON c.id=w.competitor_id
                LEFT JOIN competitor_schedule s ON s.competitor_id=w.competitor_id ORDER BY c.name""", connection)
        frame["connector_status"] = [self.connector_status(row) for row in frame.to_dict("records")]
        return frame

    @staticmethod
    def connector_status(row: dict) -> str:
        """implemented < verified (real valid offer) < operational (verified + terms reviewed + scheduler ok)."""
        if row["access_status"] == "blocked":
            return "blocked"
        if row["access_status"] == "robots_restricted":
            return "not_allowed"
        if row["access_status"] == "pending_authorization":
            return "pending_authorization"
        if not row["confirmed"]:
            return "implemented"
        return "operational" if row["terms_reviewed"] and row["access_status"] == "robots_allowed" else "verified"

    def update_config(self, competitor_id: str, **values: Any) -> None:
        allowed = {"locate_strategy", "search_url_template", "price_selector", "availability_selector",
                   "structured_type", "tax_policy", "pause_seconds", "max_requests", "concurrency",
                   "timeout_seconds", "retries", "terms_reviewed", "terms_note", "access_status"}
        unknown = set(values) - allowed
        if unknown:
            raise WebSearchError(f"Campos no configurables: {', '.join(sorted(unknown))}")
        self.config(competitor_id)
        checks = {
            "tax_policy": lambda v: v in TAX_POLICIES, "access_status": lambda v: v in ACCESS_STATES,
            "pause_seconds": lambda v: 1 <= float(v) <= 3600, "max_requests": lambda v: 1 <= int(v) <= 200,
            "concurrency": lambda v: int(v) == 1, "timeout_seconds": lambda v: 1 <= float(v) <= 120,
            "retries": lambda v: 0 <= int(v) <= 3,
            "locate_strategy": lambda v: v and set(str(v).split(",")) <= set(STRATEGIES),
            "search_url_template": lambda v: not v or (str(v).startswith("https://") and "{query}" in str(v)),
        }
        for name, value in values.items():
            if name in checks and not checks[name](value):
                raise WebSearchError(f"Valor no válido para {name}")
        if values.get("terms_reviewed") and not str(values.get("terms_note", "")).strip():
            raise WebSearchError("Indique quién revisó las condiciones de uso y cuándo (nota)")
        with self.store._connect() as connection, connection:
            for name, value in values.items():
                connection.execute(f"UPDATE competitor_web_config SET {name}=? WHERE competitor_id=?",
                                   (int(value) if name == "terms_reviewed" else value, competitor_id))

    def confirm_market(self, competitor_id: str, market: str, currency: str) -> None:
        """Record the market/currency the competitor really sells in (verified by a person, never assumed)."""
        if not re.fullmatch(r"[A-Z]{2}", market or "") or not re.fullmatch(r"[A-Z]{3}", currency or ""):
            raise WebSearchError("Mercado (ISO-2) y moneda (ISO-3) verificados en mayúsculas")
        self.config(competitor_id)
        with self.store._connect() as connection, connection:
            connection.execute("UPDATE competitors SET market=?,currency=? WHERE id=?", (market, currency, competitor_id))

    def add_known_url(self, competitor_id: str, ean: str, url: str) -> None:
        """Register a product page a person already knows; it is verified (not trusted) on the next lookup."""
        product, config = self._product(ean), self.config(competitor_id)
        if urlparse(url).scheme != "https" or not _same_site(url, config["domain"]):
            raise WebSearchError("La URL debe ser HTTPS y del dominio del competidor")
        with self.store._connect() as connection, connection:
            connection.execute("""INSERT INTO competitor_product_urls(competitor_id,ean,url,match_status,status,verified_at)
                VALUES(?,?,?,'manual_review','probable',?) ON CONFLICT(competitor_id,ean,url) DO UPDATE SET
                status=CASE WHEN status='needs_relocation' THEN 'probable' ELSE status END""",
                               (competitor_id, product["ean"], url, _iso(self._now())))

    def _lock(self, competitor_id: str) -> FileLock:
        # Same lock as price_scheduler: a manual search and a scheduled run never hit a competitor together.
        return FileLock(f"{self.store.database_path}.competitor-{competitor_id}.lock", timeout=0)

    # access -----------------------------------------------------------------------------------
    def check_access(self, competitor_id: str) -> dict[str, Any]:
        """Read robots.txt once and record whether public product pages may be consulted by an agent."""
        config = self.config(competitor_id)
        base = config["site_url"]
        fetcher = self._factory(config)
        stamp = _iso(self._now())
        try:
            rules = fetcher.rules(base)
            state = fetcher.robots_state(base)
            root_ok = rules.allowed(base)
            sitemaps = ", ".join(rules.sitemaps[:3]) or "ninguno"
            search_ok = (not config["search_url_template"]
                         or rules.allowed(config["search_url_template"].replace("{query}", "x")))
            status = "robots_allowed" if root_ok and not rules.deny_all else "robots_restricted"
            search_text = ("sin plantilla de búsqueda configurada" if not config["search_url_template"]
                           else f"búsqueda interna {'permitida' if search_ok else 'no permitida'}")
            summary = f"{state}; raíz {'permitida' if root_ok else 'no permitida'}; {search_text}; sitemaps: {sitemaps}"
        except ConnectorBlocked as exc:
            status, summary = "blocked", str(exc)
        except ConnectorError as exc:
            status, summary = "unknown", f"No se pudo comprobar: {exc}"
        with self.store._connect() as connection, connection:
            connection.execute("""UPDATE competitor_web_config SET access_status=?,robots_checked_at=?,robots_summary=?,
                blocked_at=CASE WHEN ?='blocked' THEN ? ELSE blocked_at END,
                last_error=CASE WHEN ? IN ('blocked','unknown') THEN ? ELSE last_error END WHERE competitor_id=?""",
                               (status, stamp, summary[:500], status, stamp, status, summary[:300], competitor_id))
        return {"competitor": config["name"], "access_status": status, "summary": summary,
                "requests": fetcher.requests}

    @staticmethod
    def _gate(config: dict) -> str:
        return {"robots_allowed": "", "unknown": "Acceso no comprobado: ejecute la comprobación de robots.txt",
                "robots_restricted": "robots.txt no permite consultar el sitio",
                "blocked": "Sitio con bloqueo previo (403/429/CAPTCHA): no se insiste",
                "pending_authorization": "Pendiente de autorización del competidor"}[config["access_status"]]

    # lookup -----------------------------------------------------------------------------------
    def _product(self, ean: str) -> dict:
        ean = normalize_gtin(ean)
        with self.store._connect() as connection:
            row = connection.execute("SELECT * FROM products WHERE ean=?", (ean,)).fetchone()
        if row is None:
            raise WebSearchError("EAN no importado en el catálogo")
        return dict(row)

    def _indexed(self, competitor_id: str, ean: str) -> list[dict]:
        with self.store._connect() as connection:
            return [dict(r) for r in connection.execute("""
                SELECT * FROM competitor_product_urls WHERE competitor_id=? AND ean=? AND status IN ('verified','probable')
                ORDER BY CASE status WHEN 'verified' THEN 0 ELSE 1 END, verified_at DESC""", (competitor_id, ean))]

    def _known_urls(self, competitor_id: str, ean: str) -> set[str]:
        with self.store._connect() as connection:
            return {r[0] for r in connection.execute(
                "SELECT url FROM competitor_product_urls WHERE competitor_id=? AND ean=?", (competitor_id, ean))}

    def locate(self, ean: str, competitor_id: str, *, fetcher: WebFetcher | None = None,
               persist_index: bool = True) -> Outcome:
        """Find and verify the competitor's page for one EAN. Does not record the offer."""
        product, config = self._product(ean), self.config(competitor_id)
        outcome = Outcome(product["ean"], competitor_id, config["name"], "not_found")
        reason = self._gate(config)
        if reason:
            outcome.state, outcome.detail = "not_allowed", reason
            return outcome
        fetcher = fetcher or self._factory(config)
        strategies = config["locate_strategy"].split(",")
        tried: set[str] = set()
        best: Outcome | None = None
        try:
            candidates = self._candidate_stream(product, config, fetcher, strategies)
            for origin, url in candidates:
                if url in tried:
                    continue
                tried.add(url)
                found = self._visit(product, config, fetcher, url, origin, persist_index)
                rank = {"exact": 3, "probable": 2, "rejected": 1}
                if best is None or rank.get(found.state, 0) > rank.get(best.state, 0):
                    best = found if found.state in rank else best
                if found.state == "exact":
                    break
        except ConnectorBlocked as exc:
            outcome.state, outcome.detail = "blocked", str(exc)
            self._note_failure(competitor_id, str(exc), blocked=True)
            outcome.requests = fetcher.requests
            return outcome
        except RobotsDenied as exc:
            outcome.state, outcome.detail = "not_allowed", str(exc)
            outcome.requests = fetcher.requests
            return outcome
        except ConnectorError as exc:
            outcome.state, outcome.detail = "error", str(exc)
            self._note_failure(competitor_id, str(exc))
            outcome.requests = fetcher.requests
            return outcome
        result = best or outcome
        result.requests = fetcher.requests
        if best is None:
            result.detail = "No se localizó una ficha por los métodos permitidos"
        self._touch_query(competitor_id, ok=result.state in {"exact", "probable", "not_found", "rejected"})
        return result

    def _candidate_stream(self, product: dict, config: dict, fetcher: WebFetcher, strategies: list[str]):
        competitor_id = config["competitor_id"]
        if "index" in strategies:
            for row in self._indexed(competitor_id, product["ean"]):
                yield "index", row["url"]
        known = self._known_urls(competitor_id, product["ean"])
        if "sitemap" in strategies:
            for url in self._sitemap_candidates(product, config, fetcher):
                if url not in known:
                    yield "sitemap", url
        if "site_search" in strategies and config["search_url_template"]:
            for url in self._site_search_candidates(product, config, fetcher):
                if url not in known:
                    yield "site_search", url
        if "external" in strategies and self.external_search is not None:
            for level, query in search_plan(product)[:2]:
                for url in self.external_search(query, config["domain"])[:MAX_CANDIDATES]:
                    if _same_site(url, config["domain"]) and url not in known:
                        yield "external", url

    def _sitemap_candidates(self, product: dict, config: dict, fetcher: WebFetcher) -> list[str]:
        base = config["site_url"]
        rules = fetcher.rules(base)
        pending = list(rules.sitemaps) or [urljoin(base, "/sitemap.xml")]
        model, color = key(product["model"]), key(product["color"])
        found: list[str] = []
        fetched = 0
        while pending and fetched < MAX_SITEMAPS and len(found) < MAX_CANDIDATES:
            sitemap = pending.pop(0)
            if not _same_site(sitemap, config["domain"]):
                continue
            try:
                body = fetcher.get(sitemap, max_bytes=MAX_SITEMAP_BYTES).text
            except PageGone:
                continue
            fetched += 1
            locs = _LOC.findall(body)
            if "<sitemapindex" in body.lower():
                hinted = [u for u in locs if _PRODUCT_SITEMAP.search(u)] or locs
                pending.extend(hinted)
                continue
            for loc in locs:
                path = key(urlparse(loc).path)
                if model and model in path and color and color in path and _same_site(loc, config["domain"]):
                    found.append(loc)
        return found[:MAX_CANDIDATES]

    def _site_search_candidates(self, product: dict, config: dict, fetcher: WebFetcher) -> list[str]:
        model = key(product["model"])
        found: list[str] = []
        for level, query in search_plan(product)[:3]:
            url = config["search_url_template"].replace("{query}", quote(query))
            if not fetcher.rules(url).allowed(url):
                raise RobotsDenied("robots.txt no permite la búsqueda interna del sitio")
            for href, text in parse_html(fetcher.get(url).text).links:
                target = urljoin(url, href)
                if model and model in key(urlparse(target).path + " " + text) and _same_site(target, config["domain"]) \
                        and target not in found:
                    found.append(target)
            if found:
                break
        return found[:MAX_CANDIDATES]

    def _visit(self, product: dict, config: dict, fetcher: WebFetcher, url: str, origin: str,
               persist_index: bool) -> Outcome:
        competitor_id = config["competitor_id"]
        outcome = Outcome(product["ean"], competitor_id, config["name"], "error", url=url, method=f"web:{origin}")
        try:
            fetched = fetcher.get(url)
        except PageGone as exc:
            outcome.detail = str(exc)
            if persist_index:
                self._mark_relocation(competitor_id, product["ean"], url)
            return outcome
        page = extract_product(fetched.text, fetched.url, price_selector=config["price_selector"],
                               availability_selector=config["availability_selector"], tax_policy=config["tax_policy"])
        if page is None:
            outcome.detail = "La página no publica datos de producto"
            return outcome
        match = classify(product, page.candidate())
        status = match.status
        if page.ambiguous and status == EXACT:
            status = "manual_review"
            match.reasons.append("Ficha ambigua (varias variantes u ofertas): revisión manual")
        outcome.page, outcome.url, outcome.match_status = page, fetched.url, status
        outcome.notes = [*match.reasons, *page.notes]
        outcome.method = f"web:{page.method}"
        outcome.state = {EXACT: "exact", PROBABLE: "probable", REJECTED: "rejected"}.get(status, "probable")
        outcome.detail = "; ".join(outcome.notes)
        if persist_index and outcome.state in {"exact", "probable", "rejected"}:
            self._upsert_index(product, config, outcome)
        return outcome

    # persistence ------------------------------------------------------------------------------
    def _upsert_index(self, product: dict, config: dict, outcome: Outcome) -> None:
        page = outcome.page
        status = {"exact": "verified", "probable": "probable", "rejected": "rejected"}[outcome.state]
        stamp = _iso(self._now())
        with self.store._connect() as connection, connection:
            connection.execute("""
                INSERT INTO competitor_product_urls(competitor_id,ean,url,model_code,color_code,caliber,ean_published,
                    match_status,status,verified_at,last_price,last_availability,last_ok_at,failures)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0)
                ON CONFLICT(competitor_id,ean,url) DO UPDATE SET match_status=excluded.match_status,
                    status=excluded.status,verified_at=excluded.verified_at,caliber=excluded.caliber,
                    ean_published=excluded.ean_published,failures=0,
                    last_price=COALESCE(excluded.last_price,competitor_product_urls.last_price),
                    last_availability=CASE WHEN excluded.last_price IS NULL THEN competitor_product_urls.last_availability
                                           ELSE excluded.last_availability END,
                    last_ok_at=COALESCE(excluded.last_ok_at,competitor_product_urls.last_ok_at)
            """, (config["competitor_id"], product["ean"], outcome.url, product["model"], product["color"],
                  page.caliber, page.ean_published, outcome.match_status, status, stamp, page.price,
                  page.availability, stamp if outcome.state == "exact" and page.has_price else None))

    def _mark_relocation(self, competitor_id: str, ean: str, url: str) -> None:
        with self.store._connect() as connection, connection:
            connection.execute("""UPDATE competitor_product_urls SET status='needs_relocation',failures=failures+1
                WHERE competitor_id=? AND ean=? AND url=?""", (competitor_id, ean, url))

    def _note_failure(self, competitor_id: str, message: str, *, blocked: bool = False) -> None:
        stamp = _iso(self._now())
        with self.store._connect() as connection, connection:
            connection.execute("""UPDATE competitor_web_config SET error_count=error_count+1,last_error=?,
                last_query_at=?,access_status=CASE WHEN ? THEN 'blocked' ELSE access_status END,
                blocked_at=CASE WHEN ? THEN ? ELSE blocked_at END WHERE competitor_id=?""",
                               (message[:300], stamp, int(blocked), int(blocked), stamp, competitor_id))

    def _touch_query(self, competitor_id: str, *, ok: bool) -> None:
        stamp = _iso(self._now())
        with self.store._connect() as connection, connection:
            connection.execute("""UPDATE competitor_web_config SET last_query_at=?,
                last_ok_at=CASE WHEN ? THEN ? ELSE last_ok_at END WHERE competitor_id=?""",
                               (stamp, int(ok), stamp, competitor_id))

    @staticmethod
    def frame_row(product: dict, config: dict, outcome: Outcome, stamp: str) -> dict[str, Any]:
        """Row for ``import_offers``; the importer enforces market, currency, domain and VAT rules."""
        page = outcome.page
        status = {"exact": "exact_match", "probable": "probable_match", "rejected": "rejected"}.get(
            outcome.state, outcome.match_status or "manual_review")
        if outcome.match_status == "manual_review":
            status = "manual_review"
        priced = page is not None and page.has_price and status != "rejected"
        tax = "" if page is None or page.includes_tax is None else ("sí" if page.includes_tax else "no")
        return {
            "EAN": product["ean"], "Competidor": config["name"], "Precio": page.price if priced else "",
            "Precio anterior": page.previous_price if priced and page.previous_price else "",
            "Moneda": page.currency if page and page.currency else config["currency"],
            "Mercado": config["market"] or (page.market if page else ""),
            "URL": outcome.url, "Disponibilidad": page.availability if page else "unknown",
            "Estado coincidencia": status, "Método": outcome.method or "web",
            "Evidencia": outcome.detail or "Ficha pública verificada", "Fecha consulta": stamp,
            "Marca": page.brand if page else "", "Modelo": product["model"] if outcome.state == "exact" else "",
            "Color": page.color if page else "", "Calibre": page.caliber if page else "",
            "EAN publicado": page.ean_published if page else "", "Título": page.title if page else "",
            "IVA incluido": tax, "Tipo IVA": "" if page is None or page.vat_rate is None else page.vat_rate,
        }

    def record(self, outcome: Outcome) -> str:
        """Store a located page through the existing importer. Returns '' when stored, else the reason."""
        if outcome.page is None or outcome.state not in {"exact", "probable", "rejected"}:
            return "Nada que registrar"
        config, product = self.config(outcome.competitor_id), self._product(outcome.ean)
        if not (config["market"] or outcome.page.market) or (outcome.page.has_price and not (
                config["currency"] or outcome.page.currency)):
            return "Mercado y moneda no verificados en la ficha ni por el competidor: oferta no registrada"
        frame = pd.DataFrame([self.frame_row(product, config, outcome, _iso(self._now()))])
        report = self.store.import_offers(frame)
        if report.rejected:
            return report.rejected[0]
        with self.store._connect() as connection, connection:
            connection.execute("UPDATE competitors SET connectivity='web' WHERE id=?", (outcome.competitor_id,))
        return ""

    # runs -------------------------------------------------------------------------------------
    def run(self, eans: Iterable[str], competitor_ids: Iterable[str] | None = None, *, record: bool = True
            ) -> list[Outcome]:
        """Manual, bounded search. Competitors are processed one after another; each holds the shared lock."""
        self.ensure_config()
        with self.store._connect() as connection:
            ids = list(competitor_ids or [r[0] for r in connection.execute(
                "SELECT id FROM competitors WHERE active=1 ORDER BY name")])
        outcomes: list[Outcome] = []
        for competitor_id in ids:
            config = self.config(competitor_id)
            lock = self._lock(competitor_id)
            try:
                lock.acquire()
            except Timeout:
                outcomes += [Outcome(normalize_gtin(e), competitor_id, config["name"], "error",
                                     detail="Otra ejecución está consultando a este competidor") for e in eans]
                continue
            try:
                fetcher = self._factory(config)
                for ean in eans:
                    found = self.locate(ean, competitor_id, fetcher=fetcher)
                    if record and found.state in {"exact", "probable", "rejected"}:
                        problem = self.record(found)
                        if problem:
                            found.detail = f"{found.detail} | {problem}".strip(" |")
                    outcomes.append(found)
                    if found.state in {"blocked", "not_allowed"}:
                        break
            finally:
                lock.release()
        return outcomes

    # stored view ------------------------------------------------------------------------------
    def resolve(self, text: str) -> tuple[str, list[dict]]:
        """Catalogue products matching an EAN, brand + model + colour, model + colour or a normalised variant."""
        products = self.store.products()
        index = ReferenceIndex(products.to_dict("records") if not products.empty else [])
        level, found = index.lookup(text)
        return level or "sin resultado", list(found or [])

    def stored_results(self, text: str, *, only_in_stock: bool = False, include_non_comparable: bool = True,
                       competitor_ids: Iterable[str] | None = None) -> tuple[str, pd.DataFrame]:
        """What is already stored for an EAN/brand/model/reference, labelled by how reliable each row is."""
        level, found = self.resolve(text)
        if not found:
            return level, pd.DataFrame()
        summary, _ = self.store.dashboard()
        own = {r["ean"]: r["own_price_eur"] for r in summary.to_dict("records")}
        expiry = self.store.price_expiry_days()
        cutoff = _iso(self._now() - timedelta(days=expiry))
        wanted = set(competitor_ids or [])
        configs = {r["competitor_id"]: r for r in self.configs().to_dict("records")}
        rows: list[dict[str, Any]] = []
        with self.store._connect() as connection:
            for product in found:
                for competitor_id, config in configs.items():
                    if wanted and competitor_id not in wanted:
                        continue
                    offer = connection.execute(
                        """SELECT o.*,COALESCE(k.last_ok_at,o.captured_at) AS checked_at FROM offers o
                        LEFT JOIN offer_checks k ON k.last_offer_id=o.id
                        WHERE o.ean=? AND o.competitor_id=? ORDER BY o.captured_at DESC,o.id DESC LIMIT 1""",
                        (product["ean"], competitor_id)).fetchone()
                    base = {"EAN": product["ean"], "Competidor": config["name"], "Modelo": product["model"],
                            "Color": product["color"], "Calibre": product.get("caliber", ""), "Precio": None,
                            "Diferencia €": None, "Diferencia %": None, "Stock": "", "Última revisión": "",
                            "Estado": "", "Ficha": "", "Origen": ""}
                    if offer is None:
                        base["Estado"] = self._empty_state(connection, config, product["ean"])
                        rows.append(base)
                        continue
                    price = offer["promotional_price"] if offer["promotional_price"] is not None else offer["price"]
                    eur = price * offer["fx_to_eur"] if offer["fx_to_eur"] is not None else None
                    base.update({"Precio": eur, "Stock": {"available": "En stock", "unavailable": "Sin stock"}.get(
                        offer["availability"], "Desconocido"), "Última revisión": offer["checked_at"],
                        "Ficha": offer["url"], "Calibre": offer["caliber"] or base["Calibre"],
                        "Origen": ("Consulta web" if str(offer["method"]).startswith("web:") else "Importación")})
                    base["Estado"] = self._offer_state(offer, cutoff)
                    mine = own.get(product["ean"])
                    if eur is not None and mine and base["Estado"] in {"Precio almacenado", "Oferta sin stock"}:
                        base["Diferencia €"], base["Diferencia %"] = eur - mine, (eur - mine) / mine * 100
                    rows.append(base)
        frame = pd.DataFrame(rows)
        if only_in_stock and not frame.empty:
            frame = frame[frame["Stock"] == "En stock"]
        if not include_non_comparable and not frame.empty:
            frame = frame[frame["Estado"] == "Precio almacenado"]
        return level, frame.reset_index(drop=True)

    @staticmethod
    def _offer_state(offer, cutoff: str) -> str:
        if offer["match_status"] != "exact_match":
            return "Referencia probable pendiente de confirmar"
        if offer["price_includes_tax"] is None:
            return "IVA sin confirmar (no comparable)"
        if offer["price_includes_tax"] == 0:
            return "Precio sin IVA (no comparable)"
        if offer["availability"] != "available":
            return "Oferta sin stock"
        return "Precio caducado" if offer["checked_at"] < cutoff else "Precio almacenado"

    @staticmethod
    def _empty_state(connection, config: dict, ean: str) -> str:
        last = connection.execute("""SELECT match_status FROM search_results WHERE ean=? AND competitor_id=?
            ORDER BY captured_at DESC,id DESC LIMIT 1""", (ean, config["competitor_id"])).fetchone()
        if last and last["match_status"] == "error":
            return "Error o bloqueo"
        if config["access_status"] in {"blocked", "robots_restricted", "pending_authorization"}:
            return "Competidor no consultable"
        return "Sin datos"


def _same_site(url: str, domain: str) -> bool:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    return host == domain or host.endswith("." + domain)


class WebFichaConnector:
    """Scheduler connector (``price_scheduler``): visits known/verified fichas, one EAN per request."""

    name = "web_ficha"
    label = "Consulta web de fichas públicas"
    batch_size = 1

    def __init__(self, store: PriceComparatorStore, search: WebPriceSearch | None = None) -> None:
        self.store = store
        self.search = search or WebPriceSearch(store)

    def _competitor(self, source: str) -> str:
        host = (urlparse(source).hostname or "").lower().removeprefix("www.")
        with self.store._connect() as connection:
            row = connection.execute("SELECT id FROM competitors WHERE domain=?", (host,)).fetchone()
        if row is None:
            raise ConnectorError("La fuente no corresponde a ningún competidor registrado")
        return row["id"]

    def verify(self, source: str) -> None:
        config = self.search.config(self._competitor(source))
        if not config["terms_reviewed"]:
            raise ConnectorError("Condiciones de uso sin revisar: una consulta pública no autoriza consultas periódicas")
        if config["access_status"] != "robots_allowed":
            raise ConnectorError("Acceso no permitido o no comprobado (robots.txt)")
        if not self.search.configs().set_index("competitor_id").loc[config["competitor_id"], "confirmed"]:
            raise ConnectorError("Sin ninguna oferta real y válida obtenida: el conector no se considera operativo")

    def fetch(self, source: str, eans: list[str]) -> list[Observation]:
        competitor_id = self._competitor(source)
        config_market = self.search.config(competitor_id)["market"]
        found: list[Observation] = []
        for ean in eans:
            outcome = self.search.locate(ean, competitor_id)
            if outcome.state == "blocked":
                raise ConnectorBlocked(outcome.detail)
            if outcome.state in {"error", "not_allowed"}:
                raise ConnectorError(outcome.detail)
            if outcome.state == "exact" and outcome.priced:
                page = outcome.page
                found.append(Observation(
                    ean=outcome.ean, price=str(page.price), currency=page.currency, availability=page.availability,
                    url=outcome.url, previous_price="" if page.previous_price is None else str(page.previous_price),
                    match_status="exact_match", method=outcome.method, evidence=outcome.detail,
                    tax_included=page.includes_tax, ean_published=page.ean_published, caliber=page.caliber,
                    color=page.color, brand=page.brand, title=page.title, market=config_market or page.market))
        return found


def web_connectors(store: PriceComparatorStore) -> dict[str, Connector]:
    return {WebFichaConnector.name: WebFichaConnector(store)}


def _cli_search(args: argparse.Namespace, search: WebPriceSearch) -> int:
    eans = [normalize_gtin(e) for e in args.ean]
    if len(eans) > MAX_CLI_EANS or len(args.competitor) > MAX_CLI_COMPETITORS:
        print(f"Límite de seguridad: máximo {MAX_CLI_EANS} EAN y {MAX_CLI_COMPETITORS} competidores por ejecución manual")
        return 2
    by_name = {r["name"].lower(): r["competitor_id"] for r in search.configs().to_dict("records")}
    try:
        ids = [by_name[name.lower()] for name in args.competitor]
    except KeyError as exc:
        print(f"Competidor desconocido: {exc.args[0]}. Disponibles: {', '.join(sorted(by_name))}")
        return 2
    outcomes = search.run(eans, ids, record=not args.dry_run)
    for o in outcomes:
        price = f"{o.page.price:g} {o.page.currency}" if o.priced else "-"
        print(f"{o.competitor} | {o.ean} | {o.state} | {price} | {o.page.availability if o.page else '-'} | "
              f"{o.url or '-'} | {o.detail}")
    return 1 if any(o.state in {"blocked", "error"} for o in outcomes) else 0


def main(argv: list[str] | None = None) -> int:
    """Manual commands only. Nothing here schedules, loops or enables automatic queries."""
    parser = argparse.ArgumentParser(description="Consulta web manual y acotada de fichas públicas de competidores.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="Estado de los 14 competidores (no hace peticiones)")
    access = sub.add_parser("check-access", help="Lee robots.txt (una petición por competidor)")
    access.add_argument("--competitor", action="append", default=[], help="Nombre; repetible. Por defecto, todos")
    find = sub.add_parser("search", help="Localiza y registra ofertas de EAN concretos")
    find.add_argument("--ean", action="append", required=True)
    find.add_argument("--competitor", action="append", required=True)
    find.add_argument("--dry-run", action="store_true", help="No escribe en la base de datos")
    add = sub.add_parser("add-url", help="Añade una ficha conocida al índice")
    add.add_argument("--competitor", required=True)
    add.add_argument("--ean", required=True)
    add.add_argument("--url", required=True)
    args = parser.parse_args(argv)
    store = PriceComparatorStore()
    store.seed_defaults()
    search = WebPriceSearch(store)
    configs = search.configs()
    try:
        if args.command == "status":
            columns = ["name", "access_status", "connector_status", "located", "confirmed", "terms_reviewed",
                       "last_query_at", "error_count"]
            print(configs[columns].to_string(index=False))
            return 0
        if args.command == "check-access":
            wanted = {n.lower() for n in args.competitor}
            for row in configs.to_dict("records"):
                if wanted and row["name"].lower() not in wanted:
                    continue
                result = search.check_access(row["competitor_id"])
                print(f"{result['competitor']} | {result['access_status']} | {result['summary']}")
            return 0
        if args.command == "search":
            return _cli_search(args, search)
        by_name = {r["name"].lower(): r["competitor_id"] for r in configs.to_dict("records")}
        search.add_known_url(by_name[args.competitor.lower()], args.ean, args.url)
        print("Ficha añadida; se verificará en la próxima consulta.")
        return 0
    except (WebSearchError, ValueError, KeyError) as exc:
        print(f"Error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
