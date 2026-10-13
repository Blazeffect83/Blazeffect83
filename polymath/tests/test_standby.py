"""v0.6.0: the standby brain. While the brain's drive is gone, Polymath learns on a small brain on the SD card,
and hands everything it read to the real brain when the drive is back."""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from polymath.core import fsprobe, standby
from polymath.core.app import BrainDriveMissing, build_agent, build_components, check_storage
from polymath.core.config import HOME_FILE, MOVED_FILE, ConfigError, apply_home, load_config
from polymath.core.db import open_database
from polymath.core.scheduler import Scheduler
from polymath.interface import cli, doctor, face, feed, snapshot
from polymath.memory.documents import Document, DocumentStore
from tests.conftest import make_config

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def cfg(tmp_path: Path) -> Any:
    c = make_config(tmp_path)
    c.storage.standby_dir = tmp_path / "standby"
    c.storage.standby_dir.mkdir()
    c.paths.run_dir = tmp_path / "run"
    c.paths.run_dir.mkdir()
    return c


def drive_gone(c: Any) -> None:
    c.paths.data_dir.mkdir(parents=True, exist_ok=True)
    (c.paths.data_dir / MOVED_FILE).write_text(json.dumps({"name": "Samsung T7"}))


def drive_back(c: Any) -> None:
    (c.paths.data_dir / MOVED_FILE).unlink()


def doc(n: int, text: str | None = None) -> Document:
    return Document(source="web", external_id=f"https://example.org/{n}", title=f"Page {n}",
                    text=text or f"Page {n} says something new about topic {n}.", license="CC BY-SA 4.0",
                    url=f"https://example.org/{n}")  # fmt: skip


def args(**kw: Any) -> argparse.Namespace:
    return argparse.Namespace(max_cycles=kw.get("max_cycles"), until_idle=kw.get("until_idle", False))


# ------------------------------------------------------------------------------------------- configuration
def test_the_standby_brain_is_small_separate_and_on_its_own(cfg):
    s = standby.standby_config(cfg)
    assert s.standby and not cfg.standby  # the real configuration is untouched
    assert s.paths.data_dir == cfg.storage.standby_dir and s.paths.db_path.is_relative_to(cfg.storage.standby_dir)
    assert s.body.disk_budget_gb == cfg.storage.standby_budget_gb == 3.0
    assert s.body.disk_min_free_gb == 2.0 and cfg.body.disk_min_free_gb == 10.0  # an SD card has less room
    assert not s.paths.require_separate_mount and not s.storage.move_home
    assert not s.storage.mount_root.exists()  # the drive pool sees no drives: nothing spills from the SD card
    assert s.paths.run_dir == cfg.paths.run_dir  # the same snapshot: the feed keeps working


def test_settings_and_their_limits(tmp_path):
    c = make_config(tmp_path)
    assert c.storage.standby and c.storage.standby_after_s == 300.0
    assert c.storage.standby_dir == Path("/var/lib/polymath/standby")
    base = (tmp_path / "polymath.toml").read_text()
    for extra, msg in (("standby = true\n", "unknown key"), ("[storage]\nstandby_after_s = 5\n", "at least 30"),
                       ("[storage]\nstandby_budget_gb = 500\n", "between 0.5 and 50")):  # fmt: skip
        f = tmp_path / "bad.toml"
        f.write_text(extra + base if extra.startswith("standby") else base + extra)
        with pytest.raises(ConfigError, match=msg):
            load_config(f, env={})


def test_no_dumps_on_the_standby_brain(cfg, tmp_path):
    db = open_database(tmp_path / "x.sqlite3")
    main = set(build_components(cfg, db, planners=False).registry.kinds())
    side = set(build_components(standby.standby_config(cfg), db, planners=False).registry.kinds())
    assert standby.FOLD_KIND in main and standby.FOLD_KIND not in side
    assert main >= standby.DROPPED_KINDS and not standby.DROPPED_KINDS & side
    assert {"feeds.poll", "crawl.step", "wikipedia.titles", "perception.read", "reason.infer"} <= side
    db.close()


