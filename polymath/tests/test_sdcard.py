"""v0.7.0: SD-card mode. The whole brain runs on the SD card while the SSD cannot be trusted, no drive is touched,
and what it learned there moves onto the SSD by itself once the mode is off."""

from __future__ import annotations

import argparse
import configparser
import json
import shutil
import subprocess
import time
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from polymath.agents import faults
from polymath.core import fsprobe, issues, sdcard, standby
from polymath.core.app import build_agent, build_components
from polymath.core.config import HOME_FILE, ConfigError, load_config
from polymath.core.db import open_database
from polymath.core.scheduler import Scheduler
from polymath.interface import cli, doctor, feed, snapshot
from polymath.memory.documents import Document, DocumentStore
from tests.conftest import make_config

ROOT = Path(__file__).resolve().parent.parent
Usage = namedtuple("Usage", "total used free")


def sd_config(tmp_path: Path, extra: str = "") -> Any:
    """The test configuration with SD-card mode switched on the way the script does it: a drop-in."""
    d = tmp_path / "polymath.d"
    (d / sdcard.DROPIN).unlink(missing_ok=True)  # make_config loads without it
    make_config(tmp_path)
    d.mkdir(exist_ok=True)
    (d / "10-dirs.toml").write_text(
        f'[storage]\nsd_dir = "{tmp_path / "brain"}"\nstandby_dir = "{tmp_path / "standby"}"\n'
    )
    (d / sdcard.DROPIN).write_text(f"[storage]\nsd_only = true\n{extra}")  # what scripts/sd-card-mode.sh writes
    c = load_config(tmp_path / "polymath.toml", env={})
    c.paths.run_dir = tmp_path / "run"
    c.paths.run_dir.mkdir(exist_ok=True)
    return c


def doc(n: int) -> Document:
    return Document(source="web", external_id=f"https://example.org/{n}", title=f"Page {n}",
                    text=f"Page {n} says something new about topic {n}.", license="CC BY-SA 4.0",
                    url=f"https://example.org/{n}")  # fmt: skip


def args(**kw: Any) -> argparse.Namespace:
    return argparse.Namespace(max_cycles=kw.get("max_cycles"), until_idle=kw.get("until_idle", False))


# ------------------------------------------------------------------------------------------- configuration
def test_dropins_are_merged_in_name_order_and_polymath_toml_is_never_edited(tmp_path):
    make_config(tmp_path)
    main = (tmp_path / "polymath.toml").read_text()
    d = tmp_path / "polymath.d"
    d.mkdir()
    (d / "10-a.toml").write_text("[storage]\nspill_at = 0.5\n")
    (d / "20-b.toml").write_text("[storage]\nspill_at = 0.6\n[loop]\nidle_sleep = 0.25\n")
    (d / "sd-card.toml.off").write_text("[storage]\nsd_only = true\n")  # switched off: not a drop-in
    c = load_config(tmp_path / "polymath.toml", env={})
    assert c.storage.spill_at == 0.6 and c.loop.idle_sleep == 0.25  # the later file wins, key by key
    assert c.paths.data_dir == tmp_path / "data" and not c.storage.sd_only  # the rest from polymath.toml
    assert (tmp_path / "polymath.toml").read_text() == main
    (d / "30-bad.toml").write_text("[storage\n")
    with pytest.raises(ConfigError, match=r"30-bad\.toml"):
        load_config(tmp_path / "polymath.toml", env={})
    (d / "30-bad.toml").write_text("[storage]\nno_such_key = 1\n")
    with pytest.raises(ConfigError, match="unknown key"):  # a typo in a drop-in is caught like one in the file
        load_config(tmp_path / "polymath.toml", env={})


