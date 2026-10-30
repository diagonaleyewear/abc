"""Simulated competitor site for the web finder tests: no real network, deterministic robots.txt and pages."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from price_comparator import PriceComparatorStore
from price_web import WebPriceSearch
from price_web_fetch import WebFetcher
from tests.test_price_observations import COMPETITORS, PRODUCTS

NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
BASE = "https://www.visiofactory.es"
ROBOTS_OK = "User-agent: *\nDisallow: /cart\nSitemap: https://www.visiofactory.es/sitemap.xml\n"

PRADA = {
    "8056597875349": {"model": "PR 27ZS", "color": "16K08Z", "caliber": "54-21", "price": 239, "stock": True,
                      "slug": "pr27zs-16k08z-54-21.html", "vytria": 312.0},
    "8056597877329": {"model": "PR 65ZS", "color": "ZVN09T", "caliber": "55-20", "price": 187, "stock": False,
                      "slug": "pr-65zs-zvn-09t-55-20.html", "vytria": 195.0},
    "8056597875226": {"model": "PR 26ZS", "color": "16K08Z", "caliber": "55-16", "price": 219, "stock": True,
                      "slug": "pr26zs-16k08z-55-16.html", "vytria": 273.0},
}


def url_of(slug: str) -> str:
    return f"{BASE}/gafas-de-sol/todas-las-solares/{slug}"


def page(*, name: str, price: float | str | None, gtin: str | None = None, stock: bool = True, lang: str | None = "es-ES",
         currency: str = "EUR", pretax: float | None = None, brand: str = "Prada", previous: float | None = None,
         extra_offer: dict | None = None) -> str:
    offer: dict = {"@type": "Offer", "priceCurrency": currency, "price": str(price),
                   "availability": "https://schema.org/" + ("InStock" if stock else "OutOfStock")}
    if previous:
        offer["priceSpecification"] = {"@type": "UnitPriceSpecification", "priceType": "https://schema.org/ListPrice",
                                       "price": str(previous), "priceCurrency": currency}
    product = {"@context": "https://schema.org/", "@type": "Product", "name": name, "sku": name,
               "brand": {"@type": "Brand", "name": brand}, "offers": [offer, extra_offer] if extra_offer else offer}
    if gtin:
        product["gtin13"] = gtin
    if price is None:
        del product["offers"]
    meta = f'<meta property="product:pretax_price:amount" content="{pretax}">' if pretax else ""
    html_tag = f'<html lang="{lang}">' if lang else "<html>"
    return (f"{html_tag}<head><title>{name}</title>{meta}"
            f'<script type="application/ld+json">{json.dumps(product)}</script></head><body><h1>{name}</h1></body></html>')


def prada_page(ean: str, **overrides) -> str:
    data = PRADA[ean]
    args = {"name": f"Prada {data['model']} {data['color']} {data['caliber']}", "price": data["price"], "gtin": ean,
            "stock": data["stock"], "pretax": round(data["price"] / 1.21, 6)}
    args.update(overrides)
    return page(**args)


class FakeSite:
    """URL -> (status, body). Counts every request so tests can prove that blocked hosts are left alone."""

    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, str]] = {f"{BASE}/robots.txt": (200, ROBOTS_OK)}
        self.calls: list[str] = []

    def add(self, url: str, body: str = "", status: int = 200) -> None:
        self.routes[url] = (status, body)

    def transport(self, url, headers, timeout):
        self.calls.append(url)
        if url not in self.routes:
            return 404, {}, b""
        status, body = self.routes[url]
        if isinstance(body, Exception):
            raise body
        return status, {}, body.encode("utf-8")

    def factory(self, config: dict) -> WebFetcher:
        return WebFetcher(transport=self.transport, pause_seconds=1, max_requests=config["max_requests"],
                          timeout=5, retries=config["retries"], sleep=lambda _s: None)


def make_search(tmp_path, site: FakeSite, *, with_vytria: bool = True) -> tuple[PriceComparatorStore, WebPriceSearch]:
    from tests.test_price_review_fixes import _vytria

    store = PriceComparatorStore(tmp_path / "web.sqlite3")
    store.import_products(PRODUCTS)
    store.import_competitors(COMPETITORS)
    if with_vytria:
        for ean, data in PRADA.items():
            _vytria(store, ean, data["vytria"])
    search = WebPriceSearch(store, fetcher_factory=site.factory, clock=lambda: NOW)
    return store, search


def competitor_id(store: PriceComparatorStore, name: str = "Visio Factory") -> str:
    return next(r["id"] for r in store.competitors().to_dict("records") if r["name"] == name)


def index_url(store: PriceComparatorStore, cid: str, ean: str, url: str) -> None:
    with store._connect() as connection, connection:
        connection.execute("""INSERT OR IGNORE INTO competitor_product_urls(competitor_id,ean,url,match_status,status,
            verified_at) VALUES(?,?,?,?,?,?)""", (cid, ean, url, "manual_review", "probable", "2026-10-30T13:45:00+00:00"))


__all__ = []
