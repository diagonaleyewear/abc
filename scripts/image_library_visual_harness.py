"""Visual harness for the image library: real page, synthetic repository, real browser.

    python scripts/image_library_visual_harness.py capture OUTPUT_DIR [--label before|after]

Seeds a throw-away repository (synthetic pictures only, nothing from the NAS), starts the
real Streamlit page for each theme and takes Playwright screenshots at the supported
viewports. It also reports horizontal overflow and the computed colours that matter for
contrast. Nothing outside the temporary directory is read or written.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VIEWPORTS = {"desktop-1440": (1440, 900), "laptop-1024": (1024, 800), "tablet-768": (768, 1000), "mobile-375": (375, 812)}
BACKGROUNDS = ((255, 255, 255), (12, 12, 12), (30, 110, 60), (214, 206, 190), None)


def _picture(index: int) -> bytes:
    from PIL import Image, ImageDraw

    background = BACKGROUNDS[index % len(BACKGROUNDS)]
    image = Image.new("RGBA", (1200, 600), background + (255,) if background else (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    colour = (40, 40, 40, 255) if background in (None, BACKGROUNDS[0], BACKGROUNDS[4]) else (240, 240, 240, 255)
    for centre in (380, 820):
        draw.ellipse((centre - 170, 200, centre + 170, 400), outline=colour, width=18)
    draw.line((550, 290, 650, 290), fill=colour, width=16)
    stream = BytesIO()
    if background and index % 2 == 0:
        image = image.convert("RGB").convert("P", palette=Image.Palette.ADAPTIVE, colors=16)
    image.save(stream, "PNG")
    return stream.getvalue()


def seed(repository) -> None:
    """Named Kering products, Luxoptica products whose stored 'modelo' is a file name, and bare EANs."""
    views = ("frontal", "perspectiva", "lateral", "detalle")
    cases = []
    for number in range(8):
        cases.append((f"80560{number:07d}", "Kering", {"modelo": ["SL 751 JEANNE", "GU1060S", "BOSS 1450", "GG1190S"][number % 4]}, views[: 1 + number % 4]))
    for number in range(8, 22):
        cases.append((f"80561{number:07d}", "Luxoptica", {"modelo": "frontal.img"}, views[: 1 + number % 3]))
    for number in range(22, 28):
        cases.append((f"80562{number:07d}", "Luxoptica", {"modelo": f"RB{3000 + number}__frontal.jpg"}, ("frontal", "lateral")))
    for number in range(28, 40):
        cases.append((f"80563{number:07d}", "Luxoptica", {"modelo": f"OX{5000 + number}", "marca": "Oakley"}, views[: 1 + number % 4]))
    for number in range(40, 46):
        cases.append((f"80564{number:07d}", "Kering", {}, ("frontal",)))
    for index, (ean, provider, metadata, present) in enumerate(cases):
        for offset, view in enumerate(present):
            repository.save(ean, f"{ean}-{view}.png", _picture(index + offset), provider=provider,
                            origin="synthetic", view=view, metadata=metadata or None)


def environment(base: Path) -> dict:
    return {
        **os.environ,
        "IMAGE_REPOSITORY_ROOT": str(base / "images"), "IMAGE_REPOSITORY_STATE_ROOT": str(base / "state"),
        "IMAGE_THUMBNAIL_CACHE_ROOT": str(base / "thumbnails"), "KERING_DATA_ROOT": str(base / "kering"),
        "PROCESS_ACTIVITY_PATH": str(base / "receipts" / "activity.sqlite3"),
        "IMAGE_THUMBNAIL_CACHE_RESERVE_BYTES": "1", "TMPDIR": str(base / "tmp"), "TMP": str(base / "tmp"), "TEMP": str(base / "tmp"),
    }


def page() -> None:
    import streamlit as st

    sys.path.insert(0, str(ROOT))
    from app_pages.luxoptica import render_images_page

    st.set_page_config(layout="wide", initial_sidebar_state="expanded")
    st.sidebar.write("Menú")
    st.session_state["auth_role"] = "admin"
    render_images_page(lambda *_: None, lambda: None, lambda: None)


MEASURE = """() => {
  const root = document.documentElement;
  const css = (selector, property) => { const el = document.querySelector(selector); return el ? getComputedStyle(el)[property] : null; };
  return {
    overflowX: root.scrollWidth > root.clientWidth + 1, scrollWidth: root.scrollWidth, clientWidth: root.clientWidth,
    appBackground: css('.stApp', 'backgroundColor'), title: css('.cd-header__title', 'color'),
    statValue: css('.cd-stat strong', 'color'), statBackground: css('.cd-stat', 'backgroundColor'),
    cardTitle: css('.cd-card__title', 'color'), cardBackground: css('[class*="st-key-lib_card_"]', 'backgroundColor'),
    eyebrow: css('.cd-card__eyebrow', 'color'), ean: css('.cd-card__ean', 'color'),
    cards: document.querySelectorAll('.cd-card').length,
    cardWidths: [...document.querySelectorAll('.cd-card')].slice(0, 8).map(el => Math.round(el.getBoundingClientRect().width)),
    cardHeights: [...new Set([...document.querySelectorAll('[class*="st-key-lib_card_"]')].map(el => Math.round(el.getBoundingClientRect().height)))],
    columns: [...new Set([...document.querySelectorAll('[class*="st-key-lib_card_"]')].map(el => Math.round(el.getBoundingClientRect().left)))].length,
    filtersOverlap: (() => { const f = [...document.querySelectorAll('.st-key-lib_filters > div')].map(el => el.getBoundingClientRect());
      return f.some((a, i) => f.some((b, j) => i < j && a.left < b.right - 1 && b.left < a.right - 1 && a.top < b.bottom - 1 && b.top < a.bottom - 1)); })(),
  };
}"""


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _serve(base: Path, theme: str):
    port = _free_port()
    process = subprocess.Popen(
        [sys.executable, "-m", "streamlit", "run", str(Path(__file__).resolve()), "--server.headless", "true",
         "--server.port", str(port), "--server.fileWatcherType", "none", "--browser.gatherUsageStats", "false",
         "--theme.base", theme, "--theme.primaryColor", "#417B7B"],
        cwd=ROOT, env=environment(base), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return process, port


def capture(output: Path, label: str) -> None:
    from playwright.sync_api import sync_playwright

    output.mkdir(parents=True, exist_ok=True)
    report = {}
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        (base / "tmp").mkdir()
        os.environ.update(environment(base))
        sys.path.insert(0, str(ROOT))
        from image_repository import ImageRepository

        seed(ImageRepository())
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            for theme in ("dark", "light"):
                process, port = _serve(base, theme)
                try:
                    for name, (width, height) in VIEWPORTS.items():
                        if theme == "light" and name not in ("desktop-1440", "mobile-375"):
                            continue
                        tab = browser.new_page(viewport={"width": width, "height": height})
                        for _ in range(60):
                            try:
                                tab.goto(f"http://127.0.0.1:{port}", wait_until="load", timeout=5000)
                                break
                            except Exception:
                                time.sleep(1)
                        tab.wait_for_selector(".cd-card", timeout=60000)
                        tab.wait_for_timeout(1500)
                        if width <= 640:
                            tab.locator('[data-testid="stSidebarCollapseButton"] button').first.click()
                            tab.wait_for_timeout(800)
                        report[f"{theme}-{name}"] = tab.evaluate(MEASURE)
                        tab.screenshot(path=str(output / f"{label}-{theme}-{name}.png"), full_page=False)
                        if name in ("desktop-1440", "mobile-375"):
                            tab.screenshot(path=str(output / f"{label}-{theme}-{name}-full.png"), full_page=True)
                        tab.close()
                    if theme == "dark":
                        tab = browser.new_page(viewport={"width": 1440, "height": 900})
                        tab.goto(f"http://127.0.0.1:{port}", wait_until="load")
                        tab.wait_for_selector(".cd-card", timeout=60000)
                        tab.get_by_role("button", name="Ver fotografías").first.click()
                        tab.wait_for_selector(".cd-photo, img", timeout=30000)
                        tab.wait_for_timeout(1500)
                        tab.screenshot(path=str(output / f"{label}-dark-detail-desktop-1440.png"), full_page=False)
                        tab.close()
                        tab = browser.new_page(viewport={"width": 1440, "height": 900})
                        tab.goto(f"http://127.0.0.1:{port}", wait_until="load")
                        tab.wait_for_selector(".cd-card", timeout=60000)
                        box = tab.get_by_placeholder("EAN (completo o parcial)", exact=False)
                        box.fill("SL 751")
                        box.press("Enter")
                        tab.wait_for_timeout(2500)
                        tab.screenshot(path=str(output / f"{label}-dark-named-desktop-1440.png"), full_page=False)
                        tab.close()
                finally:
                    process.terminate()
            browser.close()
    (output / f"{label}-measurements.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


def main(arguments: list[str]) -> int:
    if len(arguments) >= 2 and arguments[0] == "capture":
        label = arguments[arguments.index("--label") + 1] if "--label" in arguments else "after"
        capture(Path(arguments[1]), label)
        return 0
    print(__doc__)
    return 2


try:
    import streamlit as _streamlit

    _under_streamlit = _streamlit.runtime.exists()
except Exception:
    _under_streamlit = False

if _under_streamlit:
    page()
elif __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
