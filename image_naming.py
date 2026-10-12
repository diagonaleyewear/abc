"""Shared Luxoptica filename contract; unknown views never imply coverage."""
from pathlib import Path


VIEW_PATTERNS = {
    "frontal": ("noshad", "fr"),
    "perspectiva": ("noshad", "qt"),
    "lateral": ("shad", "lt"),
}


def sanitize_filename(name: str) -> str:
    cleaned = (name or "attachment.bin").strip()
    for char in '\\/:*?"<>|':
        cleaned = cleaned.replace(char, "_")
    return cleaned or "attachment.bin"


def market_view_from_image_name(file_name: str) -> str | None:
    parts = [part.lower() for part in Path(file_name).stem.split("__")]
    if len(parts) < 4:
        return None
    return {("noshad", "fr"): "V1", ("noshad", "qt"): "V2",
            ("shad", "lt"): "V3"}.get((parts[-2], parts[-1]))


def classified_view(name: str) -> str:
    return {"V1": "frontal", "V2": "perspectiva", "V3": "lateral"}.get(
        market_view_from_image_name(name), "unknown")


def kering_filename(model: str, color: str, view: str, extension: str, source_name: str) -> str:
    if view not in VIEW_PATTERNS:
        return sanitize_filename(source_name)
    shade, angle = VIEW_PATTERNS[view]
    return sanitize_filename(f"{model}__{color}__{shade}__{angle}{extension}")
