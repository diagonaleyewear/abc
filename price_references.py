"""Commercial-reference normalisation, ordered search plan and match classification.

Pure functions, no network and no database: they only decide *what* to look for and *whether* a candidate
page/feed row is the catalogue product. Fetching stays in the authorised connectors.

An exact published EAN makes calibre informational, never model or colour. Without verified EAN, brand + model +
colour code must be confirmed. Calibre is informational and does not change identity. Ambiguous evidence stays out
of comparisons.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Iterable, Mapping
import unicodedata

EXACT, PROBABLE, REJECTED = "exact_match", "probable_match", "rejected"
LEVELS = ("ean", "brand_model_color", "model_color", "normalized_variants")

_SEPARATORS = re.compile(r"[\s\-_/.·,;:|\\]+")
# The bridge must end the number: "54 0PR_..." or "C54 16K08Z" (a colour code) are not a calibre.
_CALIBER = re.compile(r"(?<!\d)(\d{2})\s*(?:[-/x×□]|\s)\s*(\d{1,2})(?:mm)?(?![\dA-Za-z])", re.IGNORECASE)
_LENS_ONLY = re.compile(r"^\d{2}$")
_LENS_TOKEN = re.compile(r"(?<![\dA-Za-z])C(\d{2})(?![\dA-Za-z])|\s(\d{2})\s*$")
_CODE_CALIBER = re.compile(r"^0?[A-Z]{1,4}_[A-Z0-9]+?(\d{2})(\d{2})$")


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


def extract_lens(text: Any) -> str:
    """Lens width alone ('C54', or a trailing '... 54'); only plausible eyewear sizes."""
    match = _LENS_TOKEN.search(_ascii(text).strip())
    lens = int(match.group(1) or match.group(2)) if match else 0
    return str(lens) if 38 <= lens <= 66 else ""


def caliber_from_code(code: Any) -> str:
    """Manufacturer-style codes such as '0PR_27ZS16K08Z5421' end in lens + bridge ('54-21')."""
    match = _CODE_CALIBER.match(_ascii(code).strip().upper())
    if not match:
        return ""
    lens, bridge = int(match.group(1)), int(match.group(2))
    return f"{lens}-{bridge}" if 38 <= lens <= 66 and 10 <= bridge <= 26 else ""


@dataclass(frozen=True)
class Reference:
    brand: str = ""
    model: str = ""
    color: str = ""
    caliber: str = ""
    ean: str = ""
    ambiguous: bool = False

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

    Known models may follow a commercial collection name. Only complete token sequences match;
    a trailing descriptive colour never replaces the manufacturer code. Without known models,
    the last token is the colour (catalogue-reference lookup, not free page prose).
    """
    brand_list = list(brands)
    raw = str(text or "")
    caliber = extract_caliber(raw) or extract_lens(raw)
    if caliber:
        raw = _CALIBER.sub(" ", raw) if "-" in caliber else _LENS_TOKEN.sub(" ", raw)
    brand, rest = strip_brand(raw, brand_list)
    parts = tokens(rest)
    matches: list[Reference] = []
    for model in sorted(set(models), key=lambda m: -len(key(m))):
        model_key = key(model)
        if not model_key:
            continue
        for start in range(len(parts)):
            for end in range(start + 1, len(parts) + 1):
                if key("".join(parts[start:end])) != model_key:
                    continue
                color_parts = []
                reference_caliber = caliber
                for part in parts[end:]:
                    if not re.search(r"\d", part):
                        break
                    if color_parts and _LENS_ONLY.fullmatch(part) and 38 <= int(part) <= 66:
                        reference_caliber = reference_caliber or part
                        break
                    color_parts.append(part)
                matches.append(Reference(brand, model, " ".join(color_parts), reference_caliber))
    if matches:
        distinct = {(match.model_key, match.color_key) for match in matches}
        if len(distinct) > 1:
            return Reference(brand=brand, caliber=caliber, ambiguous=True)
        return matches[0]
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
    observations: list[str] = field(default_factory=list)
    normalized: dict[str, Any] = field(default_factory=dict)

    @property
    def comparable(self) -> bool:
        return self.status == EXACT


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
    own_model, own_color, own_brand = key(product.get("model")), key(product.get("color")), key(product.get("brand"))
    title = str(candidate.get("title") or "")
    published = str(candidate.get("ean_published") or "").strip()
    ean = _valid_gtin(published) if published else ""
    references = [parse_reference(text, brands=[str(product.get("brand") or "")],
                                  models=[str(product.get("model") or "")])
                  for text in [title, *(candidate.get("identifiers") or [])]]
    relevant = [ref for index, ref in enumerate(references)
                if index == 0 or ref.model_key == own_model or
                (ref.color_key and any(char.isalpha() for char in ref.model) and
                 any(char.isdigit() for char in ref.model) and
                 not caliber_from_code((candidate.get("identifiers") or [])[index - 1]))]
    models = {ref.model_key for ref in relevant
              if ref.model_key == own_model or any(char.isdigit() for char in ref.model)}
    colors = {ref.color_key for ref in relevant if any(char.isdigit() for char in ref.color)}
    if candidate.get("model"):
        models.add(key(candidate["model"]))
    if any(char.isdigit() for char in str(candidate.get("color") or "")):
        colors.add(key(candidate["color"]))
    brands = {key(ref.brand) for ref in references if ref.brand}
    if candidate.get("brand"):
        brands.add(key(candidate["brand"]))
    brand_tokens = " ".join(tokens(product.get("brand")))
    if brand_tokens and f" {brand_tokens} " in f" {' '.join(tokens(title))} ":
        brands.add(own_brand)
    calibers = {normalize_caliber(value) for value in
                [candidate.get("caliber"), *(ref.caliber for ref in references)] if normalize_caliber(value)}
    observed_caliber = normalize_caliber(candidate.get("caliber")) or (sorted(calibers)[0] if calibers else "")
    caliber_state = compare_calibers(product.get("caliber"), observed_caliber)
    verified_ean = bool(ean and ean == _valid_gtin(product.get("ean")))
    normalized = {"catalogue": {"ean": product.get("ean"), "brand": own_brand, "model": own_model,
                                "color": own_color, "caliber": normalize_caliber(product.get("caliber"))},
                  "listing": {"ean": ean, "brands": sorted(brands), "models": sorted(models),
                              "colors": sorted(colors), "calibers": sorted(calibers)},
                  "verified_ean": verified_ean, "caliber_comparison": caliber_state,
                  "caliber_informational": True}
    reasons = []
    observations = []
    if verified_ean and caliber_state != "equal":
        observations.append(f"Calibre informativo por EAN publicado idéntico: catálogo "
                            f"{normalize_caliber(product.get('caliber')) or 'desconocido'}, "
                            f"ficha {observed_caliber or 'desconocido'} ({caliber_state})")
    contradictory_calibers = any(compare_calibers(a, b) == "different" for a in calibers for b in calibers)
    if contradictory_calibers and verified_ean:
        observations.append("Calibre informativo: medidas publicadas contradictorias")
    def result(status: str, basis: str = "") -> MatchResult:
        return MatchResult(status, basis, reasons, observations, normalized)

    if published and not ean:
        reasons.append("EAN publicado no válido: no se aplica la excepción de calibre")
    if candidate.get("ambiguous") or any(ref.ambiguous for ref in references):
        reasons.append("Identificación ambigua: requiere revisión")
        return result(PROBABLE)
    identity_fields = [(own_brand, brands), (own_model, models), (own_color, colors)]
    contradictions = any(found and found != {expected} for expected, found in identity_fields)
    for name, expected, found in [("Marca", own_brand, brands), ("Modelo", own_model, models),
                                  ("Color", own_color, colors)]:
        if found and found != {expected}:
            reasons.append(f"Distinto {name.lower()}: catálogo {expected or 'desconocido'}, "
                           f"ficha {', '.join(sorted(found))}")
        elif not verified_ean and (not expected or not found):
            reasons.append(f"{name} no confirmado: catálogo {expected or 'desconocido'}, "
                           f"ficha {', '.join(sorted(found)) or 'desconocido'}")
    if ean and ean != product.get("ean"):
        reasons.insert(0, f"EAN publicado distinto: {ean}; revisión manual obligatoria")
        return result(REJECTED if contradictions else PROBABLE)
    if contradictions:
        return result(PROBABLE if verified_ean else REJECTED)
    if reasons:
        return result(PROBABLE)
    reasons.append("EAN publicado idéntico; marca, modelo y código de color confirmados" if verified_ean else
                   "Marca, modelo y código de color coinciden")
    return result(EXACT, "ean" if verified_ean else "reference")


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