def test_standby_planners_queue_no_dumps_or_backups(cfg, tmp_path):
    from polymath.body import maintenance
    from polymath.senses import sources, wikidata_props

    db = open_database(tmp_path / "x.sqlite3")
    s = standby.standby_config(cfg)
    agent = SimpleNamespace(config=s, db=db, scheduler=Scheduler(db), services={})
    sources.planner(agent)
    maintenance.planner(agent)
    wikidata_props.planner(agent)
    kinds = {r["kind"] for r in db.query("SELECT kind FROM jobs")}
    assert not kinds & {"sources.plan", "body.backup", "wikidata.propindex"}
    assert "body.housekeeping" in kinds  # the rest of its upkeep goes on
    db.close()


# ------------------------------------------------------------------------------------------- the marker
def test_begin_resume_and_discard(cfg):
    assert standby.read_marker(cfg) is None and standby.pending(cfg) is None
    first = standby.begin(cfg, reason="drive gone", now=100.0)
    assert standby.begin(cfg)["id"] == first["id"]  # a restart in standby resumes the same brain
    assert first["started"] == 100.0 and first["reason"] == "drive gone"
    assert standby.pending(cfg) is None  # nothing read yet: no database
    open_database(standby.standby_config(cfg).paths.db_path).close()
    assert standby.pending(cfg)["id"] == first["id"]
    standby.discard(cfg)
    assert cfg.storage.standby_dir.is_dir() and not any(cfg.storage.standby_dir.iterdir())  # systemd's dir stays


# ------------------------------------------------------------------------------------------- learning on standby
def test_run_standby_learns_on_the_sd_card_and_says_so(cfg):
    drive_gone(cfg)
    assert cli.run_standby(cfg, args(until_idle=True)) == 0
    mark = standby.read_marker(cfg)
    assert mark and "not plugged in" in mark["reason"]
    sdb = open_database(standby.standby_config(cfg).paths.db_path)
    note = sdb.scalar("SELECT text FROM events WHERE kind = 'issue'")
    assert "learning on the standby brain on the SD card" in note  # the caretaker's inbox knows
    sdb.close()


def test_run_standby_goes_back_as_soon_as_the_real_brain_answers(cfg):
    drive_gone(cfg)

    def plug_back_in() -> None:
        deadline = time.monotonic() + 10
        while standby.read_marker(cfg) is None and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.3)
        drive_back(cfg)

    threading.Thread(target=plug_back_in, daemon=True).start()
    t0 = time.monotonic()
    assert cli.run_standby(cfg, args(), check_every=0.1) == 75  # systemd starts it again, on the real brain
    assert time.monotonic() - t0 < 15


def test_run_standby_is_not_needed_when_the_brain_answers(cfg):
    assert cli.run_standby(cfg, args(until_idle=True)) == 75 and standby.read_marker(cfg) is None


def test_waiting_gives_up_after_the_standby_delay(cfg):
    drive_gone(cfg)
    t = [0.0]

    def tick(s: float) -> None:
        t[0] += s

    stop = threading.Event()
    assert not cli.wait_for_storage(cfg, notifier=SimpleNamespace(ready=lambda *a: None, watchdog=lambda **k: None),
                                    stop=stop, sleep=tick, clock=lambda: t[0], timeout=60.0)  # fmt: skip
    assert not stop.is_set() and 60.0 <= t[0] <= 65.0


# ------------------------------------------------------------------------------------------- folding back
def standby_brain_with(cfg: Any, docs: list[Document]) -> None:
    standby.begin(cfg, now=time.time() - 3600)
    sdb = open_database(standby.standby_config(cfg).paths.db_path)
    store = DocumentStore(sdb)
    with sdb.transaction():
        store.register_source("web", "varies", None, None, "the open web")
        for d in docs:
            store.add(d)
        sdb.execute("INSERT INTO wd_entities(qid, label, fetched) VALUES('Q42', 'Douglas Adams', 1.0)")
        sdb.execute("INSERT INTO frontier(url, host, state, added, fetched_at, http_status) "
                    "VALUES('https://example.org/1', 'example.org', 'done', 1.0, 2.0, 200)")  # fmt: skip
        sdb.execute("INSERT INTO events(at, kind, text, detail) VALUES(3.0, 'issue', 'feed X failed', '{}')")
    sdb.close()


