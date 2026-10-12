from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import date
import hashlib
from io import BytesIO
import json
import multiprocessing
from pathlib import Path
import sqlite3
from threading import Event
from zipfile import ZipFile

import pytest
from PIL import Image

import graph_mail_downloader as graph
from image_repository import ImageRepository, collision_name, repository_root
from kering_images import ImageStore, VIEWS, execute_run, kering_image_name, kering_zip_name, process_ean
import migrate_image_repository as migration


EAN = "0012345678901"
LUX_NAME = "0RB2140__901_001A__noshad__fr.png"


def photo(color=(20, 30, 40), format="PNG", size=(600, 600)):
    output = BytesIO()
    Image.new("RGB", size, color).save(output, format)
    return output.getvalue()


@pytest.fixture(autouse=True)
def isolated_repository(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAGE_REPOSITORY_ROOT", str(tmp_path / "common"))
    monkeypatch.setattr(graph, "_get_market_ids_by_ean", lambda _: {})


class Portal:
    def __init__(self):
        self.calls = []

    def fetch(self, ean, pending):
        self.calls.append((ean, pending))
        return {view: photo((index * 80, 5, 10)) for index, view in enumerate(VIEWS) if view in pending}


def order():
    return {"id": 7, "name": "PO7", "supplier_id": 9, "date_order": "2026-09-04 10:00:00",
            "state": "purchase", "lines": [{"id": 1, "product_id": 2, "ean": EAN}]}


def save_original(repository, name=LUX_NAME, content=None, **kwargs):
    return repository.save(EAN, name, content or photo(), provider="Luxoptica",
                           origin="offline", view="V1", **kwargs)


def test_root_is_not_provider_or_working_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("M365_DOWNLOAD_ROOT", str(tmp_path / "wrong-provider-root"))
    monkeypatch.chdir(tmp_path)
    assert graph.load_m365_config().download_root == repository_root()
    store = ImageStore(tmp_path / "history-only")
    assert store.path(EAN, "frontal") == repository_root() / EAN / "frontal.img"


def test_luxoptica_reused_by_kering_only_missing_views_fetched(tmp_path):
    repository = ImageRepository()
    original, _ = save_original(repository)
    graph._create_market_images(original.path, original.path.parent, {"Farfetch": "FF", "Miinto": "MI"})
    store = ImageStore(tmp_path / "kering")
    portal = Portal()
    result = process_ean(store, portal, EAN)
    assert portal.calls == [(EAN, ("lateral", "perspectiva"))]
    assert result["views"] == {"frontal": "Reutilizada", "lateral": "Descargada", "perspectiva": "Descargada"}
    assert len(repository.valid_views(EAN)) == 3
    assert not (store.root / "images").exists()
    assert original.path.read_bytes() == photo()


def test_reuse_independent_of_provider_and_history_root(tmp_path):
    first = ImageStore(tmp_path / "history-one")
    portal = Portal()
    process_ean(first, portal, EAN)
    second = ImageStore(tmp_path / "history-two")
    result = process_ean(second, portal, EAN)
    assert result["tries"] == 0
    assert set(result["views"].values()) == {"Reutilizada"}
    assert len(portal.calls) == 1


def test_market_variants_not_counted_and_names_preserved():
    repository = ImageRepository()
    original, _ = save_original(repository)
    paths = graph._create_market_images(original.path, original.path.parent, {"Farfetch": "123", "Miinto": "456"})
    assert paths == [original.path, original.path]
    records = repository.records(EAN)
    assert {record.name for record in records} == {LUX_NAME, "123_V1.png", "456_V1.jpeg"}
    assert len({record.path for record in records}) == 1
    assert repository.pending_views(EAN, VIEWS) == ("lateral", "perspectiva")
    assert graph._create_market_images(original.path, original.path.parent, {"Farfetch": "123", "Miinto": "456"}) == []
    with ZipFile(BytesIO(repository.zip_eans([EAN], market="Miinto"))) as archive:
        assert archive.namelist() == [f"{EAN}/456_V1.jpeg"]
        assert archive.read(archive.namelist()[0]) == photo()


def test_collision_preserves_both_and_checksum_deduplicates():
    repository = ImageRepository()
    first, _ = save_original(repository, "same.png")
    other = photo((90, 30, 40))
    second, created = save_original(repository, "same.png", other)
    assert created and first.path != second.path
    assert first.path.name == "same.png"
    assert second.path.name == collision_name("same.png", hashlib.sha256(other).hexdigest())
    third, created = repository.save(EAN, "alternate.jpeg", other, provider="Kering",
                                     origin="different", view="frontal")
    assert not created and third.path == second.path
    assert first.path.read_bytes() == photo() and second.path.read_bytes() == other
    with repository.connect() as connection:
        assert connection.execute("SELECT count(*) FROM assets").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM events WHERE action='conflict'").fetchone()[0] == 1
    with ZipFile(BytesIO(repository.zip_eans([EAN]))) as archive:
        assert len(archive.namelist()) == len(set(archive.namelist())) == 3
        assert archive.read(f"{EAN}/same.png") == photo()
        assert archive.read(f"{EAN}/{second.path.name}") == other


def test_corrupt_primary_does_not_hide_valid_alternative():
    repository = ImageRepository()
    first, _ = save_original(repository)
    alternative, _ = save_original(repository, content=photo((90, 30, 40)))
    first.path.write_bytes(b"corrupt")
    assert repository.valid_views(EAN)["frontal"] == alternative.path


def test_invalid_or_visually_duplicated_images_not_counted():
    repository = ImageRepository()
    save_original(repository)
    repository.save(EAN, "lateral.png", photo(), provider="Another", origin="offline", view="lateral")
    repository.save(EAN, "small.png", photo(size=(599, 600)), provider="Another", origin="offline",
                    view="perspectiva", allow_invalid=True)
    assert set(repository.valid_views(EAN)) == {"frontal"}
    with pytest.raises(OSError):
        repository.save(EAN, "html.png", b"<html>login</html>", provider="Another", origin="offline")


def process_writer(root, provider):
    repository = ImageRepository(Path(root))
    record, _ = repository.save(EAN, "same.png", photo(), provider=provider, origin="process", view="frontal")
    return str(record.path)


def test_cross_process_writes_share_locks_and_checksum():
    root = repository_root()
    with ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context("spawn")) as executor:
        paths = list(executor.map(process_writer, [str(root)] * 2, ["Luxoptica", "Kering"]))
    assert paths[0] == paths[1]
    repository = ImageRepository()
    assert len(repository.records(EAN)) == 2
    assert len(list((root / EAN).iterdir())) == 1


