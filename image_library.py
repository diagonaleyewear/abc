"""Pure grouping, filtering and paging logic for the image library.

Works on the local catalog records only: no filesystem, Odoo or provider access.
Views come exclusively from the stored classification (`record.view`); nothing is
reclassified here, so the Kering positional rule stays where it is applied.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from math import ceil
from pathlib import PureWindowsPath

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

# Metadata keys, in order of trust. Nothing here is derived from the stored file name.
NAME_KEYS = ("nombre_comercial", "denominacion_comercial", "product_name")
MODEL_KEYS = ("modelo",)
REFERENCE_KEYS = ("referencia", "reference")
BRAND_KEYS = ("marca", "brand")
# Importers store a name-derived "modelo" (file prefix before "__", or the whole file name when
# there is no prefix). Those values, view words and placeholders are technical, not commercial.
_FILE_EXTENSION = re.compile(r"\.[a-z][a-z0-9]{1,4}$", re.IGNORECASE)
_TECHNICAL_WORDS = frozenset({
    "modelo-unknown", "unknown", "desconocido", "sin modelo", "n/a", "na", "null", "none", "-", "--",
    "frontal", "perspectiva", "lateral", "detalle", "front", "side", "perspective", "detail",
    "v1", "v2", "v3", "original", "img", "image", "imagen", "foto", "photo",
})


@dataclass(frozen=True)
class ProductGroup:
    ean: str
    model: str
    reference: str
    brand: str
    records: tuple[ImageRecord, ...]
    cover: ImageRecord
    latest: str
    name: str = ""

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
    def has_commercial_title(self) -> bool:
        return bool(self.name or self.model or self.reference)

    @property
    def title(self) -> str:
        """Commercial name, else model/reference, else a neutral EAN label; never a file name."""
        return self.name or self.model or self.reference or f"EAN {self.ean}"


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


def _file_names(records: list[ImageRecord]) -> set[str]:
    names = set()
    for record in records:
        name = str(record.name or "").strip().casefold()
        if name:
            names.update({name, PureWindowsPath(name).stem})
    return names


def is_technical_value(value: str, records: list[ImageRecord]) -> bool:
    """True when `value` looks like a file name, view word, placeholder or the EAN itself."""
    text = str(value or "").strip()
    folded = text.casefold()
    if not text or folded in _TECHNICAL_WORDS or "__" in text or "/" in text or "\\" in text:
        return True
    if _FILE_EXTENSION.search(text) or folded in _file_names(records):
        return True
    digits = text.replace(" ", "")
    return digits.isdigit() and len(digits) >= 8


def _candidates(record: ImageRecord, keys: tuple[str, ...]):
    for key in keys:
        yield record.metadata.get(key)
    identity = record.metadata.get("identity")
    if isinstance(identity, dict) and keys == MODEL_KEYS:
        yield identity.get("model")


def _meta(records: list[ImageRecord], *keys: str) -> str:
    """First non-technical value of `keys` across the EAN's records (verified metadata only)."""
    for record in records:
        for value in _candidates(record, keys):
            text = str(value or "").strip()
            if text and not is_technical_value(text, records):
                return text
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
            model=_meta(items, *MODEL_KEYS),
            reference=_meta(items, *REFERENCE_KEYS),
            brand=_meta(items, *BRAND_KEYS),
            records=tuple(items),
            cover=_cover(items),
            latest=max(r.date for r in items),
            name=_meta(items, *NAME_KEYS),
        ))
    groups.sort(key=lambda g: (g.latest, g.ean), reverse=True)
    return groups


def _matches_query(group: ProductGroup, query: str) -> bool:
    needle = query.casefold()
    haystack = (group.ean, group.name, group.model, group.reference, group.brand, *group.providers)
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
    """Counters with explicit semantics: images, distinct EAN, distinct verified models."""
    return {
        "images": sum(len(g.records) for g in groups),
        "eans": len(groups),
        "models": len({g.model or g.name for g in groups if g.model or g.name}),
    }


def metadata_coverage(records: list[ImageRecord]) -> list[dict]:
    """Per provider: how many EAN carry a verified model and how many only a technical value.

    Read-only diagnosis of the stored metadata; nothing is corrected or reclassified.
    """
    by_provider: dict[str, dict[str, list[ImageRecord]]] = {}
    for record in records:
        by_provider.setdefault(record.provider or "Sin proveedor", {}).setdefault(record.ean, []).append(record)
    rows = []
    for provider, eans in sorted(by_provider.items()):
        verified, technical, empty, models = 0, 0, 0, set()
        for items in eans.values():
            model = _meta(items, *MODEL_KEYS)
            raw = any(str(r.metadata.get(key) or "").strip() for r in items for key in MODEL_KEYS)
            if model:
                verified += 1
                models.add(model)
            elif raw:
                technical += 1
            else:
                empty += 1
        rows.append({
            "provider": provider, "images": sum(len(items) for items in eans.values()), "eans": len(eans),
            "eans_with_verified_model": verified, "eans_with_technical_model_only": technical,
            "eans_without_model": empty, "distinct_models": len(models),
        })
    return rows


def filter_options(groups: list[ProductGroup]) -> dict[str, list[str]]:
    providers = sorted({p for g in groups for p in g.providers})
    markets = sorted({m for g in groups for m in g.markets})
    return {
        "providers": [ALL, *providers],
        "markets": [ALL, "Original", *markets],
    }
