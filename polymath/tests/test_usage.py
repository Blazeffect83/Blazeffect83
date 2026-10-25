"""v0.9.0: the SD card it has now lasts: where the room goes, when it would run out, and backups that fit."""

from __future__ import annotations

import time
from collections import namedtuple
from types import SimpleNamespace

from polymath.body import maintenance as mt
from polymath.body import usage
from polymath.core.config import SD_BACKUP_EVERY_DAYS, SD_BACKUPS
from polymath.core.scheduler import Scheduler
from polymath.interface import cli
from polymath.interface.dashboard import DashboardData
from tests.test_body import ctx_for

HOUR = 3600.0


def vitals(db, free_by_hour: list[float], *, now: float) -> None:
    """One free-space reading per hour, the last one an hour ago."""
    n = len(free_by_hour)
    with db.transaction():
        for i, free in enumerate(free_by_hour):
            db.execute("INSERT OR REPLACE INTO vitals(at, temp_c, load1, mem_mb, disk_free_gb, data_gb, mode) "
                       "VALUES(?,?,?,?,?,?,?)", (now - (n - i) * HOUR, 50.0, 1.0, 2000.0, free, 5.0, "normal"))  # fmt: skip


# ------------------------------------------------------------------ the forecast
def test_no_forecast_without_six_hours_of_history(db):
    now = time.time()
    assert usage.forecast(db, keep_free_gb=3.0, now=now) is None
    vitals(db, [20.0, 19.9, 19.8], now=now)  # three hours: too little
    assert usage.forecast(db, keep_free_gb=3.0, now=now) is None


def test_a_falling_disk_gets_days_left_and_a_steady_one_none(db):
    now = time.time()
    vitals(db, [20.0 - 0.1 * h for h in range(48)], now=now)  # 0.1 GB an hour: 2.4 GB a day
    fc = usage.forecast(db, keep_free_gb=3.0, now=now)
    assert fc is not None and abs(fc["decline_gb_per_day"] - 2.4) < 0.01 and fc["hours"] == 47.0
    assert abs(fc["days_left"] - (fc["free_gb"] - 3.0) / 2.4) < 0.1
    db.execute("DELETE FROM vitals")
    vitals(db, [15.0 + 0.001 * (h % 3) for h in range(24)], now=now)
    steady = usage.forecast(db, keep_free_gb=3.0, now=now)
    assert steady is not None and steady["days_left"] is None
    db.execute("DELETE FROM vitals")
    vitals(db, [2.0 - 0.1 * h for h in range(12)], now=now)  # already under the reserve: zero, never negative
    under = usage.forecast(db, keep_free_gb=3.0, now=now)
    assert under is not None and under["days_left"] == 0.0


def test_old_readings_do_not_count(db):
    now = time.time()
    vitals(db, [50.0 - h for h in range(24)], now=now - 10 * 86400)  # a fast fall, ten days ago
    vitals(db, [20.0] * 12, now=now)
    fc = usage.forecast(db, keep_free_gb=3.0, now=now)
    assert fc is not None and fc["days_left"] is None


# ------------------------------------------------------------------ the report
def test_files_counts_the_database_backups_indexes_and_downloads(config, db):
    for name in ("polymath-20261001-020000.sqlite3.xz", "polymath-20261002-020000.sqlite3.xz"):
        config.paths.backup_dir.mkdir(parents=True, exist_ok=True)
        (config.paths.backup_dir / name).write_bytes(b"b" * 1000)
    config.paths.index_dir.mkdir(parents=True, exist_ok=True)
    (config.paths.index_dir / "hnsw.bin").write_bytes(b"i" * 500)
    (config.paths.raw_dir / "a").mkdir(parents=True, exist_ok=True)
    (config.paths.raw_dir / "a" / "dump.bz2").write_bytes(b"r" * 300)
    (config.paths.raw_dir / "link").symlink_to(config.paths.raw_dir / "a" / "dump.bz2")  # never counted twice
    f = usage.files(config)
    assert f["backups"] == 2000 and f["backup_count"] == 2 and f["indexes"] == 500 and f["downloads"] == 300
    assert f["database"] >= config.paths.db_path.stat().st_size > 0
    assert f["free"] > 0 and f["disk"] >= f["free"] and f["budget"] == int(config.body.disk_budget_gb * 1e9)
    assert f["sd_card"] is False