def test_download_lock_shared_across_history_roots(tmp_path):
    entered, release = Event(), Event()
    class SlowPortal(Portal):
        def fetch(self, ean, pending):
            entered.set()
            assert release.wait(10)
            return super().fetch(ean, pending)
    portal = SlowPortal()
    one = ImageStore(tmp_path / "one")
    two = ImageStore(tmp_path / "two")
    with ThreadPoolExecutor(max_workers=2) as pool:
        work = pool.submit(process_ean, one, portal, EAN)
        assert entered.wait(10)
        assert process_ean(two, portal, EAN)["reason"] == "ean_en_proceso"
        release.set()
        assert work.result(timeout=10)["reason"] == ""
    assert len(portal.calls) == 1


def test_failure_after_bytes_before_metadata_is_resumable(monkeypatch):
    repository = ImageRepository()
    original_records = repository.records
    monkeypatch.setattr(repository, "records", lambda *args: (_ for _ in ()).throw(OSError("after commit")))
    with pytest.raises(OSError):
        save_original(repository)
    monkeypatch.setattr(repository, "records", original_records)
    record, created = save_original(repository)
    assert not created and record.path.read_bytes() == photo()
    assert len(repository.records(EAN)) == 1


def test_orders_use_eans_history_and_zip_without_copies(tmp_path):
    store = ImageStore(tmp_path / "history")
    first = store.create_run([order()], "offline", 9, date(2026, 9, 4), date(2026, 9, 4))
    execute_run(store, first, lambda _: order(), Portal())
    before = store.history()[0]
    next_run = store.create_run([order()], "offline", 9, date(2026, 9, 4), date(2026, 9, 4))
    execute_run(store, next_run, lambda _: order(), Portal())
    previous = next(row for row in store.history() if row["run_id"] == first)
    assert previous["results"] == before["results"] and previous["snapshot"] == before["snapshot"]
    assert len(store.repository.order_records("Kering", "7")) == 3
    assert len(list((repository_root() / EAN).iterdir())) == 3
    with ZipFile(BytesIO(store.zip_order(order()))) as archive:
        assert set(archive.namelist()) == {f"{EAN}/{record.name}" for record in store.repository.records(EAN)} | {"manifest.json"}


