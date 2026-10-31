"""Offline visual evidence using real pages and isolated synthetic state.

Run: python scripts/cloud_identity_visual_harness.py OUTPUT --label before|after
No production credentials, Odoo calls, provider jobs or existing databases are used.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import socket
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(os.environ.get("CLOUD_VISUAL_ROOT", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT))


def forbidden(*args, **kwargs):
    raise AssertionError("Visual fixture must not contact Odoo or run provider jobs")


def fixture_navigation():
    """Use production route metadata and the real role-aware navigation builder."""
    import streamlit as st
    import cloud_design as design
    from app_navigation import build_navigation

    tree = ast.parse((ROOT / "app_enhanced.py").read_text(encoding="utf-8"))
    pages = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        if isinstance(call.func, ast.Attribute) and call.func.attr == "Page":
            kwargs = {}
            for kw in call.keywords:
                if kw.arg in {"title", "icon", "url_path", "default", "visibility"}:
                    if isinstance(kw.value, ast.Subscript):
                        kwargs[kw.arg] = design.NAV_ICONS[ast.literal_eval(kw.value.slice)]
                    else:
                        kwargs[kw.arg] = ast.literal_eval(kw.value)
            pages[node.targets[0].id] = st.Page(render_fixture, **kwargs)
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name) and n.func.id == "build_navigation")
    mapping = {ast.literal_eval(key): pages[value.id] for key, value in zip(call.args[2].keys, call.args[2].values)}
    return build_navigation(st, "admin", mapping)


def render_fixture() -> None:
    import streamlit as st
    import cloud_design as design

    st.session_state["auth_role"] = "admin"
    design.inject_styles()
    route = st.session_state.get("visual_route", "")
    screen = st.query_params.get("screen") or {
        "": "home", "repositorio-imagenes": "library", "comparador-precios": "comparator",
        "proveedores-imagenes": "providers",
    }.get(route, "controls")
    with st.sidebar:
        st.header("Cloud")
        st.caption("Entorno visual · datos sintéticos")
        st.markdown("[Cabecera A](?screen=a) · [Cabecera B](?screen=b) · [Controles](?screen=controls)")
    if screen == "home":
        from cloud_dashboard import ProcessStatus, render_dashboard

        render_dashboard({}, loader=lambda role: {
            "role": role, "queried": 1793390400,
            "processes": [ProcessStatus("kering", "Kering · imágenes de pedidos", "kering"),
                          ProcessStatus("luxoptica", "Luxottica · imágenes", "images")],
        })
    elif screen in {"a", "b"}:
        design.render(design.home_header_html(
            (ROOT / "logo" / "logo.png").read_bytes(), variant=screen))
        st.button("Actualizar", type="primary")
        st.caption("Última consulta · Estado persistido; sin acceso a proveedores.")
    elif screen == "library":
        from app_pages.image_library_page import render_images_page

        render_images_page(lambda *_: None, lambda: None, lambda: None)
    elif screen == "comparator":
        from app_pages.price_comparator import render_price_comparator_page

        render_price_comparator_page()
    elif screen in {"providers", "marcolin", "nuorder", "safilo"}:
        from provider_images_ui import render_provider_page

        render_provider_page(forbidden, selected_provider=None if screen == "providers" else screen)
    else:
        design.page_title("Controles compartidos", "ABC · Master Data · Configuración")
        st.caption("Galería visual de componentes. No reproduce la funcionalidad ni los datos del módulo seleccionado.")
        st.button("Buscar", type="primary")
        st.button("Actualizar", disabled=True, type="primary")
        st.checkbox("Seleccionado", value=True)
        st.checkbox("Deshabilitado", value=True, disabled=True)
        st.radio("Proveedor", ["Luxottica", "Kering"], horizontal=True)
        st.segmented_control("Vista", ["Catálogo", "Histórico"], default="Catálogo")
        st.multiselect("Marcas", ["Diagonal", "Otra"], default=["Diagonal"])
        st.multiselect("Marcas deshabilitadas", ["Diagonal"], default=["Diagonal"], disabled=True)
        st.selectbox("Selector", ["Primera", "Segunda"])
        st.text_input("Referencia")
        st.slider("Intervalo", 0, 100, 40)
        with st.form("fixture"):
            st.text_input("Nombre")
            st.form_submit_button("Guardar", type="primary")
        tabs = st.tabs(["Resumen", "Análisis", "Importación / exportación"])
        with tabs[0]:
            st.dataframe({"EAN": ["0012345678901"], "Estado": ["Pendiente"]},
                         selection_mode="multi-row", on_select="rerun")
        st.success("Proceso correcto")
        st.warning("Requiere revisión")
        st.error("Error de validación")
        design.render(design.empty_state_html("Sin resultados", "Revise los filtros."))


def prepare_fixture(base: Path):
    from scripts.image_library_visual_harness import environment, seed
    from image_repository import ImageRepository

    (base / "tmp").mkdir()
    env = environment(base)
    env.update({
        "PROVIDER_DATA_ROOT": str(base / "providers"),
        "PRICE_COMPARATOR_DB_PATH": str(base / "prices.sqlite3"),
        "CLOUD_VISUAL_ROOT": str(ROOT),
    })
    os.environ.update(env)
    seed(ImageRepository())
    if (ROOT / "static").is_dir():
        shutil.copytree(ROOT / "static", base / "static")
    entrypoint = base / "visual_app.py"
    entrypoint.write_text(
        "import runpy\n"
        f"runpy.run_path({str(Path(__file__).resolve())!r}, run_name='__main__')\n",
        encoding="utf-8",
    )
    return env, entrypoint


def preview(port: int) -> None:
    with tempfile.TemporaryDirectory(prefix="cloud-preview-") as directory:
        env, entrypoint = prepare_fixture(Path(directory))
        print(f"Isolated preview: http://127.0.0.1:{port} · synthetic state: {directory}", flush=True)
        subprocess.run([
            sys.executable, "-m", "streamlit", "run", str(entrypoint),
            "--server.port", str(port), "--server.address", "127.0.0.1",
            "--server.headless", "true", "--server.fileWatcherType", "none",
            "--browser.gatherUsageStats", "false",
        ], cwd=ROOT, env=env, check=True)


def capture(output: Path, label: str, source: Path | None = None) -> None:
    global ROOT
    if source:
        ROOT = source.resolve()
        sys.path.insert(0, str(ROOT))
    from playwright.sync_api import sync_playwright

    output.mkdir(parents=True, exist_ok=True)
    report = {}
    with tempfile.TemporaryDirectory(prefix="cloud-visual-") as directory:
        base = Path(directory)
        env, entrypoint = prepare_fixture(base)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                for theme in ("light", "dark"):
                    with socket.socket() as sock:
                        sock.bind(("127.0.0.1", 0))
                        port = sock.getsockname()[1]
                    with (output / f"{label}-{theme}-server.log").open("w") as log:
                        process = subprocess.Popen([
                            sys.executable, "-m", "streamlit", "run", str(entrypoint),
                            "--server.port", str(port), "--server.address", "127.0.0.1",
                            "--server.headless", "true", "--server.fileWatcherType", "none",
                            "--browser.gatherUsageStats", "false", "--theme.base", theme,
                        ], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
                        try:
                            for attempt in range(60):
                                try:
                                    urllib.request.urlopen(f"http://127.0.0.1:{port}/_stcore/health", timeout=1)
                                    break
                                except OSError:
                                    if process.poll() is not None or attempt == 59:
                                        raise RuntimeError(f"Preview failed; see {log.name}")
                                    time.sleep(.5)
                            screens = ["home", "controls", "library", "comparator", "marcolin", "nuorder", "safilo"]
                            if label != "before":
                                screens += ["a", "b"]
                            for screen in screens:
                                for name, width in (("desktop", 1440), ("mobile", 375)):
                                    page = browser.new_page(viewport={"width": width, "height": 1000},
                                                            color_scheme=theme)
                                    route = {"library": "repositorio-imagenes", "comparator": "comparador-precios",
                                             "marcolin": "proveedores-imagenes", "nuorder": "proveedores-imagenes",
                                             "safilo": "proveedores-imagenes"}.get(screen, "")
                                    page.goto(f"http://127.0.0.1:{port}/{route}?screen={screen}")
                                    page.locator('[data-testid="stButton"], .cd-card, [data-testid="stTabs"], [data-testid="stException"]').first.wait_for(
                                        timeout=60000)
                                    page.wait_for_timeout(800)
                                    page.evaluate("document.fonts.ready")
                                    errors = page.locator('[data-testid="stException"]').all_text_contents()
                                    if errors:
                                        raise AssertionError(f"{theme}/{screen}: {errors}")
                                    if width == 375:
                                        collapse = page.locator('[data-testid="stSidebarCollapseButton"] button')
                                        bounds = collapse.bounding_box()
                                        if bounds and bounds["x"] >= 0:
                                            collapse.click()
                                            page.wait_for_timeout(300)
                                    else:
                                        more = page.get_by_test_id("stSidebarNavViewButton")
                                        if more.is_visible():
                                            more.click()
                                            page.wait_for_timeout(300)
                                    if label == "after" and screen == "controls":
                                        page.get_by_role("button", name="Buscar", exact=True).hover()
                                        page.get_by_role("textbox", name="Referencia", exact=True).focus()
                                        page.get_by_text("Seleccionado", exact=True).click()
                                        assert not page.get_by_role("checkbox", name="Seleccionado", exact=True).is_checked()
                                        page.get_by_text("Seleccionado", exact=True).click()
                                        assert page.get_by_role("checkbox", name="Seleccionado", exact=True).is_checked()
                                        page.get_by_text("Kering", exact=True).click()
                                        assert page.get_by_role("radio", name="Kering", exact=True).is_checked()
                                        page.get_by_role("tab", name="Análisis", exact=True).click()
                                        page.get_by_role("tab", name="Resumen", exact=True).click()
                                        page.locator(".cd-empty").wait_for()
                                        page.wait_for_timeout(1000)
                                    page.locator('[data-testid="stMain"]').evaluate("(el) => { el.scrollTop = 0; }")
                                    data = page.evaluate("""() => ({
                                        overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
                                        appBackground: getComputedStyle(document.querySelector('.stApp')).backgroundColor,
                                        primary: [...document.querySelectorAll('button[kind^="primary"]')].map(e => ({
                                            text: e.textContent, disabled: e.disabled,
                                            background: getComputedStyle(e).backgroundColor, color: getComputedStyle(e).color,
                                            width: e.getBoundingClientRect().width, height: e.getBoundingClientRect().height
                                        })),
                                        font: getComputedStyle(document.querySelector('.stApp')).fontFamily,
                                        interLoaded: document.fonts.check('16px Inter'),
                                        fontFaces: [...document.fonts].filter(f => f.family === 'Inter').map(f => f.status),
                                        tags: [...document.querySelectorAll('[data-baseweb="tag"]')].map(e => ({
                                            background: getComputedStyle(e).backgroundColor, color: getComputedStyle(e).color
                                        })),
                                        nav: [...document.querySelectorAll('[data-testid="stSidebarNavLink"]')].map(e => ({
                                            label: e.textContent, current: e.getAttribute('aria-current'),
                                            color: getComputedStyle(e).color, background: getComputedStyle(e).backgroundColor
                                        })),
                                        tabs: [...document.querySelectorAll('[role="tab"]')].map(e => ({
                                            selected: e.getAttribute('aria-selected'), color: getComputedStyle(e).color
                                        }))
                                    })""")
                                    if label == "after":
                                        assert "loaded" in data["fontFaces"], f"Inter not loaded: {theme}/{screen}"
                                        assert data["appBackground"] == (
                                            "rgb(14, 17, 23)" if theme == "dark" else "rgb(255, 255, 255)"
                                        ), f"Incorrect {theme} theme: {data['appBackground']}"
                                        import cloud_design as design

                                        for button in data["primary"]:
                                            if button["disabled"]:
                                                continue
                                            def hex_colour(value):
                                                channels = value.removeprefix("rgb(").removesuffix(")").split(",")
                                                return "#" + "".join(f"{int(channel):02x}" for channel in channels)
                                            assert design.contrast_ratio(
                                                hex_colour(button["color"]), hex_colour(button["background"])
                                            ) >= 4.5, f"Insufficient primary contrast: {theme}/{screen}/{button}"
                                    report[f"{theme}-{screen}-{name}"] = data
                                    page.screenshot(path=str(output / f"{label}-{theme}-{screen}-{name}.png"),
                                                    full_page=True)
                                    page.close()
                        finally:
                            process.terminate()
                            process.wait(timeout=20)
            finally:
                browser.close()
    (output / f"{label}-measurements.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    assert not any(item["overflow"] for item in report.values()), "Horizontal overflow detected"
    print(f"{label}: {len(report)} captures; no horizontal overflow")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("output", type=Path, nargs="?")
        parser.add_argument("--label", choices=("before", "after"))
        parser.add_argument("--source", type=Path, help="Pristine baseline worktree, for reproducible before captures")
        parser.add_argument("--preview", action="store_true", help="Serve an isolated interactive preview")
        parser.add_argument("--port", type=int, default=8601)
        args = parser.parse_args()
        if args.preview:
            preview(args.port)
        elif args.output and args.label:
            capture(args.output, args.label, args.source)
        else:
            parser.error("Provide OUTPUT and --label, or --preview")
    else:
        import streamlit as st

        st.set_page_config(layout="wide")
        navigation = fixture_navigation()
        st.session_state["visual_route"] = navigation.url_path
        navigation.run()
