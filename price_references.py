"""Commercial-reference normalisation, ordered search plan and match classification.

Pure functions, no network and no database: they only decide *what* to look for and *whether* a candidate
page/feed row is the catalogue product. Fetching stays in the authorised connectors.

Identity of a frame is (brand, model, colour code, calibre): the same model in another colour or calibre is a
different product, so offers are never mixed. An exact match needs the published EAN, or model + colour +
calibre confirmed on both sides. Anything weaker is ``probable_match`` and stays out of price comparisons.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Iterable, Mapping
import unicodedata

EXACT, PROBABLE, REJECTED = "exact_match", "probable_match", "rejected"
LEVELS = ("ean", "brand_model_color", "model_color", "normalized_variants")

_SEPARATORS = re.compile(r"[\s\-_/.·,;:|\\]+")
_CALIBER = re.compile(r"(?<!\d)(\d{2})\s*(?:[-/x×□]|\s)\s*(\d{1,2})(?!\d)", re.IGNORECASE)
_LENS_ONLY = re.compile(r"^\d{2}$")


def _ascii(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("□", "x").replace("×", "x")
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def key(value: Any) -> str:
    """Case, accent, space, hyphen, slash and punctuation-insensitive identity key. Zeros are preserved."""
    return re.sub(r"[^A-Z0-9]", "", _ascii(value).upper())


def tokens(value: Any) -> list[str]:
    return [token for token in _SEPARATORS.split(_ascii(value).upper().strip()) if token]


def normalize_caliber(value: Any) -> str:
    """'54□21', '54-21', '54/21', '54 21' -> '54-21'; '63' -> '63'; anything else -> ''."""
    text = _ascii(value).strip()
    if not text:
        return ""
    match = _CALIBER.search(text)
    if match:
        return f"{match.group(1)}-{int(match.group(2))}"
    return text if _LENS_ONLY.fullmatch(text) else ""


def compare_calibers(left: Any, right: Any) -> str:
    """'equal', 'different' or 'unknown'. A lens-only value is compared with the lens of the other."""
    a, b = normalize_caliber(left), normalize_caliber(right)
    if not a or not b:
        return "unknown"
    a_lens, _, a_bridge = a.partition("-")
    b_lens, _, b_bridge = b.partition("-")
    if a_lens != b_lens:
        return "different"
    if a_bridge and b_bridge and a_bridge != b_bridge:
        return "different"
    return "equal"


def extract_caliber(text: Any) -> str:
    match = _CALIBER.search(_ascii(text))
    return f"{match.group(1)}-{int(match.group(2))}" if match else ""


@dataclass(frozen=True)
class Reference:
    brand: str = ""
    model: str = ""
    color: str = ""
    caliber: str = ""
    ean: str = ""

    @property
    def model_key(self) -> str:
        return key(self.model)

    @property
    def color_key(self) -> str:
        return key(self.color)


def strip_brand(text: str, brands: Iterable[str]) -> tuple[str, str]:
    """Remove a leading known brand ('BOTTEGA VENETA BV1274S 005' -> 'Bottega Veneta', 'BV1274S 005')."""
    cleaned = " ".join(tokens(text))
    best = ""
    for brand in brands:
        brand_tokens = " ".join(tokens(brand))
        if brand_tokens and (cleaned == brand_tokens or cleaned.startswith(brand_tokens + " ")) \
                and len(brand_tokens) > len(best):
            best = brand_tokens
    if not best:
        return "", cleaned
    original = next(b for b in brands if " ".join(tokens(b)) == best)
    return original, cleaned[len(best):].strip()


def parse_reference(text: str, *, brands: Iterable[str] = (), models: Iterable[str] = ()) -> Reference:
    """Split a free reference into brand, model, colour and calibre.

    With known ``models`` (the catalogue) the split is exact, so models with spaces ('PR 27ZS',
    'SL 596 DUNE') and colours with separators work. Without them the last token is the colour.
    """
    brand_list = list(brands)
    brand, rest = strip_brand(str(text or ""), brand_list)
    caliber = extract_caliber(rest)
    if caliber:
        rest = _CALIBER.sub(" ", rest, count=1)
    parts = tokens(rest)
    for model in sorted(set(models), key=lambda m: -len(key(m))):
        model_key = key(model)
        if not model_key:
            continue
        for index in range(1, len(parts) + 1):
            if key("".join(parts[:index])) == model_key:
                return Reference(brand, model, " ".join(parts[index:]), caliber)
    if len(parts) >= 2:
        return Reference(brand, " ".join(parts[:-1]), parts[-1], caliber)
    return Reference(brand, parts[0] if parts else "", "", caliber)


def search_plan(product: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Ordered queries for a catalogue product: EAN, brand+model+colour, model+colour, normalised variants."""
    brand, model, color = (str(product.get(name) or "").strip().upper() for name in ("brand", "model", "color"))
    ean = str(product.get("ean") or "").strip()
    plan: list[tuple[str, str]] = []
    if ean:
        plan.append(("ean", ean))
        original = str(product.get("original_ean") or "").strip()
        if original and original != ean:
            plan.append(("ean", original))
    if model and color:
        plan.append(("brand_model_color", f"{brand} {model} {color}".strip()))
        plan.append(("model_color", f"{model} {color}"))
        compact_model, compact_color = key(model), key(color)
        variants = [f"{compact_model} {compact_color}", f"{compact_model}{compact_color}",
                    f"{compact_model}-{compact_color}", f"{model.replace(' ', '-')}-{color.replace(' ', '-')}"]
        seen = {plan[-1][1], plan[-2][1]}
        for variant in variants:
            if variant not in seen:
                seen.add(variant)
                plan.append(("normalized_variants", variant))
    return plan


