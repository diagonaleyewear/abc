from hashlib import sha256
from io import BytesIO
from unittest.mock import Mock

from PIL import Image
import pytest
from streamlit.testing.v1 import AppTest

import cloud_design as design
import image_library as library
from image_repository import ImageRecord, ImageRepository
import image_thumbnails as thumbnails
import kering_images_ui as ui

PAGE = (
    "import streamlit as st\n"
    "from app_pages.luxoptica import render_images_page\n"
    "st.session_state['auth_role']='{role}'\n"
    "def render_sidebar_shell(*args): pass\n"
    "def require_admin_access(): pass\n"
    "def render_footer(): pass\n"
    "render_images_page(render_sidebar_shell, require_admin_access, render_footer)"
)


def photo(color=(20, 30, 40)):
    stream = BytesIO()
    Image.new("RGB", (1200, 600), color).save(stream, "PNG")
    return stream.getvalue()


def record(ean, view, *, id=1, market="", provider="Kering", name=None, model="", date="2026-01-01T00:00:00"):
    return ImageRecord(id, ean, f"c{id}", None, None, provider, "synthetic", view, market,
                       name or f"{view}-{id}.png", date, {"modelo": model} if model else {})


@pytest.fixture
def repository(tmp_path, monkeypatch):
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    for key in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.setenv(key, str(temporary))
    monkeypatch.setattr(thumbnails.tempfile, "gettempdir", lambda: str(temporary))
    monkeypatch.setenv("IMAGE_REPOSITORY_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("IMAGE_REPOSITORY_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("IMAGE_THUMBNAIL_CACHE_ROOT", str(tmp_path / "thumbnails"))
    monkeypatch.setenv("KERING_DATA_ROOT", str(tmp_path / "kering"))
    monkeypatch.setenv("PROCESS_ACTIVITY_PATH", str(tmp_path / "receipts" / "activity.sqlite3"))
    monkeypatch.setenv("IMAGE_THUMBNAIL_CACHE_RESERVE_BYTES", "1")
    ui.service.clear()
    yield ImageRepository()
    ui.service.clear()


def add(repository, ean, view, n=0, *, provider="Kering", market="", model=None):
    return repository.save(
        ean, f"{ean}-{view}-{n}.png", photo((n * 7 % 255, 40, 90)), provider=provider,
        origin="synthetic", view=view, market=market, metadata={"modelo": model} if model else None,
    )[0]


def run(role="admin"):
    app = AppTest.from_string(PAGE.format(role=role), default_timeout=30)
    app.run()
    assert not app.exception
    return app


def test_groups_one_card_per_ean_keeping_leading_zeros_and_semantics():
    records = [
        record("0012", "frontal", id=1, model="M1"), record("0012", "lateral", id=2, model="M1"),
        record("0012", "detalle", id=3, model="M1", market="Farfetch"),
        record("0099", "frontal", id=4, model="M1"), record("0100", "frontal", id=5),
    ]
    groups = library.group_records(records)
    assert sorted(g.ean for g in groups) == ["0012", "0099", "0100"]
    assert library.summary(groups) == {"images": 5, "eans": 3, "models": 1}
    first = next(g for g in groups if g.ean == "0012")
    assert first.cover.view == "frontal" and not first.cover.market
    assert first.missing_core == ("V2",)
    assert next(g for g in groups if g.ean == "0100").title == "EAN 0100"


def test_detail_images_are_never_promoted_to_v2():
    group = library.group_records([record("1", "detalle", id=i) for i in range(1, 4)])[0]
    assert group.missing_core == ("V1", "V2", "V3")
    assert library.view_label("detalle") == "Detalle" and library.view_label("raro") == "Sin clasificar"


def test_filters_and_partial_ean_search():
    groups = library.group_records([
        record("0012345", "frontal", id=1, model="Aviator", provider="Kering"),
        record("0012345", "perspectiva", id=2, provider="Kering"),
        record("0012345", "lateral", id=3, provider="Kering"),
        record("7700999", "frontal", id=4, model="Round", provider="Luxottica", market="Miinto"),
    ])
    find = lambda **kw: [g.ean for g in library.filter_groups(groups, library.LibraryFilters(**kw))]
    assert find(query="0012") == ["0012345"] and find(query="999") == ["7700999"]
    assert find(query="aviator") == ["0012345"] and find(query="luxot") == ["7700999"]
    assert find(provider="Luxottica") == ["7700999"] and find(market="Miinto") == ["7700999"]
    assert find(market="Original") == ["0012345"]
    assert find(view="V2") == ["0012345"] and find(view="Originales") == ["0012345"]
    assert find(availability=library.AVAILABILITY_FILTERS[1]) == ["0012345"]
    assert find(availability=library.AVAILABILITY_FILTERS[2]) == ["7700999"]
    assert find(query="nada") == []


def test_pagination_clamps_and_reports_ranges():
    items = list(range(50))
    page = library.paginate(items, 99, 24)
    assert (page.number, page.pages, page.first, page.last, len(page.items)) == (3, 3, 49, 50, 2)
    assert library.paginate([], 1, 24).first == 0 and library.paginate(items, 0, 24).number == 1
    assert library.paginate(items, 1, 7).pages == 3


def test_card_html_is_commercial_escaped_and_not_colour_only():
    html = design.product_card_html(
        title="<b>Modelo</b>", brand="Marca", reference="R-1", ean="0012", count=1,
        views=(("V1", True), ("V2", False)), preview=None)
    assert "&lt;b&gt;" in html and "<b>" not in html
    assert "Sin miniatura" in html and "1 imagen<" in html and "EAN 0012" in html
    assert "V1 ✓" in html and "V2 –" in html and "no disponible" in html
    assert "<style" not in html and "stBaseButton" not in design.STYLES and "data-testid" not in design.STYLES


def test_page_renders_cards_per_product_and_pages_by_product(repository):
    for index in range(30):
        for view in ("frontal", "lateral"):
            add(repository, f"{index + 1:013d}", view, index)
    app = run()
    first_page = [b for b in app.button if b.key and b.key.startswith("lib_open_")]
    assert len(first_page) == 24
    assert any("Página 1 de 2" in m.value for m in app.get("html"))
    app.button(key="lib_next").click().run()
    assert len([b for b in app.button if b.key.startswith("lib_open_")]) == 6
    app.text_input(key="lib_query").set_value("0000000000007").run()
    assert [b.key for b in app.button if b.key.startswith("lib_open_")] == ["lib_open_0000000000007"]
    app.selectbox(key="lib_view").select("V2").run()
    assert any("Sin resultados" in m.value for m in app.get("html"))
    assert app.text_input(key="lib_query").value == "0000000000007" and app.selectbox(key="lib_view").value == "V2"
    app.button(key="lib_clear").click().run()
    assert app.text_input(key="lib_query").value == "" and not app.exception


def test_filters_survive_opening_product_and_return(repository):
    add(repository, "0000000000001", "frontal", 1, model="Aviator")
    add(repository, "0000000000002", "frontal", 2, model="Round")
    app = run()
    assert "data:image/jpeg;base64," in " ".join(item.value for item in app.get("html"))
    app.text_input(key="lib_query").set_value("round").run()
    app.button(key="lib_open_0000000000002").click().run()
    assert app.button(key="lib_back")
    app.button(key="lib_back").click().run()
    assert app.text_input(key="lib_query").value == "round"
    assert [b.key for b in app.button if b.key.startswith("lib_open_")] == ["lib_open_0000000000002"]


def test_product_detail_zip_on_demand_download_and_originals_intact(repository, monkeypatch):
    saved = [add(repository, "0001234567890", view, n) for n, view in enumerate(("frontal", "lateral", "detalle"))]
    before = {r.path: sha256(r.path.read_bytes()).hexdigest() for r in saved}
    zipper = Mock(wraps=repository.zip_eans_for_user)
    monkeypatch.setattr(ImageRepository, "zip_eans_for_user", lambda self, *a, **k: zipper(*a, **k))
    app = run()
    app.button(key="lib_open_0001234567890").click().run()
    assert not app.exception and zipper.call_count == 0
    assert any("V2" in item.value for item in app.get("html"))
    assert len(app.get("download_button")) == 1
    app.button(key="lib_photo_next").click().run()
    assert not app.exception and app.get("download_button")
    app.button(key="lib_prepare_zip").click().run()
    assert zipper.call_count == 1 and len(app.get("download_button")) == 2
    app.run()
    assert zipper.call_count == 1
    assert {r.path: sha256(r.path.read_bytes()).hexdigest() for r in saved} == before


def test_technical_details_only_for_admin(repository):
    add(repository, "0001234567890", "frontal", 1)
    admin = run("admin")
    admin.button(key="lib_open_0001234567890").click().run()
    assert [e.label for e in admin.expander] == ["Detalles técnicos y trazabilidad"]
    other = run("masterdata")
    other.button(key="lib_open_0001234567890").click().run()
    assert not other.expander


def test_missing_and_damaged_images_use_placeholders_without_failing(repository):
    broken = add(repository, "0000000000001", "frontal", 1)
    add(repository, "0000000000002", "frontal", 2)
    broken.path.write_bytes(b"not an image")
    app = run()
    assert not app.exception
    html = " ".join(item.value for item in app.get("html"))
    assert "Sin miniatura" in html
    app.button(key="lib_open_0000000000001").click().run()
    assert not app.exception and (app.warning or app.error)


def test_empty_library_shows_empty_state_not_error(repository):
    app = run()
    assert not app.exception and not app.error
    assert any("aún no tiene imágenes" in item.value for item in app.get("html"))


def test_browsing_never_builds_zip_or_contacts_providers(repository, monkeypatch):
    add(repository, "0000000000001", "frontal", 1)
    zipper = Mock()
    monkeypatch.setattr(ImageRepository, "zip_eans", zipper)
    app = run()
    app.button(key="image_gallery_refresh").click().run()
    assert not app.exception and zipper.call_count == 0


def test_images_route_uses_only_the_new_library_page():
    import ast
    from pathlib import Path
    import app_pages.luxoptica as luxoptica
    from app_pages import image_library_page

    assert luxoptica.render_images_page is image_library_page.render_images_page
    tree = ast.parse((Path(__file__).parents[1] / "app_enhanced.py").read_text(encoding="utf-8"))
    assert "render_luxoptica_images_page" not in {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    page = next(n for n in tree.body if isinstance(n, ast.Assign)
                and getattr(n.targets[0], "id", "") == "LUXOPTICA_IMAGES_PAGE")
    assert "render_images_page" in ast.unparse(page) and "repositorio-imagenes" in ast.unparse(page)


def test_dockerfile_ships_every_local_module_imported_by_app_pages():
    import ast
    from pathlib import Path
    root = Path(__file__).parents[1]
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    for page in (root / "app_pages").glob("*.py"):
        for node in ast.walk(ast.parse(page.read_text(encoding="utf-8"))):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else (
                [node.module] if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module else [])
            for name in names:
                module = name.split(".")[0]
                if (root / f"{module}.py").is_file():
                    assert f"{module}.py" in dockerfile, f"{module}.py no se copia en el Dockerfile ({page.name})"


def meta_record(ean, view, metadata, *, id=1, provider="Luxoptica", name=None):
    return ImageRecord(id, ean, f"c{id}", None, None, provider, "synthetic", view, "",
                       name or f"{view}-{id}.jpg", "2026-01-01T00:00:00", metadata)


@pytest.mark.parametrize("technical", [
    "frontal.img", "frontal.jpg", "RB3025__frontal.jpg", "frontal", "lateral", "perspectiva", "0000000000001",
    "8056597123456", "", "  ", "n/a", "ruta/a/frontal.png", "C:\\fotos\\frontal.png",
])
def test_technical_values_never_become_the_commercial_model(technical):
    group = library.group_records([meta_record("0000000000001", "frontal", {"modelo": technical})])[0]
    assert group.model == "" and not group.has_commercial_title
    assert group.title == "EAN 0000000000001"


def test_model_resolution_order_name_then_model_then_ean():
    named = library.group_records([meta_record("1", "frontal", {"nombre_comercial": "Aviator Classic", "modelo": "RB3025"})])[0]
    assert named.title == "Aviator Classic" and named.has_commercial_title
    modelled = library.group_records([meta_record("2", "frontal", {"modelo": "RB3025"})])[0]
    assert modelled.title == "RB3025"
    referenced = library.group_records([meta_record("3", "frontal", {"modelo": "frontal.img", "referencia": "RB3025"})])[0]
    assert referenced.title == "RB3025"
    identity = library.group_records([meta_record("4", "frontal", {"identity": {"model": "SL 751 JEANNE"}})])[0]
    assert identity.title == "SL 751 JEANNE"
    assert library.group_records([meta_record("5", "frontal", {})])[0].title == "EAN 5"


def test_brand_is_kept_separate_from_model_and_never_invented():
    group = library.group_records([meta_record("1", "frontal", {"modelo": "RB3025", "marca": "Ray-Ban"})])[0]
    assert group.brand == "Ray-Ban" and group.title == "RB3025"
    assert library.group_records([meta_record("2", "frontal", {"modelo": "RB3025"})])[0].brand == ""


def test_file_name_of_the_record_is_not_a_title_even_with_unusual_extension():
    group = library.group_records([meta_record("1", "frontal", {"modelo": "foto-1"}, name="foto-1.webp")])[0]
    assert group.model == ""


def test_images_stay_attached_to_their_ean_when_models_repeat():
    groups = library.group_records([
        meta_record("1", "frontal", {"modelo": "RB3025"}, id=1), meta_record("1", "lateral", {"modelo": "RB3025"}, id=2),
        meta_record("2", "frontal", {"modelo": "RB3025"}, id=3),
    ])
    assert {g.ean: len(g.records) for g in groups} == {"1": 2, "2": 1}
    assert library.summary(groups) == {"images": 3, "eans": 2, "models": 1}


def test_summary_does_not_count_file_names_as_models():
    groups = library.group_records([
        meta_record("1", "frontal", {"modelo": "frontal.img"}, id=1),
        meta_record("2", "frontal", {"modelo": "RB3025__frontal.jpg"}, id=2),
        meta_record("3", "frontal", {"modelo": "RB3025"}, id=3),
    ])
    assert library.summary(groups)["models"] == 1


def test_metadata_coverage_reports_each_provider():
    rows = {r["provider"]: r for r in library.metadata_coverage([
        meta_record("1", "frontal", {"modelo": "SL 1"}, id=1, provider="Kering"),
        meta_record("2", "frontal", {"modelo": "frontal.img"}, id=2),
        meta_record("2", "lateral", {"modelo": "frontal.img"}, id=3),
        meta_record("3", "frontal", {}, id=4),
    ])}
    assert rows["Kering"]["eans_with_verified_model"] == 1
    lux = rows["Luxoptica"]
    assert (lux["images"], lux["eans"], lux["eans_with_technical_model_only"], lux["eans_without_model"]) == (3, 2, 1, 1)
    assert lux["eans_with_verified_model"] == 0 and lux["distinct_models"] == 0


def test_search_finds_commercial_name_and_brand():
    groups = library.group_records([meta_record("1", "frontal", {"nombre_comercial": "Aviator", "marca": "Ray-Ban"})])
    find = lambda q: [g.ean for g in library.filter_groups(groups, library.LibraryFilters(query=q))]
    assert find("aviator") == ["1"] and find("ray") == ["1"] and find("frontal.img") == []


def test_core_view_states_are_not_changed_by_this_fix():
    group = library.group_records([record("1", "frontal", id=1), record("1", "perspectiva", id=2)])[0]
    assert group.present_views >= {"frontal", "perspectiva"} and group.missing_core == ("V3",)


def test_card_without_commercial_name_is_neutral_and_never_shows_a_file_name():
    html = design.product_card_html(
        title="EAN 0012", brand="", reference="", ean="0012", count=2, named=False, provider="Luxoptica",
        views=(("V1", True), ("V2", False), ("V3", False)), preview=None)
    assert "Modelo no disponible" in html and "frontal" not in html
    assert "EAN 0012" in html and "faltan V2, V3" in html and "Luxoptica" in html
    assert "cd-card__title--neutral" in html


def test_card_with_name_shows_provider_brand_and_model_separately():
    html = design.product_card_html(
        title="RB3025", brand="Ray-Ban", reference="RB3025", ean="0012", count=3, provider="Luxoptica",
        views=(("V1", True), ("V2", True), ("V3", True)), preview=None)
    assert "Luxoptica" in html and "Ray-Ban" in html and "RB3025" in html and "faltan" not in html
    assert "3 imágenes" in html


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_design_tokens_meet_wcag_aa_in_both_themes(theme):
    colours = design.palette(theme)
    for foreground, background, minimum in design.CONTRAST_PAIRS:
        ratio = design.contrast_ratio(colours[foreground], colours[background])
        assert ratio >= minimum, f"{theme}: {foreground} sobre {background} = {ratio:.2f}"


def test_auto_theme_resolves_both_palettes_in_the_browser_without_hardcoded_colours():
    assert "light-dark(" in design.STYLES
    assert design.palette("auto")["ink"] == f"light-dark({design.palette('light')['ink']}, {design.palette('dark')['ink']})"
    assert design.palette("dark")["ink"] != design.palette("light")["ink"]


def test_styles_are_responsive_and_scoped_to_our_own_classes():
    css = design.STYLES
    assert "repeat(auto-fill" in css and "st-key-lib_grid" in css and "st-key-lib_filters" in css
    assert "focus-visible" in css and "stBaseButton" not in css and "data-testid" not in css
    assert "repeat(4" not in css
    for selector in css.split("{")[:-1]:
        for part in selector.split("}")[-1].split(","):
            part = part.strip()
            assert not part or part.startswith(("div.st-key-", "div[class*=", ".cd-", "ul.cd-", "@", "li.cd-", "a.cd-", "p.cd-", "article.cd-", "h3.cd-")) or "cd-" in part, part


def test_page_uses_one_container_grid_not_fixed_columns(repository):
    add(repository, "0000000000001", "frontal", 1)
    app = run()
    assert not app.exception
    from pathlib import Path
    source = (Path(__file__).parents[1] / "app_pages" / "image_library_page.py").read_text(encoding="utf-8")
    assert "lib_grid" in source and "st.columns(COLUMNS)" not in source and "COLUMNS = 4" not in source
