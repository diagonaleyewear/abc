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
from string import Formatter
from typing import Any, Callable, Iterable
from urllib.parse import quote, urljoin, urlparse

from filelock import FileLock, Timeout
import pandas as pd

from price_comparator import PriceComparatorStore, normalize_gtin
from price_connectors import Connector, ConnectorBlocked, ConnectorError, Observation
from price_references import EXACT, PROBABLE, REJECTED, ReferenceIndex, classify, key, search_plan, tokens
from price_web_extract import (ProductPage, extract_edel_search_cards, extract_product, parse_html,
                               resolve_market)
from price_web_fetch import MAX_SITEMAP_BYTES, PageGone, RobotsDenied, WebFetcher

ACCESS_STATES = ("unknown", "robots_allowed", "robots_restricted", "blocked", "pending_authorization")
TAX_POLICIES = ("unknown", "included", "excluded")
STRATEGIES = ("index", "sitemap", "site_search", "external")
SEARCH_MODES = ("auto", "ean", "model_color", "known")
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
    reason: str = ""  # key of FAILURES when something prevents a comparable offer

    @property
    def priced(self) -> bool:
        return self.state == "exact" and self.page is not None and self.page.has_price


class WebSearchError(ValueError):
    pass


# reason -> (what happened and what to do, action). Actions: url = ask for another URL; review = keep the URL and
# review the extractor or the reference; wait = access blocked, do not retry; authorization = wait for the
# terms review/authorisation; retry = temporary, try again later; none = nothing to do.
FAILURES: dict[str, tuple[str, str]] = {
    "gone": ("La URL no existe (HTTP 404/410): la ficha se retiró o cambió de dirección. Facilite la URL actual.",
             "url"),
    "wrong_product": ("La ficha es de otro producto (marca o modelo distintos). Facilite la URL correcta; la anterior "
                      "se conserva y el histórico de precios no se toca.", "url"),
    "variant_differs": ("La ficha es del mismo modelo pero de otro color o calibre. Busque la variante exacta y "
                        "facilite su URL.", "url"),
    "ean_conflict": ("La ficha publica un EAN distinto del solicitado. La coincidencia no se acepta automáticamente; "
                     "revise manualmente la identidad y la evidencia antes de conservar la URL.", "review"),
    "ambiguous": ("La ficha es probable o ambigua (varias variantes u ofertas, o datos insuficientes): confirme la "
                  "referencia antes de guardarla; no genera ofertas comparables.", "review"),
    "no_product_data": ("La página responde pero no publica datos de producto legibles. La URL se conserva: necesita "
                        "revisión del extractor, no otra URL.", "review"),
    "no_price": ("La ficha es la correcta pero no se ha encontrado el precio. La URL se conserva: necesita revisión "
                 "del extractor (o del selector de precio), no otra URL.", "review"),
    "vat_unconfirmed": ("Ficha y precio localizados, pero el IVA no está confirmado: la oferta se guarda sin entrar "
                        "en las comparativas. Compruebe en la web si el precio incluye IVA y configúrelo en el "
                        "competidor.", "review"),
    "market_currency": ("El precio no es comparable todavía: moneda o mercado no verificados, o distintos de EUR/ES. "
                        "La oferta no entra en las comparativas; confirme mercado y moneda del competidor.", "review"),
    "redirect": ("La URL redirige fuera del dominio del competidor y no se sigue. Facilite la URL definitiva.", "url"),
    "robots": ("robots.txt no permite consultar esta ruta. La URL se conserva; no se consulta hasta que el acceso "
               "esté autorizado.", "authorization"),
    "authorization": ("El competidor está pendiente de autorización. La URL se conserva; no se consulta hasta "
                      "entonces.", "authorization"),
    "http_blocked": ("El sitio rechazó o limitó la petición (HTTP 403/429). No es un producto inexistente: la URL se "
                     "conserva y no se reintenta; espere a que se autorice o se levante el bloqueo.", "wait"),
    "captcha": ("El sitio presenta un CAPTCHA o control antibot. No es un producto inexistente: la URL se conserva y "
                "no se intenta superarlo.", "wait"),
    "unchecked": ("No se pudo comprobar el acceso (robots.txt). La URL se conserva; repita la comprobación más "
                  "tarde.", "retry"),
    "temporary": ("Error temporal de conexión o del servidor. No se ha guardado nada ni se reintenta solo: inténtelo "
                  "de nuevo más tarde.", "retry"),
}


def failure_reason(exc: Exception) -> str:
    """Map a fetch exception to a FAILURES key; a 403/429 or CAPTCHA is never presented as a missing product."""
    text = str(exc)
    if isinstance(exc, RobotsDenied):
        return "robots"
    if isinstance(exc, PageGone):
        return "gone"
    if isinstance(exc, ConnectorBlocked):
        return "captcha" if re.search(r"captcha|antibot", text, re.I) else "http_blocked"
    if "otro dominio" in text:
        return "redirect"
    return "temporary"


def mismatch_reason(status: str, reasons: list[str]) -> str:
    """Why a page is not the requested product, from the reasons given by ``classify``."""
    text = "; ".join(reasons).lower()
    found = re.search(r"(?:contradice:|distinto) ([^:;]*)", text)
    if found:
        return "wrong_product" if re.search(r"marca|modelo", found.group(1)) else "variant_differs"
    if "ean publicado distinto" in text:
        return "ean_conflict"
    return "wrong_product" if status == REJECTED else "ambiguous"


