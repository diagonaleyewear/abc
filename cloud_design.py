"""Cloud Diagonal design system: tokens and reusable components.

Colour/radius/font values come from the public Vytria theme (theme.css, custom.css):
body text #42403B, page tint #fbfbfa, neutral #f2f2f2, border #d9d9d9, accent #003399,
highlight #FFCB03, radii 0/4px, Arial sans body with Cormorant Garamond for display text.
The font file is not bundled (licence unverified): headings fall back to Times New Roman (lining figures, so references such as GU1060S stay legible; Georgia uses old-style figures).

Every colour comes from a theme palette (`TOKENS["color"]` light, `TOKENS["color_dark"]` dark) chosen from the
active Streamlit theme, so text is never painted with a fixed colour over a background it does not own.
`CONTRAST_PAIRS` lists the text/background pairs that must reach WCAG AA; tests check them for both palettes.

Styles target our own `cd-*` classes plus the documented `st-key-*` classes of containers that this
design system creates itself (`lib_grid`, `lib_filters`, `lib_card_*`). No Streamlit-internal selectors.
Builders return escaped HTML strings so they can be tested without Streamlit.
"""
from __future__ import annotations

import base64
from html import escape

TOKENS = {
    "color": {
        "page": "#ffffff", "ink": "#111111", "text": "#42403B", "muted": "#5f5d58", "surface": "#ffffff",
        "tint": "#fbfbfa", "neutral": "#f2f2f2", "border": "#d9d9d9", "accent": "#003399",
        "accent_soft": "#eef2fb", "highlight": "#FFCB03", "success": "#1e6b3a", "warning": "#7a5200",
        "danger": "#b3361a", "frame": "#f4f4f2", "frame_text": "#4a4842",
    },
    "color_dark": {
        "page": "#0e1117", "ink": "#f7f6f2", "text": "#dcdad4", "muted": "#b3b1ab", "surface": "#171b24",
        "tint": "#12151c", "neutral": "#222732", "border": "#3b4252", "accent": "#9db8ff",
        "accent_soft": "#1b2a55", "highlight": "#FFCB03", "success": "#6fd596", "warning": "#f2c15c",
        "danger": "#ff9a82", "frame": "#f4f4f2", "frame_text": "#4a4842",
    },
    "font": {
        "body": 'Arial, "Helvetica Neue", Helvetica, sans-serif',
        "display": '"Cormorant Garamond", "Times New Roman", Georgia, serif',
    },
    "space": {"xs": "4px", "sm": "8px", "md": "16px", "lg": "24px", "xl": "32px"},
    "radius": {"sm": "0", "md": "4px"},
    "control": {"height": "40px", "target": "44px"},
    "breakpoint": {"mobile": 640, "tablet": 1024},
    "card": {"media_ratio": "4 / 3", "min_width": "200px"},
}

_S = TOKENS["space"]

# (foreground, background, minimum ratio): WCAG AA is 4.5 for normal text, 3 for large text.
CONTRAST_PAIRS = (
    ("ink", "page", 4.5), ("text", "page", 4.5), ("muted", "page", 4.5),
    ("ink", "surface", 4.5), ("text", "surface", 4.5), ("muted", "surface", 4.5), ("accent", "surface", 4.5),
    ("ink", "neutral", 4.5), ("text", "neutral", 4.5), ("muted", "neutral", 4.5),
    ("accent", "accent_soft", 4.5), ("frame_text", "frame", 4.5),
    ("accent", "tint", 4.5), ("text", "tint", 4.5), ("ink", "tint", 4.5),
    ("success", "tint", 4.5), ("warning", "tint", 4.5), ("danger", "tint", 4.5),
)


def _linear(channel: int) -> float:
    value = channel / 255
    return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4


def _luminance(colour: str) -> float:
    red, green, blue = (int(colour[index:index + 2], 16) for index in (1, 3, 5))
    return 0.2126 * _linear(red) + 0.7152 * _linear(green) + 0.0722 * _linear(blue)


