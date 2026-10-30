from datetime import datetime, timedelta, timezone

import pytest

from price_comparator import PriceComparatorStore
from price_connectors import ConnectorBlocked, ConnectorError, FeedConnector, validate_source
from price_scheduler import CompetitorScheduler, SchedulerConfigError
from tests.test_price_comparator import _store

EAN = "0036000291452"
HEADER = "ean,precio,moneda,disponibilidad,url\n"


class Clock:
    def __init__(self):
        self.now = datetime(2025, 1, 1, 8, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


class Feed:
    def __init__(self, price="100", error=None, rows=None):
        self.price, self.error, self.rows, self.calls = price, error, rows, 0

    def __call__(self, url):
        self.calls += 1
        if self.error:
            raise self.error
        rows = self.rows if self.rows is not None else f"{EAN},{self.price},EUR,available,https://eur.example/p\n"
        return (HEADER + rows).encode()


def _setup(tmp_path, feed=None, **config):
    store = _store(tmp_path)
    with store._connect() as c, c:
        c.execute("UPDATE competitors SET market='ES'")
    feed = feed or Feed()
    clock = Clock()
    scheduler = CompetitorScheduler(store, {"feed_csv": FeedConnector(get=feed)}, clock=clock,
                                    sleep=lambda s: None)
    cid = [c for c in scheduler.overview()["competitor_id"] if "eur" in c][0]
    scheduler.configure(cid, connector="feed_csv", source="https://eur.example/feed.csv",
                        authorization_note="Acuerdo con la tienda", **config)
    scheduler.verify(cid)
    scheduler.enable(cid)
    return store, scheduler, clock, feed, cid


def _count(store, table):
    with store._connect() as c:
        return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_unconfigured_competitors_are_pending_and_cannot_be_enabled(tmp_path):
    scheduler = CompetitorScheduler(_store(tmp_path), {"feed_csv": FeedConnector(get=Feed())})
    overview = scheduler.overview()
    assert set(overview["state_label"]) == {"Pendiente de configurar"}
    with pytest.raises(SchedulerConfigError):
        scheduler.enable(overview["competitor_id"][0])
    assert scheduler.run_once() == []


def test_verify_requires_authorisation_and_working_source(tmp_path):
    store = _store(tmp_path)
    scheduler = CompetitorScheduler(store, {"feed_csv": FeedConnector(get=Feed(error=ConnectorError("caído")))})
    cid = scheduler.overview()["competitor_id"][0]
    scheduler.configure(cid, connector="feed_csv", source="https://eur.example/feed.csv")
    with pytest.raises(SchedulerConfigError):
        scheduler.verify(cid)
    scheduler.configure(cid, connector="feed_csv", source="https://eur.example/feed.csv", authorization_note="ok")
    with pytest.raises(ConnectorError):
        scheduler.verify(cid)
    assert scheduler.state(scheduler.config(cid)) == "unverified"


def test_changing_the_source_requires_new_verification(tmp_path):
    _, scheduler, _, _, cid = _setup(tmp_path)
    assert scheduler.state(scheduler.config(cid)) == "active"
    scheduler.configure(cid, connector="feed_csv", source="https://eur.example/other.csv",
                        authorization_note="ok")
    assert scheduler.state(scheduler.config(cid)) == "unverified"


@pytest.mark.parametrize("source", ["http://x.example/f.csv", "https://u:p@x.example/f.csv",
                                    "https://x.example/f.csv?token=abc", "../secret.csv", ""])
def test_sources_with_credentials_or_unsafe_are_rejected(source):
    with pytest.raises(ConnectorError):
        validate_source(source)


def test_repeated_checks_do_not_duplicate_and_record_last_ok(tmp_path):
    store, scheduler, clock, feed, cid = _setup(tmp_path)
    first = scheduler.run_once()[0]
    assert (first.changed, first.status) == (1, "ok")
    clock.advance(hours=25)
    second = scheduler.run_once()[0]
    assert (second.unchanged, second.changed) == (1, 0)
    assert _count(store, "offers") == 1
    with store._connect() as c:
        row = c.execute("SELECT checks,last_ok_at FROM offer_checks").fetchone()
    assert row["checks"] == 2 and row["last_ok_at"].startswith("2025-01-02")


def test_price_change_keeps_history(tmp_path):
    store, scheduler, clock, feed, cid = _setup(tmp_path)
    scheduler.run_once()
    feed.price = "90"
    clock.advance(hours=25)
    assert scheduler.run_once()[0].changed == 1
    assert _count(store, "offers") == 2


def test_not_due_run_does_nothing(tmp_path):
    _, scheduler, clock, feed, _ = _setup(tmp_path)
    scheduler.run_once()
    calls = feed.calls
    clock.advance(hours=1)
    assert scheduler.run_once() == []
    assert feed.calls == calls


def test_block_suspends_connector_and_keeps_jobs(tmp_path):
    store, scheduler, _, _, cid = _setup(tmp_path)
    scheduler.connectors["feed_csv"]._get = Feed(error=ConnectorBlocked("HTTP 429"))
    summary = scheduler.run_once()[0]
    assert summary.status == "blocked"
    assert scheduler.state(scheduler.config(cid)) == "suspended"
    assert scheduler.overview().set_index("competitor_id").loc[cid, "queued"] == 1
    assert scheduler.run_once() == []
    with pytest.raises(SchedulerConfigError):
        scheduler.enable(cid)
    scheduler.resume(cid)
    scheduler.connectors["feed_csv"]._get = Feed()
    assert scheduler.run_once()[0].changed == 1


def test_transient_errors_back_off_and_end_in_error(tmp_path):
    store, scheduler, clock, _, cid = _setup(tmp_path, max_attempts=2)
    scheduler.connectors["feed_csv"]._get = Feed(error=ConnectorError("timeout"))
    assert scheduler.run_once()[0].errors == 1
    assert scheduler.run_once() == []  # still backing off
    clock.advance(hours=1)
    scheduler.run_once()
    with store._connect() as c:
        assert c.execute("SELECT status FROM scheduler_jobs").fetchone()[0] == "error"


def test_unlisted_ean_is_not_stored(tmp_path):
    store, scheduler, *_ = _setup(tmp_path, feed=Feed(rows=""))
    assert scheduler.run_once()[0].not_listed == 1
    assert _count(store, "offers") == 0


def test_running_jobs_are_recovered_after_restart(tmp_path):
    store, scheduler, clock, feed, cid = _setup(tmp_path)
    scheduler.run_once()
    clock.advance(hours=25)
    with store._connect() as c, c:
        c.execute("""INSERT INTO scheduler_jobs(competitor_id,ean,cycle_started_at,status,attempts,not_before,created_at)
                     VALUES(?,?,?,'running',1,?,?)""", (cid, EAN, "x", "2025-01-01T00:00:00+00:00", "x"))
        c.execute("UPDATE scheduler_jobs SET status='done' WHERE status='done'")
    assert scheduler.run_once()[0].ok == 1
    with store._connect() as c:
        assert c.execute("SELECT COUNT(*) FROM scheduler_jobs WHERE status='running'").fetchone()[0] == 0


def test_concurrent_run_of_same_competitor_is_refused(tmp_path):
    _, scheduler, _, _, cid = _setup(tmp_path)
    with scheduler._lock(cid):
        assert scheduler.run_once()[0].status == "busy"
        assert scheduler.is_running()


def test_pause_and_disable_stop_runs(tmp_path):
    _, scheduler, _, _, cid = _setup(tmp_path)
    scheduler.pause(cid)
    assert scheduler.run_once() == []
    scheduler.resume(cid)
    scheduler.disable(cid)
    assert scheduler.run_once() == []


def test_plan_spreads_work_and_respects_request_limit(tmp_path):
    store, scheduler, *_ , cid = _setup(tmp_path, interval_hours=10, pause_seconds=5)
    plan = scheduler.plan(cid)
    assert plan["products"] == 1 and plan["batches"] == 1 and plan["fits_interval"]


def test_retention_purges_old_history_but_keeps_latest(tmp_path):
    store, scheduler, clock, feed, cid = _setup(tmp_path)
    scheduler.run_once()
    feed.price = "90"
    clock.advance(hours=25)
    scheduler.run_once()
    clock.advance(days=400)
    store.set_setting("retention_months", "12")
    result = scheduler.purge()
    assert result["offers"] == 1 and _count(store, "offers") == 1
    with pytest.raises(ValueError):
        store.set_setting("retention_months", "5")


def test_feed_connector_reads_local_files_only_inside_folder(tmp_path):
    (tmp_path / "f.csv").write_text(f"EAN;Precio;Moneda;Disponibilidad;URL\n{EAN};9,5;eur;available;https://a.example/\n",
                                    encoding="utf-8")
    connector = FeedConnector(directory=tmp_path)
    connector.verify("f.csv")
    assert connector.fetch("f.csv", [EAN])[0].ean == EAN
    with pytest.raises(ConnectorError):
        connector.verify("missing.csv")
    (tmp_path / "bad.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(ConnectorError):
        connector.verify("bad.csv")


def test_odoo_frequency_is_configurable_and_separate(tmp_path):
    from vytria_price_sync import SCHEDULE_INTERVAL, VytriaPriceSync

    sync = VytriaPriceSync(PriceComparatorStore(tmp_path / "x.sqlite3"), reader=object())
    assert sync.interval == SCHEDULE_INTERVAL
    sync.set_interval_hours(6)
    assert sync.interval == timedelta(hours=6)
    with pytest.raises(ValueError):
        sync.set_interval_hours(0)


def test_stop_request_keeps_queue_for_the_next_pass(tmp_path):
    store, scheduler, clock, feed, cid = _setup(tmp_path)
    scheduler.stop_requested = True
    assert scheduler.run_once()[0].requests == 0
    assert feed.calls == 1  # only the verification call
    with store._connect() as c:
        assert c.execute("SELECT COUNT(*) FROM scheduler_jobs WHERE status='pending'").fetchone()[0] == 1
    scheduler.stop_requested = False
    assert scheduler.run_once()[0].ok == 1


def test_cli_configure_status_runs_and_manual_once(tmp_path, monkeypatch, capsys):
    import price_scheduler

    store, scheduler, *_ , cid = _setup(tmp_path)
    monkeypatch.setenv("PRICE_COMPARATOR_DB_PATH", str(store.database_path))
    monkeypatch.setattr(price_scheduler, "CONNECTORS", {"feed_csv": FeedConnector(get=Feed())})
    assert price_scheduler.main(["--configure", cid, "--interval-hours", "6", "--max-requests", "3"]) == 0
    config = scheduler.config(cid)
    assert (config["interval_hours"], config["max_requests_per_run"], bool(config["verified_at"])) == (6, 3, True)
    assert price_scheduler.main(["--once", "--force", "--competitor", cid]) == 0
    assert price_scheduler.main(["--status"]) == 0
    assert price_scheduler.main(["--runs", "5"]) == 0
    out = capsys.readouterr().out
    assert "cada 6 h" in out and "[cli] ok" in out and "En ejecución ahora: no" in out
    assert price_scheduler.main(["--configure", cid, "--interval-hours", "0"]) == 1