def comparability_gap(page: ProductPage, config: dict) -> str:
    """First reason why an exact page does not yield a comparable offer (empty when it does)."""
    if not page.has_price:
        return "no_price"
    market, _, conflict = resolve_market(page, config)
    currency = page.currency or config["currency"]
    if currency != "EUR" or market != "ES" or conflict:
        return "market_currency"
    return "vat_unconfirmed" if page.includes_tax is None else ""


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
                       (SELECT COUNT(*) FROM competitor_product_urls u WHERE u.competitor_id=w.competitor_id
                          AND u.status='pending_verification') AS candidates,
                       (SELECT COUNT(DISTINCT o.ean) FROM offers o WHERE o.competitor_id=w.competitor_id
                          AND o.match_status='exact_match' AND o.method LIKE 'web:%') AS confirmed
                FROM competitor_web_config w JOIN competitors c ON c.id=w.competitor_id
                LEFT JOIN competitor_schedule s ON s.competitor_id=w.competitor_id ORDER BY c.name""", connection)
        frame["connector_status"] = [self.connector_status(row) for row in frame.to_dict("records")]
        return frame

    def competitor_states(self, comparable: dict[str, int] | None = None) -> pd.DataFrame:
        """Three independent states per competitor, derived from the stored configuration and URL index:
        location (is there a page), extraction (did it yield real data) and operability (is periodic access
        authorised). A working extractor never implies authorisation."""
        comparable = comparable or {}
        configs = self.configs()
        with self.store._connect() as connection:
            urls = pd.read_sql_query("SELECT competitor_id,status,last_price FROM competitor_product_urls", connection)
            offers = {r[0]: r[1] for r in connection.execute(
                "SELECT competitor_id,COUNT(DISTINCT ean) FROM offers GROUP BY competitor_id")}
        rows = []
        for cfg in configs.to_dict("records"):
            mine = urls[urls["competitor_id"] == cfg["competitor_id"]]
            statuses = set(mine["status"])
            priced = bool(((mine["status"] == "verified") & mine["last_price"].notna()).any())
            if "verified" in statuses:
                location = "verified"
            elif statuses & {"probable", "pending_verification", "needs_relocation", "rejected"}:
                location = "candidate"
            else:
                location = "none"
            if priced:
                extraction = "verified"
            elif statuses & {"verified", "probable"}:
                extraction = "incomplete"
            elif statuses & {"rejected", "needs_relocation"}:
                extraction = "failed"
            else:
                extraction = "untested"
            if cfg["access_status"] == "blocked":
                operability = "blocked"
            elif cfg["access_status"] == "robots_restricted":
                operability = "not_operational"
            elif cfg["terms_reviewed"] and cfg["access_status"] == "robots_allowed":
                operability = "authorized"
            else:
                operability = "pending_review"
            rows.append({"competitor_id": cfg["competitor_id"], "name": cfg["name"], "location": location,
                         "extraction": extraction, "operability": operability,
                         "offers": int(offers.get(cfg["competitor_id"], 0)),
                         "comparable": int(comparable.get(cfg["name"], 0))})
        return pd.DataFrame(rows)

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
                   "timeout_seconds", "retries", "terms_reviewed", "terms_note", "access_status",
                   "sales_market", "market_verified", "market_evidence_url", "market_note",
                   "search_mode", "search_preferred", "search_alternatives", "search_verified", "search_evidence"}
        unknown = set(values) - allowed
        if unknown:
            raise WebSearchError(f"Campos no configurables: {', '.join(sorted(unknown))}")
        config = self.config(competitor_id)
        checks = {
            "tax_policy": lambda v: v in TAX_POLICIES, "access_status": lambda v: v in ACCESS_STATES,
            "pause_seconds": lambda v: 1 <= float(v) <= 3600, "max_requests": lambda v: 1 <= int(v) <= 200,
            "concurrency": lambda v: int(v) == 1, "timeout_seconds": lambda v: 1 <= float(v) <= 120,
            "retries": lambda v: 0 <= int(v) <= 3,
            "locate_strategy": lambda v: v and set(str(v).split(",")) <= set(STRATEGIES),
            "sales_market": lambda v: not v or bool(re.fullmatch(r"[A-Z]{2}", str(v))),
            "market_verified": lambda v: v in (True, False, 0, 1),
            "search_mode": lambda v: v in SEARCH_MODES,
            "search_preferred": lambda v: v in ("ean", "model_color"),
            "search_alternatives": lambda v: not v or set(str(v).split(",")) <= {"ean", "model_color", "sitemap"},
            "search_verified": lambda v: v in (True, False, 0, 1),
        }
        for name, value in values.items():
            if name in checks and not checks[name](value):
                raise WebSearchError(f"Valor no válido para {name}")
        if values.get("terms_reviewed") and not str(values.get("terms_note", "")).strip():
            raise WebSearchError("Indique quién revisó las condiciones de uso y cuándo (nota)")
        merged = {**config, **values}
        if merged["search_url_template"]:
            validate_search_template(merged["search_url_template"], config["domain"])
        if merged["search_verified"] and (not merged["search_url_template"] or not merged["search_evidence"].strip()):
            raise WebSearchError("La búsqueda GET necesita plantilla validada y evidencia de revisión/autorización")
        if merged["market_verified"]:
            if not merged["sales_market"] or not merged["market_note"].strip() or not merged["market_evidence_url"]:
                raise WebSearchError("Confirme mercado con URL de evidencia y nota de quién/cuándo lo verificó")
            validate_competitor_url(merged["market_evidence_url"], config["domain"])
        market_changed = any(name in values and values[name] != config[name] for name in (
            "sales_market", "market_verified", "market_evidence_url", "market_note"))
        with self.store._connect() as connection, connection:
            for name, value in values.items():
                connection.execute(f"UPDATE competitor_web_config SET {name}=? WHERE competitor_id=?",
                                   (int(value) if name in {"terms_reviewed", "market_verified", "search_verified"}
                                    else value, competitor_id))
            if market_changed:
                connection.execute("UPDATE competitor_web_config SET market_verified_at=? WHERE competitor_id=?",
                                   (_iso(self._now()) if merged["market_verified"] else None, competitor_id))

    def confirm_market(self, competitor_id: str, market: str, currency: str) -> None:
        """Record the market/currency the competitor really sells in (verified by a person, never assumed)."""
        if not re.fullmatch(r"[A-Z]{2}", market or "") or not re.fullmatch(r"[A-Z]{3}", currency or ""):
            raise WebSearchError("Mercado (ISO-2) y moneda (ISO-3) verificados en mayúsculas")
        self.config(competitor_id)
        with self.store._connect() as connection, connection:
            connection.execute("UPDATE competitors SET market=?,currency=? WHERE id=?", (market, currency, competitor_id))

    def add_known_url(self, competitor_id: str, ean: str, url: str) -> None:
        """Register a page a person already knows without querying it: it stays pending until it is verified."""
        product, config = self._product(ean), self.config(competitor_id)
        self._store_pending(product, competitor_id, validate_competitor_url(url, config["domain"]),
                            "Aportada manualmente; pendiente de verificar")

    def _registered(self, competitor_id: str, ean: str) -> list[dict]:
        with self.store._connect() as connection:
            return [dict(r) for r in connection.execute(
                "SELECT * FROM competitor_product_urls WHERE competitor_id=? AND ean=?", (competitor_id, ean))]

    def _store_pending(self, product: dict, competitor_id: str, url: str, note: str) -> None:
        stamp = _iso(self._now())
        with self.store._connect() as connection, connection:
            connection.execute("""
                INSERT INTO competitor_product_urls(competitor_id,ean,url,match_status,status,verified_at,source,note,
                    submitted_at) VALUES(?,?,?,'manual_review','pending_verification',?,'user',?,?)
                ON CONFLICT(competitor_id,ean,url) DO UPDATE SET note=excluded.note,
                    status=CASE WHEN status IN ('verified','probable') THEN status ELSE 'pending_verification' END""",
                               (competitor_id, product["ean"], url, stamp, note[:300], stamp))

    def needs_url(self, ean: str, competitor_id: str) -> bool:
        """True when the user should be asked for the page: nothing current is registered (a pending URL counts)."""
        return not any(r["status"] in CURRENT_URL_STATES for r in self._registered(competitor_id, normalize_gtin(ean)))

    def url_state(self, ean: str, competitor_id: str) -> dict[str, str]:
        """How the registered page of a product at a competitor must be presented."""
        rows = {r["status"]: r for r in self._registered(competitor_id, normalize_gtin(ean))}
        blocked = self.config(competitor_id)["access_status"] == "blocked"
        for status in ("verified", "probable", "pending_verification", "needs_relocation", "rejected"):
            row = rows.get(status)
            if row is None:
                continue
            key_ = page_state_key(row, blocked)
            return {"key": key_, "text": URL_STATE_TEXT[key_], "url": row["url"], "note": row["note"]}
        return {"key": "none", "text": URL_STATE_TEXT["none"], "url": "", "note": ""}

    def registered_pages(self, eans: Iterable[str] | None = None, competitor_id: str | None = None) -> pd.DataFrame:
        """Every registered page (candidate or verified) with its state and the last stored price. A page is not an
        offer: the price shown is only what a previous check stored, and says nothing about comparability."""
        configs = {r["competitor_id"]: r for r in self.configs().to_dict("records")}
        wanted = set(eans) if eans is not None else None
        with self.store._connect() as connection:
            rows = [dict(r) for r in connection.execute(
                """SELECT u.*,p.model,p.color FROM competitor_product_urls u LEFT JOIN products p ON p.ean=u.ean
                WHERE u.status<>'replaced' AND (? IS NULL OR u.competitor_id=?) ORDER BY u.competitor_id,u.ean""",
                (competitor_id, competitor_id))]
        out = []
        for row in rows:
            if wanted is not None and row["ean"] not in wanted or row["competitor_id"] not in configs:
                continue
            config = configs[row["competitor_id"]]
            key_ = page_state_key(row, config["access_status"] == "blocked")
            out.append({"competitor_id": row["competitor_id"], "Competidor": config["name"], "EAN": row["ean"],
                        "Modelo": row["model"] or "", "Color": row["color"] or "", "Estado": URL_STATE_TEXT[key_],
                        "estado_clave": key_, "Ficha": row["url"], "Precio": row["last_price"],
                        "Stock": {"available": "En stock", "unavailable": "Sin stock"}.get(
                            row["last_availability"], ""),
                        "Última consulta": row["last_ok_at"] or "", "Nota": row["note"],
                        "Comprobación de precio": "Realizada" if row["last_price"] is not None else "Pendiente"})
        return pd.DataFrame(out)

    def submit_url(self, ean: str, competitor_id: str, url: str, *, replace: bool = False,
                   fetcher: WebFetcher | None = None) -> Submission:
        """A person supplies the exact product page. It is validated, checked against the access rules, read once and
        verified against the requested product; being supplied by a person never makes it verified."""
        self._pilot_guard(ean, competitor_id)
        product, config = self._product(ean), self.config(competitor_id)
        try:
            clean = validate_competitor_url(url, config["domain"])
        except WebSearchError as exc:
            return Submission("invalid", str(exc))
        rows = self._registered(competitor_id, product["ean"])
        if any(r["url"] == clean and r["status"] in {"verified", "probable"} for r in rows):
            return Submission("duplicate", "Esta URL ya está registrada para este producto y competidor.", clean)
        current = [r for r in rows if r["status"] in {"verified", "probable"} and r["url"] != clean]
        if current and not replace:
            return Submission("already_registered", f"Ya hay una ficha vigente ({current[0]['url']}). "
                                                    "Marque «Sustituir la ficha registrada» para cambiarla.", clean)
        lock = self._lock(competitor_id)
        try:
            lock.acquire()
        except Timeout:
            return Submission("error", "Otra ejecución está consultando a este competidor", clean)
        try:
            return self._verify_submitted(product, competitor_id, clean, replace, fetcher)
        finally:
            lock.release()

    def _verify_submitted(self, product: dict, competitor_id: str, url: str, replace: bool,
                          fetcher: WebFetcher | None) -> Submission:
        config = self.config(competitor_id)
        fetcher = fetcher or self._factory(config)
        if config["access_status"] == "unknown":
            self._check_access_locked(competitor_id, fetcher=fetcher)
            config = self.config(competitor_id)
        reason = self._gate(config)
        if reason:
            return self._keep_pending(product, config, url, reason, self._gate_reason(config))
        try:
            outcome = self._visit(product, config, fetcher, url, "user", persist_index=False)
        except ConnectorBlocked as exc:
            self._note_failure(competitor_id, str(exc), blocked=True)
            return self._keep_pending(product, self.config(competitor_id), url, str(exc), failure_reason(exc))
        except RobotsDenied as exc:
            return self._keep_pending(product, config, url, str(exc), "robots")
        except ConnectorError as exc:
            code = failure_reason(exc)
            return self._submission("error", f"No se pudo consultar la ficha: {exc}.", url, reason=code)
        self._touch_query(competitor_id, ok=outcome.state != "error")
        if outcome.state == "exact":
            self._upsert_index(product, config, outcome, source="user", note="URL aportada y verificada")
            self._supersede(competitor_id, product["ean"], url, replace)
            problem = self.record(outcome)
            text = "Coincidencia exacta: URL verificada y guardada."
            return self._submission("verified", f"{text} {problem}".strip(), url, outcome, outcome.reason)
        if outcome.state == "probable":
            return self._submission("needs_confirmation", f"Coincidencia probable o ambigua: {outcome.detail}", url,
                                    outcome, outcome.reason or "ambiguous")
        if outcome.state == "rejected":
            self._upsert_index(product, config, outcome, source="user", note="URL aportada: producto distinto")
            return self._submission("rejected", f"Coincidencia incorrecta: {outcome.detail}", url, outcome,
                                    outcome.reason)
        if outcome.reason == "no_product_data":
            self._store_pending(product, competitor_id, url, "Extracción fallida: la página no publica datos de producto")
            return self._submission("needs_review", f"{outcome.detail}.", url, outcome, outcome.reason)
        return self._submission("error", outcome.detail or "La página no se pudo interpretar", url, outcome,
                                outcome.reason or "temporary")

    @staticmethod
    def _submission(state: str, message: str, url: str = "", outcome: Outcome | None = None,
                    reason: str = "") -> Submission:
        """Append what to do next so every failure says what happened and what the person can do."""
        help_text, action = FAILURES.get(reason, ("", "none"))
        return Submission(state, f"{message} {help_text}".strip(), url, outcome, reason, action)

    @staticmethod
    def _gate_reason(config: dict) -> str:
        if config["access_status"] == "blocked":
            return "captcha" if re.search(r"captcha|antibot", config.get("last_error") or "", re.I) else "http_blocked"
        return {"robots_restricted": "robots", "pending_authorization": "authorization"}.get(
            config["access_status"], "unchecked")

    def save_for_review(self, submission: Submission, *, replace: bool = False) -> Submission:
        """Store a probable page, after a person confirmed it, as a reference pending review. It never yields a
        comparable offer: the page is not recorded and its match status stays non-exact."""
        outcome = submission.outcome
        if submission.state != "needs_confirmation" or outcome is None:
            raise WebSearchError("Solo se pueden guardar coincidencias probables pendientes de confirmar")
        product, config = self._product(outcome.ean), self.config(outcome.competitor_id)
        self._upsert_index(product, config, outcome, source="user",
                           note="Probable: confirmada por una persona; pendiente de revisión")
        self._supersede(outcome.competitor_id, product["ean"], submission.url, replace)
        return Submission("saved_for_review", "Guardada como referencia pendiente de revisión; no genera ofertas "
                                              "comparables.", submission.url, outcome)

    def _keep_pending(self, product: dict, config: dict, url: str, reason: str, code: str) -> Submission:
        self._store_pending(product, config["competitor_id"], url, reason)
        state = "blocked" if config["access_status"] == "blocked" else "not_allowed"
        return self._submission(state, f"{reason}. La URL queda guardada como pendiente de verificación; no se "
                                       "consulta.", url, reason=code)

    def _supersede(self, competitor_id: str, ean: str, keep: str, replace: bool) -> None:
        """A new verified page replaces dead ones (and, if asked, current ones). Offer history is untouched."""
        states = "('needs_relocation','verified','probable')" if replace else "('needs_relocation')"
        with self.store._connect() as connection, connection:
            connection.execute(f"""UPDATE competitor_product_urls SET status='replaced' WHERE competitor_id=? AND ean=?
                AND url<>? AND status IN {states}""", (competitor_id, ean, keep))

    def _lock(self, competitor_id: str) -> FileLock:
        # Same lock as price_scheduler: a manual search and a scheduled run never hit a competitor together.
        return FileLock(f"{self.store.database_path}.competitor-{competitor_id}.lock", timeout=0)

    # access -----------------------------------------------------------------------------------
    def check_access(self, competitor_id: str, fetcher: WebFetcher | None = None) -> dict[str, Any]:
        """Check a registered competitor's robots.txt, independently of commercial pilot permissions."""
        self.config(competitor_id)
        try:
            with self._lock(competitor_id):
                return self._check_access_locked(competitor_id, fetcher=fetcher)
        except Timeout as exc:
            raise WebSearchError("Otra ejecución está consultando a este competidor; espere para comprobar robots.txt") from exc

    def _check_access_locked(self, competitor_id: str, fetcher: WebFetcher | None = None) -> dict[str, Any]:
        config = self.config(competitor_id)
        base = validate_competitor_url(config["site_url"], config["domain"])
        parsed = urlparse(base)
        base = f"https://{parsed.netloc}/"
        fetcher = fetcher or self._factory(config)
        before = fetcher.requests
        stamp = _iso(self._now())
        try:
            if config["access_status"] == "blocked":
                raise ConnectorBlocked("Sitio con bloqueo previo (403/429/CAPTCHA): no se insiste")
            rules = fetcher.inspect_robots(base)
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
                blocked_at=CASE WHEN ?='blocked' THEN COALESCE(blocked_at,?) ELSE blocked_at END,
                last_error=CASE WHEN ? IN ('blocked','unknown') THEN ? ELSE '' END WHERE competitor_id=?""",
                               (status, stamp, summary[:500], status, stamp, status, summary[:300], competitor_id))
        return {"competitor": config["name"], "access_status": status, "summary": summary,
                "requests": fetcher.requests - before}

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
            rows = connection.execute("""
                SELECT * FROM competitor_product_urls WHERE competitor_id=? AND ean=?
                  AND status IN ('verified','probable','pending_verification')
                ORDER BY CASE status WHEN 'verified' THEN 0 WHEN 'probable' THEN 1 ELSE 2 END, verified_at DESC
                """, (competitor_id, ean))
            return [dict(r) for r in rows]

    def _known_urls(self, competitor_id: str, ean: str) -> set[str]:
        with self.store._connect() as connection:
            return {r[0] for r in connection.execute(
                "SELECT url FROM competitor_product_urls WHERE competitor_id=? AND ean=?", (competitor_id, ean))}

    def locate(self, ean: str, competitor_id: str, *, fetcher: WebFetcher | None = None,
               persist_index: bool = True) -> Outcome:
        """Find and verify the competitor's page for one EAN. Does not record the offer."""
        self._pilot_guard(ean, competitor_id)
        product, config = self._product(ean), self.config(competitor_id)
        outcome = Outcome(product["ean"], competitor_id, config["name"], "not_found")
        reason = self._gate(config)
        if reason:
            outcome.state, outcome.detail, outcome.reason = "not_allowed", reason, self._gate_reason(config)
            return outcome
        fetcher = fetcher or self._factory(config)
        from price_pilot import pilot_scope

        strategies = ["index"]
        if pilot_scope(self.store) is None and config["search_mode"] != "known":
            allowed = config["locate_strategy"].split(",")
            if "site_search" in allowed and config["search_verified"]:
                strategies.append("site_search")
            if config["search_mode"] == "auto":
                strategies.extend(method for method in ("sitemap", "external") if method in allowed)
        tried: set[str] = set()
        best: Outcome | None = None
        try:
            if "site_search" in strategies and config["domain"].removeprefix("www.") == "edel-optics.es":
                for origin, url in self._candidate_stream(product, config, fetcher, ["index"]):
                    if url in tried:
                        continue
                    tried.add(url)
                    found = self._visit(product, config, fetcher, url, origin, persist_index)
                    if found.state == "exact":
                        best = found
                        break
                    if found.state in {"probable", "rejected"} and (
                            best is None or found.state == "probable"):
                        best = found
                if best is not None and best.state == "exact":
                    best.requests = fetcher.requests
                    self._touch_query(competitor_id, ok=True)
                    return best
                if best is None or best.state != "exact":
                    recognized, cards, source_url, total, fallback_urls = self._edel_search_cards(
                        product, config, fetcher, config["search_mode"])
                    strategies = [method for method in strategies if method != "site_search"]
                    if recognized:
                        result = self._best_card_outcome(cards, source_url, total)
                        if result is not None:
                            if result.state == "exact" and persist_index:
                                self._upsert_index(product, config, result)
                            result.requests = fetcher.requests
                            self._touch_query(competitor_id, ok=result.state in {
                                "exact", "probable", "rejected", "not_found"})
                            return result
                        for url in fallback_urls:
                            if url in tried:
                                continue
                            tried.add(url)
                            found = self._visit(product, config, fetcher, url, "site_search", persist_index)
                            if found.state == "exact":
                                best = found
                                break
                            if found.state in {"probable", "rejected"} and (
                                    best is None or found.state == "probable"):
                                best = found
                        if best is not None:
                            best.requests = fetcher.requests
                            self._touch_query(competitor_id, ok=best.state in {
                                "exact", "probable", "rejected"})
                            return best
                        if best is None:
                            best = Outcome(product["ean"], competitor_id, config["name"], "not_found",
                                           detail=f"{total} tarjetas revisadas; ninguna coincide con la referencia",
                                           method="web:edel-optics-search")
                        best.requests = fetcher.requests
                        self._touch_query(competitor_id, ok=True)
                        return best
                    for url in fallback_urls:
                        if url in tried:
                            continue
                        tried.add(url)
                        found = self._visit(product, config, fetcher, url, "site_search", persist_index)
                        if found.state == "exact":
                            best = found
                            break
                        if found.state in {"probable", "rejected"} and (
                                best is None or found.state == "probable"):
                            best = found
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
            outcome.state, outcome.detail, outcome.reason = "blocked", str(exc), failure_reason(exc)
            self._note_failure(competitor_id, str(exc), blocked=True)
            outcome.requests = fetcher.requests
            return outcome
        except RobotsDenied as exc:
            outcome.state, outcome.detail, outcome.reason = "not_allowed", str(exc), "robots"
            outcome.requests = fetcher.requests
            return outcome
        except ConnectorError as exc:
            outcome.state, outcome.detail, outcome.reason = "error", str(exc), failure_reason(exc)
            self._note_failure(competitor_id, str(exc))
            outcome.requests = fetcher.requests
            return outcome
        result = best or outcome
        result.requests = fetcher.requests
        if best is None:
            result.detail = "No se localizó una ficha por los métodos permitidos"
        self._touch_query(competitor_id, ok=result.state in {"exact", "probable", "not_found", "rejected"})
        return result

    def _edel_search_cards(self, product: dict, config: dict, fetcher: WebFetcher, method: str
                           ) -> tuple[bool, list[Outcome], str, int, list[str]]:
        """Return card-level matches for the authorized query; recognized result pages are never visited as fichas."""
        methods = [method] if method in {"ean", "model_color"} else [
            config.get("search_preferred", "ean"), *config.get("search_alternatives", "").split(",")]
        outcomes: list[Outcome] = []
        fallback_urls: list[str] = []
        source_url = ""
        total = 0
        recognized = False
        for search_method in dict.fromkeys(methods):
            single = {**config, "search_mode": search_method}
            for _, search_url in product_search_urls(product, single, modes=[search_method]):
                if not fetcher.rules(search_url).allowed(search_url):
                    raise RobotsDenied("robots.txt no permite la búsqueda interna del sitio")
                fetched = fetcher.get(search_url)
                source_url = fetched.url
                pages = extract_edel_search_cards(fetched.text, fetched.url, product,
                                                  tax_policy=config["tax_policy"])
                if not pages:
                    links = parse_html(fetched.text).links
                    model = key(product["model"])
                    for href, text in links:
                        target = urljoin(fetched.url, href)
                        evidence = key(urlparse(target).path + " " + text)
                        if (product["ean"] in evidence or (model and model in evidence)) \
                                and _same_site(target, config["domain"]):
                            target = validate_competitor_url(target, config["domain"])
                            if target != fetched.url and target not in fallback_urls:
                                fallback_urls.append(target)
                    if not links and re.search(r"fetch\s*\(|XMLHttpRequest|__NEXT_DATA__", fetched.text):
                        raise ConnectorError("Búsqueda dependiente de AJAX/JavaScript: revisar mecanismo público y "
                                             "autorización; no se consultan endpoints ni se automatiza navegador")
                    continue
                recognized = True
                total += len(pages)
                for page in pages:
                    try:
                        product_url = validate_competitor_url(urljoin(fetched.url, page.url), config["domain"])
                    except WebSearchError:
                        continue
                    if product_url == fetched.url or any(card.url == product_url for card in outcomes):
                        continue
                    page.url = product_url
                    match = classify(product, page.candidate())
                    if match.status == REJECTED:
                        continue
                    outcome = Outcome(product["ean"], config["competitor_id"], config["name"],
                                      "exact" if match.status == EXACT and not page.ambiguous else "probable",
                                      url=product_url, detail="; ".join([
                                          f"Tarjeta localizada en {fetched.url}", *match.reasons,
                                          *match.observations, *page.notes]),
                                      method="web:edel-optics-search-card", page=page,
                                      match_status=match.status)
                    outcome.reason = comparability_gap(page, config) if outcome.state == "exact" else "ambiguous"
                    outcomes.append(outcome)
        return recognized, outcomes, source_url, total, fallback_urls[:MAX_CANDIDATES]

    @staticmethod
    def _best_card_outcome(cards: list[Outcome], search_url: str, total: int) -> Outcome | None:
        if not cards:
            return None
        exact = [card for card in cards if card.state == "exact"]
        selected = exact or cards
        result = selected[0]
        if len(selected) > 1:
            result.state = "probable"
            result.match_status = "manual_review"
            result.page.ambiguous = True
            result.reason = "ambiguous"
            result.detail += f"; hay {len(selected)} tarjetas compatibles entre {total} resultados"
        return result

    def _candidate_stream(self, product: dict, config: dict, fetcher: WebFetcher, strategies: list[str]):
        competitor_id = config["competitor_id"]
        known = self._known_urls(competitor_id, product["ean"])
        for method in strategies:
            if method == "index":
                for row in self._indexed(competitor_id, product["ean"]):
                    yield method, row["url"]
            elif method == "sitemap":
                for url in self._sitemap_candidates(product, config, fetcher):
                    if url not in known:
                        yield method, url
            elif method == "site_search" and config["search_url_template"]:
                for url in self._site_search_candidates(product, config, fetcher):
                    if url not in known:
                        yield method, url
            elif method == "external" and self.external_search is not None:
                for level, query in search_plan(product)[:2]:
                    for url in self.external_search(query, config["domain"])[:MAX_CANDIDATES]:
                        if _same_site(url, config["domain"]) and url not in known:
                            yield method, url

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
        for mode, url in product_search_urls(product, config):
            if not fetcher.rules(url).allowed(url):
                raise RobotsDenied("robots.txt no permite la búsqueda interna del sitio")
            result = fetcher.get(url).text
            links = parse_html(result).links
            for href, text in links:
                target = urljoin(url, href)
                evidence = key(urlparse(target).path + " " + text)
                if (product["ean"] in evidence or (model and model in evidence)) and _same_site(target, config["domain"]):
                    target = validate_competitor_url(target, config["domain"])
                    if target not in found:
                        found.append(target)
            if found:
                break
            if not links and re.search(r"fetch\s*\(|XMLHttpRequest|__NEXT_DATA__", result):
                raise ConnectorError("Búsqueda dependiente de AJAX/JavaScript: revisar mecanismo público y "
                                     "autorización; no se consultan endpoints ni se automatiza navegador")
        return found[:MAX_CANDIDATES]

    def _visit(self, product: dict, config: dict, fetcher: WebFetcher, url: str, origin: str,
               persist_index: bool) -> Outcome:
        competitor_id = config["competitor_id"]
        self._pilot_guard(product["ean"], competitor_id)
        outcome = Outcome(product["ean"], competitor_id, config["name"], "error", url=url, method=f"web:{origin}")
        try:
            fetched = fetcher.get(url)
        except PageGone as exc:
            outcome.detail, outcome.reason = str(exc), "gone"
            if persist_index:
                self._mark_relocation(competitor_id, product["ean"], url)
            return outcome
        page = extract_product(fetched.text, fetched.url, price_selector=config["price_selector"],
                               availability_selector=config["availability_selector"], tax_policy=config["tax_policy"])
        if page is None:
            outcome.detail, outcome.reason = "La página no publica datos de producto", "no_product_data"
            return outcome
        match = classify(product, page.candidate())
        status = match.status
        if page.ambiguous and status == EXACT:
            status = "manual_review"
            match.reasons.append("Ficha ambigua (varias variantes u ofertas): revisión manual")
        outcome.page, outcome.url, outcome.match_status = page, fetched.url, status
        if not _same_site(fetched.url, config["domain"]):
            outcome.state, outcome.detail = "error", "La URL redirige fuera del dominio del competidor"
            outcome.reason = "redirect"
            return outcome
        outcome.notes = [*match.reasons, *match.observations, *page.notes]
        _, market_note, _ = resolve_market(page, config)
        outcome.notes.append(market_note)
        outcome.method = f"web:{page.method}"
        outcome.state = {EXACT: "exact", PROBABLE: "probable", REJECTED: "rejected"}.get(status, "probable")
        outcome.detail = "; ".join(outcome.notes)
        outcome.reason = comparability_gap(page, config) if outcome.state == "exact" else (
            "ambiguous" if status == "manual_review" else mismatch_reason(status, match.reasons))
        if persist_index and outcome.state in {"exact", "probable", "rejected"}:
            self._upsert_index(product, config, outcome)
        return outcome

    # persistence ------------------------------------------------------------------------------
    def _upsert_index(self, product: dict, config: dict, outcome: Outcome, *, source: str = "auto",
                      note: str = "") -> None:
        page = outcome.page
        status = {"exact": "verified", "probable": "probable", "rejected": "rejected"}[outcome.state]
        stamp = _iso(self._now())
        with self.store._connect() as connection, connection:
            connection.execute("""
                INSERT INTO competitor_product_urls(competitor_id,ean,url,model_code,color_code,caliber,ean_published,
                    match_status,status,verified_at,last_price,last_availability,last_ok_at,failures,source,note)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)
                ON CONFLICT(competitor_id,ean,url) DO UPDATE SET match_status=excluded.match_status,
                    status=excluded.status,verified_at=excluded.verified_at,caliber=excluded.caliber,
                    ean_published=excluded.ean_published,failures=0,
                    note=CASE WHEN excluded.note<>'' THEN excluded.note ELSE competitor_product_urls.note END,
                    last_price=COALESCE(excluded.last_price,competitor_product_urls.last_price),
                    last_availability=CASE WHEN excluded.last_price IS NULL THEN competitor_product_urls.last_availability
                                           ELSE excluded.last_availability END,
                    last_ok_at=COALESCE(excluded.last_ok_at,competitor_product_urls.last_ok_at)
            """, (config["competitor_id"], product["ean"], outcome.url, product["model"], product["color"],
                  page.caliber, page.ean_published, outcome.match_status, status, stamp, page.price,
                  page.availability, stamp if outcome.state == "exact" and page.has_price else None, source, note))

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
        market, market_note, conflict = resolve_market(page, config) if page else ("", "", False)
        if conflict:
            status = "manual_review"
        priced = page is not None and page.has_price and status != "rejected"
        tax = "" if page is None or page.includes_tax is None else ("sí" if page.includes_tax else "no")
        return {
            "EAN": product["ean"], "Competidor": config["name"], "Precio": page.price if priced else "",
            "Precio anterior": page.previous_price if priced and page.previous_price else "",
            "Moneda": page.currency if page and page.currency else config["currency"],
            "Mercado": market or "UNKNOWN",
            "URL": outcome.url, "Disponibilidad": page.availability if page else "unknown",
            "Estado coincidencia": status, "Método": outcome.method or "web",
            "Evidencia": (outcome.detail or "Ficha pública verificada") + " | " + market_note, "Fecha consulta": stamp,
            "Marca": page.brand if page else "", "Modelo": product["model"] if outcome.state == "exact" else "",
            "Color": page.color if page else "", "Calibre": page.caliber if page else "",
            "EAN publicado": page.ean_published if page else "", "Título": page.title if page else "",
            "Identificadores": page.identifiers if page else [], "Ambigua": bool(page and page.ambiguous),
            "IVA incluido": tax, "Tipo IVA": "" if page is None or page.vat_rate is None else page.vat_rate,
        }

    def record(self, outcome: Outcome) -> str:
        """Store a located page through the existing importer. Returns '' when stored, else the reason."""
        if outcome.page is None or outcome.state not in {"exact", "probable", "rejected"}:
            return "Nada que registrar"
        config, product = self.config(outcome.competitor_id), self._product(outcome.ean)
        frame = pd.DataFrame([self.frame_row(product, config, outcome, _iso(self._now()))])
        report = self.store.import_offers(frame)
        if report.rejected:
            return report.rejected[0]
        with self.store._connect() as connection, connection:
            connection.execute("UPDATE competitors SET connectivity='web' WHERE id=?", (outcome.competitor_id,))
        return ""

    # runs -------------------------------------------------------------------------------------
    def _pilot_guard(self, ean: str | None, competitor_id: str) -> None:
        from price_pilot import PilotError, require_pilot_access

        try:
            require_pilot_access(self.store, ean, competitor_id)
        except PilotError as exc:
            raise WebSearchError(str(exc)) from exc

    def run(self, eans: Iterable[str], competitor_ids: Iterable[str] | None = None, *, record: bool = True
            ) -> list[Outcome]:
        """Manual, bounded search. Competitors are processed one after another; each holds the shared lock."""
        self.ensure_config()
        with self.store._connect() as connection:
            ids = list(competitor_ids or [r[0] for r in connection.execute(
                "SELECT id FROM competitors WHERE active=1 ORDER BY name")])
        eans = list(eans)
        for cid in ids:
            for ean in eans:
                self._pilot_guard(ean, cid)
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
                            "Estado": "", "Ficha": "", "Origen": "",
                            "Ficha registrada": self.url_state(product["ean"], competitor_id)["text"]}
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
        if connection.execute("""SELECT 1 FROM competitor_product_urls WHERE competitor_id=? AND ean=?
                                 AND status IN ('verified','probable','pending_verification') LIMIT 1""",
                              (config["competitor_id"], ean)).fetchone():
            return "Ficha registrada: falta comprobar el precio"
        if config["access_status"] in {"blocked", "robots_restricted", "pending_authorization"}:
            return "Competidor no consultable"
        return "Sin datos"


