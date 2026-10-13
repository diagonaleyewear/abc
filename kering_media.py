"""Evidence-based view adapter.

Recognised original filenames always win. Otherwise the user-confirmed Kering viewer order
applies, only to a complete viewer without contradictions; anything else is flagged for review.
"""
from pathlib import Path
from urllib.parse import unquote, urlsplit

from image_naming import classified_view, sanitize_filename


POSITIONAL_RULE = "kering_viewer_position_v1"
# Confirmed by the user on 2026-10-13: viewer slot 1 is lateral (V3), slot 2 frontal (V1),
# slot 3 a detail shot. No slot implies perspectiva (V2).
VIEWER_POSITIONS = ("lateral", "frontal", "detalle")
POSITIONAL_PROVENANCE = {
    "source": "kering_positional_rule", "confirmed_by": "user", "confirmed_on": "2026-10-13",
    "mapping": {"1": "lateral (V3)", "2": "frontal (V1)", "3": "detalle"},
}
REVIEW_INCOMPLETE = "visor_incompleto"
REVIEW_CONFLICT = "etiquetas_contradictorias"


def identify_media(image: dict) -> dict:
    original_name = Path(unquote(urlsplit(image["url"]).path)).name
    signals = {"url_filename": original_name}
    for attribute in ("original_name", "title", "label"):
        if image.get(attribute):
            signals[attribute] = image[attribute]
    detected = {source: classified_view(value) for source, value in signals.items()}
    recognized = {view for view in detected.values() if view != "unknown"}
    view = next(iter(recognized)) if len(recognized) == 1 else "unknown"
    return {
        "original_name": original_name, "reference": image.get("reference", ""),
        "signals": signals, "signal_sources": [source for source, value in detected.items() if value != "unknown"],
        "rule": "conflicting_signals" if len(recognized) > 1 else
                "luxoptica_original_filename_v1" if recognized else "insufficient_evidence",
        "normalized_view": view,
        "source_name": sanitize_filename(original_name),
    }


def resource_key(url: str) -> tuple[str, str]:
    """Same CDN resource regardless of query/fragment (cache busters, carousel clones)."""
    parts = urlsplit(url)
    return parts.netloc.lower(), unquote(parts.path)


def contradicts_position(detection: dict, position: int) -> bool:
    if detection["rule"] == "conflicting_signals":
        return True
    explicit = detection["normalized_view"]
    if explicit == "unknown":
        return False
    if position < len(VIEWER_POSITIONS):
        return explicit != VIEWER_POSITIONS[position]
    # Extra slots may carry explicit perspectiva (V2) but not a second lateral/frontal.
    return explicit in VIEWER_POSITIONS


def classify_viewer(images: list[dict]) -> list[dict]:
    """Classify the product viewer in DOM order; download completion order never matters."""
    unique, seen = [], set()
    for image in images:
        key = resource_key(image["url"])
        if key not in seen:
            seen.add(key)
            unique.append(image)
    detections = [identify_media(image) for image in unique]
    contradicts = [contradicts_position(detection, position) for position, detection in enumerate(detections)]
    reasons = set()
    if len(unique) < len(VIEWER_POSITIONS):
        reasons.add(REVIEW_INCOMPLETE)
    if any(contradicts):
        reasons.add(REVIEW_CONFLICT)
    review = sorted(reasons)
    results = []
    for position, (image, detection) in enumerate(zip(unique, detections)):
        result = {**detection, "url": image["url"], "explicit_view": detection["normalized_view"],
                  "viewer_position": position + 1, "viewer_size": len(unique)}
        if review:
            # Recognised original filenames keep their view; position is not applied to anyone.
            result["review"] = {"required": True, "reasons": review,
                                "contradicts_position": contradicts[position]}
            if contradicts[position] and position < len(VIEWER_POSITIONS):
                result["review"]["position_expected"] = VIEWER_POSITIONS[position]
        elif position < len(VIEWER_POSITIONS) and detection["normalized_view"] == "unknown":
            result.update(rule=POSITIONAL_RULE, normalized_view=VIEWER_POSITIONS[position],
                          provenance=dict(POSITIONAL_PROVENANCE))
        results.append(result)
    return results
