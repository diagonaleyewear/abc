"""Cloud Diagonal design system: tokens and reusable components.

Colour/radius/font values come from the public Vytria theme (theme.css, custom.css):
body text #42403B, page tint #fbfbfa, neutral #f2f2f2, border #d9d9d9, accent #003399,
highlight #FFCB03, radii 0/4px, Arial sans body with Cormorant Garamond for display text.
The font file is not bundled (licence unverified): headings fall back to Georgia.

All styles target our own `cd-*` classes. No Streamlit-internal selectors are used.
Builders return escaped HTML strings so they can be tested without Streamlit.
"""
from __future__ import annotations

import base64
from html import escape

TOKENS = {
    "color": {
        "ink": "#000000", "text": "#42403B", "muted": "#5f5d58", "surface": "#ffffff",
        "tint": "#fbfbfa", "neutral": "#f2f2f2", "border": "#d9d9d9", "accent": "#003399",
        "highlight": "#FFCB03", "success": "#1e6b3a", "warning": "#7a5200", "danger": "#b3361a",
    },
    "font": {
        "body": 'Arial, "Helvetica Neue", Helvetica, sans-serif',
        "display": '"Cormorant Garamond", Georgia, "Times New Roman", serif',
    },
    "space": {"xs": "4px", "sm": "8px", "md": "16px", "lg": "24px", "xl": "32px"},
    "radius": {"sm": "0", "md": "4px"},
    "control": {"height": "40px", "target": "44px"},
    "breakpoint": {"mobile": 640, "tablet": 1024},
    "card": {"media_ratio": "4 / 3", "min_width": "200px"},
}

_C, _S = TOKENS["color"], TOKENS["space"]

STYLES = f"""
<style>
.cd-header {{ padding: {_S['sm']} 0 0; margin-bottom: {_S['sm']}; font-family: {TOKENS['font']['body']}; }}
.cd-header__title {{ font-family: {TOKENS['font']['display']}; color: {_C['ink']}; font-size: 2rem; font-weight: 600; line-height: 1.15; margin: 0; }}
.cd-header__sub {{ color: {_C['muted']}; margin: {_S['xs']} 0 0; font-size: .95rem; }}
ul.cd-stats {{ display: flex; flex-wrap: wrap; gap: {_S['sm']}; margin: {_S['md']} 0 0; padding: 0; padding-inline-start: 0; list-style: none; }}
ul.cd-stats li.cd-stat {{ margin: 0; background: {_C['neutral']}; border-radius: {TOKENS['radius']['md']}; padding: {_S['sm']} {_S['md']}; color: {_C['text']}; font-size: .9rem; }}
.cd-stat strong {{ color: {_C['ink']}; font-size: 1.15rem; margin-right: {_S['xs']}; }}
.cd-card {{ background: {_C['surface']}; border: 1px solid {_C['border']}; border-radius: {TOKENS['radius']['md']}; overflow: hidden; display: flex; flex-direction: column; font-family: {TOKENS['font']['body']}; }}
.cd-card__media {{ aspect-ratio: {TOKENS['card']['media_ratio']}; flex: 0 0 auto; background: {_C['neutral']}; display: flex; align-items: center; justify-content: center; overflow: hidden; }}
.cd-card__media img {{ width: 100%; height: 100%; object-fit: contain; display: block; }}
.cd-card__placeholder {{ color: {_C['muted']}; font-size: .85rem; text-align: center; padding: {_S['sm']}; }}
.cd-card__body {{ padding: {_S['sm']} {_S['md']} {_S['md']}; display: flex; flex-direction: column; gap: 2px; min-height: 142px; }}
.cd-card__brand {{ margin: 0; color: {_C['muted']}; font-size: .75rem; letter-spacing: .06em; text-transform: uppercase; min-height: 1em; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.cd-card__title {{ margin: 0; color: {_C['ink']}; font-size: 1rem; font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.cd-card__meta {{ margin: 0; color: {_C['text']}; font-size: .85rem; font-variant-numeric: tabular-nums; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
ul.cd-badges {{ display: flex; flex-wrap: wrap; gap: {_S['xs']}; margin: {_S['xs']} 0 0; padding: 0; padding-inline-start: 0; list-style: none; }}
ul.cd-badges li.cd-badge {{ margin: 0; font-size: .75rem; font-weight: 700; padding: 1px 6px; border-radius: {TOKENS['radius']['md']}; border: 1px solid {_C['border']}; }}
ul.cd-badges li.cd-badge--on {{ color: {_C['accent']}; border-color: {_C['accent']}; background: #eef2fb; }}
ul.cd-badges li.cd-badge--off {{ color: {_C['muted']}; text-decoration: line-through; }}
.cd-photo {{ background: {_C['neutral']}; border: 1px solid {_C['border']}; border-radius: {TOKENS['radius']['md']}; aspect-ratio: {TOKENS['card']['media_ratio']}; display: flex; align-items: center; justify-content: center; overflow: hidden; }}
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
            f'<li class="cd-badge cd-badge--{state}" aria-label="{escape(label)} {spoken}">'
            f'{escape(label)} {mark}</li>'
        )
    return f'<ul class="cd-badges" aria-label="Vistas">{"".join(parts)}</ul>'


def product_card_html(*, title: str, brand: str, reference: str, ean: str, count: int,
                      views: tuple[tuple[str, bool], ...], preview: bytes | None,
                      placeholder: str = "Sin miniatura") -> str:
    noun = "imagen" if count == 1 else "imágenes"
    ref = f"Ref. {reference} · " if reference else ""
    return (
        '<article class="cd-card">'
        f'<div class="cd-card__media">{_media(preview, title, placeholder)}</div>'
        '<div class="cd-card__body">'
        f'<p class="cd-card__brand">{escape(brand)}</p>'
        f'<h3 class="cd-card__title" title="{escape(title, quote=True)}">{escape(title)}</h3>'
        f'<p class="cd-card__meta">{escape(ref)}EAN {escape(ean)}</p>'
        f'<p class="cd-card__meta">{count} {noun}</p>'
        f'{view_badges_html(views)}'
        '</div></article>'
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