def _same_site(url: str, domain: str) -> bool:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    return host == domain or host.endswith("." + domain)


def validate_competitor_url(url: str, domain: str) -> str:
    """Return the cleaned URL or raise: HTTPS only, the competitor's own domain, no credentials, no custom port,
    no IP literal or local name. Fragments are dropped. DNS targets are checked again when the page is fetched."""
    raw = (url or "").strip()
    if not raw or len(raw) > MAX_URL_LENGTH or any(ch.isspace() or ord(ch) < 32 for ch in raw):
        raise WebSearchError("La URL no es válida")
    parts = urlparse(raw)
    if parts.scheme != "https":
        raise WebSearchError("La URL debe usar HTTPS")
    try:
        host, port = (parts.hostname or "").lower(), parts.port
    except ValueError:
        raise WebSearchError("La URL no es válida") from None
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise WebSearchError("La URL no puede incluir credenciales")
    if port not in (None, 443):
        raise WebSearchError("La URL no puede usar un puerto no estándar")
    if not host or "." not in host or host.endswith(".local") or re.fullmatch(r"[\d.]+|[0-9a-f:]+", host):
        raise WebSearchError("La URL debe usar el nombre de dominio del competidor, no una dirección IP o local")
    if not _same_site(raw, domain):
        raise WebSearchError(f"La URL no pertenece al dominio autorizado del competidor ({domain})")
    return parts._replace(netloc=host, fragment="").geturl()