def contrast_ratio(foreground: str, background: str) -> float:
    """WCAG 2.x contrast ratio between two #rrggbb colours."""
    lighter, darker = sorted((_luminance(foreground), _luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def palette(theme: str) -> dict[str, str]:
    """'light' / 'dark' give raw hex tokens. 'auto' gives `light-dark()` values, resolved by the
    browser from the `color-scheme` Streamlit sets on the app, so a theme switch needs no rerun."""
    if theme == "auto":
        light, dark = TOKENS["color"], TOKENS["color_dark"]
        return {key: f"light-dark({light[key]}, {dark[key]})" for key in light}
    return TOKENS["color_dark"] if theme == "dark" else TOKENS["color"]


def active_theme() -> str:
    return "auto"


def build_styles(theme: str = "light") -> str:
    _C = palette(theme)
    return f"""
<style>
.cd-header {{ padding: {_S['sm']} 0 0; margin-bottom: {_S['sm']}; font-family: {TOKENS['font']['body']}; }}
.cd-header__title {{ font-family: {TOKENS['font']['display']}; color: {_C['ink']}; font-size: 2rem; font-weight: 600; line-height: 1.15; margin: 0; overflow-wrap: anywhere; }}
.cd-header__sub {{ color: {_C['muted']}; margin: {_S['xs']} 0 0; font-size: .95rem; }}
ul.cd-stats {{ display: flex; flex-wrap: wrap; gap: {_S['sm']}; margin: {_S['md']} 0 0; padding: 0; padding-inline-start: 0; list-style: none; }}
ul.cd-stats li.cd-stat {{ margin: 0; background: {_C['neutral']}; border: 1px solid {_C['border']}; border-radius: {TOKENS['radius']['md']}; padding: {_S['sm']} {_S['md']}; color: {_C['text']}; font-size: .9rem; }}
.cd-stat strong {{ color: {_C['ink']}; font-size: 1.15rem; margin-right: {_S['xs']}; }}
div.st-key-lib_grid.st-key-lib_grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 200px), 1fr)); gap: {_S['md']}; align-items: start; }}
div.st-key-lib_grid.st-key-lib_grid > div {{ width: auto; min-width: 0; max-width: none; }}
div.st-key-lib_filters.st-key-lib_filters {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 150px), 1fr)); gap: {_S['sm']} {_S['md']}; align-items: start; }}
div.st-key-lib_filters.st-key-lib_filters > div {{ width: auto; min-width: 0; max-width: none; }}
div.st-key-lib_thumbs.st-key-lib_thumbs {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 140px), 1fr)); gap: {_S['md']}; align-items: start; }}
div.st-key-lib_thumbs.st-key-lib_thumbs > div {{ width: auto; min-width: 0; max-width: none; }}
div[class*="st-key-lib_card_"][class*="st-key-lib_card_"] {{ background: {_C['surface']}; border-color: {_C['border']}; border-radius: {TOKENS['radius']['md']}; transition: border-color .15s, box-shadow .15s; }}
div[class*="st-key-lib_card_"][class*="st-key-lib_card_"]:hover {{ border-color: {_C['accent']}; }}
div[class*="st-key-lib_card_"][class*="st-key-lib_card_"]:has(:focus-visible) {{ border-color: {_C['accent']}; box-shadow: 0 0 0 2px {_C['accent']}; }}
.cd-card {{ display: flex; flex-direction: column; gap: 2px; font-family: {TOKENS['font']['body']}; color: {_C['text']}; }}
.cd-card__media {{ aspect-ratio: {TOKENS['card']['media_ratio']}; flex: 0 0 auto; background: {_C['frame']}; border-radius: {TOKENS['radius']['md']}; display: flex; align-items: center; justify-content: center; overflow: hidden; margin-bottom: {_S['sm']}; }}
.cd-card__media img {{ width: 100%; height: 100%; object-fit: contain; display: block; }}
.cd-card__placeholder {{ color: {_C['frame_text']}; font-size: .85rem; text-align: center; padding: {_S['sm']}; }}
.cd-card__eyebrow {{ margin: 0; color: {_C['muted']}; font-size: .72rem; font-weight: 700; letter-spacing: .06em; text-transform: uppercase; min-height: 1.2em; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.cd-card__brand::before {{ content: "·"; margin: 0 {_S['xs']}; }}
.cd-card__brand--only::before {{ content: none; }}
.cd-card__title {{ margin: 0; color: {_C['ink']}; font-family: {TOKENS['font']['display']}; font-size: 1.2rem; font-weight: 700; line-height: 1.2; min-height: 2.4em; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; overflow-wrap: anywhere; }}
.cd-card__title--neutral {{ color: {_C['muted']}; font-family: {TOKENS['font']['body']}; font-size: .95rem; font-weight: 400; font-style: italic; }}
.cd-card__ean {{ margin: 0; color: {_C['ink']}; font-size: .92rem; font-weight: 700; font-variant-numeric: tabular-nums; letter-spacing: .02em; overflow-wrap: anywhere; }}
.cd-card__meta {{ margin: 0; color: {_C['text']}; font-size: .82rem; font-variant-numeric: tabular-nums; min-height: 1.3em; overflow-wrap: anywhere; }}
ul.cd-badges {{ display: flex; flex-wrap: wrap; gap: {_S['xs']}; margin: {_S['xs']} 0 {_S['xs']}; padding: 0; padding-inline-start: 0; list-style: none; }}
ul.cd-badges li.cd-badge {{ margin: 0; font-size: .75rem; font-weight: 700; padding: 2px 8px; border-radius: {TOKENS['radius']['md']}; border: 1px solid {_C['border']}; }}
ul.cd-badges li.cd-badge--on {{ color: {_C['accent']}; border-color: {_C['accent']}; background: {_C['accent_soft']}; }}
ul.cd-badges li.cd-badge--off {{ color: {_C['muted']}; border-style: dashed; background: transparent; }}
.cd-photo {{ background: {_C['frame']}; border: 1px solid {_C['border']}; border-radius: {TOKENS['radius']['md']}; aspect-ratio: {TOKENS['card']['media_ratio']}; display: flex; align-items: center; justify-content: center; overflow: hidden; }}
.cd-photo img {{ width: 100%; height: 100%; object-fit: contain; }}
.cd-caption {{ color: {_C['text']}; font-size: .85rem; margin: {_S['xs']} 0 {_S['sm']}; overflow-wrap: anywhere; }}
.cd-notice {{ border-left: 4px solid {_C['accent']}; background: {_C['tint']}; color: {_C['text']}; padding: {_S['sm']} {_S['md']}; margin: {_S['sm']} 0; font-size: .92rem; }}
.cd-notice--warning {{ border-left-color: {_C['highlight']}; color: {_C['warning']}; }}
.cd-notice--error {{ border-left-color: {_C['danger']}; color: {_C['danger']}; }}
.cd-notice--success {{ border-left-color: {_C['success']}; color: {_C['success']}; }}
.cd-empty {{ text-align: center; padding: {_S['xl']} {_S['md']}; border: 1px dashed {_C['border']}; border-radius: {TOKENS['radius']['md']}; color: {_C['text']}; background: {_C['tint']}; }}
.cd-empty h3 {{ font-family: {TOKENS['font']['display']}; color: {_C['ink']}; margin: 0 0 {_S['xs']}; }}
.cd-pager {{ color: {_C['text']}; font-size: .9rem; text-align: center; padding: {_S['sm']} 0; }}
@media (max-width: {TOKENS['breakpoint']['mobile']}px) {{
  .cd-header__title {{ font-size: 1.6rem; }}
}}
</style>
"""


STYLES = build_styles("auto")
STYLES_LIGHT = build_styles("light")
STYLES_DARK = build_styles("dark")


def image_data_uri(content: bytes) -> str:
    mime = "image/png" if content[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(content).decode('ascii')}"


def _media(preview: bytes | None, alt: str, placeholder: str) -> str:
    if preview:
        return f'<img src="{image_data_uri(preview)}" alt="{escape(alt, quote=True)}" loading="lazy">'
    return f'<div class="cd-card__placeholder" role="img" aria-label="{escape(placeholder, quote=True)}">{escape(placeholder)}</div>'


def page_header_html(title: str, subtitle: str = "", stats: tuple[tuple[str, str], ...] = ()) -> str:
    items = "".join(
        f'<li class="cd-stat"><strong>{escape(value)}</strong>{escape(label)}</li>' for value, label in stats
    )
    sub = f'<p class="cd-header__sub">{escape(subtitle)}</p>' if subtitle else ""
    stat_list = f'<ul class="cd-stats" aria-label="Resumen">{items}</ul>' if items else ""
    return f'<header class="cd-header"><h1 class="cd-header__title">{escape(title)}</h1>{sub}{stat_list}</header>'


def view_badges_html(views: tuple[tuple[str, bool], ...]) -> str:
    """Each badge carries text and a mark, never colour alone."""
    parts = []
    for label, present in views:
        state, mark, spoken = ("on", "✓", "disponible") if present else ("off", "–", "no disponible")
        parts.append(
            f'<li class="cd-badge cd-badge--{state}" aria-label="{escape(label)} {spoken}" '
            f'title="{escape(label)} {spoken}">{escape(label)} {mark}</li>'
        )
    return f'<ul class="cd-badges" aria-label="Vistas">{"".join(parts)}</ul>'


def product_card_html(*, title: str, brand: str, reference: str, ean: str, count: int,
                      views: tuple[tuple[str, bool], ...], preview: bytes | None,
                      placeholder: str = "Sin miniatura", provider: str = "", named: bool = True) -> str:
    """Card content. `named` is False when no verified commercial name exists: the card then
    shows a neutral label and the EAN, never a file name."""
    noun = "imagen" if count == 1 else "imágenes"
    missing = [label for label, present in views if not present]
    gaps = f'<span class="cd-card__missing"> · faltan {escape(", ".join(missing))}</span>' if missing else ""
    eyebrow = ""
    if provider:
        eyebrow += f'<span class="cd-card__provider">{escape(provider)}</span>'
    if brand:
        lone = "" if provider else " cd-card__brand--only"
        eyebrow += f'<span class="cd-card__brand{lone}">{escape(brand)}</span>'
    shown = title if named else "Modelo no disponible"
    alt = title if named else f"EAN {ean}"
    heading = "cd-card__title" if named else "cd-card__title cd-card__title--neutral"
    ref = f'<p class="cd-card__meta">Ref. {escape(reference)}</p>' if reference and reference != title else '<p class="cd-card__meta"></p>'
    return (
        '<article class="cd-card">'
        f'<div class="cd-card__media">{_media(preview, alt, placeholder)}</div>'
        f'<p class="cd-card__eyebrow">{eyebrow}</p>'
        f'<h3 class="{heading}" title="{escape(shown, quote=True)}">{escape(shown)}</h3>'
        f'<p class="cd-card__ean">EAN {escape(ean)}</p>'
        f'{ref}'
        f'<p class="cd-card__meta"><span class="cd-card__count">{count} {noun}</span>{gaps}</p>'
        f'{view_badges_html(views)}'
        '</article>'
    )


def photo_html(preview: bytes | None, alt: str, placeholder: str = "Sin miniatura") -> str:
    return f'<div class="cd-photo">{_media(preview, alt, placeholder)}</div>'


def caption_html(text: str) -> str:
    return f'<p class="cd-caption">{escape(text)}</p>'


def notice_html(message: str, kind: str = "info") -> str:
    role = "alert" if kind == "error" else "status"
    return f'<div class="cd-notice cd-notice--{escape(kind)}" role="{role}">{escape(message)}</div>'


def empty_state_html(title: str, message: str) -> str:
    return f'<div class="cd-empty" role="status"><h3>{escape(title)}</h3><p>{escape(message)}</p></div>'


def pager_text(first: int, last: int, total: int, number: int, pages: int, noun: str) -> str:
    if not total:
        return f"0 {noun}"
    return f"{first}–{last} de {total:,} {noun} · Página {number} de {pages}".replace(",", ".")


def inject_styles() -> None:
    import streamlit as st

    st.html(STYLES)


def render(markup: str) -> None:
    import streamlit as st

    st.html(markup)
