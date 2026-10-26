"""Phase 10: thermal / disk guard, vitals, backups with rotation and restore, eviction, retired settings."""

from __future__ import annotations

import json
import lzma
import os
import shutil
import sqlite3
import threading
import time
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace

import pytest

from polymath.body import maintenance as mt
from polymath.body.guard import Guard, data_bytes
from polymath.body.sensors import Vitals
from polymath.core.app import build_agent
from polymath.core.jobs import JobContext, noop_handler
from polymath.core.scheduler import Job, Scheduler, local_phase
from polymath.memory.documents import Document, DocumentStore
from polymath.memory.graph import KnowledgeGraph
from polymath.memory.text_index import TextIndex
from tests.fixtures.typing import some


# ---------------------------------------------------------------------- guard
class Clock:
    def __init__(self) -> None:
        self.t = 1_790_000_000.0

    def __call__(self) -> float:
        return self.t


def guard_for(config, db, temps, *, free=500.0):
    clock = Clock()
    state = {"temp": temps, "free": free}

    def sensors():
        return Vitals(state["temp"], 0.5, 4, 1000.0, state["free"], 10.0)

    g = Guard(config, db, clock=clock, sensors=sensors)
    return g, clock, state, None


def test_thermal_modes_with_hysteresis(config, db):
    g, clock, state, _ = guard_for(config, db, 60.0)
    seq = []
    for t in (60, 76, 74, 71, 83, 80, 78.5, 76, 70):
        state["temp"] = float(t)
        clock.t += 1
        st = g.observe()
        seq.append((t, st.mode, st.intensity))
    assert [m for _t, m, _i in seq] == [
        "normal",
        "throttle",
        "throttle",
        "normal",
        "pause",
        "pause",
        "pause",
        "throttle",
        "normal",
    ]
    assert dict((t, i) for t, _m, i in seq)[83] == 0.0 and dict((t, i) for t, _m, i in seq)[76] == 0.5
    state["temp"] = None
    assert g.observe().mode == "normal"  # no sensor: keep the last thermal state


def test_retired_minecraft_settings_are_ignored_not_fatal(tmp_path, caplog):
    """A configuration written for the version with the Minecraft player check still loads after upgrade."""
    from polymath.core.config import ConfigError, load_config

    f = tmp_path / "old.toml"
    f.write_text(
        '[body]\nminecraft_host = "127.0.0.1"\nminecraft_port = 25565\nminecraft_check = true\n'
        "minecraft_poll_seconds = 60.0\nthrottle_celsius = 70.0\n"
    )
    with caplog.at_level("WARNING", logger="polymath.config"):
        cfg = load_config(f, env={})
    assert cfg.body.throttle_celsius == 70.0 and not hasattr(cfg.body, "minecraft_check")
    assert "minecraft_check" in caplog.text
    f.write_text("[senses]\nminecraft_port = 1\n")  # retired only where it used to live
    with pytest.raises(ConfigError, match="unknown key"):
        load_config(f, env={})


def test_vitals_no_longer_record_players(config, db):
    g, *_ = guard_for(config, db, 50.0)
    st = g.observe()
    assert st.mode == "normal" and not hasattr(st, "players_online")
    cols = {r["name"] for r in db.query("PRAGMA table_info(vitals)")}
    assert "players" not in cols and db.scalar("SELECT mode FROM vitals") == "normal"


def test_disk_pressure_requests_eviction_and_allows_it_while_paused(config, db):
    config.body.disk_min_free_gb = 10.0
    g, clock, state, _ = guard_for(config, db, 50.0, free=8.0)
    st = g.observe()
    assert st.mode == "yield" and st.allow == []
    assert db.one("SELECT kind, priority FROM jobs")["kind"] == "body.evict"
    state["free"] = 3.0
    st = g.observe()
    assert st.mode == "pause" and st.allow == ["body.evict", "caretaker.step"]  # Orin keeps watching too
    assert db.scalar("SELECT COUNT(*) FROM jobs WHERE kind='body.evict'") == 1  # one request per hour
    state["temp"] = 90.0
    assert g.observe().allow == ["caretaker.step"]  # too hot: nothing runs, not even eviction; only Orin watches
    config.body.disk_budget_gb = 0.000001
    state.update(temp=50.0, free=500.0)
    clock.t += 3600
    assert g.observe().mode == "normal"
    assert db.scalar("SELECT COUNT(*) FROM jobs WHERE kind='body.evict'") == 2  # over budget