@pytest.mark.parametrize("name,model,key,view", [
    (LUX_NAME, "RB2140", ("RB2140", "901"), "V1"),
    ("0RB2140__901__noshad__qt.PNG", "RB2140", ("RB2140", "901"), "V2"),
    ("GG0998S__001__shad__lt.png", "GG0998S", ("GG0998S", "001"), "V3"),
    ("plain.JPG", "plain.JPG", None, None),
])
def test_exact_existing_luxoptica_naming_regression(name, model, key, view):
    assert graph._sanitize_filename(name) == name
    assert graph._model_from_image_name(name) == model
    assert graph._image_model_color_key(name) == key
    assert graph._market_view_from_image_name(name) == view


@pytest.mark.parametrize("suffix,view", [("noshad__fr", "V1"), ("noshad__qt", "V2"), ("shad__lt", "V3")])
def test_exact_market_filenames_regression(suffix, view):
    assert graph._market_image_names(f"0RB2140__901__{suffix}.png", {"Farfetch": "FF-01", "Miinto": "0002"}) == {
        "Farfetch": f"FF-01_{view}.png", "Miinto": f"0002_{view}.jpeg"}
    assert graph._market_image_names(f"0RB2140__901__{suffix}.jpg", {"Farfetch": "FF-01"}) == {}
    assert graph._sanitize_filename('bad/a\\b:c*?.PNG') == "bad_a_b_c__.PNG"


@pytest.mark.parametrize("format,extension", [("PNG", "png"), ("JPEG", "jpg"), ("WEBP", "webp")])
@pytest.mark.parametrize("view", ["frontal", "lateral", "perspectiva", "detalle"])
def test_exact_kering_filename_and_extension_regression(format, extension, view):
    assert kering_image_name(view) == f"{view}.img"
    assert kering_zip_name(view, photo(format=format)) == f"{view}.{extension}"


def legacy_files(tmp_path):
    source = tmp_path / "legacy-lux"
    image = source / "RB2140" / EAN / LUX_NAME
    image.parent.mkdir(parents=True)
    image.write_bytes(photo())
    variant = image.parent / "Miinto" / "123_V1.jpeg"
    variant.parent.mkdir()
    variant.write_bytes(photo())
    (source / ".mail_download_state.json").write_text(json.dumps({"processed_message_ids": ["old-message"]}))
    return source


