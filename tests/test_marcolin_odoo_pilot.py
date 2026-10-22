import ast
import hashlib
import json
import os
import random
import sqlite3
import subprocess
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "marcolin_odoo_pilot.py"
sys.path.insert(0, str(ROOT / "scripts"))
import marcolin_odoo_pilot as pilot  # noqa: E402
from image_repository import ImageRepository  # noqa: E402
from provider_support import photo  # noqa: E402

TODAY = date(2026, 9, 30)
SYNTHETIC_PARTNER, SYNTHETIC_CONTACT, SYNTHETIC_OTHER = 1001, 1002, 1900


def upc12(body11: str) -> str:
    return next(c for c in (body11 + str(d) for d in range(10)) if pilot.gtin_valid(c))


# Synthetic codes; the first has a leading zero and must stay literal.
U = [upc12("0123456789" + "0"), upc12("07700000001"), upc12("07700000002"), upc12("07700000003"),
     upc12("07700000004"), upc12("07700000005")]


def odoo(lines, *, partner_name="Supplier Test"):
    """lines: (order_id, order_name, partner_id, state, barcode, ref)."""
    engine = create_engine("sqlite://")
    with engine.begin() as c:
        for ddl in (
            "CREATE TABLE res_partner(id INTEGER PRIMARY KEY, name TEXT, active BOOLEAN DEFAULT 1,"
            " supplier_rank INTEGER DEFAULT 0, commercial_partner_id INTEGER)",
            "CREATE TABLE purchase_order(id INTEGER PRIMARY KEY, name TEXT, partner_id INTEGER, date_order TEXT, state TEXT)",
            "CREATE TABLE purchase_order_line(id INTEGER PRIMARY KEY, order_id INTEGER, product_id INTEGER,"
            " name TEXT, product_qty REAL, display_type TEXT)",
            "CREATE TABLE product_product(id INTEGER PRIMARY KEY, barcode TEXT, default_code TEXT)",
        ):
            c.execute(text(ddl))
        c.execute(text("""
            INSERT INTO res_partner VALUES
                (:partner, :name, 1, 1, :partner),
                (:contact, 'Synthetic contact', 1, 0, :partner),
                (:other, 'Other supplier', 1, 1, :other)
        """), {"partner": SYNTHETIC_PARTNER, "contact": SYNTHETIC_CONTACT, "other": SYNTHETIC_OTHER,
               "name": partner_name})
        seen = set()
        for n, (order_id, name, partner, state, barcode, ref) in enumerate(lines, 1):
            if order_id not in seen:
                seen.add(order_id)
                c.execute(text("INSERT INTO purchase_order VALUES (:i,:n,:p,'2026-09-10 10:00:00',:s)"),
                          {"i": order_id, "n": name, "p": partner, "s": state})
            c.execute(text("INSERT INTO product_product VALUES (:i,:b,:r)"), {"i": n, "b": barcode, "r": ref})
            c.execute(text("INSERT INTO purchase_order_line VALUES (:l,:o,:l,'desc',1,NULL)"), {"l": n, "o": order_id})
    return engine


def args(tmp_path, *extra):
    return pilot.build_parser().parse_args([
        "--partner", str(SYNTHETIC_PARTNER), "--expect-name", "Supplier Test",
        "--out", str(tmp_path / "out"), "--days", "60", *extra,
    ])


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


def helpers():
    return pilot.repository_helpers()


def snapshot(folder: Path):
    return {str(p.relative_to(folder)): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            if p.is_file() else None for p in sorted(folder.rglob("*"))}


LINES = [
    (1, "PO-SYN-1", SYNTHETIC_PARTNER, "purchase", U[0], "REF-A"),
    (1, "PO-SYN-1", SYNTHETIC_PARTNER, "purchase", U[1], "REF-B"),
    (2, "PO-SYN-2", SYNTHETIC_CONTACT, "done", U[2], "REF-C"),
    (3, "PO-SYN-3", SYNTHETIC_PARTNER, "cancel", U[3], "REF-D"),
    (4, "PO-SYN-4", SYNTHETIC_PARTNER, "draft", U[4], "REF-E"),
    (5, "PO-SYN-5", SYNTHETIC_OTHER, "purchase", U[5], "REF-F"),
    (6, "PO-SYN-6", SYNTHETIC_PARTNER, "purchase", None, "REF-G"),
    (6, "PO-SYN-6", SYNTHETIC_PARTNER, "purchase", "12345", "REF-H"),
]