def test_vitals_logged_every_five_minutes(config, db):
    g, clock, _state, _ = guard_for(config, db, 55.5)
    for _ in range(4):
        g.observe()
        clock.t += 100
    rows = db.query("SELECT * FROM vitals ORDER BY at")
    assert len(rows) == 2 and rows[0]["temp_c"] == 55.5 and rows[0]["mode"] == "normal"
    db.execute("INSERT INTO vitals(at, mode) VALUES(1, 'normal')")
    clock.t += 400
    g.observe()
    assert db.scalar("SELECT COUNT(*) FROM vitals WHERE at = 1") == 0  # 30-day retention


def test_data_bytes_excludes_free_pages(config, db):
    before = data_bytes(db, config.paths.data_dir)
    db.execute("CREATE TABLE junk(x BLOB)")
    db.executemany("INSERT INTO junk VALUES(?)", [(os.urandom(4000),) for _ in range(500)])
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    grown = data_bytes(db, config.paths.data_dir)
    db.execute("DROP TABLE junk")
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert grown > before + 1.5e6 and data_bytes(db, config.paths.data_dir) < grown - 1.5e6


def test_paused_agent_runs_only_maintenance(config, monkeypatch):
    agent = build_agent(config, planners=False)
    agent.start()
    hits = []

    def counted(ctx):
        hits.append(1)
        return noop_handler(ctx)

    agent.registry.register("body.test", counted, "t")
    agent.scheduler.enqueue("t.other", {}, key="o", priority=9)
    agent.scheduler.enqueue("body.test", {}, key="b")
    from polymath.core.loop import BodyState

    paused = BodyState(mode="pause", intensity=0, reasons=["disk"], allow=["body.test"])
    monkeypatch.setattr(agent.body, "observe", lambda: paused)
    assert agent.cycle().status == "done" and hits == [1]
    assert agent.cycle().status == "paused"  # only the unrelated job is left
    assert agent.db.kv_get("paused_reason") == "disk"
    agent.shutdown()
    agent.db.close()


def test_local_phase():
    phase = local_phase(3)
    assert 0 <= phase < 86400
    lt = time.localtime(time.time())
    assert (phase + lt.tm_gmtoff) % 86400 == 3 * 3600


# --------------------------------------------------------------------- backups
def ctx_for(config, db, kind, payload=None):
    s = Scheduler(db)
    jid, _ = s.enqueue(kind, payload or {}, key=f"{kind}:{time.time_ns()}")
    ticks = []
    ctx = JobContext(
        config=config,
        db=db,
        scheduler=s,
        job=Job(jid, kind, f"k{jid}", payload or {}, None, 0, 0, 0, 5, 0),
        deadline=time.monotonic() + 30,
        stop_event=threading.Event(),
        services={},
        on_tick=lambda: ticks.append(1),
    )
    return ctx, ticks


def test_backup_verify_rotate_and_restore(config, db):
    db.kv_set("marker", "before")
    config.body.backup_keep = 2
    paths = []
    with db.transaction():  # jobs run inside the agent's write transaction
        for _ in range(3):
            ctx, ticks = ctx_for(config, db, "body.backup")
            out = mt.backup_job(ctx)
            assert out.result["ok"] and ticks
            paths.append(out.result["path"])
            time.sleep(1.05)  # names carry seconds
    kept = mt.backups(config.paths.backup_dir)
    assert [p.name for p in kept] == [os.path.basename(p) for p in paths[:0:-1]]
    assert db.scalar("SELECT COUNT(*) FROM backups WHERE ok=1") == 3
    raw = lzma.decompress(kept[0].read_bytes())
    assert raw[:16] == b"SQLite format 3\x00"
    db.kv_set("marker", "after")
    db.close()
    old = mt.restore(kept[0], config.paths.db_path)
    assert old.exists()
    con = sqlite3.connect(config.paths.db_path)
    assert json.loads(con.execute("SELECT value FROM kv WHERE key='marker'").fetchone()[0]) == "before"
    con.close()
    broken = config.paths.backup_dir / "polymath-broken.sqlite3.xz"
    broken.write_bytes(lzma.compress(b"SQLite format 3\x00" + b"\x00" * 4000))
    with pytest.raises(Exception):  # noqa: B017 - sqlite raises DatabaseError or our RuntimeError
        mt.restore(broken, config.paths.db_path)


def test_backup_skipped_without_room(config, db, monkeypatch):
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr("shutil.disk_usage", lambda _p: usage(100, 99, 1))
    ctx, _ = ctx_for(config, db, "body.backup")
    out = mt.backup_job(ctx)
    assert out.result["ok"] is False and "skipped" in out.result["detail"]
    assert db.one("SELECT ok, detail FROM backups")["ok"] == 0 and not mt.backups(config.paths.backup_dir)