def fold_ctx(c: Any, db: Any, job_id: int) -> Any:
    job = Scheduler(db).claim([standby.FOLD_KIND])
    assert job is not None and job.id == job_id
    return SimpleNamespace(config=c, db=db, job=job, scheduler=Scheduler(db), time_left=lambda: 15.0)


def test_folding_hands_everything_to_the_real_brain_once(cfg):
    standby_brain_with(cfg, [doc(1), doc(2), doc(3, "Already known text.")])
    main = open_database(cfg.paths.db_path)
    with main.transaction():
        DocumentStore(main).add(doc(3, "Already known text."))  # the real brain has this one
        main.execute("INSERT INTO frontier(url, host, state, added) VALUES('https://example.org/1', 'example.org', "
                     "'new', 1.0)")  # fmt: skip
    agent = SimpleNamespace(config=cfg, db=main, scheduler=Scheduler(main))
    standby.planner(agent)
    job_id = int(main.scalar("SELECT id FROM jobs WHERE kind = ?", (standby.FOLD_KIND,)))
    out = standby.fold_job(fold_ctx(cfg, main, job_id))
    assert out.done and out.value == 2.0
    titles = {r["title"]: r["state"] for r in main.query("SELECT title, state FROM documents")}
    assert titles == {"Page 1": "new", "Page 2": "new", "Page 3": "new"}  # new ones queued for reading again
    assert main.scalar("SELECT COUNT(*) FROM documents") == 3  # the known one is not doubled
    assert main.scalar("SELECT state FROM wd_entities WHERE qid = 'Q42'") == "new"
    assert main.scalar("SELECT state FROM frontier WHERE url = 'https://example.org/1'") == "done"  # not fetched again
    assert main.scalar("SELECT text FROM events WHERE kind = 'issue'") == "(on standby) feed X failed"
    line = main.one("SELECT text, detail FROM events WHERE kind = 'standby'")
    assert "2 documents it read there are now in the real brain" in line["text"]
    assert json.loads(line["detail"])["status"] == "folded"
    assert not any(cfg.storage.standby_dir.iterdir())  # the standby brain is gone
    standby.planner(agent)  # nothing left to fold
    assert main.scalar("SELECT COUNT(*) FROM jobs WHERE kind = ?", (standby.FOLD_KIND,)) == 1
    main.close()


def test_folding_resumes_where_it_stopped(cfg, monkeypatch):
    standby_brain_with(cfg, [doc(1), doc(2), doc(3)])
    main = open_database(cfg.paths.db_path)
    standby.planner(SimpleNamespace(config=cfg, db=main, scheduler=Scheduler(main)))
    monkeypatch.setattr(standby, "FOLD_BATCH", 1)
    t = [1000.0]

    def slow() -> float:  # each look at the clock costs 0.6 s: one batch per slice
        t[0] += 0.6
        return t[0]

    monkeypatch.setattr(standby.time, "monotonic", slow)
    job_id = int(main.scalar("SELECT id FROM jobs WHERE kind = ?", (standby.FOLD_KIND,)))
    ctx = fold_ctx(cfg, main, job_id)
    ctx.time_left = lambda: 0.0
    first = standby.fold_job(ctx)
    assert not first.done and first.checkpoint["added"] == 1
    assert standby.read_marker(cfg) is not None  # not removed before it is all folded
    ctx.job.checkpoint = first.checkpoint
    while True:
        out = standby.fold_job(ctx)
        if out.done:
            break
        ctx.job.checkpoint = out.checkpoint
    assert main.scalar("SELECT COUNT(*) FROM documents") == 3 and standby.read_marker(cfg) is None
    main.close()


def test_a_standby_brain_folded_already_is_only_cleared(cfg):
    standby_brain_with(cfg, [doc(1)])
    main = open_database(cfg.paths.db_path)
    main.kv_set(standby.FOLDED_KEY, [standby.read_marker(cfg)["id"]])
    standby.planner(SimpleNamespace(config=cfg, db=main, scheduler=Scheduler(main)))
    assert main.scalar("SELECT COUNT(*) FROM jobs") == 0 and not any(cfg.storage.standby_dir.iterdir())
    main.close()