def test_inventory_is_dry_run_alias_simulated_and_recovery_idempotent(tmp_path):
    source = legacy_files(tmp_path)
    target = repository_root()
    before = {str(path): migration.checksum(path) for path in source.rglob("*") if path.is_file()}
    plan = migration.inventory([("Luxoptica", source)], target)
    assert not target.exists()
    assert not plan["blockers"]
    originals = [entry for entry in plan["entries"] if entry["image"]]
    assert len({entry["destination"] for entry in originals}) == 1
    assert migration.apply(plan)["verified"] == 3
    assert migration.apply(plan)["verified"] == 3
    assert {str(path): migration.checksum(path) for path in source.rglob("*") if path.is_file()} == before
    repository = ImageRepository()
    assert len(repository.records(EAN)) == 2
    pending = json.loads((target / ".market_pending.json").read_text())
    assert next(iter(pending.values()))["missing_markets"] == ["Farfetch"]
    assert json.loads((target / ".mail_download_state.json").read_text())["processed_message_ids"] == ["old-message"]
    recovered = tmp_path / "recovered"
    assert migration.recover(plan, recovered)["recovered"] == 3
    assert migration.recover(plan, recovered)["recovered"] == 3
    for entry in plan["entries"]:
        assert migration.checksum(recovered / entry["backup"]) == entry["checksum"]


def test_migration_collision_between_providers_and_resumption(tmp_path, monkeypatch):
    a = tmp_path / "lux" / EAN
    b = tmp_path / "other" / EAN
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    (a / "frontal.png").write_bytes(photo())
    (b / "frontal.png").write_bytes(photo((90, 10, 20)))
    plan = migration.inventory([("Luxoptica", a.parent), ("Other", b.parent)], repository_root())
    assert sum(entry["conflict"] for entry in plan["entries"]) == 1
    copier = migration.copy_verified
    def interrupted(source, destination, expected, **kwargs):
        if source == b / "frontal.png":
            raise OSError("simulated stop")
        copier(source, destination, expected, **kwargs)
    monkeypatch.setattr(migration, "copy_verified", interrupted)
    with pytest.raises(OSError):
        migration.apply(plan)
    with pytest.raises(ValueError, match="sin migrar"):
        migration.verify(plan)
    monkeypatch.setattr(migration, "copy_verified", copier)
    assert migration.apply(plan)["verified"] == 2
    assert migration.apply(plan)["verified"] == 2
    repository = ImageRepository()
    assert len({record.path for record in repository.records(EAN)}) == 2
    assert (repository.root / EAN / "frontal.png").read_bytes() == photo()


def test_migration_preserves_history_and_order_snapshots(tmp_path):
    source = tmp_path / "old-kering"
    store = ImageStore(source)
    run = store.create_run([order()], "offline", 9, date(2026, 9, 4), date(2026, 9, 4))
    with store.connect() as connection:
        connection.execute("UPDATE runs SET ended=1,status='Finalizado' WHERE id=?", (run,))
        connection.execute("UPDATE attempts SET status='Completo',results=? WHERE run_id=?",
                           (json.dumps({EAN: {"views": {"frontal": "Descargada"}, "identity": {"ean": EAN}}}), run))
    legacy = source / "images" / "kering" / EAN / "frontal.img"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(photo())
    before = store.history()[0]
    plan = migration.inventory([("Kering", source)], repository_root())
    migration.apply(plan)
    restored = ImageStore(source)
    assert restored.history()[0] == before
    assert restored.path(EAN, "frontal").name == "frontal.img"
    assert restored.repository.order_records("Kering", "7")
    assert legacy.exists()
    assert migration.recover(plan, tmp_path / "recovery")["recovered"] == len(plan["entries"])


def test_unknown_ean_blocks_until_explicit_mapping(tmp_path):
    source = tmp_path / "unknown"
    source.mkdir()
    image = source / "unrecognized.png"
    image.write_bytes(photo())
    plan = migration.inventory([("Another", source)], repository_root())
    with pytest.raises(ValueError, match="sin resolver"):
        migration.apply(plan)
    assert not repository_root().exists()
    plan = migration.inventory([("Another", source)], repository_root(), {
        "files": {str(image): {"ean": EAN, "view": "frontal", "orders": ["99"]}}})
    migration.apply(plan)
    assert ImageRepository().order_records("Another", "99")[0].name == image.name


