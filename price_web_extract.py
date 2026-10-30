"""Extraction of product facts from a public product page (no network access here).

Priority: JSON-LD ``Product``/``Offer`` > page metadata (OpenGraph/itemprop) > configured CSS selector.
Nothing is invented: a field the page does not publish stays empty, and the VAT treatment stays
unknown unless the page states it explicitly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
import json
import re
from typing import Any, Iterable
from urllib.parse import urlparse

from price_comparator import normalize_gtin
from price_references import extract_caliber

_LD_JSON = re.compile(r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.I | re.S)
_AVAILABLE = {"instock", "limitedavailability", "onlineonly", "instoreonly"}
_UNAVAILABLE = {"outofstock", "soldout", "discontinued"}
_VOID = {"meta", "link", "img", "br", "hr", "input", "source", "area", "base", "col", "embed", "wbr"}


@dataclass
class ProductPage:
    url: str
    method: str = ""
    structured_type: str = ""
    title: str = ""
    brand: str = ""
    color: str = ""
    caliber: str = ""
    ean_published: str = ""
    identifiers: list[str] = field(default_factory=list)
    price: float | None = None
    previous_price: float | None = None
    currency: str = ""
    availability: str = "unknown"
    includes_tax: bool | None = None
    vat_rate: float | None = None
    market: str = ""
    ambiguous: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def has_price(self) -> bool:
        return self.price is not None and bool(self.currency)

    def candidate(self) -> dict[str, str]:
        """The listing as seen by ``price_references.classify``."""
        return {"brand": self.brand, "color": self.color, "caliber": self.caliber,
                "ean_published": self.ean_published, "title": " ".join([self.title, *self.identifiers]).strip()}


def parse_price(value: Any) -> float | None:
    """'239', '239.00', '239,00', '1.234,56', '1,234.56' and '239,00 €' -> float; anything else -> None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value >= 0 else None
    text = re.sub(r"[^\d.,]", "", str(value))
    if not text or not re.search(r"\d", text):
        return None
    if "," in text and "." in text:
        decimal = "," if text.rfind(",") > text.rfind(".") else "."
        text = text.replace("." if decimal == "," else ",", "").replace(decimal, ".")
    elif "," in text:
        head, _, tail = text.rpartition(",")
        text = f"{head}.{tail}" if len(tail) in {1, 2} else text.replace(",", "")
    elif text.count(".") > 1:
        text = text.replace(".", "")
    try:
        result = float(text)
    except ValueError:
        return None
    return result if result >= 0 else None


def availability_of(value: Any) -> str:
    token = re.sub(r"[^a-z]", "", str(value or "").rsplit("/", 1)[-1].lower())
    if token in _AVAILABLE:
        return "available"
    if token in _UNAVAILABLE:
        return "unavailable"
    return "unknown"