def test_sd_card_mode_puts_the_whole_brain_on_the_sd_card(tmp_path, monkeypatch):
    brain = tmp_path / "brain"
    (brain / "db").mkdir(parents=True)
    (brain / "db" / "polymath.sqlite3").write_bytes(b"\0" * 1_000_000)  # what the brain uses already
    (brain / HOME_FILE).write_text(json.dumps({"budget_gb": 900.0}))  # never read in SD-card mode
    monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage(32e9, 19e9, 13e9))
    c = sd_config(tmp_path)
    assert c.paths.data_dir == brain and c.paths.db_path == brain / "db" / "polymath.sqlite3"
    assert not c.paths.require_separate_mount  # on the SD card on purpose
    st = c.storage
    assert not (st.adopt or st.move_home or st.heal or st.heal_power_cycle or st.standby)  # no drive is touched
    assert not (st.format_blank_disks or st.format_fresh_drives)
    assert st.mount_root == brain / "no-drives" and not st.mount_root.exists()  # the pool sees no drive
    assert c.body.disk_min_free_gb == 2.0 and c.body.backup_keep == 2
    assert c.body.disk_budget_gb == 11.0  # 1 MB used + 13 GB free − 2 GB kept for the system
    assert c.senses.openalex_files == 2  # never big-brain mode on an SD card
    fixed = sd_config(tmp_path, "sd_budget_gb = 5.0\nsd_keep_free_gb = 3.0\n")
    assert fixed.body.disk_budget_gb == 5.0 and fixed.body.disk_min_free_gb == 3.0
    env = load_config(tmp_path / "polymath.toml", env={"POLYMATH_DATA_DIR": str(tmp_path / "elsewhere")})
    assert env.paths.data_dir == tmp_path / "elsewhere"  # the environment still wins (tests, development)
    monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage(8e9, 7.5e9, 0.4e9))
    assert sd_config(tmp_path).body.disk_budget_gb == 1.0  # a full card: at least 1 GB, the guard frees room


def test_sd_card_settings_and_their_limits(tmp_path):
    c = make_config(tmp_path)
    assert not c.storage.sd_only and c.storage.sd_dir == Path("/var/lib/polymath/brain")
    assert c.storage.sd_keep_free_gb == 2.0 and c.storage.sd_budget_gb == 0.0
    for extra, msg in (("sd_keep_free_gb = 0.1\n", "sd_keep_free_gb"), ("sd_budget_gb = -1.0\n", "sd_budget_gb")):
        with pytest.raises(ConfigError, match=msg):
            sd_config(tmp_path, extra)
    shipped = (ROOT / "config" / "polymath.toml").read_text()
    assert "sd_only = false" in shipped and 'sd_dir = "/var/lib/polymath/brain"' in shipped


# ------------------------------------------------------------------------------------------- it runs everything
def test_everything_runs_on_the_sd_card_dumps_included(tmp_path):
    c = sd_config(tmp_path)
    db = open_database(tmp_path / "x.sqlite3")
    kinds = set(build_components(c, db, planners=False).registry.kinds())
    assert standby.DROPPED_KINDS - {"body.spill", "body.recall"} <= kinds  # unlike the standby brain
    assert standby.FOLD_KIND in kinds  # it can still take in a standby brain
    db.close()


def test_no_drive_is_touched_heal_and_attach_do_nothing(tmp_path, monkeypatch):
    from polymath.body.heal import Healer
    from polymath.body.volumes import Helper

    c = sd_config(tmp_path)

    def never(*a: Any, **k: Any) -> Any:
        raise AssertionError("a drive was looked at in SD-card mode")

    monkeypatch.setattr(fsprobe, "probe", never)
    healer = Healer(c, never, services=("polymath.service",), mounts_at=never, state_path=tmp_path / "heal.json")
    assert healer.heal() == {"brain": "sd-card", "action": "none"}
    helper = Helper(c, never, lock=tmp_path / "lock")
    out = helper.attach("/dev/sda2")
    assert out.action == "skipped" and "adopt is off" in out.detail


def test_the_agent_marks_its_sd_card_brain_and_tells_the_caretaker(tmp_path):
    c = sd_config(tmp_path)
    assert cli.cmd_run(c, args(until_idle=True)) == 0
    mark = sdcard.read_marker(c)
    assert mark is not None and mark["fold_after"] == 0 and mark["seeded_from"] == ""
    db = open_database(c.paths.db_path)
    assert db.kv_get("sd_card")["since"] == mark["started"]
    found = [i for i in issues.recent(db, 0) if i["code"] == "sd_card"]
    assert found and found[0]["known"] == ["sd-card-mode"]  # the caretaker knows what it is
    assert "sudo ./scripts/sd-card-mode.sh off" in found[0]["text"]
    db.close()
    assert cli.cmd_run(c, args(until_idle=True)) == 0
    again = sdcard.read_marker(c)
    assert again is not None and again["id"] == mark["id"]  # a restart keeps the same brain