def test_backup_planner(config, db):
    agent = SimpleNamespace(config=config, scheduler=Scheduler(db))
    mt.planner(agent)
    assert db.one("SELECT kind, priority FROM jobs")["kind"] == "body.backup"


# -------------------------------------------------------------------- eviction
def test_eviction_frees_least_valuable_first(config, db):
    store = DocumentStore(db)
    text = TextIndex(db, store)
    g = KnowledgeGraph(db)
    ids = {}
    words = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta", "iota", "kappa", "lambda", "omicron"]
    for i, (source, word) in enumerate(
        [("feed", "zebra"), ("web", "yak"), ("wikipedia", "xylophone"), ("feed", "walrus")]
    ):
        body = f"The {word} is described here. " + " ".join(words * 40) + f" more {i}"
        did, _ = store.add(Document(source, str(i), word.title(), body, "CC"))
        text.index(did, word.title(), body)
        ids[word] = did
    db.execute("UPDATE documents SET fetched = fetched - 100 WHERE id=?", (ids["walrus"],))  # oldest feed item
    e = g.upsert_entity("Q1", "X")
    f = g.upsert_entity("Q2", "Y")
    g.add_triple(e, g.predicate("P1"), o=f, kind="pattern", source="text", doc_id=ids["zebra"])  # evidence
    exact, _ = store.add(Document("web", "dup", "Copy", some(store.get(ids["yak"])).text, "CC"))
    assert db.one("SELECT state, codec FROM documents WHERE id=?", (exact,))["codec"] == "evicted"  # never stored
    near, _ = store.add(Document("web", "near", "Near copy", some(store.get(ids["yak"])).text + " (mirror)", "CC"))
    db.execute("UPDATE documents SET state='duplicate' WHERE id=?", (near,))  # what MinHash dedup decides later
    raw = config.paths.raw_dir / "old" / "dump.bz2"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"x" * 1000)
    os.utime(raw, (time.time() - 30 * 86400,) * 2)
    kept = config.paths.raw_dir / "live.bz2"
    kept.write_bytes(b"y" * 1000)
    os.utime(kept, (time.time() - 30 * 86400,) * 2)
    Scheduler(db).enqueue("feeds.ingest", {"dest": "live.bz2"}, key="live")

    config.body.disk_budget_gb = 1e-9  # everything is over budget
    ctx, _ = ctx_for(config, db, "body.evict")
    out = mt.evict_job(ctx)
    res = out.result
    assert res["raw_files_removed"] == 1 and not raw.exists() and kept.exists()  # in-use download kept
    assert res["duplicate_bodies"] == 1
    states = {r["id"]: r["codec"] for r in db.query("SELECT id, codec FROM documents")}
    assert states[ids["zebra"]] != "evicted"  # it is the evidence for a fact
    assert states[ids["walrus"]] == states[ids["yak"]] == "evicted"
    assert res["documents_evicted"] >= 3 and res["freed_bytes"] > 1000
    assert not text.search("walrus") and text.search("zebra")
    meta = json.loads(db.scalar("SELECT meta FROM documents WHERE id=?", (ids["walrus"],)))
    assert "evicted" in meta and db.scalar("SELECT license FROM documents WHERE id=?", (ids["walrus"],)) == "CC"
    assert some(store.get(ids["walrus"])).text == ""
    config.body.disk_budget_gb = 1e6
    ctx, _ = ctx_for(config, db, "body.evict")
    assert mt.evict_job(ctx).result["need_mb"] == 0