def test_stale_inventory_and_corrupted_backups_are_rejected(tmp_path):
    source = legacy_files(tmp_path)
    plan = migration.inventory([("Luxoptica", source)], repository_root())
    original = source / "RB2140" / EAN / LUX_NAME
    original.write_bytes(photo((90, 10, 20)))
    with pytest.raises(ValueError, match="Origen modificado"):
        migration.apply(plan)
    assert not repository_root().exists()
    plan = migration.inventory([("Luxoptica", source)], repository_root())
    migration.apply(plan)
    backup = repository_root() / ".migration" / plan["id"] / "originals" / plan["entries"][0]["backup"]
    backup.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="Verificacion fallida"):
        migration.verify(plan)
    with pytest.raises(ValueError):
        migration.recover(plan, tmp_path / "restored")


def test_nas_replica_catalog_and_recovery_backups(tmp_path):
    source = legacy_files(tmp_path)
    plan = migration.inventory([("Luxoptica", source)], repository_root())
    migration.apply(plan)
    repository = ImageRepository()
    nas = tmp_path / "nas"
    repository.sync_nas(nas)
    repository.sync_nas(nas)
    replica = ImageRepository(nas)
    assert len(replica.records(EAN)) == 2
    assert replica.valid_views(EAN).keys() == repository.valid_views(EAN).keys()
    for entry in plan["entries"]:
        assert migration.checksum(nas / ".migration" / plan["id"] / "originals" / entry["backup"]) == entry["checksum"]
    assert migration.verify(plan, nas)["verified"] == 3
    assert migration.recover(plan, tmp_path / "nas-recovery", nas)["recovered"] == 3
    replica.records(EAN)[0].path.write_bytes(b"unexpected-other-content")
    with pytest.raises(ValueError, match="Conflicto en replica"):
        repository.sync_nas(nas)


def test_zip_import_uses_common_flat_paths_and_preserves_originals(tmp_path, monkeypatch):
    monkeypatch.setattr(graph, "_load_eans_by_image_key", lambda: {("RB2140", "901"): EAN})
    archive = tmp_path / "response.zip"
    with ZipFile(archive, "w") as output:
        output.writestr(f"../../nested/{LUX_NAME}", photo())
        output.writestr("report.txt", b"not an image")
    paths = graph._extract_zip(archive, repository_root())
    assert paths == [repository_root() / EAN / LUX_NAME]
    assert archive.exists()
    assert graph._extract_zip(archive, repository_root()) == paths
    assert len(ImageRepository().records(EAN)) == 1
    assert graph._refresh_pending_market_images(repository_root()) == (0, 0)


def test_unidentified_zip_image_fails_explicitly_and_archive_retained(tmp_path):
    archive = tmp_path / "unrecognized.zip"
    with ZipFile(archive, "w") as output:
        output.writestr("unknown.png", photo())
    with pytest.raises(ValueError, match="EAN no identificado"):
        graph._extract_zip(archive, repository_root())
    assert archive.exists()


def test_catalog_metadata_dates_provenance_and_views():
    record, _ = save_original(ImageRepository(), date="2026-09-04T12:00:00+00:00",
                              metadata={"modelo": "RB2140"})
    row = ImageRepository().catalog_rows()[0]
    assert row["EAN"] == EAN and row["Proveedor"] == "Luxoptica"
    assert row["Origen"] == "offline" and row["Vista"] == "frontal"
    assert row["Archivo"] == LUX_NAME and row["Ruta"] == str(record.path)
    assert row["Checksum"] == hashlib.sha256(photo()).hexdigest()