def test_a_brain_started_from_the_sd_cards_copy_folds_back_only_what_is_new(tmp_path):
    c = sd_config(tmp_path)
    db = open_database(c.paths.db_path)  # the SD card's old copy of the brain, moved into place by the script
    with db.transaction():
        DocumentStore(db).register_source("web", "varies", None, None, "the open web")
        for n in (1, 2, 3):
            DocumentStore(db).add(doc(n))
    db.close()
    (c.paths.data_dir / sdcard.SEED).write_text(json.dumps({"from": "/srv/polymath-sd-copy", "at": 1}))
    db = open_database(c.paths.db_path)
    mark = sdcard.begin(c, db, now=1000.0)
    assert mark["fold_after"] == 3 and mark["seeded_from"] == "/srv/polymath-sd-copy"
    db.close()


# ------------------------------------------------------------------------------------------- back to the SSD
def sd_brain(c: Any, started: float) -> None:
    """An SD-card brain in ``c.storage.sd_dir``: seeded with pages 1-2, then pages 3-4 learned in SD-card mode."""
    root = Path(c.storage.sd_dir)
    sdb = open_database(root / "db" / "polymath.sqlite3")
    store = DocumentStore(sdb)
    with sdb.transaction():
        store.register_source("web", "varies", None, None, "the open web")
        for n in (1, 2):
            store.add(doc(n))
        sdb.execute("INSERT INTO wd_entities(qid, label, fetched) VALUES('Q1', 'old', ?)", (started - 100,))
        sdb.execute("INSERT INTO events(at, kind, text, detail) VALUES(?, 'issue', 'before', '{}')", (started - 100,))
    (root / sdcard.MARKER).write_text(json.dumps({"id": "sd1", "started": started, "fold_after": 2}))
    with sdb.transaction():
        for n in (3, 4):
            store.add(doc(n))
        sdb.execute("INSERT INTO wd_entities(qid, label, fetched) VALUES('Q2', 'new', ?)", (started + 10,))
        sdb.execute("INSERT INTO frontier(url, host, state, added, fetched_at, http_status) "
                    "VALUES('https://example.org/x', 'example.org', 'done', 1.0, ?, 200)", (started + 20,))  # fmt: skip
        sdb.execute("INSERT INTO events(at, kind, text, detail) VALUES(?, 'issue', 'during', '{}')", (started + 30,))
    sdb.close()


def ssd_config(tmp_path: Path) -> Any:
    c = make_config(tmp_path)  # SD-card mode off: the brain is on the SSD again (data_dir)
    c.storage.sd_dir = tmp_path / "brain"
    c.storage.standby_dir = tmp_path / "standby"
    c.storage.standby_dir.mkdir(exist_ok=True)
    return c


def run_fold(c: Any, main: Any) -> Any:
    job = Scheduler(main).claim([standby.FOLD_KIND])
    assert job is not None
    ctx = SimpleNamespace(config=c, db=main, job=job, scheduler=Scheduler(main), time_left=lambda: 15.0)
    return standby.fold_job(ctx)