def test_cli_backup_list_and_restore(tmp_path, capsys):
    from polymath.interface import cli
    from tests.conftest import make_config

    cfg = make_config(tmp_path)
    path = str(tmp_path / "polymath.toml")
    assert cli.main(["--config", path, "init"]) == 0
    assert cli.main(["--config", path, "backup"]) == 0
    capsys.readouterr()
    assert cli.main(["--config", path, "backup", "--list"]) == 0
    listed = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert len(listed) == 1 and listed[0]["bytes"] > 0
    assert cli.main(["--config", path, "restore", os.path.basename(listed[0]["path"])]) == 0
    assert json.loads(capsys.readouterr().out)["restored"].endswith(".sqlite3.xz")
    from polymath.core.db import Database

    db = Database(cfg.paths.db_path)
    db.kv_set("heartbeat", {"ts": time.time(), "state": "running", "cycle": 1})
    db.close()
    assert cli.main(["--config", path, "restore", listed[0]["path"]]) == 5  # never under a running agent
    assert cli.main(["--config", path, "backup"]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["queued_via"] == "inbox"


def test_agent_pulse_file_during_long_slices(config):
    agent = build_agent(config, planners=False)
    agent.start()
    pulse = config.paths.data_dir / "heartbeat"
    assert pulse.exists()
    old = time.time() - 1000
    os.utime(pulse, (old, old))

    def long_job(ctx):
        ctx.tick()  # what long handlers do to feed the watchdog
        return noop_handler(ctx)

    agent.registry.register("t.long", long_job, "t")
    agent._pulsed = -1e18
    agent.scheduler.enqueue("t.long", {}, key="l")
    agent.cycle()
    assert time.time() - pulse.stat().st_mtime < 60
    agent.shutdown()
    agent.db.close()


def test_a_disk_pause_holds_until_room_is_back_and_says_why(config, db):
    config.body.disk_min_free_gb = 3.0  # the SD card's (sd_keep_free_gb)
    g, _clock, state, _ = guard_for(config, db, 50.0, free=1.4)
    st = g.observe()
    assert st.mode == "pause" and db.kv_get("body_reason") == "only 1.4 GB free on the data disk"
    state["free"] = 1.9  # eviction freed a little: still paused, not on and off every few minutes
    st = g.observe()
    assert st.mode == "pause" and st.reasons[0] == "only 1.9 GB free on the data disk (resumes at 2.2 GB)"
    state["free"] = 2.3
    st = g.observe()
    assert st.mode == "yield" and db.kv_get("body_reason") == "only 2.3 GB free on the data disk"
    state["free"] = 1.9  # below the resume point but above the pause point: light work, not a pause
    assert g.observe().mode == "yield"


# ------------------------------------------------------------------ v0.12.0: every backup is test-restored
def test_every_backup_is_test_restored_and_a_broken_one_is_replaced(config, db):
    store = DocumentStore(db)
    store.add(Document("wikipedia", "1", "Mars", "Mars is a planet. " * 40, "CC"))
    with db.transaction():
        ctx, _ = ctx_for(config, db, "body.backup")
        first = mt.backup_job(ctx).result["path"]
    counts = json.loads(db.scalar("SELECT counts FROM backups WHERE path = ?", (first,)))
    assert counts["documents"] == 1 and counts["db_bytes"] > 0
    ctx, ticks = ctx_for(config, db, "body.verify_backup")
    out = mt.verify_backup_job(ctx)
    assert out.result["ok"] == 1 and "restored and checked: quick_check ok; 1 documents" in out.result["detail"]
    assert ticks and not list(config.paths.backup_dir.glob(".verify-*"))  # the scratch copy is gone
    assert mt.verify_backup_job(ctx_for(config, db, "body.verify_backup")[0]).result == {"tested": 0}
    # a backup that lost rows on the way (or whose file is damaged) fails, is deleted and taken again
    time.sleep(1.05)
    with db.transaction():
        second = mt.backup_job(ctx_for(config, db, "body.backup")[0]).result["path"]
    db.execute("UPDATE backups SET counts = json_set(counts, '$.documents', 2) WHERE path = ?", (second,))
    ctx, _ = ctx_for(config, db, "body.verify_backup")
    out = mt.verify_backup_job(ctx)
    assert out.result["ok"] == 0 and "rows are missing: documents 1 instead of 2" in out.result["detail"]
    assert not Path(second).exists() and Path(first).exists()
    assert db.scalar("SELECT COUNT(*) FROM jobs WHERE kind = 'body.backup' AND key LIKE 'verify:rebackup:%'") == 1
    issue = db.one("SELECT text, detail FROM events WHERE kind = 'issue' AND text LIKE '%failed its test restore%'")
    assert "a new backup is being taken now" in issue["text"] and "backup_broken" in issue["detail"]
    damaged = config.paths.backup_dir / "polymath-29990101-000000.sqlite3.xz"
    damaged.write_bytes(b"not xz at all")
    ok, why = mt.trial_restore(damaged, config.paths.backup_dir / ".verify-x", {})
    assert not ok and "does not restore" in why and not (config.paths.backup_dir / ".verify-x").exists()


def test_a_backup_is_not_tested_without_room_on_the_sd_card_or_when_gone(config, db, monkeypatch):
    with db.transaction():
        path = Path(mt.backup_job(ctx_for(config, db, "body.backup")[0]).result["path"])
    usage = namedtuple("usage", "total used free")
    real = shutil.disk_usage
    monkeypatch.setattr("shutil.disk_usage", lambda _p: usage(100, 99, 1))
    out = mt.verify_backup_job(ctx_for(config, db, "body.verify_backup")[0])
    assert out.result["ok"] is None and "not tested: 0.0 GB free" in out.result["detail"]
    monkeypatch.setattr("shutil.disk_usage", real)
    db.execute("UPDATE backups SET verified = NULL")
    monkeypatch.setattr("polymath.core.config.brain_on_sd_card", lambda cfg, **k: True)
    out = mt.verify_backup_job(ctx_for(config, db, "body.verify_backup")[0])
    assert "not tested on the SD card itself" in out.result["detail"]
    db.execute("UPDATE backups SET verified = NULL")
    path.unlink()
    out = mt.verify_backup_job(ctx_for(config, db, "body.verify_backup")[0])
    assert out.result["ok"] is None and "gone before it was tested" in out.result["detail"]
    agent = SimpleNamespace(config=config, scheduler=Scheduler(db))
    before = db.scalar("SELECT COUNT(*) FROM jobs WHERE kind = 'body.verify_backup'")
    mt.planner(agent)
    assert db.scalar("SELECT COUNT(*) FROM jobs WHERE kind = 'body.verify_backup'") == before + 1  # daily


# ------------------------------------------------------------------ v0.12.0: texts kept as pointers, read again
def test_articles_holding_up_facts_go_last_as_pointers_and_come_back_whole(config, db):
    from polymath.memory import inventory

    store = DocumentStore(db)
    text = TextIndex(db, store)
    g = KnowledgeGraph(db)
    body = "The quokka lives on Rottnest Island. " * 60
    did, _ = store.add(Document("wikipedia", "Quokka", "Quokka", body, "CC BY-SA"))
    text.index(did, "Quokka", body)
    e, f = g.upsert_entity("Q1", "Quokka"), g.upsert_entity("Q2", "Rottnest Island")
    g.add_triple(e, g.predicate("P1"), o=f, kind="pattern", source="text", doc_id=did)  # it holds up a fact
    config.body.disk_budget_gb = 1e-9
    res = mt.evict_job(ctx_for(config, db, "body.evict")[0]).result
    assert res["documents_evicted"] == 0 and res["kept_as_pointers"] == 1
    meta = json.loads(db.scalar("SELECT meta FROM documents WHERE id = ?", (did,)))
    assert meta["refetch"] == "wikipedia" and not text.search("quokka")
    assert db.scalar("SELECT COUNT(*) FROM provenance WHERE doc_id = ?", (did,)) == 1  # the fact stays
    # room again (the brain moved onto a big drive): it joins the relearning queue, the most facts first
    config.body.disk_budget_gb = 1e6
    config.body.disk_min_free_gb = 0.000001
    monkeypatch_free = inventory.REFILL_ROOM_GB
    inventory.REFILL_ROOM_GB = 0.000001
    try:
        assert inventory.refill(db, config, time.time()) == 1
        assert inventory.refill(db, config, time.time()) == 0  # once
    finally:
        inventory.REFILL_ROOM_GB = monkeypatch_free
    row = db.one("SELECT volume_id, how, priority, state FROM relearn WHERE doc_id = ?", (did,))
    assert row["volume_id"] == "evicted" and row["how"] == "wikipedia" and row["priority"] >= 10
    # read again (wikipedia.titles stores the same article): its text and passages are back
    _id, status = store.add(Document("wikipedia", "Quokka", "Quokka", body, "CC BY-SA"))
    assert status == "updated" and some(store.get(did)).text == body and text.search("quokka")
    assert "refetch" not in json.loads(db.scalar("SELECT meta FROM documents WHERE id = ?", (did,)))


def test_no_room_no_refill(config, db, monkeypatch):
    from polymath.memory import inventory

    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr("shutil.disk_usage", lambda _p: usage(100, 99, 1))
    assert not inventory.room_to_refill(config) and inventory.refill(db, config, time.time()) == 0
    monkeypatch.setattr("shutil.disk_usage", lambda _p: (_ for _ in ()).throw(OSError("gone")))
    assert not inventory.room_to_refill(config)


def test_backup_list_shows_each_test_restore(config, db, capsys):
    import argparse

    from polymath.interface import cli

    with db.transaction():
        path = mt.backup_job(ctx_for(config, db, "body.backup")[0]).result["path"]
    mt.verify_backup_job(ctx_for(config, db, "body.verify_backup")[0])
    assert cli.cmd_backup(config, argparse.Namespace(list=True)) == 0
    row = json.loads(capsys.readouterr().out.splitlines()[0])
    assert row["path"] == path and row["tested"] == "ok" and "quick_check ok" in row["test"]
