"""Pure grouping, filtering and paging logic for the image library.

Works on the local catalog records only: no filesystem, Odoo or provider access.
Views come exclusively from the stored classification (`record.view`); nothing is
reclassified here, so the Kering positional rule stays where it is applied.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import ceil

from image_repository import ImageRecord

# Stored view -> label. "perspectiva" is V2 only when it was really identified upstream.
CORE_VIEWS = (("frontal", "V1"), ("perspectiva", "V2"), ("lateral", "V3"))
VIEW_LABELS = {**dict(CORE_VIEWS), "detalle": "Detalle"}
VIEW_ORDER = {view: index for index, view in enumerate(("frontal", "perspectiva", "lateral", "detalle"))}
VIEW_NAMES = {
    "frontal": "V1 frontal", "perspectiva": "V2", "lateral": "V3 lateral", "detalle": "Detalle",
}
VIEW_FILTERS = {
    "Todas": None, "V1": "frontal", "V2": "perspectiva", "V3": "lateral",
    "Detalle": "detalle", "Originales": "originales",
}
AVAILABILITY_FILTERS = ("Todos", "Vistas completas (V1, V2, V3)", "Con vistas pendientes")
PAGE_SIZES = (12, 24, 48)
ALL = "Todos"


@dataclass(frozen=True)
class ProductGroup:
    ean: str
    model: str
    reference: str
    brand: str
    records: tuple[ImageRecord, ...]
    cover: ImageRecord
    latest: str

    @property
    def providers(self) -> tuple[str, ...]:
        return tuple(sorted({r.provider for r in self.records if r.provider}))

    @property
    def markets(self) -> tuple[str, ...]:
        return tuple(sorted({r.market for r in self.records if r.market}))

    @property
    def present_views(self) -> frozenset[str]:
        return frozenset(r.view for r in self.records)

    @property
    def missing_core(self) -> tuple[str, ...]:
        return tuple(label for view, label in CORE_VIEWS if view not in self.present_views)

    @property
    def title(self) -> str:
        return self.model or self.reference or f"EAN {self.ean}"


@dataclass(frozen=True)
class LibraryFilters:
    query: str = ""
    provider: str = ALL
    market: str = ALL
    view: str = "Todas"
    availability: str = ALL

    def signature(self) -> tuple:
        return (self.query, self.provider, self.market, self.view, self.availability)


@dataclass(frozen=True)
class Page:
    items: tuple
    number: int
    pages: int
    total: int
    first: int
    last: int


def view_label(view: str) -> str:
    return VIEW_LABELS.get(view, "Sin clasificar")


def view_name(view: str) -> str:
    return VIEW_NAMES.get(view, "Sin clasificar")


def market_label(market: str) -> str:
    return market or "Original"


def record_sort_key(record: ImageRecord) -> tuple:
    return (VIEW_ORDER.get(record.view, len(VIEW_ORDER)), bool(record.market), record.market, record.name)


def _cover(records: list[ImageRecord]) -> ImageRecord:
    """Representative image: the original V1 if stored, otherwise the first view in order."""
    return min(records, key=lambda r: (bool(r.market), VIEW_ORDER.get(r.view, len(VIEW_ORDER)), r.name))


def _meta(records: list[ImageRecord], *keys: str) -> str:
    for record in records:
        for key in keys:
            value = str(record.metadata.get(key, "") or "").strip()
            if value:
                return value
    return ""


def group_records(records: list[ImageRecord]) -> list[ProductGroup]:
    """One group per EAN (kept as text: leading zeros preserved), newest first."""
    by_ean: dict[str, list[ImageRecord]] = {}
    for record in records:
        by_ean.setdefault(record.ean, []).append(record)
    groups = []
    for ean, items in by_ean.items():
        items.sort(key=record_sort_key)
        groups.append(ProductGroup(
            ean=ean,
            model=_meta(items, "modelo"),
            reference=_meta(items, "referencia", "reference"),
            brand=_meta(items, "marca", "brand"),
            records=tuple(items),
            cover=_cover(items),
            latest=max(r.date for r in items),
        ))
    groups.sort(key=lambda g: (g.latest, g.ean), reverse=True)
    return groups


def _matches_query(group: ProductGroup, query: str) -> bool:
    needle = query.casefold()
    haystack = (group.ean, group.model, group.reference, group.brand, *group.providers)
    return any(needle in value.casefold() for value in haystack)


def _market_pass(group: ProductGroup, market: str) -> bool:
    if market == ALL:
        return True
    wanted = "" if market == "Original" else market
    return any(r.market == wanted for r in group.records)


def _view_pass(group: ProductGroup, view: str) -> bool:
    target = VIEW_FILTERS.get(view)
    if target is None:
        return True
    if target == "originales":
        return any(not r.market for r in group.records)
    return target in group.present_views


def filter_groups(groups: list[ProductGroup], filters: LibraryFilters) -> list[ProductGroup]:
    query = filters.query.strip()
    result = []
    for group in groups:
        if query and not _matches_query(group, query):
            continue
        if filters.provider != ALL and filters.provider not in group.providers:
            continue
        if not _market_pass(group, filters.market) or not _view_pass(group, filters.view):
            continue
        if filters.availability == AVAILABILITY_FILTERS[1] and group.missing_core:
            continue
        if filters.availability == AVAILABILITY_FILTERS[2] and not group.missing_core:
            continue
        result.append(group)
    return result


def paginate(items: list, number: int, size: int) -> Page:
    if size not in PAGE_SIZES:
        size = PAGE_SIZES[1]
    total = len(items)
    pages = max(1, ceil(total / size))
    number = min(max(1, int(number)), pages)
    start = (number - 1) * size
    chunk = tuple(items[start:start + size])
    return Page(chunk, number, pages, total, start + 1 if chunk else 0, start + len(chunk))


def summary(groups: list[ProductGroup]) -> dict[str, int]:
    """Counters with explicit semantics: images, distinct EAN, distinct non-empty models."""
    return {
        "images": sum(len(g.records) for g in groups),
        "eans": len(groups),
        "models": len({g.model for g in groups if g.model}),
    }


def filter_options(groups: list[ProductGroup]) -> dict[str, list[str]]:
    providers = sorted({p for g in groups for p in g.providers})
    markets = sorted({m for g in groups for m in g.markets})
    return {
        "providers": [ALL, *providers],
        "markets": [ALL, "Original", *markets],
    }