# --- codes ----------------------------------------------------------------------------------

def test_upc12_kept_literally_and_equivalents_deduplicated():
    padded = "0" + U[0]
    result = pilot.normalize_codes([U[0], padded, U[0], "123", U[1]])
    assert result["valid"] == [U[0], U[1]] and result["repeated"] == 1
    assert result["equivalent_forms"] == [{"kept": U[0], "equivalent": padded}]
    assert result["invalid"] == ["123"] and U[0].startswith("0") and len(U[0]) == 12
    assert pilot.split_codes("a\r\nb,c; d\n") == ["a", "b", "c", "d"]


def test_portal_groups_keep_every_upc_and_never_filter_by_status():
    payload = {"groups": [
        {"portal_ref": "G1", "status": "inactive_in_collection", "upcs": [U[0], U[1]]},
        {"portal_ref": "G2", "status": "inactive_in_collection", "upcs": [U[2]]},
        {"portal_ref": "G3", "status": "inactive_in_collection", "upcs": [U[3], U[4]]},
        {"portal_ref": "G4", "status": "inactive_in_collection", "upcs": [U[5]]},
    ]}
    out = pilot.group_by_portal_ref(U, payload)
    assert len(out["groups"]) == 4
    assert out["statuses"] == {"inactive_in_collection": 4}
    assert out["ref_by_upc"][U[0]] == "G1" == out["ref_by_upc"][U[1]]
    assert {code for group in out["groups"] for code in group["upcs"]} == set(U)
    assert not out["unassigned_upcs"] and not out["multi_assigned"] and not out["empty_groups"]


def test_portal_groups_report_unassigned_empty_and_multi_assigned_codes():
    payload = {"groups": [
        {"portal_ref": "G1", "status": "inactive", "upcs": [U[0], U[1]]},
        {"portal_ref": "G2", "status": "active", "upcs": [U[0]]},
        {"portal_ref": "G3", "status": "inactive", "upcs": []},
    ]}
    out = pilot.group_by_portal_ref(U[:3], payload)
    assert out["unassigned_upcs"] == [U[2]]
    assert out["multi_assigned"] == {pilot.code_key(U[0]): ["G1", "G2"]}
    assert out["empty_groups"] == ["G3"]


# --- catalog read-only ----------------------------------------------------------------------

def test_missing_catalog_creates_nothing(tmp_path):
    missing = tmp_path / "state" / ".catalog.sqlite3"
    with pytest.raises(pilot.Blocker) as error:
        pilot.open_catalog(missing)
    assert error.value.code == "catalogo_inexistente" and not (tmp_path / "state").exists()
    with pytest.raises(pilot.Blocker) as unset:
        pilot.open_catalog(pilot.resolve_catalog(None, env={}))
    assert unset.value.code == "catalogo_no_configurado"


def test_catalog_untouched_and_no_side_effects(tmp_path):
    path = tmp_path / ".catalog.sqlite3"
    make_catalog(path, [(U[1], "frontal", "m/a.jpg")]).close()
    before = snapshot(tmp_path)
    report, code = pilot.build_report(args(tmp_path, "--catalog", str(path), "--upc", U[1]), engine=odoo(LINES),
                                      helpers=helpers(), today=TODAY)
    assert code == 0 and snapshot(tmp_path) == before
    assert report["coverage"][U[1]]["label"] == "parciales"
    assert report["coverage"][U[1]]["basis"] == "solo_metadatos_del_catalogo"
    assert report["scope"]["requests_created"] == 0 and report["scope"]["association_saved"] is False


def test_no_records_is_reported_with_caveat_and_leading_zero_equivalent_found(tmp_path):
    path = tmp_path / "c.sqlite3"
    make_catalog(path, [("0" + U[0], "frontal", "m/a.jpg")]).close()
    report, _ = pilot.build_report(args(tmp_path, "--catalog", str(path), "--upc", U[0], "--upc", U[1]),
                                   engine=odoo(LINES), helpers=helpers(), today=TODAY)
    assert report["coverage"][U[0]]["records"] == 1       # found through the equivalent 13-digit form
    assert report["coverage"][U[1]]["label"] == "sin_registros"
    assert "no prueba ausencia" in pilot.REASONS["sin_registros"]