def test_the_standby_brain_never_folds_into_itself(cfg):
    standby_brain_with(cfg, [doc(1)])
    s = standby.standby_config(cfg)
    sdb = open_database(s.paths.db_path)
    standby.planner(SimpleNamespace(config=s, db=sdb, scheduler=Scheduler(sdb)))
    assert sdb.scalar("SELECT COUNT(*) FROM jobs") == 0
    sdb.close()


def test_a_full_cycle_drive_gone_standby_drive_back_fold(cfg):
    """The whole story with real agents: the drive goes, standby reads, the drive comes back, all of it moves."""
    main = build_agent(cfg)  # the real brain exists
    main.db.close()
    drive_gone(cfg)
    with pytest.raises(BrainDriveMissing):
        check_storage(cfg)
    assert cli.run_standby(cfg, args(until_idle=True)) == 0
    sdb = open_database(standby.standby_config(cfg).paths.db_path)
    with sdb.transaction():
        DocumentStore(sdb).add(doc(7))  # what it read on standby
    sdb.close()
    drive_back(cfg)
    agent = build_agent(cfg)
    standby.planner(agent)
    assert agent.run(until_idle=True) >= 1
    assert agent.db.scalar("SELECT title FROM documents WHERE title = 'Page 7'") == "Page 7"
    assert standby.read_marker(cfg) is None
    agent.db.close()


# ------------------------------------------------------------------------------------------- what you see
def test_feed_face_and_doctor_show_the_standby_brain(cfg):
    drive_gone(cfg)
    s = standby.standby_config(cfg)
    standby.begin(cfg, now=time.time() - 120)
    agent = build_agent(s)
    agent.start()  # running: its heartbeat is fresh
    snapshot.planner(agent)
    st, _age = snapshot.read_status(snapshot.path_for(cfg))
    assert st["standby"]["since"] and st["standby"]["documents"] == 0
    head = ["".join(t for t, _ in line) for line in feed.status_lines(st)]
    assert "learning on standby" in head[0]
    assert "on the standby brain since" in "".join(t for t, _ in feed.now_line(st))
    f = face.Face()
    f.status = st
    assert f.mood() == "standby"
    checks = {c.name: c for c in doctor.run_checks(cfg, "http://x", health=lambda u: ("refused", 0.0, None))}
    assert checks["standby"].level == doctor.WARN and "standby brain on the SD card" in checks["standby"].detail
    line = "".join(t for t, _ in feed.render_event({"kind": "note", "at": time.time(), "what": "standby",
                                                    "status": "folded", "text": "back on its drive"}))  # fmt: skip
    assert "standby" in line and "back on its drive" in line
    agent.db.close()


def test_the_agent_unit_gives_the_standby_brain_its_directory():
    unit = (ROOT / "deploy/polymath.service").read_text()
    assert "StateDirectory=polymath/standby" in unit and "StateDirectoryMode=0750" in unit


# ------------------------------------------------------------------------------------------- found on the way
def test_loading_the_configuration_never_reads_a_frozen_drive(tmp_path, monkeypatch):
    c = make_config(tmp_path)
    c.paths.data_dir.mkdir(parents=True, exist_ok=True)
    os.mkfifo(c.paths.data_dir / HOME_FILE)  # a read would block forever, like a frozen drive
    monkeypatch.setattr(fsprobe, "probe", lambda p, **k: fsprobe.HANG)
    before = c.body.disk_budget_gb
    apply_home(c)  # returns at once: it asked first, and the drive did not answer
    assert c.body.disk_budget_gb == before


def test_polymath_run_resumes_the_standby_brain_after_a_restart(tmp_path, monkeypatch, capsys):
    """`polymath run` with the drive still gone and a standby brain already started goes straight back to it."""
    c = make_config(tmp_path)
    with (tmp_path / "polymath.toml").open("a") as f:
        f.write(f'\n[storage]\nstandby_dir = "{tmp_path / "standby"}"\n')
    c.storage.standby_dir = tmp_path / "standby"
    drive_gone(c)
    standby.begin(c)
    monkeypatch.setattr(cli, "WAIT_CHECK_S", 0.05)
    t0 = time.monotonic()
    assert cli.main(["--config", str(tmp_path / "polymath.toml"), "run", "--until-idle"]) == 0
    assert time.monotonic() - t0 < 30
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["standby"] is True and out["drive_back"] is False
    assert standby.standby_config(c).paths.db_path.exists()