def test_back_on_the_ssd_what_was_learned_on_the_sd_card_moves_onto_it(tmp_path):
    started = time.time() - 7200
    c = ssd_config(tmp_path)
    sd_brain(c, started)
    main = open_database(c.paths.db_path)
    with main.transaction():
        DocumentStore(main).register_source("web", "varies", None, None, "the open web")
        for n in (1, 2):
            DocumentStore(main).add(doc(n))  # what the SD card's copy was started with: on the SSD already
        main.execute("INSERT INTO frontier(url, host, state, added) VALUES('https://example.org/x', 'example.org', "
                     "'new', 1.0)")  # fmt: skip
    standby.planner(SimpleNamespace(config=c, db=main, scheduler=Scheduler(main)))
    job = main.one("SELECT payload FROM jobs WHERE kind = ?", (standby.FOLD_KIND,))
    assert job is not None
    assert json.loads(job["payload"]) == {"id": "sd1", "kind": "sd-card"}
    out = run_fold(c, main)
    assert out.done and out.value == 2.0
    assert {r["title"] for r in main.query("SELECT title FROM documents")} == {"Page 1", "Page 2", "Page 3", "Page 4"}
    assert [r["qid"] for r in main.query("SELECT qid FROM wd_entities")] == ["Q2"]  # only what came in SD-card mode
    assert main.scalar("SELECT state FROM frontier WHERE url = 'https://example.org/x'") == "done"
    noted = [r["text"] for r in main.query("SELECT text FROM events WHERE kind = 'issue'")]
    assert noted == ["(on the SD card) during"]  # only what it noted in SD-card mode
    line = main.one("SELECT text, detail FROM events WHERE kind = 'standby'")
    assert line is not None
    assert "on the SD card: 2 documents it read there are now in the real brain" in line["text"]
    assert json.loads(line["detail"])["from"] == "sd-card"
    assert not any(c.storage.sd_dir.iterdir())  # the SD-card brain is gone, its directory stays (systemd's)
    standby.planner(SimpleNamespace(config=c, db=main, scheduler=Scheduler(main)))
    assert main.scalar("SELECT COUNT(*) FROM jobs WHERE kind = ?", (standby.FOLD_KIND,)) == 1  # nothing left
    main.close()


def test_the_sd_card_brain_never_folds_into_itself_but_takes_in_a_standby_brain(tmp_path):
    c = sd_config(tmp_path)
    sd_brain(c, time.time() - 60)  # this *is* the brain now
    standby.begin(c)
    sdb = open_database(standby.standby_config(c).paths.db_path)
    sdb.close()
    db = open_database(c.paths.db_path)
    standby.planner(SimpleNamespace(config=c, db=db, scheduler=Scheduler(db)))
    payloads = [json.loads(r["payload"]) for r in db.query("SELECT payload FROM jobs")]
    assert [p["kind"] for p in payloads] == ["standby"]
    job = Scheduler(db).claim([standby.FOLD_KIND])
    assert job is not None
    job.payload = {"id": "sd1", "kind": "sd-card"}  # an old job from before SD-card mode was switched on again
    ctx = SimpleNamespace(config=c, db=db, job=job, scheduler=Scheduler(db), time_left=lambda: 15.0)
    assert standby.fold_job(ctx).done and sdcard.read_marker(c) is not None  # dropped: nothing removed
    db.close()


def test_a_fold_already_done_only_clears_the_sd_card_brain(tmp_path):
    c = ssd_config(tmp_path)
    sd_brain(c, time.time() - 60)
    main = open_database(c.paths.db_path)
    main.kv_set(standby.FOLDED_KEY, ["sd1"])
    standby.planner(SimpleNamespace(config=c, db=main, scheduler=Scheduler(main)))
    assert main.scalar("SELECT COUNT(*) FROM jobs") == 0 and not any(c.storage.sd_dir.iterdir())
    main.close()


def test_a_full_cycle_ssd_then_sd_card_then_back_to_the_ssd(tmp_path):
    """The whole story with real agents: the SSD brain exists; SD-card mode on, it learns there; mode off, the SSD
    brain takes in what it learned, and the SD-card brain is gone."""
    ssd = ssd_config(tmp_path)
    agent = build_agent(ssd)  # the brain on the SSD
    with agent.db.transaction():
        DocumentStore(agent.db).register_source("web", "varies", None, None, "the open web")
        DocumentStore(agent.db).add(doc(1))
    agent.db.close()
    on = sd_config(tmp_path)  # sd-card-mode.sh on: the drop-in
    assert cli.cmd_run(on, args(until_idle=True)) == 0
    sdb = open_database(on.paths.db_path)
    with sdb.transaction():
        DocumentStore(sdb).register_source("web", "varies", None, None, "the open web")
        DocumentStore(sdb).add(doc(9))  # what it read on the SD card
    sdb.close()
    (tmp_path / "polymath.d" / sdcard.DROPIN).rename(tmp_path / "polymath.d" / "sd-card.toml.off")  # … off
    off = load_config(tmp_path / "polymath.toml", env={})
    assert not off.storage.sd_only and off.paths.data_dir == ssd.paths.data_dir
    agent = build_agent(off)
    standby.planner(agent)
    assert agent.run(until_idle=True) >= 1
    titles = {r["title"] for r in agent.db.query("SELECT title FROM documents")}
    assert titles == {"Page 1", "Page 9"}
    assert not any(Path(off.storage.sd_dir).iterdir())  # the SD-card brain is gone, everything is on the SSD
    agent.db.close()