@pytest.mark.parametrize("kind", ["corrupt", "schema"])
def test_inaccessible_catalog_is_unknown_not_absent(tmp_path, kind):
    path = tmp_path / "c.sqlite3"
    if kind == "corrupt":
        path.write_bytes(b"not a database" * 50)
    else:
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE assets(id INTEGER)")
        connection.commit()
        connection.close()
    report, code = pilot.build_report(args(tmp_path, "--catalog", str(path), "--upc", U[1]), engine=odoo(LINES),
                                      helpers=helpers(), today=TODAY)
    assert code == 6 and report["catalog"]["status"] == "inaccesible"
    assert report["coverage"][U[1]]["label"] == "desconocida"
    assert report["coverage_summary"]["sin_registros"] == 0
    assert report["proposal"][0]["Motivo"] == pilot.REASONS["desconocida"]


def test_active_wal_rows_are_seen_without_checkpoint_or_immutable(tmp_path):
    path = tmp_path / "c.sqlite3"
    writer = make_catalog(path, wal=True)
    writer.execute("INSERT INTO assets VALUES (1,?,?,?,?)", (U[1], "c", "p", "m/a.jpg"))
    writer.execute("INSERT INTO representations VALUES (1,1,'marcolin','zip','frontal','','n','d','{}')")
    writer.commit()
    wal = Path(str(path) + "-wal")
    assert wal.exists() and wal.stat().st_size > 0
    size = wal.stat().st_size
    try:
        connection, info = pilot.open_catalog(path)
        try:
            found = pilot.fetch_records(connection, [U[1]])
        finally:
            connection.close()
        assert len(found[pilot.code_key(U[1])]) == 1 and info["wal_file_present"]
        assert info["journal_mode"] == "wal" and wal.stat().st_size >= size
    finally:
        writer.close()
    assert "immutable" not in SCRIPT.read_text(encoding="utf-8").replace("never ``immutable=1``", "")


def test_verify_files_checks_path_checksum_fingerprint_and_staging(tmp_path):
    repo = ImageRepository(tmp_path / "images", state=tmp_path / "state", backups=tmp_path / "bak")
    data = photo(5)
    repo.save(U[1], "front.png", data, provider="marcolin", origin="test", view="frontal")
    records = repo.records(U[1])
    assert records
    catalog = tmp_path / "state" / ".catalog.sqlite3"
    ns = args(tmp_path, "--catalog", str(catalog), "--image-root", str(tmp_path / "images"), "--verify-files",
              "--upc", U[1])
    report, _ = pilot.build_report(ns, engine=odoo(LINES), helpers=helpers(), today=TODAY)
    assert report["coverage"][U[1]]["basis"] == "archivos_verificados"
    assert report["coverage"][U[1]]["views"] == ["frontal"] and not report["coverage"][U[1]]["issues"]
    record = {"record_id": 1, "view": "frontal", "path": "staging/x.jpg", "checksum": "x", "pixels": "y"}
    assert pilot.check_record(record, helpers(), tmp_path, True)[2] == "ruta_excluida"
    record["path"] = "../x.jpg"
    assert pilot.check_record(record, helpers(), tmp_path, True)[2] == "ruta_excluida"
    record["path"] = "nope.jpg"
    assert pilot.check_record(record, helpers(), tmp_path, True)[2] == "archivo_ausente"
    (tmp_path / "bad.jpg").write_bytes(photo(9))
    record["path"], record["checksum"] = "bad.jpg", "wrong"
    assert pilot.check_record(record, helpers(), tmp_path, True)[2] == "checksum_incorrecto"


# --- Odoo ------------------------------------------------------------------------------------

def test_odoo_filters_cancelled_draft_other_partner_and_groups_contacts(tmp_path):
    report, code = pilot.build_report(args(tmp_path, "--catalog", str(tmp_path / "none")), engine=odoo(LINES),
                                      helpers=helpers(), today=TODAY)
    found = {p["UPC"] for p in report["proposal"]}
    assert code == 6 and report["identity"]["name_matches_expected"] and report["identity"]["family_size"] == 2
    assert found == {U[0], U[1], U[2]} and report["odoo"]["lines_without_barcode"] == 1
    assert U[3] not in found and U[4] not in found and U[5] not in found
    assert report["input"]["invalid"] == ["12345"]