MAX_URL_LENGTH = 500


def validate_search_template(template: str, domain: str) -> None:
    try:
        fields = []
        for _, name, spec, conversion in Formatter().parse(template):
            if name is not None:
                if name not in {"ean", "model", "color", "query"} or spec or conversion:
                    raise WebSearchError("Variables permitidas: {ean}, {model}, {color}, {query}")
                fields.append(name)
        if not fields or "{" in urlparse(template).netloc or "}" in urlparse(template).netloc:
            raise WebSearchError("La plantilla necesita variables solo en la ruta o consulta, no en el dominio")
        validate_competitor_url(template.format(ean="8056597875349", model="PR27ZS", color="16K08Z", query="x"), domain)
    except ValueError as exc:
        raise WebSearchError(f"Plantilla de búsqueda no válida: {exc}") from exc


def product_search_urls(product: dict, config: dict, modes: list[str] | None = None) -> list[tuple[str, str]]:
    mode = config.get("search_mode", "auto")
    if mode == "known":
        return []
    if not config.get("search_verified"):
        return []
    template = config["search_url_template"]
    validate_search_template(template, config["domain"])
    preferred = config.get("search_preferred", "ean")
    methods = modes if modes is not None else (
        [mode] if mode != "auto" else [preferred, *config.get("search_alternatives", "").split(",")])
    model = " ".join(tokens(product["model"]))
    color = key(product["color"])
    values = {"ean": normalize_gtin(product["ean"]), "model": model, "color": color}
    urls = []
    for method in dict.fromkeys(methods):
        if method not in ("ean", "model_color"):
            continue
        # EAN-only / model-only templates cannot silently change the requested search strategy.
        fields = {name for _, name, _, _ in Formatter().parse(template) if name}
        if method == "ean" and fields - {"ean", "query"} or method == "model_color" and "ean" in fields:
            raise WebSearchError("La plantilla no es compatible con el método seleccionado; utilice {query}")
        query = values["ean"] if method == "ean" else " ".join(
            (" ".join(tokens(product["brand"])), model, color))
        expanded = template.format(**{k: quote(v, safe="") for k, v in {**values, "query": query}.items()})
        urls.append((method, validate_competitor_url(expanded, config["domain"])))
    return urls