@dataclass
class MatchResult:
    status: str
    basis: str = ""
    reasons: list[str] = field(default_factory=list)

    @property
    def comparable(self) -> bool:
        return self.status == EXACT


def _contains_reference(title: str, model_key: str, color_key: str) -> bool:
    parts = tokens(title)
    for start in range(len(parts)):
        for end in range(start + 1, len(parts) + 1):
            if key("".join(parts[start:end])) != model_key:
                continue
            for stop in range(end + 1, min(end + 4, len(parts)) + 1):
                if key("".join(parts[end:stop])) == color_key:
                    return True
    return False


def _valid_gtin(value: Any) -> str:
    from price_comparator import normalize_gtin

    try:
        return normalize_gtin(value)
    except ValueError:
        return ""


def classify(product: Mapping[str, Any], candidate: Mapping[str, Any]) -> MatchResult:
    """Decide whether ``candidate`` (a competitor listing) is the catalogue ``product``.

    candidate keys (all optional): ean_published, brand, model, color, caliber, title.
    """
    reasons: list[str] = []
    own_model, own_color, own_brand = key(product.get("model")), key(product.get("color")), key(product.get("brand"))
    title = str(candidate.get("title") or "")
    brand = key(candidate.get("brand"))
    brand_evidence = bool(brand) or (bool(own_brand) and own_brand in key(title))

    published = str(candidate.get("ean_published") or "").strip()
    ean = _valid_gtin(published) if published else ""
    if published and not ean:
        reasons.append("EAN publicado no válido: se ignora")
    if ean and ean != product.get("ean"):
        return MatchResult(REJECTED, reasons=[f"EAN publicado distinto: {ean}"])

    model_given, color_given = key(candidate.get("model")), key(candidate.get("color"))
    if not (model_given and color_given) and title and own_model and own_color \
            and _contains_reference(title, own_model, own_color):
        model_given, color_given = own_model, own_color
    caliber_state = compare_calibers(product.get("caliber"), candidate.get("caliber") or extract_caliber(title))

    conflicts = []
    if brand and brand != own_brand:
        conflicts.append(f"marca {candidate.get('brand')}")
    if model_given and model_given != own_model:
        conflicts.append(f"modelo {candidate.get('model')}")
    if color_given and color_given != own_color:
        conflicts.append(f"color {candidate.get('color')}")
    if caliber_state == "different":
        conflicts.append("calibre")
    if ean:
        if conflicts:
            return MatchResult(PROBABLE, reasons=reasons + [
                "EAN idéntico pero contradice: " + ", ".join(conflicts) + ": revisar"])
        return MatchResult(EXACT, "ean", reasons + ["EAN publicado idéntico"])
    if conflicts:
        return MatchResult(REJECTED, reasons=reasons + ["Distinto " + ", ".join(conflicts)])

    if not (model_given and color_given):
        return MatchResult(REJECTED, reasons=reasons + ["No se identifican modelo y color"])
    if caliber_state != "equal":
        why = ("el calibre del catálogo es desconocido" if not normalize_caliber(product.get("caliber"))
               else "la ficha no publica el calibre")
        return MatchResult(PROBABLE, reasons=reasons + [f"Modelo y color coinciden pero {why}: revisar"])
    if not brand_evidence:
        return MatchResult(PROBABLE, reasons=reasons + ["Falta evidencia de la marca: revisar"])
    return MatchResult(EXACT, "reference", reasons + ["Marca, modelo, color y calibre coinciden"])

class ReferenceIndex:
    """Catalogue lookup by EAN or reference, following the search order of the plan."""

    def __init__(self, products: Iterable[Mapping[str, Any]]) -> None:
        self.products = [dict(product) for product in products]
        self.brands = sorted({p["brand"] for p in self.products if p.get("brand")})
        self.models = sorted({p["model"] for p in self.products if p.get("model")})

    def by_ean(self, value: Any) -> dict | None:
        ean = _valid_gtin(value)
        return next((p for p in self.products if p["ean"] == ean), None) if ean else None

    def lookup(self, text: str) -> tuple[str, list[dict]]:
        """Return (level, products). More than one product means colour/calibre variants: do not pick one."""
        if self.by_ean(text):
            return "ean", [self.by_ean(text)]
        ref = parse_reference(text, brands=self.brands, models=self.models)
        compact = key(re.sub(_CALIBER, " ", _ascii(text)))
        for level, test in (
            ("brand_model_color", lambda p: ref.brand and key(p["brand"]) == key(ref.brand)
             and key(p["model"]) == ref.model_key and key(p["color"]) == ref.color_key),
            ("model_color", lambda p: key(p["model"]) == ref.model_key and ref.color_key
             and key(p["color"]) == ref.color_key),
            ("normalized_variants", lambda p: compact in {key(p["model"]) + key(p["color"]),
                                                          key(p["brand"]) + key(p["model"]) + key(p["color"])}),
        ):
            found = [p for p in self.products if test(p)]
            if found and ref.caliber:
                narrowed = [p for p in found if compare_calibers(p.get("caliber"), ref.caliber) != "different"]
                found = narrowed or found
            if found:
                return level, found
        return "", []



