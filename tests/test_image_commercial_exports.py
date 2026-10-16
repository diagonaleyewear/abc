from io import BytesIO
import json
from zipfile import ZipFile

import pytest
from PIL import Image

import graph_mail_downloader as graph
from image_exports import (
    build_commercial_export_plan,
    main,
    publish_commercial_export_plan,
)
from image_repository import ImageRepository


EAN = "0012345678901"


@pytest.fixture(autouse=True)
def isolated_repository(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAGE_REPOSITORY_ROOT", str(tmp_path / "images"))


def image_bytes(color: tuple[int, int, int], image_format: str = "PNG") -> bytes:
    output = BytesIO()
    Image.new("RGB", (800, 800), color).save(output, image_format)
    return output.getvalue()


def save_view(repository, view: str, name: str, content: bytes, provider: str = "Kering"):
    return repository.save(
        EAN, name, content, provider=provider, origin="fixture", view=view,
    )[0]


def commercial_ids():
    return {EAN: {"Farfetch": "FF-001", "Miinto": "70eb4458-0cdb-48be-99fc-afc72667624a"}}


def test_exports_all_views_without_changing_deduplicated_originals():
    repository = ImageRepository()
    originals = {
        "frontal": save_view(repository, "frontal", "opaque-frontal", image_bytes((10, 0, 0))),
        "perspectiva": save_view(repository, "perspectiva", "opaque-perspective", image_bytes((0, 10, 0))),
        "lateral": save_view(repository, "lateral", "opaque-lateral", image_bytes((0, 0, 10))),
    }
    original_paths = {record.path for record in originals.values()}
    plan = build_commercial_export_plan(repository, commercial_ids())

    published = publish_commercial_export_plan(repository, plan)

    assert len(published) == 6
    for market, extension, market_id in (
        ("Farfetch", ".png", "FF-001"),
        ("Miinto", ".jpeg", commercial_ids()[EAN]["Miinto"]),
    ):
        for view, code in (("frontal", "V1"), ("perspectiva", "V2"), ("lateral", "V3")):
            export = repository.root / "commercial_exports" / market / EAN / f"{market_id}_{code}{extension}"
            assert export.is_file()
            assert export.read_bytes() == originals[view].path.read_bytes()
    assert {record.path for record in repository.records(EAN)} == original_paths
    assert len({record.path for record in repository.records(EAN)}) == 3
    assert all(path.exists() for path in original_paths)
    assert plan.missing_ids == {}
    assert plan.missing_views == {}


def test_partial_export_reports_missing_ids_and_views():
    repository = ImageRepository()
    save_view(repository, "frontal", "kering-frontal.png", image_bytes((1, 2, 3)))
    save_view(repository, "lateral", "kering-lateral.png", image_bytes((3, 2, 1)))

    plan = build_commercial_export_plan(repository, {EAN: {"Farfetch": "FF-002"}})
    published = publish_commercial_export_plan(repository, plan)

    assert len(published) == 2
    assert plan.missing_ids == {EAN: ["Miinto"]}
    assert plan.missing_views == {f"Farfetch:{EAN}": ["V2"]}
    assert {path.name for path in published} == {"FF-002_V1.png", "FF-002_V3.png"}


def test_extensionless_kering_png_gets_commercial_extension_without_renaming_source():
    repository = ImageRepository()
    original = save_view(repository, "frontal", "opaque-resource", image_bytes((11, 12, 13)))

    plan = build_commercial_export_plan(repository, commercial_ids())
    publish_commercial_export_plan(repository, plan)

    assert original.path.name == "opaque-resource"
    assert original.path.read_bytes() == image_bytes((11, 12, 13))
    assert (repository.root / "commercial_exports" / "Farfetch" / EAN / "FF-001_V1.png").read_bytes() == original.path.read_bytes()
    assert (repository.root / "commercial_exports" / "Miinto" / EAN /
            f"{commercial_ids()[EAN]['Miinto']}_V1.jpeg").read_bytes() == original.path.read_bytes()


def test_farfetch_converts_jpeg_but_miinto_preserves_png_byte_contract():
    repository = ImageRepository()
    original_bytes = image_bytes((30, 20, 10), "JPEG")
    original = save_view(repository, "frontal", "kering-frontal.jpg", original_bytes)

    plan = build_commercial_export_plan(repository, commercial_ids())
    published = publish_commercial_export_plan(repository, plan)

    farfetch = repository.root / "commercial_exports" / "Farfetch" / EAN / "FF-001_V1.png"
    assert farfetch.read_bytes() != original_bytes
    with Image.open(farfetch) as image:
        assert image.format == "PNG"
    assert all(path.parent.parent.name != "Miinto" for path in published)
    assert plan.missing_views[f"Miinto:{EAN}"] == ["V1", "V2", "V3"]
    assert any(issue == f"unsupported_image:{EAN}:Miinto:frontal" for issue in plan.issues)
    assert original.path.read_bytes() == original_bytes


def test_conflicting_destination_blocks_without_overwriting_source_or_export():
    repository = ImageRepository()
    original = save_view(repository, "frontal", "front.png", image_bytes((7, 8, 9)))
    destination = repository.root / "commercial_exports" / "Farfetch" / EAN / "FF-001_V1.png"
    destination.parent.mkdir(parents=True)
    conflict_bytes = image_bytes((90, 80, 70))
    destination.write_bytes(conflict_bytes)

    plan = build_commercial_export_plan(repository, commercial_ids())

    assert "destination_conflict:commercial_exports/Farfetch/" + EAN + "/FF-001_V1.png" in plan.blockers
    with pytest.raises(ValueError, match="Publicacion bloqueada"):
        publish_commercial_export_plan(repository, plan)
    assert destination.read_bytes() == conflict_bytes
    assert original.path.read_bytes() == image_bytes((7, 8, 9))


def test_publication_is_idempotent_and_dry_run_does_not_write(monkeypatch, capsys):
    repository = ImageRepository()
    save_view(repository, "frontal", "front.png", image_bytes((15, 16, 17)))
    monkeypatch.setattr(graph, "_get_market_ids_by_ean", lambda eans: commercial_ids())

    assert main(["--dry-run"]) == 0
    dry_run = json.loads(capsys.readouterr().out)
    assert dry_run["mode"] == "dry-run"
    assert dry_run["bytes_to_write"] > 0
    assert not (repository.root / "commercial_exports").exists()

    plan = build_commercial_export_plan(repository, commercial_ids())
    first = publish_commercial_export_plan(repository, plan)
    repeated = build_commercial_export_plan(repository, commercial_ids())
    second = publish_commercial_export_plan(repository, repeated)
    assert len(first) == len(second) == 2
    assert repeated.bytes_to_write == 0
    assert all(not entry.write_required for entry in repeated.entries)


def test_unsafe_odoo_identifier_and_ambiguous_views_are_not_published():
    repository = ImageRepository()
    save_view(repository, "frontal", "front-a.png", image_bytes((21, 22, 23)))
    save_view(repository, "frontal", "front-b.png", image_bytes((31, 32, 33)))

    ambiguous = build_commercial_export_plan(repository, commercial_ids())
    assert f"ambiguous_view:{EAN}:frontal" in ambiguous.issues
    assert ambiguous.entries == []

    unsafe = build_commercial_export_plan(repository, {EAN: {"Farfetch": "../escape"}})
    assert f"invalid_market_id:{EAN}:Farfetch" in unsafe.blockers
    with pytest.raises(ValueError, match="Publicacion bloqueada"):
        publish_commercial_export_plan(repository, unsafe)
    assert not (repository.root.parent / "escape_V1.png").exists()


def test_luxoptica_zip_import_materializes_commercial_exports(tmp_path, monkeypatch):
    repository = ImageRepository()
    archive = tmp_path / "luxoptica.zip"
    source_name = "0RB2140__901_001A__noshad__fr.png"
    content = image_bytes((41, 42, 43))
    with ZipFile(archive, "w") as output:
        output.writestr(source_name, content)
    monkeypatch.setattr(graph, "_load_eans_by_image_key", lambda: {("RB2140", "901"): EAN})
    monkeypatch.setattr(graph, "_get_market_ids_by_ean", lambda eans: commercial_ids())

    imported = graph._extract_zip(archive, repository.root)

    original = repository.root / EAN / source_name
    exported = repository.root / "commercial_exports" / "Farfetch" / EAN / "FF-001_V1.png"
    assert imported and original.read_bytes() == content
    assert exported.read_bytes() == content
    assert any(record.market == "Farfetch" for record in repository.records(EAN))


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self.rows


class FakeConnection:
    def __init__(self, rows, batches):
        self.rows = rows
        self.batches = batches

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, query, parameters):
        assert "SELECT pp.barcode" in str(query)
        self.batches.append(parameters["eans"])
        return FakeResult([row for row in self.rows if row["ean"] in parameters["eans"]])