LOCATION_TEXT = {"none": "Sin ficha", "candidate": "URL candidata", "verified": "Ficha verificada"}
EXTRACTION_TEXT = {"untested": "Sin probar", "verified": "Verificada con datos reales", "incomplete": "Incompleta",
                   "failed": "Fallida"}
OPERABILITY_TEXT = {"pending_review": "Pendiente de revisión de condiciones", "authorized": "Autorizado",
                    "blocked": "Bloqueado", "not_operational": "No operativo (robots.txt)"}
CURRENT_URL_STATES = {"verified", "probable", "pending_verification"}
URL_STATE_TEXT = {
    "auto": "Ficha localizada automáticamente",
    "pending": "URL aportada por el usuario, pendiente de verificar",
    "candidate": "Ficha candidata conocida, pendiente de verificar",
    "verified_user": "URL verificada y guardada",
    "wrong": "Coincidencia incorrecta",
    "blocked": "Acceso bloqueado",
    "probable": "Coincidencia probable: pendiente de revisión",
    "relocation": "La ficha ya no responde: facilite la nueva URL",
    "none": "Sin ficha registrada",
}


def page_state_key(row: dict, blocked: bool) -> str:
    status = row["status"]
    if status == "verified":
        return "auto" if row["source"] == "auto" else "verified_user"
    if status == "pending_verification":
        return "blocked" if blocked else ("candidate" if row["source"] == "known_urls" else "pending")
    return {"probable": "probable", "needs_relocation": "relocation", "rejected": "wrong"}.get(status, "none")


