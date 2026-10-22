import hashlib
import random
import sqlite3
import sys
import threading
from contextlib import closing
from pathlib import Path

import pytest

from image_repository import ImageRepository
from provider_images import (
    FAILED, ProviderService, ProviderSettings, ProviderStore, StoreMigrationRequired, ean_key, equivalent_forms,
    migrate_request_store, migration_report, normalize_eans,
)
from provider_support import EANS, make_ean

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import provider_requests_migrate as migrate_cli  # noqa: E402

UPC = make_ean("003600029145")[1:]  # 12 digits, valid GTIN
EAN13 = "0" + UPC  # same GTIN, same check digit
SETTINGS = ProviderSettings("marcolin", max_eans_per_request=50, max_requests_per_day=50)

LEGACY_SCHEMA = """
CREATE TABLE requests(id INTEGER PRIMARY KEY, provider TEXT NOT NULL, mode TEXT NOT NULL DEFAULT 'portal',
  remote_id TEXT, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 1, polls INTEGER NOT NULL DEFAULT 0,
  last_error TEXT, retry_of INTEGER, created_at REAL NOT NULL, updated_at REAL NOT NULL, next_poll_at REAL);
CREATE TABLE request_eans(request_id INTEGER NOT NULL REFERENCES requests(id), provider TEXT NOT NULL,
  ean TEXT NOT NULL, result TEXT NOT NULL DEFAULT '', live INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY(request_id, ean));
CREATE UNIQUE INDEX one_live_request_per_ean ON request_eans(provider, ean) WHERE live = 1;
CREATE TABLE events(id INTEGER PRIMARY KEY, request_id INTEGER, at REAL NOT NULL, from_state TEXT, to_state TEXT,
  detail TEXT);
"""


def legacy_store(root: Path, rows):
    """rows: (request_id, provider, ean, live). Builds a store as #70 created it before the shared identity."""
    root.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(root / "provider_requests.sqlite")) as c:
        c.executescript(LEGACY_SCHEMA)
        for rid, provider, ean, live in rows:
            c.execute("INSERT OR IGNORE INTO requests(id,provider,state,created_at,updated_at) VALUES(?,?,?,?,?)",
                      (rid, provider, "pending" if live else "cancelled", 1.0, 1.0))
            c.execute("INSERT INTO request_eans(request_id,provider,ean,live) VALUES(?,?,?,?)",
                      (rid, provider, ean, live))
        c.execute("INSERT INTO events(request_id,at,to_state,detail) VALUES(1,1.0,'ready','historia')")
        c.commit()


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def store(tmp_path):
    return ProviderStore(tmp_path / "state")


def live_rows(store):
    with closing(store.connect()) as c:
        return c.execute("SELECT ean, ean_key FROM request_eans WHERE live=1").fetchall()


def test_equivalent_codes_share_identity_but_keep_the_original_literal():
    assert len(UPC) == 12 and ean_key(UPC) == ean_key(EAN13) == "0" * 2 + UPC
    assert normalize_eans([UPC, EAN13, UPC]) == ([UPC], [])
    assert normalize_eans([EAN13, UPC]) == ([EAN13], [])
    assert equivalent_forms([UPC, EAN13, "bad"]) == [{"kept": UPC, "equivalent": EAN13}]


@pytest.mark.parametrize("first,second", [(UPC, EAN13), (EAN13, UPC)])
def test_live_request_blocks_the_equivalent_form(store, first, second):
    created = store.plan(SETTINGS, [first])
    again = store.plan(SETTINGS, [second])
    assert len(created["created"]) == 1 and again["created"] == [] and again["already_live"] == [second]
    assert [r["ean"] for r in live_rows(store)] == [first]
    assert store.get(created["created"][0])["eans"][0]["ean"] == first  # shown/exported as entered


def test_same_call_with_both_forms_creates_one_request_with_the_first_literal(store):
    result = store.plan(SETTINGS, [EAN13, UPC])
    assert len(result["created"]) == 1 and [r["ean"] for r in live_rows(store)] == [EAN13]


def test_database_rejects_equivalent_live_form_even_bypassing_the_planner(store):
    store.plan(SETTINGS, [UPC])
    other = store.plan(SETTINGS, EANS[1:2])["created"][0]
    with closing(store.connect()) as c, pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO request_eans(request_id,provider,ean) VALUES(?,?,?)", (other, "marcolin", EAN13))


def test_identity_is_per_provider_and_cancelled_history_does_not_block(store):
    other = ProviderSettings("safilo", max_eans_per_request=50)
    assert store.plan(SETTINGS, [UPC])["created"] and store.plan(other, [EAN13])["created"]
    rid = store.plan(SETTINGS, EANS[2:3])["created"][0]
    store.cancel(rid)
    assert store.plan(SETTINGS, EANS[2:3])["created"]