def test_explicit_codes_restrict_the_query_and_report_what_odoo_lacks(tmp_path):
    report, _ = pilot.build_report(args(tmp_path, "--catalog", str(tmp_path / "none"), "--upc", U[1], "--upc", U[5]),
                                   engine=odoo(LINES), helpers=helpers(), today=TODAY)
    assert report["odoo"]["requested_found"] == {U[1]: True, U[5]: False}
    assert report["odoo"]["lines_read"] == 1


def test_partner_name_mismatch_stops(tmp_path):
    report, code = pilot.build_report(args(tmp_path), engine=odoo(LINES, partner_name="Otro proveedor"),
                                      helpers=helpers(), today=TODAY)
    assert code == 3 and report["blocker"]["code"] == "partner_no_coincide" and "proposal" not in report


def test_lines_are_bounded(tmp_path):
    report, _ = pilot.build_report(args(tmp_path, "--max-lines", "1", "--catalog", str(tmp_path / "none")),
                                   engine=odoo(LINES), helpers=helpers(), today=TODAY)
    assert report["odoo"]["lines_read"] == 1 and report["odoo"]["truncated"] is True


def test_proposal_remains_capped_at_eight(tmp_path):
    codes = [upc12(f"{7_000_000_000 + index:011}") for index in range(10)]
    lines = [
        (index + 1, f"PO-SYN-{index}", SYNTHETIC_PARTNER, "purchase", code, f"REF-{index}")
        for index, code in enumerate(codes)
    ]
    catalog = tmp_path / "empty.sqlite3"
    make_catalog(catalog).close()
    report, code = pilot.build_report(
        args(tmp_path, "--catalog", str(catalog), "--size", "99"),
        engine=odoo(lines), helpers=helpers(), today=TODAY,
    )
    assert code == 0 and len(report["proposal"]) == 8


class DownEngine:
    dialect = type("D", (), {"name": "postgresql"})()

    def connect(self):
        raise RuntimeError("password=SECRET host=10.0.0.1")


def test_odoo_down_reports_class_only_without_secrets(tmp_path, capsys):
    code = pilot.main(["--partner", str(SYNTHETIC_PARTNER), "--expect-name", "Supplier Test",
                       "--out", str(tmp_path / "o")], engine=DownEngine(), helpers=helpers())
    shown = capsys.readouterr().out + "".join(p.read_text(encoding="utf-8") for p in (tmp_path / "o").glob("*.json"))
    assert code == 2 and "odoo_no_disponible" in shown and "SECRET" not in shown and "10.0.0.1" not in shown


def test_report_lists_proposal_and_keeps_literal_upc_in_csv(tmp_path, capsys):
    catalog = tmp_path / "c.sqlite3"
    make_catalog(catalog).close()
    groups = tmp_path / "g.json"
    groups.write_text(json.dumps({"groups": [{"portal_ref": "G1", "status": "inactive", "upcs": [U[0], U[1]]}]}))
    code = pilot.main(["--partner", str(SYNTHETIC_PARTNER), "--expect-name", "Supplier Test",
                       "--out", str(tmp_path / "o"), "--catalog", str(catalog), "--days", "60",
                       "--upc", U[0], "--upc", U[1], "--portal-groups", str(groups)], engine=odoo(LINES),
                      helpers=helpers())
    assert code == 0
    csv_text = next((tmp_path / "o").glob("*.csv")).read_text(encoding="utf-8-sig")
    assert U[0] in csv_text and "G1" in csv_text
    assert "Referencia_portal" in csv_text
    assert U[0] not in capsys.readouterr().out  # stdout carries aggregates only


def test_output_files_are_exclusive_and_owner_only(tmp_path):
    report = {"schema": "x", "proposal": [{"UPC": U[0]}]}
    first = pilot.write_report(report, tmp_path / "o")
    second = pilot.write_report(report, tmp_path / "o")
    assert first != second
    if os.name != "nt":
        assert oct(first.stat().st_mode & 0o777) == "0o600"