def test_the_report_in_words_with_and_without_detail(config, db):
    now = time.time()
    text = "\n".join(usage.lines(config, db))
    assert "the brain's disk:" in text and "backups" in text and "not enough history yet" in text
    vitals(db, [20.0 - 0.05 * h for h in range(24)], now=now)
    text = "\n".join(usage.lines(config, db, detail=True))
    assert "GB/day: about" in text and "facts are kept" in text
    db.execute("DELETE FROM vitals")
    vitals(db, [20.0] * 24, now=now)
    assert "not enough history" in "\n".join(usage.lines(config, None))  # no database: no forecast, no crash
    assert "forecast: steady" in "\n".join(usage.lines(config, db))
    config.storage.sd_only = True
    assert usage.lines(config, None)[0].startswith("the SD card:")
    assert usage.gb(2.5e9) == "2.50 GB" and usage.gb(5e6) == "5 MB"


def test_the_database_breakdown_groups_tables_by_what_they_hold(config, db):
    db.execute("CREATE TABLE zz_extra(x BLOB)")
    with db.transaction():
        for _ in range(200):
            db.execute("INSERT INTO zz_extra(x) VALUES(randomblob(8000))")
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    groups = dict(usage.breakdown(config.paths.db_path))
    assert groups["other"] >= 1_500_000  # the unknown table lands in "other"
    assert {"search index", "knowledge graph"} <= set(groups)
    assert list(groups.values()) == sorted(groups.values(), reverse=True)
    detail = usage.lines(config, db, detail=True)
    assert any(line.strip().startswith("other") for line in detail)
    assert usage.breakdown(config.paths.data_dir / "missing.sqlite3") == []


def test_breakdown_of_a_file_that_is_not_a_database_is_empty(tmp_path):
    bad = tmp_path / "x.sqlite3"
    bad.write_bytes(b"not a database at all" * 100)
    assert usage.breakdown(bad) == []


# ------------------------------------------------------------------ backups that fit
def test_sd_card_mode_backs_up_weekly_and_keeps_one(config):
    assert mt.backup_every_s(config) == 86400.0
    config.storage.sd_only = True
    assert mt.backup_every_s(config) == SD_BACKUP_EVERY_DAYS * 86400.0 and SD_BACKUPS == 1