def test_concurrent_planners_with_equivalent_forms_create_one_live_request(tmp_path):
    root = tmp_path / "state"
    ProviderStore(root)
    barrier, errors, results = threading.Barrier(4), [], []

    def worker(code):
        try:
            local = ProviderStore(root)
            barrier.wait()
            results.append(local.plan(SETTINGS, [code]))
        except Exception as error:  # surfaced below
            errors.append(error)

    threads = [threading.Thread(target=worker, args=(code,)) for code in (UPC, EAN13, UPC, EAN13)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert sum(len(r["created"]) for r in results) == 1 and sum(len(r["already_live"]) for r in results) == 3
    assert len(live_rows(ProviderStore(root))) == 1


def test_retry_does_not_recreate_a_request_already_live_under_the_equivalent_form(tmp_path):
    clock = [1_800_000_000.0]
    store = ProviderStore(tmp_path / "s", clock=lambda: clock[0])
    repo = ImageRepository(tmp_path / "i", state=tmp_path / "r", backups=tmp_path / "b")
    service = ProviderService(store, repo, SETTINGS, rng=random.Random(0))
    first = store.plan(SETTINGS, [UPC])["created"][0]
    store.transition(first, "submitting", expect="ready")
    store.transition(first, FAILED, expect="submitting")
    assert store.plan(SETTINGS, [EAN13])["created"]  # failed request freed the identity
    assert store.retry(SETTINGS, first) is None
    assert service.store is store


def test_preview_reports_live_equivalents_and_equivalent_duplicates(tmp_path):
    from provider_images import ProviderConnector
    store = ProviderStore(tmp_path / "s")
    repo = ImageRepository(tmp_path / "i", state=tmp_path / "r", backups=tmp_path / "b")
    connector = ProviderConnector(ProviderService(store, repo, SETTINGS, rng=random.Random(0)))
    store.plan(SETTINGS, [UPC])
    preview = connector.preview_eans([EAN13, EANS[1], EANS[1]])
    assert preview["already_live"] == [EAN13] and preview["new"] == [EANS[1]]
    assert connector.preview_eans([UPC, EAN13])["equivalent_forms"] == [{"kept": UPC, "equivalent": EAN13}]


# --- explicit migration of existing stores ---------------------------------------------------

def test_legacy_store_is_not_opened_or_changed_automatically(tmp_path):
    root = tmp_path / "state"
    legacy_store(root, [(1, "marcolin", UPC, 1)])
    before = digest(root / "provider_requests.sqlite")
    with pytest.raises(StoreMigrationRequired):
        ProviderStore(root)
    assert digest(root / "provider_requests.sqlite") == before and migration_report(root)["state"] == "legacy"


def test_dry_run_changes_nothing_and_apply_is_backed_up_and_lossless(tmp_path, capsys):
    root = tmp_path / "state"
    legacy_store(root, [(1, "marcolin", UPC, 1), (2, "marcolin", EAN13, 0), (3, "safilo", EAN13, 1)])
    db = root / "provider_requests.sqlite"
    before = digest(db)
    assert migrate_cli.main(["--root", str(root)]) == 0
    assert digest(db) == before and not list(root.glob("*.pre-ean-key-*"))
    assert migrate_cli.main(["--root", str(root), "--apply"]) == 0
    backups = list(root.glob("*.pre-ean-key-*"))
    assert len(backups) == 1
    with closing(sqlite3.connect(backups[0])) as c:  # content-equal copy of the pre-migration store
        assert c.execute("SELECT COUNT(*) FROM request_eans").fetchone()[0] == 3
        assert "ean_key" not in {r[1] for r in c.execute("PRAGMA table_xinfo(request_eans)")}
    migrated = ProviderStore(root)
    with closing(migrated.connect()) as c:
        assert c.execute("SELECT COUNT(*) FROM request_eans").fetchone()[0] == 3
        assert c.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
        assert [r[0] for r in c.execute("SELECT ean FROM request_eans ORDER BY request_id, ean")] == [UPC, EAN13, EAN13]
    assert migrated.plan(SETTINGS, [EAN13])["already_live"] == [EAN13]  # now enforced
    assert migrate_cli.main(["--root", str(root), "--apply"]) == 0
    assert "current" in capsys.readouterr().out and len(list(root.glob("*.pre-ean-key-*"))) == 1


def test_conflicting_live_requests_block_apply_and_nothing_is_deleted(tmp_path, capsys):
    root = tmp_path / "state"
    legacy_store(root, [(1, "marcolin", UPC, 1), (2, "marcolin", EAN13, 1)])
    db = root / "provider_requests.sqlite"
    before = digest(db)
    report = migration_report(root)
    assert report["state"] == "legacy" and len(report["conflicts"]) == 1
    assert sorted(report["conflicts"][0]["live_rows"]) == [f"1:{UPC}", f"2:{EAN13}"]
    assert migrate_cli.main(["--root", str(root), "--apply"]) == 3
    assert digest(db) == before and not list(root.glob("*.pre-ean-key-*"))
    assert migrate_request_store(root, apply=True)["applied"] is False
    capsys.readouterr()


def test_migration_of_missing_or_current_store_is_a_noop(tmp_path):
    assert migrate_request_store(tmp_path / "none", apply=True) == {"state": "absent", "conflicts": [], "applied": False}
    ProviderStore(tmp_path / "state")
    assert migrate_request_store(tmp_path / "state", apply=True)["state"] == "current"
