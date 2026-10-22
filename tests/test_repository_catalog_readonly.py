import hashlib
import sqlite3
from pathlib import Path

import pytest

import repository_catalog_readonly as ro
from image_repository import ImageRepository
from provider_support import make_ean, photo

U1 = make_ean("003600029145")[1:]  # synthetic UPC-12
U2 = make_ean("007700000001")[1:]


def snapshot(folder: Path):
    return {str(p.relative_to(folder)): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            if p.is_file() else None for p in sorted(folder.rglob("*"))}


def make_catalog(path: Path, rows=(), wal=False):
    connection = sqlite3.connect(path)
    if wal:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.executescript(
        "CREATE TABLE assets(id INTEGER PRIMARY KEY, ean TEXT, checksum TEXT, pixels TEXT, path TEXT);"
        "CREATE TABLE representations(id INTEGER PRIMARY KEY, asset_id INTEGER, provider TEXT, origin TEXT,"
        " view TEXT, market TEXT, name TEXT, date TEXT, metadata TEXT);")
    for n, (ean, view, rel) in enumerate(rows, 1):
        connection.execute("INSERT INTO assets VALUES (?,?,?,?,?)", (n, ean, f"c{n}", f"p{n}", rel))
        connection.execute("INSERT INTO representations VALUES (?,?,?,?,?,?,?,?,?)",
                           (n, n, "marcolin", "zip", view, "", "n", "2026-09-01", "{}"))
    connection.commit()
    return connection


@pytest.fixture(autouse=True)
def never_construct_repository(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("ImageRepository must not be constructed by the read-only reader")
    monkeypatch.setattr(ImageRepository, "__init__", boom)


def test_missing_or_unset_catalog_creates_nothing(tmp_path):
    missing = tmp_path / "state" / ".catalog.sqlite3"
    result = ro.catalog_coverage([U1], catalog=missing)
    assert result["catalog"]["blocker"] == "catalogo_inexistente" and not (tmp_path / "state").exists()
    assert result["coverage"][U1]["label"] == "desconocida"
    assert ro.catalog_coverage([U1], env={})["catalog"]["blocker"] == "catalogo_no_configurado"


def test_reading_leaves_a_real_catalog_byte_identical(tmp_path):
    path = tmp_path / ".catalog.sqlite3"
    make_catalog(path, [(U1, "frontal", "m/a.jpg")]).close()
    before = snapshot(tmp_path)
    result = ro.catalog_coverage([U1, U2], catalog=path)
    assert snapshot(tmp_path) == before
    assert result["coverage"][U1]["label"] == "parciales"
    assert result["coverage"][U1]["basis"] == ro.BASIS_METADATA
    assert result["coverage"][U2]["label"] == "sin_registros"


def test_three_states_are_distinct_and_inaccessible_is_never_absent(tmp_path):
    path = tmp_path / "c.sqlite3"
    make_catalog(path, [(U1, "frontal", "staging/x.jpg"), (U2, "lateral", "m/b.jpg")]).close()
    result = ro.catalog_coverage([U1, U2, make_ean("009900000001")[1:]], catalog=path,
                                 required_views=("lateral",))
    labels = {c: v["label"] for c, v in result["coverage"].items()}
    assert list(labels.values()) == ["sin_vistas_validas", "completas", "sin_registros"]
    path.write_bytes(b"not a database" * 50)
    broken = ro.catalog_coverage([U1], catalog=path)
    assert broken["catalog"]["status"] == "inaccesible" and broken["coverage"][U1]["label"] == "desconocida"


def test_incompatible_schema_is_unknown(tmp_path):
    path = tmp_path / "c.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE assets(id INTEGER)")
    connection.commit()
    connection.close()
    result = ro.catalog_coverage([U1], catalog=path)
    assert result["catalog"]["blocker"] == "esquema_incompatible"
    assert result["coverage"][U1]["label"] == "desconocida"


def test_leading_zero_equivalent_is_found_and_literal_code_is_the_key_of_the_result(tmp_path):
    path = tmp_path / "c.sqlite3"
    make_catalog(path, [("0" + U1, "frontal", "m/a.jpg")]).close()
    result = ro.catalog_coverage([U1], catalog=path)
    assert result["coverage"][U1]["records"] == 1


def test_active_wal_rows_are_visible_without_checkpoint(tmp_path):
    path = tmp_path / "c.sqlite3"
    writer = make_catalog(path, wal=True)
    writer.execute("INSERT INTO assets VALUES (1,?,?,?,?)", (U1, "c", "p", "m/a.jpg"))
    writer.execute("INSERT INTO representations VALUES (1,1,'marcolin','zip','frontal','','n','d','{}')")
    writer.commit()
    wal = Path(str(path) + "-wal")
    size = wal.stat().st_size
    try:
        result = ro.catalog_coverage([U1], catalog=path)
        assert result["coverage"][U1]["records"] == 1 and result["catalog"]["wal_file_present"]
        assert result["catalog"]["journal_mode"] == "wal" and wal.stat().st_size >= size > 0
    finally:
        writer.close()
    assert "immutable" not in Path(ro.__file__).read_text(encoding="utf-8").split('"""')[2]


def test_verified_files_versus_metadata(tmp_path, monkeypatch):
    monkeypatch.undo()  # a real repository is needed to build the fixture
    repo = ImageRepository(tmp_path / "images", state=tmp_path / "state", backups=tmp_path / "bak")
    repo.save(U1, "front.png", photo(5), provider="marcolin", origin="test", view="frontal")
    catalog = tmp_path / "state" / ".catalog.sqlite3"
    before = snapshot(tmp_path)
    meta = ro.catalog_coverage([U1], catalog=catalog)
    files = ro.catalog_coverage([U1], catalog=catalog, image_root=tmp_path / "images", verify_files=True)
    assert snapshot(tmp_path) == before
    assert meta["coverage"][U1]["basis"] == ro.BASIS_METADATA and meta["coverage"][U1]["views"] == ["frontal"]
    assert files["coverage"][U1]["basis"] == ro.BASIS_FILES and not files["coverage"][U1]["issues"]
    stored = next((tmp_path / "images").rglob("*.png"))
    stored.write_bytes(photo(6))  # tamper: checksum no longer matches
    broken = ro.catalog_coverage([U1], catalog=catalog, image_root=tmp_path / "images", verify_files=True)
    assert broken["coverage"][U1]["issues"][0]["issue"] == "checksum_incorrecto"
    assert broken["coverage"][U1]["label"] == "sin_vistas_validas"
    missing_root = ro.catalog_coverage([U1], catalog=catalog, verify_files=True)
    assert missing_root["catalog"]["blocker"] == "raiz_imagenes_no_configurada"


@pytest.mark.parametrize("relative,issue", [("staging/x.jpg", "ruta_excluida"), ("../x.jpg", "ruta_excluida"),
                                            ("nope.jpg", "archivo_ausente")])
def test_path_rules(tmp_path, relative, issue):
    record = {"record_id": 1, "view": "frontal", "path": relative, "checksum": "x", "pixels": "y"}
    assert ro.check_record(record, tmp_path, True)[2] == issue
    assert ro.check_record({**record, "view": "bogus", "path": "m/a.jpg"}, tmp_path, False)[2] == "vista_no_valida"
