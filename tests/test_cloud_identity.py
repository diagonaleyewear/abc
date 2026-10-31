import ast
import base64
from pathlib import Path
import re
import tomllib

import pytest
from streamlit.string_util import validate_icon_or_emoji

import cloud_design as design

ROOT = Path(__file__).resolve().parents[1]


def test_native_and_custom_controls_share_one_palette():
    config = tomllib.loads((ROOT / ".streamlit" / "config.toml").read_text())
    assert design.BRAND_GREEN == config["theme"]["primaryColor"] == "#417B7B"
    assert design.palette("light")["accent"] == design.BRAND_GREEN
    assert design.palette("dark")["accent"] == config["theme"]["dark"]["primaryColor"]
    for theme in ("light", "dark"):
        colours = design.palette(theme)
        for foreground, background, minimum in design.CONTRAST_PAIRS:
            assert design.contrast_ratio(colours[foreground], colours[background]) >= minimum
        assert colours["danger"] != colours["accent"] != colours["success"]


@pytest.mark.parametrize("variant", ["a", "b"])
def test_home_header_preserves_original_logo_and_accessible_product_name(variant):
    original = (ROOT / "logo" / "logo.png").read_bytes()
    html = design.home_header_html(original, variant)
    encoded = re.search(r'data:image/png;base64,([^"]+)', html).group(1)
    assert base64.b64decode(encoded) == original
    assert 'alt="Óptica Diagonal"' in html
    assert html.count("<h1") == 1
    assert ">Cloud</h1>" in html and "Cloud Diagonal" not in html
    assert "DIAGONAL EYEWEAR" in html
    assert "Gestión inteligente, información conectada." in html
    assert html.index("DIAGONAL EYEWEAR") < html.index(">Cloud</h1>") < html.index("Gestión inteligente")
    assert "filter:" not in design.STYLES
    if variant == "a":
        assert html.index("cd-home__identity") < html.index("cd-home__logo")
        assert ".cd-home--a .cd-home__logo { margin-left: auto; }" in design.STYLES


def test_invalid_header_variant_is_explicit():
    with pytest.raises(ValueError, match="Unknown home header variant"):
        design.home_header_html(b"", "other")


def test_inter_is_local_licensed_and_shared_by_body_and_headings():
    theme = design.THEME
    assert theme["font"] == theme["headingFont"] == "Inter, sans-serif"
    assert design.TOKENS["font"]["body"] == design.TOKENS["font"]["display"] == theme["font"]
    face = theme["fontFaces"][0]
    assert face["url"] == "app/static/fonts/Inter.ttf"
    assert (ROOT / "static" / "fonts" / "Inter.ttf").stat().st_size > 100_000
    assert "SIL OPEN FONT LICENSE Version 1.1" in (ROOT / "static" / "fonts" / "OFL.txt").read_text()


def test_native_adapters_do_not_change_control_geometry_or_semantic_alerts():
    css = design.build_widget_styles()
    declarations = re.findall(r"([\w-]+)\s*:", " ".join(re.findall(r"\{([^}]+)\}", css)))
    assert set(declarations) <= {
        "color", "background-color", "outline-color", "fill", "stroke",
        "font-size", "font-weight", "letter-spacing", "line-height",
        "text-decoration", "text-underline-offset",
    }
    widget_rules = css.split('.stApp button[kind^="primary"]')[1]
    assert not re.search(r"\b(?:width|height|padding|margin|font-size|line-height)\s*:", widget_rules)
    assert 'aria-current="page"' in css
    active = css.split('[aria-current="page"]')[1].split("}")[0]
    assert "background-color: transparent" in active
    assert "text-decoration: none" in active
    label = css.split('[aria-current="page"] p')[1].split("}")[0]
    assert "text-decoration: underline" in label
    assert f"color: {design.palette('auto')['accent']}" in active
    assert "input:disabled" in css
    assert "stAlert" not in css
    comparator = (ROOT / "app_pages" / "price_comparator.py").read_text(encoding="utf-8")
    assert 'data-baseweb="tag"' not in comparator


def test_every_production_destination_uses_one_native_icon_family_without_route_changes():
    expected = {
        "HOME_PAGE": "", "ABC_HOME_PAGE": "abc", "ABC_SEARCH_PAGE": "abc-buscar",
        "ABC_REPORTS_PAGE": "abc-reportes", "ABC_HISTORY_PAGE": "abc-historico",
        "ABC_DETAIL_PAGE": "abc-analisis", "MASTER_IMPORT_PAGE": "master",
        "MASTER_DRYRUN_PAGE": "master-dry-run", "MASTER_DICTIONARY_PAGE": "master-diccionario",
        "KERING_IMAGES_PAGE": "imagenes-kering", "MASTERDATA_KERING_IMAGES_PAGE": "imagenes-kering",
        "PROVIDER_IMAGES_PAGE": "proveedores-imagenes", "LUXOPTICA_IMAGES_PAGE": "repositorio-imagenes",
        "LUXOPTICA_PENDING_PAGE": "pendiente-luxoptica", "ALERTA_PEDIDOS_PAGE": "alerta-pedidos",
        "SETTINGS_PAGE": "config", "ODOO_RECONCILIATION_PAGE": "conciliacion-odoo",
        "PRICE_COMPARATOR_PAGE": "comparador-precios",
    }
    found = {}
    tree = ast.parse((ROOT / "app_enhanced.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        if not isinstance(call.func, ast.Attribute) or call.func.attr != "Page":
            continue
        kwargs = {kw.arg: kw.value for kw in call.keywords}
        found[node.targets[0].id] = ast.literal_eval(kwargs["url_path"])
        icon = kwargs["icon"]
        assert isinstance(icon, ast.Subscript) and icon.value.id == "NAV_ICONS"
        value = design.NAV_ICONS[ast.literal_eval(icon.slice)]
        assert value.startswith(":material/") and validate_icon_or_emoji(value) == value
    assert found == expected


def test_docker_ships_the_same_theme_and_font_as_local_execution():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY .streamlit/config.toml ./.streamlit/config.toml" in dockerfile
    assert "COPY static ./static" in dockerfile
    assert "primaryColor =" not in dockerfile