class FakeEngine:
    def __init__(self, rows):
        self.rows = rows
        self.batches = []
        self.disposed = False

    def connect(self):
        return FakeConnection(self.rows, self.batches)

    def dispose(self):
        self.disposed = True


@pytest.fixture
def fake_odoo(monkeypatch):
    rows = [
        {"ean": EAN, "siteweb_id": 2, "product_website_id": "MI-001"},
        {"ean": EAN, "siteweb_id": 3, "product_website_id": "FF-001"},
    ]
    engine = FakeEngine(rows)
    monkeypatch.setattr(graph, "load_db_config", lambda: type(
        "Config", (), {
            "host": "odoo.invalid", "database": "db", "user": "reader",
            "password": "not-used", "port": 5432,
        }
    )())
    monkeypatch.setattr(graph, "create_engine", lambda *args, **kwargs: engine)
    monkeypatch.setattr(graph, "database_options", lambda: {})
    monkeypatch.setattr(graph, "checkpoint", lambda: None)
    return engine


def test_odoo_market_lookup_uses_exact_ean_and_batches_queries(fake_odoo):
    eans = {EAN, *(f"{index:013d}" for index in range(600))}

    result = graph._get_market_ids_by_ean(eans)

    assert result[EAN] == {"Miinto": "MI-001", "Farfetch": "FF-001"}
    assert [len(batch) for batch in fake_odoo.batches] == [500, 101]
    assert fake_odoo.disposed


def test_odoo_market_lookup_rejects_ambiguous_channel_ids(fake_odoo):
    fake_odoo.rows.append({"ean": EAN, "siteweb_id": 3, "product_website_id": "FF-OTHER"})

    with pytest.raises(ValueError, match="ambiguos"):
        graph._get_market_ids_by_ean({EAN})
    assert fake_odoo.disposed