def test_market_generation_when_bytes_were_first_stored_by_kering():
    repository = ImageRepository()
    repository.save(EAN, "frontal.img", photo(), provider="Kering", origin="portal", view="frontal")
    original, _ = save_original(repository)
    assert original.path.name == "frontal.img"
    graph._create_market_images(original.path, original.path.parent, {"Farfetch": "F", "Miinto": "M"})
    assert {record.name for record in repository.records(EAN)} == {
        "frontal.img", LUX_NAME, "F_V1.png", "M_V1.jpeg"}
    assert graph._refresh_pending_market_images(repository.root) == (1, 0)


def test_mail_state_prevents_redownload_after_migration(tmp_path, monkeypatch):
    source = legacy_files(tmp_path)
    migration.apply(migration.inventory([("Luxoptica", source)], repository_root()))
    config = graph.M365Config("tenant", "client", "secret", "offline", repository_root())
    monkeypatch.setattr(graph, "load_m365_config", lambda: config)
    monkeypatch.setattr(graph, "_get_access_token", lambda _: "offline")
    monkeypatch.setattr(graph, "_list_inbox_messages", lambda *args: [
        {"id": "old-message", "isRead": False, "hasAttachments": True}])
    monkeypatch.setattr(graph, "_list_attachments", lambda *args: pytest.fail("no redownload"))
    summary = graph.download_luxoptica_mail_attachments()
    assert summary.attachments_downloaded == 0
    assert summary.saved_paths == []


def test_inventory_detects_invalid_images_before_apply(tmp_path):
    source = tmp_path / "legacy" / EAN
    source.mkdir(parents=True)
    (source / "bad.png").write_bytes(b"invalid")
    plan = migration.inventory([("Another", source.parent)], repository_root())
    assert plan["entries"][0]["valid"] is False
    with pytest.raises(ValueError, match="no reutilizable"):
        migration.apply(plan)
    assert (source / "bad.png").read_bytes() == b"invalid"
    assert not repository_root().exists()


def test_upload_rechecks_completed_eans_preserves_names_and_original_file(tmp_path, monkeypatch):
    import luxoptica_auto_upload as upload
    store = ImageStore(tmp_path / "history")
    process_ean(store, Portal(), EAN)
    request = tmp_path / "upc-products-images-request-20260904_120000-lote-001.txt"
    request.write_text(f"{EAN}\n0002\n", encoding="utf-8")
    seen = []
    def fake_upload(url, username, password, ean_file, *args):
        seen.append((ean_file.name, ean_file.read_text(), ean_file))
        return True, "offline"
    monkeypatch.setattr(upload, "_upload_to_luxoptica", fake_upload)
    assert upload.upload_to_luxoptica(ean_file=request) == (True, "offline")
    assert seen[0][:2] == (request.name, "0002\n")
    assert request.read_text() == f"{EAN}\n0002\n"
    assert not seen[0][2].exists()
    request.write_text(EAN + "\n")
    assert upload.upload_to_luxoptica(ean_file=request)[0]
    assert len(seen) == 1


def test_verify_detects_lost_metadata_even_if_files_are_intact(tmp_path):
    source = legacy_files(tmp_path)
    plan = migration.inventory([("Luxoptica", source)], repository_root())
    migration.apply(plan)
    with ImageRepository().connect() as connection:
        connection.execute("DELETE FROM representations")
    with pytest.raises(ValueError, match="Metadatos no verificados"):
        migration.verify(plan)


def test_repair_updates_migration_paths_without_overwriting_corrupt_file(tmp_path):
    source = legacy_files(tmp_path)
    plan = migration.inventory([("Luxoptica", source)], repository_root())
    migration.apply(plan)
    repository = ImageRepository()
    record = repository.records(EAN)[0]
    record.path.write_bytes(b"corrupt")
    repaired, _ = repository.save(EAN, record.name, photo(), provider=record.provider,
                                   origin=record.origin, view=record.view, market=record.market,
                                   date=record.date, metadata=record.metadata)
    assert repaired.path != record.path
    assert record.path.read_bytes() == b"corrupt"
    assert migration.verify(plan)["verified"] == 3