# ------------------------------------------------------------------------------------------- what you see
def test_feed_dashboard_and_doctor_show_sd_card_mode(tmp_path):
    c = sd_config(tmp_path)
    agent = build_agent(c)
    sdcard.start(c, agent.db)
    agent.start()
    snapshot.planner(agent)
    st, _age = snapshot.read_status(snapshot.path_for(c))
    assert st is not None
    sd = st["sd_card"]
    assert sd["dir"] == str(c.paths.data_dir) and sd["budget_gb"] == round(c.body.disk_budget_gb, 1)
    assert sd["documents_new"] == 0 and sd["free_gb"] > 0
    head = "".join(t for t, _ in feed.status_lines(st)[0])
    assert "learning on the SD card" in head
    assert "SD-card mode since" in "".join(t for t, _ in feed.now_line(st))
    assert feed.Feed(agent.db).status()["sd_card"]["budget_gb"] == sd["budget_gb"]  # what the dashboard serves
    checks = {ch.name: ch for ch in doctor.run_checks(c, "http://x", health=lambda u: ("refused", 0.0, None))}
    assert checks["sd card"].level == doctor.WARN and "SD-card mode since" in checks["sd card"].detail
    assert "sd-card-mode.sh off" in checks["sd card"].fix
    assert checks["drives"].level == doctor.OK and "SD-card mode" in checks["drives"].detail
    agent.db.close()


def test_the_caretaker_knows_sd_card_mode():
    k = faults.knowledge()
    f = k.by_id["sd-card-mode"]
    assert f.matches_code("storage/sd_card") and "sd-card-mode" in k.situation["recommended"]
    off = [s for s in f.steps if "sd-card-mode.sh off" in s.command]
    assert off and off[0].needs == "approval"
    assert any("sd-card-mode.sh on" in s.command for s in k.by_id["brain-mount-frozen"].steps)
    assert any("sd-card-mode.sh on" in w for w in k.situation["waiting_on"])
    assert any(h.fault == "sd-card-mode" and h.open for h in k.history)


# ------------------------------------------------------------------------------------------- deployment
def unit(name: str) -> configparser.ConfigParser:
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str  # type: ignore[assignment,method-assign]
    cp.read(ROOT / "deploy" / name)
    return cp


def test_the_units_give_the_sd_card_brain_its_directory():
    agent = unit("polymath.service")["Service"]
    assert agent["StateDirectory"].split() == ["polymath/standby", "polymath/brain"]
    dash = unit("polymath-dashboard.service")["Service"]
    assert "-/var/lib/polymath/brain/db" in dash["ReadWritePaths"].split()


def test_the_installer_in_sd_card_mode_leaves_the_ssd_alone(tmp_path):
    stage = tmp_path / "stage"
    (stage / "etc" / "polymath" / "polymath.d").mkdir(parents=True)
    (stage / "etc" / "polymath" / "polymath.d" / sdcard.DROPIN).write_text("[storage]\nsd_only = true\n")
    out = subprocess.run([str(ROOT / "install.sh"), "--root", str(stage), "--user", "tester", "--skip-venv",
                          "--allow-sd-card"], capture_output=True, text=True, timeout=120, check=True)  # fmt: skip
    text = out.stdout + out.stderr
    assert "SD-card mode: its settings come from" in text
    assert (stage / "var" / "lib" / "polymath" / "brain" / "db").is_dir()
    assert not (stage / "srv" / "polymath").exists()  # the SSD's mount point is not touched
    conf = (stage / "etc" / "polymath" / "polymath.toml").read_text()
    assert "require_separate_mount = true" in conf  # polymath.toml is not edited: the drop-in does it all