def test_unwritable_output_gives_exit_4(tmp_path, capsys):
    blocked = tmp_path / "file"
    blocked.write_text("x")
    catalog = tmp_path / "c.sqlite3"
    make_catalog(catalog).close()
    code = pilot.main(["--partner", str(SYNTHETIC_PARTNER), "--expect-name", "Supplier Test",
                       "--out", str(blocked / "sub"), "--catalog", str(catalog), "--days", "60", "--upc", U[1]],
                      engine=odoo(LINES), helpers=helpers())
    assert code == 4 and "salida_no_escribible" in capsys.readouterr().out


# --- dependencies / stdin ---------------------------------------------------------------------

def test_missing_dependency_blocker_names_modules_only(monkeypatch, tmp_path, capsys):
    real = pilot.importlib.import_module

    def fake(name, *a):
        if name == "kering_jobs":
            raise ImportError("synthetic path /x/y", name="kering_jobs")
        return real(name, *a)

    monkeypatch.setattr(pilot.importlib, "import_module", fake)
    code = pilot.main(["--partner", str(SYNTHETIC_PARTNER), "--out", str(tmp_path / "o")])
    out = capsys.readouterr().out + "".join(p.read_text(encoding="utf-8") for p in (tmp_path / "o").glob("*.json"))
    assert code == 5 and "dependencia_ausente" in out and "kering_jobs" in out and "synthetic path" not in out


def test_script_only_imports_production_modules():
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    names = {n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)}
    names |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not names & {"provider_images", "provider_odoo_orders", "marcolin_images", "kering_images"}
    calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "ImageRepository" not in calls
    assert "__file__" not in SCRIPT.read_text(encoding="utf-8")


def test_runs_from_stdin_and_reports_blocker_without_traceback(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("DB_")}
    env.update({"PYTHONPATH": str(ROOT), "DB_HOST": "127.0.0.1", "DB_PORT": "1", "DB_NAME": "x", "DB_USER": "u",
                "DB_PASSWORD": "SYNTHETIC_TEST_DB_PASSWORD"})
    done = subprocess.run([sys.executable, "-", "--partner", str(SYNTHETIC_PARTNER),
                           "--expect-name", "Supplier Test", "--out", str(tmp_path / "o"), "--timeout-ms", "500"],
                          input=SCRIPT.read_bytes(), capture_output=True, cwd=ROOT, env=env, timeout=120)
    shown = (done.stdout + done.stderr).decode("utf-8", "replace")
    assert done.returncode in (2, 5), shown
    assert "Traceback" not in shown and "SYNTHETIC_TEST_DB_PASSWORD" not in shown


# --- pending requests for days (core connector) -------------------------------------------------

def test_request_pending_for_days_is_not_failed_nor_resubmitted(tmp_path, monkeypatch):
    from marcolin_images import DEFAULTS, MarcolinConnector, MarcolinPortal
    from provider_images import PENDING, PortalPackage, ProviderService, ProviderStore
    from provider_support import EANS
    from test_marcolin_images import Driver

    monkeypatch.setenv("MARCOLIN_AUTOMATION_ENABLED", "true")
    clock = [1_800_000_000.0]
    store = ProviderStore(tmp_path / "s", clock=lambda: clock[0])
    repo = ImageRepository(tmp_path / "i", state=tmp_path / "r", backups=tmp_path / "b")
    settings = replace(DEFAULTS, supplier_ids=(SYNTHETIC_PARTNER,), max_eans_per_request=50, poll_base_seconds=60)
    service = ProviderService(store, repo, settings, rng=random.Random(0))
    engine = odoo([
        (1, "PO-SYN-A", SYNTHETIC_PARTNER, "purchase", EANS[0], "REF-A"),
        (2, "PO-SYN-B", SYNTHETIC_PARTNER, "purchase", EANS[1], "REF-B"),
    ])
    connector = MarcolinConnector(service, engine)
    driver = Driver(["Pendiente"], PortalPackage())
    portal = MarcolinPortal(driver)
    assert len(connector.detect(date(2026, 9, 1), date(2026, 9, 30))["created"]) == 1
    service.run_cycle(portal, dry_run=False)
    for _ in range(12):  # about five days of cycles
        clock[0] += 10 * 3600
        service.run_cycle(portal, dry_run=False)
        assert connector.detect(date(2026, 9, 1), date(2026, 9, 30))["created"] == []
    assert len(driver.sent) == 1
    assert {r["state"] for r in store.list_requests("marcolin")} == {PENDING}
