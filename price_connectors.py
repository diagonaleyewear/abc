"""Competitor price connectors. Only authorised, verifiable access mechanisms live here.

A connector never scrapes: the available one reads a price feed/CSV that the competitor (or a
partner) has authorised us to use. Credentials are never stored; sources carrying them are rejected.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
import io
import ipaddress
import os
from pathlib import Path
import socket
from typing import Callable, Protocol
from urllib.parse import parse_qsl, urlparse

from price_comparator import CLOUD_DATA_DIR, normalize_gtin

MAX_FEED_BYTES = 20 * 1024 * 1024
FEED_TIMEOUT_SECONDS = 20
BLOCK_STATUSES = {401, 403, 407, 429}
SENSITIVE_QUERY_KEYS = ("token", "key", "secret", "password", "pwd", "auth", "signature", "sig")
REQUIRED_FEED_COLUMNS = {"ean", "precio", "moneda", "disponibilidad", "url"}
FEED_COLUMN_ALIASES = {
    "ean": "ean", "gtin": "ean", "upc": "ean",
    "precio": "precio", "price": "precio",
    "precio promocional": "promo", "sale price": "promo",
    "precio anterior": "previous", "coste envío": "shipping", "coste envio": "shipping",
    "moneda": "moneda", "currency": "moneda",
    "disponibilidad": "disponibilidad", "availability": "disponibilidad",
    "url": "url", "link": "url", "mercado": "mercado", "market": "mercado",
}


class ConnectorError(RuntimeError):
    """Transient or data problem; the job may be retried."""


class ConnectorBlocked(ConnectorError):
    """The provider refused or throttled us. The connector must be suspended, never retried blindly."""


@dataclass(frozen=True)
class Observation:
    ean: str
    price: str
    currency: str
    availability: str
    url: str
    promotional_price: str = ""
    previous_price: str = ""
    shipping_cost: str = ""
    market: str = ""


class Connector(Protocol):
    name: str
    label: str
    batch_size: int

    def verify(self, source: str) -> None:
        """Prove the access mechanism works without storing anything. Raise ConnectorError otherwise."""

    def fetch(self, source: str, eans: list[str]) -> list[Observation]:
        """One provider request for up to `batch_size` EANs. Unlisted EANs are simply absent."""


def validate_source(source: str) -> str:
    """Return 'https' or 'file'; reject anything that could carry credentials or reach internal hosts."""
    text = (source or "").strip()
    if not text:
        raise ConnectorError("Indique la URL HTTPS del feed autorizado o el nombre del fichero CSV")
    parsed = urlparse(text)
    if parsed.scheme == "":
        if "/" in text or "\\" in text or text.startswith("."):
            raise ConnectorError("El fichero debe indicarse solo por su nombre, dentro de la carpeta de feeds")
        return "file"
    if parsed.scheme != "https" or not parsed.hostname:
        raise ConnectorError("El feed debe usar HTTPS")
    if parsed.username or parsed.password:
        raise ConnectorError("La fuente no puede incluir credenciales; no se almacenan en la base")
    if any(any(word in key.lower() for word in SENSITIVE_QUERY_KEYS) for key, _ in parse_qsl(parsed.query)):
        raise ConnectorError("La fuente no puede incluir tokens ni claves en la URL")
    return "https"


def _assert_public_host(host: str) -> None:
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    except OSError as exc:
        raise ConnectorError("No se pudo resolver el dominio del feed") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ConnectorError("El feed apunta a una dirección interna, no permitida")


def _http_get(url: str) -> bytes:
    import requests

    _assert_public_host(urlparse(url).hostname or "")
    try:
        response = requests.get(url, timeout=FEED_TIMEOUT_SECONDS, stream=True, allow_redirects=False,
                                headers={"User-Agent": "Cloud-PriceComparator/1.0 (authorised feed)"})
    except requests.RequestException as exc:
        raise ConnectorError(f"No se pudo descargar el feed: {type(exc).__name__}") from exc
    with response:
        if response.status_code in BLOCK_STATUSES:
            raise ConnectorBlocked(f"El proveedor rechazó o limitó la petición (HTTP {response.status_code})")
        if response.status_code >= 400 or 300 <= response.status_code < 400:
            raise ConnectorError(f"El feed respondió HTTP {response.status_code}")
        body = bytearray()
        for chunk in response.iter_content(65536):
            body.extend(chunk)
            if len(body) > MAX_FEED_BYTES:
                raise ConnectorError("El feed supera el tamaño máximo permitido")
        return bytes(body)


def feeds_dir() -> Path:
    configured = os.getenv("PRICE_COMPARATOR_FEEDS_DIR")
    if configured:
        return Path(configured)
    return (CLOUD_DATA_DIR if CLOUD_DATA_DIR.is_dir() else Path(__file__).parent / "masterdata_data") / "competitor_feeds"


class FeedConnector:
    """Authorised price feed: a CSV served over HTTPS or dropped in the feeds folder."""

    name = "feed_csv"
    label = "Feed CSV autorizado"
    batch_size = 500

    def __init__(self, get: Callable[[str], bytes] = _http_get, directory: Path | None = None) -> None:
        self._get = get
        self._directory = directory

    def _read(self, source: str) -> bytes:
        if validate_source(source) == "https":
            return self._get(source)
        base = (self._directory or feeds_dir()).resolve()
        path = (base / source).resolve()
        if base not in path.parents or not path.is_file():
            raise ConnectorError("El fichero del feed no existe en la carpeta de feeds")
        if path.stat().st_size > MAX_FEED_BYTES:
            raise ConnectorError("El feed supera el tamaño máximo permitido")
        return path.read_bytes()

    @staticmethod
    def _rows(payload: bytes) -> list[dict[str, str]]:
        try:
            text = payload.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ConnectorError("El feed debe estar codificado en UTF-8") from exc
        sample = text[:4096]
        delimiter = ";" if sample.count(";") > sample.count(",") else ","
        reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
        mapped = {}
        for field in reader.fieldnames or []:
            key = FEED_COLUMN_ALIASES.get(field.strip().lower())
            if key:
                mapped[field] = key
        missing = REQUIRED_FEED_COLUMNS - set(mapped.values())
        if missing:
            raise ConnectorError(f"Faltan columnas en el feed: {', '.join(sorted(missing))}")
        return [{mapped[k]: (v or "").strip() for k, v in row.items() if k in mapped} for row in reader]

    def verify(self, source: str) -> None:
        self._rows(self._read(source))

    def fetch(self, source: str, eans: list[str]) -> list[Observation]:
        wanted = set(eans)
        found: dict[str, Observation] = {}
        for row in self._rows(self._read(source)):
            try:
                ean = normalize_gtin(row.get("ean"))
            except ValueError:
                continue
            if ean in wanted and ean not in found:
                found[ean] = Observation(
                    ean=ean, price=row.get("precio", ""), currency=row.get("moneda", "").upper(),
                    availability=row.get("disponibilidad", "").lower(), url=row.get("url", ""),
                    promotional_price=row.get("promo", ""), previous_price=row.get("previous", ""),
                    shipping_cost=row.get("shipping", ""), market=row.get("mercado", ""))
        return list(found.values())


CONNECTORS: dict[str, Connector] = {FeedConnector.name: FeedConnector()}