def test_the_planner_schedules_the_backup_at_the_right_interval(config, db):
    from polymath.core.scheduler import local_phase

    for sd, every in ((False, 86400.0), (True, SD_BACKUP_EVERY_DAYS * 86400.0)):
        config.storage.sd_only = sd
        db.execute("DELETE FROM jobs")
        before = time.time()
        mt.planner(SimpleNamespace(config=config, scheduler=Scheduler(db)))
        key = db.scalar("SELECT key FROM jobs WHERE kind = 'body.backup'")
        slot = int((before - local_phase(config.body.backup_hour)) // every)
        assert key in {f"body.backup@{slot}", f"body.backup@{slot + 1}"}, (sd, key)  # one job per interval window


def test_a_backup_short_of_room_deletes_older_backups_first(config, db, monkeypatch):
    config.paths.backup_dir.mkdir(parents=True, exist_ok=True)
    old = [config.paths.backup_dir / f"polymath-2026100{i}-020000.sqlite3.xz" for i in (1, 2, 3)]
    for p in old:
        p.write_bytes(b"x" * 100)
    real = __import__("shutil").disk_usage
    usage_t = namedtuple("usage_t", "total used free")
    state = {"calls": 0}

    def disk_usage(path):
        state["calls"] += 1
        if state["calls"] == 1:  # before: too little room
            return usage_t(100, 99, 1)
        return real(path)  # after the old backups went: room enough

    monkeypatch.setattr("shutil.disk_usage", disk_usage)
    with db.transaction():
        out = mt.backup_job(ctx_for(config, db, "body.backup")[0])
    assert out.result["ok"]
    left = mt.backups(config.paths.backup_dir)
    assert left[0] not in old and len(left) <= config.body.backup_keep  # the new backup, rotated as usual
    assert not old[0].exists() and not old[1].exists()


def test_a_backup_that_still_does_not_fit_is_skipped_but_keeps_the_newest(config, db, monkeypatch):
    config.paths.backup_dir.mkdir(parents=True, exist_ok=True)
    old = [config.paths.backup_dir / f"polymath-2026100{i}-020000.sqlite3.xz" for i in (1, 2)]
    for p in old:
        p.write_bytes(b"x" * 100)
    usage_t = namedtuple("usage_t", "total used free")
    monkeypatch.setattr("shutil.disk_usage", lambda _p: usage_t(100, 99, 1))
    out = mt.backup_job(ctx_for(config, db, "body.backup")[0])
    assert out.result["ok"] is False and "skipped" in out.result["detail"]
    assert old[1].exists() and not old[0].exists()  # the last good backup survives


def test_eviction_keeps_one_backup_in_sd_card_mode(config, db, monkeypatch):
    config.paths.backup_dir.mkdir(parents=True, exist_ok=True)
    names = [config.paths.backup_dir / f"polymath-2026100{i}-020000.sqlite3.xz" for i in (1, 2, 3)]
    for p in names:
        p.write_bytes(b"x" * 1000)
    usage_t = namedtuple("usage_t", "total used free")
    monkeypatch.setattr("shutil.disk_usage", lambda _p: usage_t(10**12, 10**12 - 10**6, 10**6))  # 1 MB free
    config.storage.sd_only = True
    mt.evict_job(ctx_for(config, db, "body.evict")[0])
    assert [p.name for p in mt.backups(config.paths.backup_dir)] == [names[2].name]
    for p in names:
        p.write_bytes(b"x" * 1000)
    config.storage.sd_only = False
    mt.evict_job(ctx_for(config, db, "body.evict")[0])
    assert len(mt.backups(config.paths.backup_dir)) == 2  # on a drive: two


# ------------------------------------------------------------------ Orin and the dashboard
def test_orin_reads_days_to_full_and_acts_before_the_disk_fills(tmp_path):
    from tests.test_caretaker import World

    w = World(tmp_path)
    try:
        now = w.t
        vitals(w.db, [40.0 - 0.6 * h for h in range(24)], now=now)  # 14.4 GB a day, ~26 GB free: under 2 days
        w.vitals(free=26.0)
        sig = w.orin().read()
        assert 0 < sig["days_to_full"] < 3
        o = w.orin()
        assert o.hard("days_to_full", 2.0) and not o.hard("days_to_full", 10.0)
        w.turn()
        assert "disk_low" in [r["kind"] for r in w.incidents()]
    finally:
        w.db.close()


def test_the_backup_age_limit_follows_the_backup_interval(tmp_path):
    from tests.test_caretaker import World

    w = World(tmp_path)
    try:
        o = w.orin()
        assert o.hard("backup_age_h", 40.0) and not o.hard("backup_age_h", 30.0)  # nightly: 36 h
        w.config.storage.sd_only = True
        o = w.orin()
        assert not o.hard("backup_age_h", 200.0) and o.hard("backup_age_h", 260.0)  # weekly: 252 h
    finally:
        w.db.close()


def test_the_dashboard_shows_where_the_room_goes(config, db):
    now = time.time()
    vitals(db, [30.0 - 0.1 * h for h in range(24)], now=now)
    data = DashboardData(config)
    s = data.storage(db)
    assert s["files"]["database"] > 0 and s["forecast"]["days_left"] is not None
    assert "free on the brain's disk" in s["lines"][0] and "GB a day" in s["lines"][1]
    db.execute("DELETE FROM vitals")
    vitals(db, [30.0] * 24, now=now)
    assert data.storage(db)["lines"][-1] == "Free space is steady."
    assert "storage" in data.insights()


def test_polymath_storage_usage(config, db, capsys, tmp_path):
    args = SimpleNamespace(op="usage", detail=True)
    assert cli.cmd_storage(config, args) == 0  # type: ignore[arg-type]
    out = capsys.readouterr()
    assert "database" in out.out and "forecast" in out.out and "reading the whole database" in out.err
    config.paths.data_dir = tmp_path / "no-brain-yet"
    assert cli.cmd_storage(config, SimpleNamespace(op="usage", detail=False)) == 0  # type: ignore[arg-type]
    assert "not enough history" in capsys.readouterr().out
