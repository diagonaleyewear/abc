"""Evidence-based view adapter: no positional or unvalidated visual inference."""
from pathlib import Path
from urllib.parse import unquote, urlsplit

from image_naming import classified_view, sanitize_filename


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
