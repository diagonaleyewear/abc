"""Polite, permission-aware HTTP access to public competitor pages.

Rules enforced here (not left to callers): robots.txt is honoured for every URL, requests are paced per
host and capped per run, the agent identifies itself, and a 401/403/407/429 response or a CAPTCHA
stops all further requests to that host. There is no proxy, IP rotation, CAPTCHA solving or evasion.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import gzip
import os
import re
import time
from typing import Callable
from urllib.parse import urljoin, urlparse

from price_connectors import BLOCK_STATUSES, ConnectorBlocked, ConnectorError, _assert_public_host

AGENT_TOKEN = "Cloud-PriceComparator"
MAX_PAGE_BYTES = 3 * 1024 * 1024
MAX_SITEMAP_BYTES = 10 * 1024 * 1024
MAX_REDIRECTS = 3
_CAPTCHA = re.compile(r"captcha|are you a robot|verify you are human|cf-challenge|access denied|"
                      r"unusual traffic|just a moment\.\.\.", re.I)
Transport = Callable[[str, dict, float], "tuple[int, dict, bytes]"]


def user_agent() -> str:
    contact = os.getenv("PRICE_WEB_CONTACT", "").strip()
    return f"{AGENT_TOKEN}/1.0 (comparador de precios de Diagonal Eyewear" + (f"; {contact})" if contact else ")")


class RobotsDenied(ConnectorError):
    """robots.txt does not allow this URL for our agent (or could not be read)."""


class PageGone(ConnectorError):
    """404/410: the stored URL no longer exists and must be located again."""


@dataclass
class RobotsRules:
    groups: list[tuple[list[str], list[tuple[bool, str]]]] = field(default_factory=list)
    sitemaps: list[str] = field(default_factory=list)
    allow_all: bool = False
    deny_all: bool = False

    @classmethod
    def parse(cls, text: str) -> "RobotsRules":
        rules = cls()
        agents: list[str] = []
        entries: list[tuple[bool, str]] = []
        collecting_agents = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            name, value = (part.strip() for part in line.split(":", 1))
            name = name.lower()
            if name == "user-agent":
                if not collecting_agents and agents:
                    rules.groups.append((agents, entries))
                    agents, entries = [], []
                agents.append(value.lower())
                collecting_agents = True
                continue
            collecting_agents = False
            if name == "sitemap" and value:
                rules.sitemaps.append(value)
            elif name in {"allow", "disallow"} and agents:
                entries.append((name == "allow", value))
        if agents:
            rules.groups.append((agents, entries))
        return rules

    def _entries(self) -> list[tuple[bool, str]]:
        token = AGENT_TOKEN.lower()
        specific = [entries for agents, entries in self.groups if any(a != "*" and a in token for a in agents)]
        generic = [entries for agents, entries in self.groups if "*" in agents]
        return [item for group in (specific or generic) for item in group]

    @staticmethod
    def _matches(pattern: str, target: str) -> bool:
        if not pattern:
            return False
        regex = re.escape(pattern).replace(r"\*", ".*")
        regex = regex[:-2] + "$" if regex.endswith(r"\$") else regex
        return re.match(regex, target) is not None

    def allowed(self, url: str) -> bool:
        if self.deny_all:
            return False
        if self.allow_all:
            return True
        parsed = urlparse(url)
        target = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
        best_length, verdict = -1, True
        for allow, pattern in self._entries():
            if self._matches(pattern, target) and (len(pattern) > best_length
                                                   or (len(pattern) == best_length and allow)):
                best_length, verdict = len(pattern), allow
        return verdict


@dataclass
class Fetched:
    url: str
    status: int
    content: bytes

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


def _requests_transport(url: str, headers: dict, timeout: float) -> tuple[int, dict, bytes]:
    import requests

    _assert_public_host(urlparse(url).hostname or "")
    try:
        response = requests.get(url, headers=headers, timeout=timeout, stream=True, allow_redirects=False)
    except requests.RequestException as exc:
        raise ConnectorError(f"Error de conexión: {type(exc).__name__}") from exc
    with response:
        body = bytearray()
        for chunk in response.iter_content(65536):
            body.extend(chunk)
            if len(body) > MAX_SITEMAP_BYTES:
                raise ConnectorError("La respuesta supera el tamaño máximo permitido")
        return response.status_code, dict(response.headers), bytes(body)


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


class WebFetcher:
    """One instance per competitor run: owns the request budget, the pacing and the block memory."""

    def __init__(self, *, transport: Transport = _requests_transport, pause_seconds: float = 3.0,
                 max_requests: int = 10, timeout: float = 15.0, retries: int = 1,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> None:
        self._transport, self._sleep, self._clock = transport, sleep, clock
        self.pause_seconds, self.max_requests, self.timeout, self.retries = pause_seconds, max_requests, timeout, retries
        self.requests = 0
        self.blocked: dict[str, str] = {}
        self._robots: dict[str, RobotsRules] = {}
        self._robots_state: dict[str, str] = {}
        self._last_request = 0.0

    def _request(self, url: str, *, max_bytes: int) -> tuple[int, dict, bytes]:
        host = _host(url)
        if host in self.blocked:
            raise ConnectorBlocked(f"Consultas detenidas en {host}: {self.blocked[host]}")
        if self.requests >= self.max_requests:
            raise ConnectorError("Límite de consultas de la ejecución alcanzado")
        headers = {"User-Agent": user_agent(), "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
                   "Accept-Language": "es-ES,es;q=0.9"}
        attempt = 0
        while True:
            wait = self.pause_seconds - (self._clock() - self._last_request)
            if self._last_request and wait > 0:
                self._sleep(wait)
            self.requests += 1
            self._last_request = self._clock()
            try:
                status, response_headers, body = self._transport(url, headers, self.timeout)
            except ConnectorBlocked:
                raise
            except ConnectorError:
                attempt += 1
                if attempt > self.retries or self.requests >= self.max_requests:
                    raise
                continue
            if status in BLOCK_STATUSES:
                self.blocked[host] = f"HTTP {status}"
                raise ConnectorBlocked(f"El sitio rechazó o limitó la petición (HTTP {status}); no se reintenta")
            if len(body) > max_bytes:
                raise ConnectorError("La respuesta supera el tamaño máximo permitido")
            return status, response_headers, body

    def rules(self, url: str) -> RobotsRules:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._robots:
            status, _, body = self._request(origin + "/robots.txt", max_bytes=MAX_PAGE_BYTES)
            if status == 200:
                rules, state = RobotsRules.parse(body.decode("utf-8", errors="replace")), "robots.txt leído"
            elif status in {404, 410}:
                rules, state = RobotsRules(allow_all=True), "sin robots.txt (sin restricciones publicadas)"
            else:
                rules, state = RobotsRules(deny_all=True), f"robots.txt no disponible (HTTP {status}): se asume denegado"
            self._robots[origin], self._robots_state[origin] = rules, state
        return self._robots[origin]

    def robots_state(self, url: str) -> str:
        parsed = urlparse(url)
        return self._robots_state.get(f"{parsed.scheme}://{parsed.netloc}", "")

    def get(self, url: str, *, max_bytes: int = MAX_PAGE_BYTES) -> Fetched:
        if urlparse(url).scheme != "https":
            raise ConnectorError("Solo se consultan páginas HTTPS")
        origin_host = _host(url)
        for _ in range(MAX_REDIRECTS + 1):
            if not self.rules(url).allowed(url):
                raise RobotsDenied("robots.txt no permite consultar esta ruta")
            status, headers, body = self._request(url, max_bytes=max_bytes)
            if status in {301, 302, 303, 307, 308}:
                target = urljoin(url, headers.get("Location") or headers.get("location") or "")
                if _host(target) != origin_host:
                    raise ConnectorError("Redirección a otro dominio: no se sigue")
                url = target
                continue
            if status in {404, 410}:
                raise PageGone(f"La página ya no existe (HTTP {status})")
            if status >= 400:
                raise ConnectorError(f"El sitio respondió HTTP {status}")
            if body[:2] == b"\x1f\x8b":
                body = gzip.decompress(body)[:max_bytes]
            if _CAPTCHA.search(body[:20000].decode("utf-8", errors="ignore")) and b"application/ld+json" not in body:
                self.blocked[origin_host] = "CAPTCHA o control antibot"
                raise ConnectorBlocked("El sitio presenta un control antibot/CAPTCHA; no se intenta superarlo")
            return Fetched(url, status, body)
        raise ConnectorError("Demasiadas redirecciones")