@dataclass
class Submission:
    state: str  # verified | needs_confirmation | saved_for_review | rejected | blocked | not_allowed | invalid |
    #             duplicate | already_registered | needs_review | error
    message: str
    url: str = ""
    outcome: Outcome | None = None
    reason: str = ""  # key of FAILURES
    action: str = ""  # url | review | wait | authorization | retry | none


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
        config = self.search.config(competitor_id)
        found: list[Observation] = []
        for ean in eans:
            outcome = self.search.locate(ean, competitor_id)
            if outcome.state == "blocked":
                raise ConnectorBlocked(outcome.detail)
            if outcome.state in {"error", "not_allowed"}:
                raise ConnectorError(outcome.detail)
            if outcome.state == "exact" and outcome.priced:
                page = outcome.page
                market, market_note, conflict = resolve_market(page, config)
                found.append(Observation(
                    ean=outcome.ean, price=str(page.price), currency=page.currency, availability=page.availability,
                    url=outcome.url, previous_price="" if page.previous_price is None else str(page.previous_price),
                    match_status="manual_review" if conflict else "exact_match", method=outcome.method,
                    evidence=outcome.detail + " | " + market_note,
                    tax_included=page.includes_tax, ean_published=page.ean_published, caliber=page.caliber,
                    color=page.color, brand=page.brand, title=page.title, market=market or "UNKNOWN",
                    identifiers=tuple(page.identifiers), ambiguous=page.ambiguous))
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