def _truthy(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return True if text in {"true", "1", "yes", "sí", "si"} else False if text in {"false", "0", "no"} else None


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else [] if value is None else [value]


def _nodes(data: Any) -> Iterable[dict]:
    for item in _as_list(data):
        if isinstance(item, dict):
            yield item
            yield from _nodes(item.get("@graph"))


def _is_type(node: dict, name: str) -> bool:
    return any(str(t).rsplit("/", 1)[-1] == name for t in _as_list(node.get("@type")))


def _name(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("name")
    return str(value).strip() if value is not None else ""


def _gtins(node: dict) -> list[str]:
    found: list[str] = []
    for key in ("gtin13", "gtin12", "gtin14", "gtin8", "gtin", "ean"):
        for raw in _as_list(node.get(key)):
            try:
                found.append(normalize_gtin(raw))
            except ValueError:
                continue
    return list(dict.fromkeys(found))


def _offer_facts(node: dict) -> dict[str, Any]:
    """price/currency/availability/VAT from the offers of one Product node; several different prices = ambiguous."""
    offers: list[dict] = []
    for offer in _as_list(node.get("offers")):
        if isinstance(offer, dict):
            inner = [o for o in _as_list(offer.get("offers")) if isinstance(o, dict)]
            offers.extend(inner or [offer])
    facts: dict[str, Any] = {"ambiguous": False, "notes": []}
    seen = []
    for offer in offers:
        price, previous, tax = None, None, _truthy(offer.get("valueAddedTaxIncluded"))
        for item in _as_list(offer.get("priceSpecification")):
            if not isinstance(item, dict):
                continue
            kind = str(item.get("priceType") or "").rsplit("/", 1)[-1].lower()
            value = parse_price(item.get("price"))
            if kind in {"listprice", "strikethroughprice", "srp", "msrp"}:
                previous = previous or value
            elif value is not None and price is None:
                price = value
            if item.get("valueAddedTaxIncluded") is not None and tax is None:
                tax = _truthy(item.get("valueAddedTaxIncluded"))
        if offer.get("price") is not None:
            price = parse_price(offer.get("price"))
        if price is None and _is_type(offer, "AggregateOffer"):
            facts["notes"].append("AggregateOffer sin precio único: no se usa lowPrice")
        seen.append((price, str(offer.get("priceCurrency") or "").upper(), availability_of(offer.get("availability")),
                     tax, previous))
    distinct = {item[:3] for item in seen if item[0] is not None}
    if len(distinct) > 1:
        facts["ambiguous"] = True
        facts["notes"].append("La ficha publica varias ofertas distintas")
    chosen = next((item for item in seen if item[0] is not None), None)
    if chosen:
        facts.update(price=chosen[0], currency=chosen[1], availability=chosen[2], includes_tax=chosen[3],
                     previous_price=chosen[4])
    elif seen:
        facts["availability"] = seen[0][2]
    return facts


def _from_json_ld(html: str, url: str) -> ProductPage | None:
    products: list[dict] = []
    for block in _LD_JSON.findall(html):
        try:
            data = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        for node in _nodes(data):
            if _is_type(node, "Product"):
                products.append(node)
            elif _is_type(node, "ProductGroup"):
                products.extend(v for v in _as_list(node.get("hasVariant")) if isinstance(v, dict))
    if not products:
        return None
    page = ProductPage(url=url, method="json-ld", structured_type="schema.org/Product")
    path = urlparse(url).path.rstrip("/").lower()
    node = next((p for p in products if p.get("url") and urlparse(str(p["url"])).path.rstrip("/").lower() == path), None)
    if node is None:
        node = products[0]
        if len({tuple(_gtins(p)) or (str(p.get("sku")),) for p in products}) > 1:
            page.ambiguous = True
            page.notes.append("La ficha describe varias variantes: no se elige ninguna")
    page.title = _name(node.get("name"))
    page.brand = _name(node.get("brand"))
    page.color = _name(node.get("color"))
    gtins = _gtins(node)
    page.ean_published = gtins[0] if gtins else ""
    page.identifiers = [str(node[k]) for k in ("mpn", "sku") if node.get(k)]
    page.caliber = extract_caliber(page.title) or extract_caliber(urlparse(url).path.rsplit("/", 1)[-1])
    facts = _offer_facts(node)
    page.ambiguous |= facts["ambiguous"]
    page.notes += facts["notes"]
    page.price, page.currency = facts.get("price"), facts.get("currency", "")
    page.previous_price = facts.get("previous_price")
    page.availability = facts.get("availability", "unknown")
    page.includes_tax = facts.get("includes_tax")
    return page


class _Collector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title = ""
        self.lang = ""
        self.elements: list[dict[str, Any]] = []
        self.links: list[tuple[str, str]] = []
        self._stack: list[dict[str, Any]] = []
        self._in_title = False
        self._link: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        attrs = {k: v or "" for k, v in attrs}
        if tag == "html" and attrs.get("lang"):
            self.lang = attrs["lang"]
        if tag == "meta":
            name = attrs.get("property") or attrs.get("name") or attrs.get("itemprop")
            if name and "content" in attrs:
                self.meta.setdefault(name.lower(), attrs["content"])
        elif tag == "title":
            self._in_title = True
        if tag == "a" and attrs.get("href"):
            self._link = [attrs["href"], ""]
        if tag in _VOID:
            if attrs.get("itemprop") and "content" in attrs:
                self.meta.setdefault(attrs["itemprop"].lower(), attrs["content"])
            return
        element = {"tag": tag, "attrs": attrs, "text": ""}
        self.elements.append(element)
        self._stack.append(element)
        if attrs.get("itemprop") and "content" in attrs:
            self.meta.setdefault(attrs["itemprop"].lower(), attrs["content"])

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag == "a" and self._link:
            self.links.append((self._link[0], self._link[1].strip()))
            self._link = None
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index]["tag"] == tag:
                del self._stack[index:]
                break

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._link is not None:
            self._link[1] += data
        for element in self._stack[-6:]:
            element["text"] += data


_SELECTOR = re.compile(r"^(?P<tag>[a-z0-9]+)?(?P<id>#[\w-]+)?(?P<classes>(?:\.[\w-]+)*)(?P<attr>\[[^\]]+\])?$", re.I)


def css_text(parsed: _Collector, selector: str) -> str:
    """First text matching one compound selector: ``tag#id.class[attr=value]`` (no combinators)."""
    match = _SELECTOR.match(selector.strip()) if selector.strip() else None
    if not match:
        return ""
    classes = [c for c in match["classes"].split(".") if c]
    attr_name = attr_value = None
    if match["attr"]:
        attr_name, _, attr_value = match["attr"][1:-1].partition("=")
        attr_value = attr_value.strip("\"'")
    for element in parsed.elements:
        attrs = element["attrs"]
        if match["tag"] and element["tag"] != match["tag"].lower():
            continue
        if match["id"] and attrs.get("id") != match["id"][1:]:
            continue
        if any(c not in attrs.get("class", "").split() for c in classes):
            continue
        if attr_name and (attr_name not in attrs or (attr_value and attrs[attr_name] != attr_value)):
            continue
        return (attrs.get("content") or " ".join(element["text"].split()))
    return ""


def parse_html(html: str) -> _Collector:
    parsed = _Collector()
    parsed.feed(html)
    parsed.close()
    return parsed


def extract_product(html: str, url: str, *, price_selector: str = "", availability_selector: str = "",
                    tax_policy: str = "unknown") -> ProductPage | None:
    """Return what the page publishes, or None when it carries no product information at all."""
    page = _from_json_ld(html, url)
    parsed = parse_html(html)
    meta = parsed.meta
    if page is None:
        title = meta.get("og:title") or " ".join(parsed.title.split())
        amount = meta.get("product:price:amount") or meta.get("og:price:amount") or meta.get("price")
        if not (title or amount):
            return None
        page = ProductPage(url=url, method="metadatos", structured_type="OpenGraph/itemprop", title=title.strip())
        page.price = parse_price(amount)
        page.currency = (meta.get("product:price:currency") or meta.get("og:price:currency")
                         or meta.get("pricecurrency") or "").upper()
        page.availability = availability_of(meta.get("product:availability") or meta.get("og:availability")
                                            or meta.get("availability"))
        page.brand = meta.get("product:brand") or meta.get("brand", "")
        for key in ("gtin13", "gtin", "product:ean"):
            if meta.get(key):
                try:
                    page.ean_published = normalize_gtin(meta[key])
                except ValueError:
                    page.notes.append("EAN publicado no válido")
                break
        page.caliber = extract_caliber(page.title) or extract_caliber(urlparse(url).path.rsplit("/", 1)[-1])
    if price_selector and page.price is None:
        selected = parse_price(css_text(parsed, price_selector))
        if selected is not None:
            page.price, page.method = selected, "selector"
            page.notes.append("Precio tomado del selector configurado")
    if availability_selector and page.availability == "unknown":
        text = css_text(parsed, availability_selector).lower()
        if re.search(r"agotado|sin stock|no disponible|out of stock", text):
            page.availability = "unavailable"
        elif re.search(r"en stock|disponible|in stock|añadir al carrito", text):
            page.availability = "available"
    if page.includes_tax is None and tax_policy in {"included", "excluded"}:
        page.includes_tax = tax_policy == "included"
        page.notes.append(f"IVA según la política verificada del competidor ({tax_policy})")
    _infer_tax_from_pretax(page, meta)
    region = re.fullmatch(r"[a-z]{2}[-_]([A-Za-z]{2})", parsed.lang.strip())
    if region:  # the page declares its own locale, e.g. <html lang="es-ES">; the domain is never used
        page.market = region.group(1).upper()
    return page


_VAT_FACTORS = {1.21: 21.0, 1.10: 10.0, 1.04: 4.0}


def _infer_tax_from_pretax(page: ProductPage, meta: dict[str, str]) -> None:
    """The page publishes both the shown price and the pre-tax price: a ratio equal to a Spanish VAT rate proves
    the shown price includes VAT. Without that evidence the VAT treatment stays as it was (unknown)."""
    pretax = parse_price(meta.get("product:pretax_price:amount"))
    if page.price and pretax and page.includes_tax is None:
        ratio = page.price / pretax
        for factor, rate in _VAT_FACTORS.items():
            if abs(ratio - factor) / factor < 0.003:
                page.includes_tax, page.vat_rate = True, rate
                page.notes.append(f"IVA incluido: precio {page.price:g} = precio sin IVA publicado {pretax:g} × {factor}")
                return
