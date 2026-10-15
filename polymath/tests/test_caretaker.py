"""v0.8.0: Orin the Caretaker (docs/CARETAKER.md C.10): notice, find the cause, fix what it may, verify, tell."""

from __future__ import annotations

import ast
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from polymath.agents import caretaker, faults
from polymath.agents.caretaker import MODELS, Check, Env, Orin
from polymath.core import issues
from polymath.core.app import build_agent, build_components
from polymath.core.db import open_database
from polymath.core.scheduler import Scheduler
from polymath.interface import doctor, face, feed, snapshot
from tests.conftest import make_config

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "polymath" / "agents" / "caretaker.py"


class World:
    """A brain, Orin's senses pointed at fixtures, and a clock that only moves when the test moves it."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.config = make_config(tmp)
        self.config.paths.run_dir = tmp / "run"
        self.config.paths.run_dir.mkdir()
        self.db = open_database(self.config.paths.db_path)
        self.t = 1_800_000_000.0
        (tmp / "status").write_text("Name:\tpython\nVmRSS:\t  512000 kB\n")
        (tmp / "sys_block").mkdir()
        self.env = Env(throttled=tmp / "get_throttled", hwmon=tmp / "hwmon", proc_status=tmp / "status",
                       sys_block=tmp / "sys_block", config_file=tmp / "polymath.toml")  # fmt: skip

    def orin(self) -> Orin:
        return Orin(self.config, self.db, env=self.env, clock=lambda: self.t)

    def turn(self) -> dict[str, Any]:
        with self.db.transaction():
            return self.orin().turn()

    def vitals(self, temp: float = 50.0, *, load: float = 1.0, free: float = 100.0, data: float = 5.0,
               mode: str = "normal") -> None:  # fmt: skip
        self.db.execute("INSERT OR REPLACE INTO vitals(at, temp_c, load1, mem_mb, disk_free_gb, data_gb, mode) "
                        "VALUES(?,?,?,?,?,?,?)", (self.t, temp, load, 2000.0, free, data, mode))  # fmt: skip

    def later(self, seconds: float) -> None:
        self.t += seconds

    def incidents(self, *states: str) -> list[Any]:
        rows = self.db.query("SELECT * FROM incidents ORDER BY id")
        return [r for r in rows if not states or r["state"] in states]

    def said(self) -> list[str]:
        return [str(r["text"]) for r in self.db.query("SELECT text FROM events WHERE kind = 'caretaker' ORDER BY id")]


@pytest.fixture()
def w(tmp_path: Path) -> Any:
    world = World(tmp_path)
    yield world
    world.db.close()


def learn_band(w: World, signal: str, values: list[float]) -> None:
    """A week of samples, so the signal has a trusted band."""
    with w.db.transaction():
        for i, v in enumerate(values):
            w.db.execute(
                "INSERT INTO signal_samples(signal, at, value) VALUES(?,?,?)", (signal, w.t - 600 * (i + 1), v)
            )
        med, mad = caretaker.bands(values)
        w.db.execute("INSERT OR REPLACE INTO signal_bands(signal, median, mad, n, updated) VALUES(?,?,?,?,?)",
                     (signal, med, mad, len(values), w.t))  # fmt: skip


# ------------------------------------------------------------------------------------------- 1. detection
def test_a_blip_is_not_an_incident_but_a_lasting_symptom_is_one(w):
    learn_band(w, "temp_c", [50.0 + (i % 3) for i in range(60)])
    w.vitals(64.0)  # far out of its band, but below the hard limit (73 °C)
    w.turn()
    assert w.incidents() == []  # one reading out of band: a blip
    w.later(120)
    w.vitals(51.0)
    w.turn()
    w.later(120)
    w.vitals(64.0)
    w.turn()
    assert w.incidents() == []  # the streak was broken
    w.later(120)
    w.vitals(65.0)
    w.turn()
    rows = w.incidents()
    assert len(rows) == 1 and rows[0]["kind"] == "heat" and rows[0]["fault"] == "body-thermal"
    w.later(120)
    w.vitals(66.0)
    w.turn()
    assert len(w.incidents()) == 1  # the same root symptom: one incident, more evidence
    syms = json.loads(w.incidents()[0]["symptoms"])
    assert [s["value"] for s in syms] == [65.0, 66.0] and syms[0]["text"] == "CPU temperature 65.0 °C (usually 51.0)"


def test_a_hard_limit_opens_an_incident_at_once(w):
    w.vitals(free=8.0)  # under 1.5 × disk_min_free_gb (15 GB): no band needed
    w.turn()
    rows = w.incidents()
    assert [r["kind"] for r in rows] == ["disk_low"]
    assert json.loads(rows[0]["symptoms"])[0]["text"] == "free space 8.0 GB"


def test_learned_bands_follow_a_slowly_changing_pi(w):
    """A brain that slowly gets busier (warmer) raises no alarm: its band moves with it."""
    for day in range(7):
        for _ in range(12):
            w.later(caretaker.SAMPLE_EVERY)
            w.vitals(50.0 + day * 1.5 + (w.t % 3))
            w.turn()
    assert w.incidents() == []
    band = w.db.one("SELECT median, n FROM signal_bands WHERE signal = 'temp_c'")
    assert band["n"] == 84 and 53.0 < band["median"] < 58.0


# ------------------------------------------------------------------------------------------- 2-3. thinking
def test_bayes_ranks_causes_like_by_hand():
    checks = {"load_high": Check(1.0, {"load": 0.85, "cooling": 0.35}, "")}
    post = caretaker.posterior({"load": 0.45, "cooling": 0.55}, {"load_high": True}, checks)
    by_hand = 0.45 * 0.85 / (0.45 * 0.85 + 0.55 * 0.35)
    assert post["load"] == pytest.approx(by_hand) and post["cooling"] == pytest.approx(1 - by_hand)
    post = caretaker.posterior({"load": 0.45, "cooling": 0.55}, {"load_high": False}, checks)
    assert post["load"] == pytest.approx(0.45 * 0.15 / (0.45 * 0.15 + 0.55 * 0.65))


def test_value_of_information_picks_the_check_that_tells_causes_apart():
    m = MODELS["ingest_stall"]
    prior = {c: 1 / len(m.causes) for c in m.causes}
    useless = {**m.checks, "coin": Check(0.1, {c: 0.5 for c in m.causes}, "a coin toss")}
    name, _score = caretaker.best_check(prior, useless, set()) or ("", 0)
    assert name in m.checks and name != "coin"
    post = caretaker.posterior(prior, {"offline_now": True}, m.checks)
    assert max(post, key=lambda c: post[c]) == "offline"
    assert caretaker.best_check({"a": 1.0}, {"x": Check(1.0, {"a": 0.9}, "")}, set()) is None  # certain: no check


def test_every_model_is_complete_and_known_to_the_caretaker_knowledge():
    k = faults.knowledge()
    for key, m in MODELS.items():
        assert m.fault in k.by_id, f"{key}: fault {m.fault} is not in faults.toml"
        assert abs(sum(m.causes.values()) - 1.0) < 1e-9, key
        assert set(m.remedies) == set(m.causes), key
        for remedies in m.remedies.values():
            assert all(r in caretaker.REMEDY_COST for r in remedies), key
        for name, chk in m.checks.items():
            assert set(chk.likes) == set(m.causes), f"{key}.{name}"
        assert set(m.cause_words) == set(m.causes), key
    assert {s.model for s in caretaker.SIGNALS} <= set(MODELS)


# ------------------------------------------------------------------------------------------- 4. acting, verifying
def test_heat_is_checked_slowed_down_verified_and_learned(w):
    w.vitals(74.0, load=5.0)  # past the hard limit (throttle 75 − 2), and busy
    w.turn()
    inc = w.incidents()[0]
    checks = json.loads(inc["checks"])
    assert checks == [{"name": "load_high", "seen": True, "at": w.t}]  # it checked before acting
    actions = json.loads(inc["actions"])
    assert [a["remedy"] for a in actions] == ["slowdown"] and inc["state"] == "verifying"
    assert w.db.kv_get("caretaker_slowdown")["factor"] == caretaker.SLOWDOWN
    assert caretaker.slowdown(w.db, w.t)[0] == caretaker.SLOWDOWN
    change = w.db.one("SELECT area, subject, summary FROM self_changes ORDER BY id DESC LIMIT 1")
    assert change["area"] == "caretaker" and change["subject"] == "slowdown" and "Orin" in change["summary"]
    w.later(MODELS["heat"].window_s + 1)
    w.vitals(60.0, load=1.0)
    w.turn()
    inc = w.incidents()[0]
    assert inc["state"] == "resolved" and inc["outcome"] == "fixed"
    assert w.db.one("SELECT a, b FROM fault_stats WHERE cause = 'heat:load' AND remedy = 'slowdown'")["a"] == 2.0
    assert w.db.kv_get("caretaker_slowdown") is None  # back to full speed
    assert any("back to full speed" in s for s in w.said())
    assert w.db.kv_get("caretaker")["mood"] == "proud"


def test_a_remedy_that_does_not_work_is_followed_by_telling_christian(w):
    w.vitals(74.0, load=0.5)  # hot while idle: poor cooling
    w.turn()
    w.later(MODELS["heat"].window_s + 1)
    w.vitals(74.5, load=0.5)
    w.turn()  # verify: still hot → failure, re-plan
    w.later(120)
    w.vitals(74.5, load=0.5)
    w.turn()
    inc = w.incidents()[0]
    assert inc["state"] == "escalated"
    assert w.db.one("SELECT b FROM fault_stats WHERE cause = 'heat:cooling' AND remedy = 'slowdown'")["b"] == 2.0
    advice = inc["advice"]
    for part in ("Orin: the Pi runs hot.", "Evidence: CPU temperature 74.0 °C; CPU temperature 74.5 °C.", "Most likely: poor cooling or a warm room (p",
                 "Do: add a heatsink or fan, or open the case  [needs your approval]"):  # fmt: skip
        assert part in advice, advice


def test_an_escalation_is_repeated_at_most_once_a_day(w):
    (w.tmp / "get_throttled").write_text("0x50005\n")
    w.turn()
    w.later(600)
    w.turn()
    tells = [s for s in w.said() if "under-voltage" in s and s.startswith("Orin:")]
    assert len(tells) == 1 and "Most likely: a weak power supply or cable (p 1.00)" in tells[0]
    assert "official 5.1 V / 3 A supply" in tells[0]
    w.later(caretaker.ESCALATE_AGAIN_S + 1)
    w.turn()
    assert len([s for s in w.said() if "under-voltage" in s and s.startswith("Orin:")]) == 2
    (w.tmp / "get_throttled").write_text("0x0\n")
    w.later(120)
    w.turn()
    assert w.incidents()[0]["state"] == "resolved"


def test_a_symptom_that_goes_away_by_itself_makes_orin_a_little_less_jumpy(w):
    w.vitals(free=8.0)
    w.turn()
    w.db.execute("UPDATE incidents SET actions = '[]', state = 'open'")  # as if nothing was done yet
    w.later(1801)
    w.vitals(free=80.0)
    w.turn()
    assert w.incidents()[0]["outcome"] == "fixed itself"
    assert w.db.kv_get("caretaker_k") == caretaker.K_DEFAULT + 0.25


def test_jobs_that_died_of_a_passing_problem_are_requeued_once(w):
    s = Scheduler(w.db, clock=lambda: w.t)
    with w.db.transaction():
        jid, _ = s.enqueue("feeds.poll", {"x": 1})
        bug, _ = s.enqueue("perception.read", {"x": 2})
        w.db.execute("UPDATE jobs SET state = 'dead', updated = ?, last_error = 'URLError: <urlopen error timed out>' "
                     "WHERE id = ?", (w.t, jid))  # fmt: skip
    w.turn()
    inc = w.incidents()[0]
    assert inc["kind"] == "dead_jobs" and json.loads(inc["actions"])[0]["remedy"] == "requeue"
    assert w.db.one("SELECT state, attempts, last_error FROM jobs WHERE id = ?", (jid,))["state"] == "queued"
    assert "requeued by Orin" in w.db.scalar("SELECT last_error FROM jobs WHERE id = ?", (jid,))
    assert w.db.kv_get("caretaker_requeued") == [jid]
    # the same job dying again later is not requeued twice
    with w.db.transaction():
        w.db.execute("UPDATE jobs SET state = 'dead', updated = ? WHERE id = ?", (w.t, jid))
        w.db.execute("UPDATE jobs SET state = 'dead', updated = ?, last_error = 'KeyError: title' WHERE id = ?",
                     (w.t, bug))  # fmt: skip
    w.later(MODELS["dead_jobs"].window_s + 1)
    w.turn()
    assert w.db.scalar("SELECT state FROM jobs WHERE id = ?", (jid,)) == "dead"


def test_a_job_that_died_of_a_bug_is_told_not_retried(w):
    s = Scheduler(w.db, clock=lambda: w.t)
    with w.db.transaction():
        jid, _ = s.enqueue("perception.read", {"x": 2})
        w.db.execute("UPDATE jobs SET state = 'dead', updated = ?, last_error = 'KeyError: title' WHERE id = ?",
                     (w.t, jid))  # fmt: skip
    for _ in range(3):
        w.turn()
        w.later(120)
    inc = w.incidents()[0]
    assert inc["state"] == "escalated" and "a fault in the job" in inc["advice"]
    assert "journalctl -u polymath -n 120 --no-pager" in inc["advice"]  # the job-dead fault's step, quoted
    assert w.db.scalar("SELECT state FROM jobs WHERE id = ?", (jid,)) == "dead"


# ------------------------------------------------------------------------------------------- 8. bounds
def crash(w: World, kind: str, n: int = 3) -> None:
    with w.db.transaction():
        jid, _ = Scheduler(w.db, clock=lambda: w.t).enqueue(kind, {"k": kind})
        w.db.execute("UPDATE jobs SET last_error = 'ValueError: same thing' WHERE id = ?", (jid,))
        for i in range(n):
            w.db.execute("INSERT INTO cycles(started, ended, action, job_id, status) VALUES(?,?,?,?, 'failed')",
                         (w.t - 60 * i, w.t - 60 * i + 1, kind, jid))  # fmt: skip


def test_a_kind_that_keeps_failing_is_quarantined_and_the_loop_holds_it_back(w):
    crash(w, "perception.phrasing")
    w.turn()
    q = w.db.kv_get("caretaker_quarantine")
    assert set(q) == {"perception.phrasing"} and q["perception.phrasing"] == w.t + caretaker.QUARANTINE_S
    assert caretaker.quarantined(w.db, w.t) == {"perception.phrasing"}
    assert caretaker.quarantined(w.db, w.t + caretaker.QUARANTINE_S + 1) == set()  # it ends by itself


def test_core_kinds_are_never_quarantined_and_never_more_than_three(w):
    for kind in ("body.backup", "net.probe", "eval.holdout", "caretaker.step", "body.evict", "a.one", "b.two",
                 "c.three", "d.four", "e.five"):  # fmt: skip
        crash(w, kind, n=5)
    for _ in range(4):
        w.turn()
        w.later(120)
    q = caretaker.quarantined(w.db, w.t)
    assert len(q) <= caretaker.MAX_QUARANTINED
    assert not q & caretaker.CORE_KINDS and not any(k.startswith("body.") for k in q)


def test_the_loop_skips_a_quarantined_kind(tmp_path):
    cfg = make_config(tmp_path)
    agent = build_agent(cfg)
    with agent.db.transaction():
        agent.scheduler.enqueue("noop", {"x": 1})
        agent.db.kv_set("caretaker_quarantine", {"noop": time.time() + 3600})
    assert agent.run(until_idle=True) == 0  # held back
    with agent.db.transaction():
        agent.db.kv_set("caretaker_quarantine", {})
    assert agent.run(until_idle=True) == 1
    agent.db.close()


def test_a_checkpoint_asked_for_by_orin_runs_between_slices(tmp_path):
    cfg = make_config(tmp_path)
    agent = build_agent(cfg)
    with agent.db.transaction():
        agent.scheduler.enqueue("noop", {"x": 1})
        agent.db.kv_set("caretaker_checkpoint", 1.0)
    agent.run(until_idle=True)
    assert agent.db.kv_get("caretaker_checkpoint") is None
    agent.db.close()


def test_the_body_guard_obeys_the_slowdown_and_orin_still_runs_in_a_pause(tmp_path):
    from polymath.body.guard import Guard
    from polymath.body.sensors import Vitals

    cfg = make_config(tmp_path)
    db = open_database(cfg.paths.db_path)
    hot = Vitals(temp_c=50.0, load1=1.0, cpus=4, mem_available_mb=2000.0, disk_free_gb=100.0, disk_used_gb=50.0)
    g = Guard(cfg, db, sensors=lambda: hot)
    db.kv_set("caretaker_slowdown", {"factor": 0.6, "until": time.time() + 600, "why": "the Pi runs hot"})
    st = g.observe()
    assert st.intensity == pytest.approx(0.6) and any("Orin" in r for r in st.reasons)
    db.kv_set("caretaker_slowdown", {"factor": 0.6, "until": time.time() - 1, "why": "x"})
    assert g.observe().intensity == 1.0  # expired by itself
    paused = Vitals(temp_c=90.0, load1=1.0, cpus=4, mem_available_mb=2000.0, disk_free_gb=100.0, disk_used_gb=50.0)
    st = Guard(cfg, db, sensors=lambda: paused).observe()
    assert st.mode == "pause" and caretaker.KIND in st.allow
    db.close()


# ------------------------------------------------------------------------------------------- the inbox
def test_every_reported_problem_reaches_orin(w):
    issues.report(w.db, "brand", "new_thing", "something nobody has seen before", now=w.t)
    issues.report(w.db, "storage", "sd_card", "SD-card mode since …", now=w.t)  # on hold at Christian's request
    issues.report(w.db, "agent", "restarts", "the agent restarted 5 times in 10 minutes", now=w.t)
    w.turn()
    by_key = {r["key"]: r for r in w.incidents()}
    new = by_key["issue:brand/new_thing"]
    assert new["state"] == "escalated" and "not known yet: new to me" in new["advice"]
    held = by_key["issue:sd-card-mode"]
    assert held["state"] == "closed" and held["outcome"] == "on hold"
    assert "on hold at Christian's request" in held["advice"]
    assert not any("SD-card mode" in s and "Do:" in s for s in w.said())  # never nags about the SSD…
    assert sum("on hold at Christian's request" in s for s in w.said()) == 1  # …only notes it, once a day
    issues.report(w.db, "storage", "sd_card", "SD-card mode, again", now=w.t + 7200)
    w.later(7200)
    w.turn()
    assert sum("on hold at Christian's request" in s for s in w.said()) == 1
    restarts = by_key["issue:agent-restarts"]
    assert restarts["state"] == "escalated"
    assert json.loads(restarts["issue_ids"])  # it took the issue on
    w.later(caretaker.INBOX_QUIET_CLOSE_S + 200)
    w.turn()
    assert all(r["state"] == "closed" for r in w.incidents())  # quiet for a day: closed


# ------------------------------------------------------------------------------------------- the install, security
def test_build_config_and_permissions_are_watched(w, monkeypatch):
    (w.tmp / "polymath.toml").write_text("[storage]\n")
    w.turn()  # first look: baselines recorded silently
    assert not [r for r in w.incidents() if r["kind"] in {"config_changed", "permissions"}]
    (w.tmp / "polymath.toml").write_text("[storage]\nadopt = false\n")
    w.config.paths.data_dir.chmod(0o777)
    from polymath import version

    monkeypatch.setattr(version, "read_build", lambda path=None: version.Build(version="9.9.9", commit="x"))
    w.later(caretaker.SECURITY_EVERY + 1)
    w.turn()
    kinds = {r["kind"]: r for r in w.incidents()}
    assert "config_changed" in kinds and "polymath.toml" in kinds["config_changed"]["advice"]
    assert "permissions" in kinds and "now 777" in json.loads(kinds["permissions"]["symptoms"])[0]["text"]
    assert "sudo chown -R polymath:polymath" in kinds["permissions"]["advice"]
    assert "build_mismatch" in kinds and "sudo systemctl restart polymath" in kinds["build_mismatch"]["advice"]


def test_in_sd_card_mode_the_ssd_is_left_alone(w, monkeypatch):
    from polymath.body import sensors

    monkeypatch.setattr(sensors, "slow_usb_disks", lambda sys_block=None: [("sda", 480)])
    w.config.storage.sd_only = True
    w.turn()
    assert not [r for r in w.incidents() if r["kind"] == "usb2"]
    w.config.storage.sd_only = False
    w.turn()
    usb2 = [r for r in w.incidents() if r["kind"] == "usb2"]
    assert usb2 and "blue USB 3 port" in usb2[0]["advice"]  # quoted from brain-drive-usb2


# ------------------------------------------------------------------------------------------- 6-7. safety
FORBIDDEN_CALLS = {"system", "popen", "Popen", "run", "call", "check_call", "check_output", "chmod", "chown",
                   "lchown", "unlink", "remove", "rmdir", "rmtree", "rename", "replace", "write_text", "write_bytes",
                   "mkdir", "socket", "connect", "kill", "symlink_to", "touch"}  # fmt: skip


def test_orin_has_no_code_path_that_acts_on_the_system():
    tree = ast.parse(SOURCE.read_text())
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {str(n.module).split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not imported & {"subprocess", "socket", "ctypes", "shlex", "pty", "signal", "urllib", "http"}
    calls = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
             for n in ast.walk(tree) if isinstance(n, ast.Call)}  # fmt: skip
    assert not calls & FORBIDDEN_CALLS, calls & FORBIDDEN_CALLS
    opens = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "open"]
    assert not opens  # it reads with read_text only
    strings = " ".join(n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str))
    assert "systemctl" not in strings and "mkfs" not in strings and "umount" not in strings


def test_security_responses_only_ever_narrow():
    """Nothing in Orin can widen what Polymath visits or loads: it never names the allow lists or the vetting."""
    text = SOURCE.read_text()
    for name in ("user_allow_domains", "crawl_allow_domains", "allow_domains", "sites SET", "blocklists_loaded",
                 "open_web", "UPDATE sites"):  # fmt: skip
        assert name not in text, name
    remedies = set(caretaker.REMEDY_COST)
    assert remedies == caretaker.TIER1 | {"tell", "note"}
    assert "blocklists" in remedies  # refreshing the safety lists only adds protection


def test_kv_switches_cannot_break_the_loop(w):
    """The loop and the body guard read Orin's switches every cycle: a malformed value is ignored, never fatal."""
    for bad in ("nonsense", ["x"], {"x.y": "soon"}, {"x.y": None}, 5):
        w.db.kv_set("caretaker_quarantine", bad)
        assert caretaker.quarantined(w.db, w.t) == set()
        w.db.kv_set("caretaker_slowdown", bad)
        assert caretaker.slowdown(w.db, w.t) == (1.0, "")
    w.db.kv_set("caretaker_quarantine", {"x.y": w.t + 10, "z.z": "soon"})
    assert caretaker.quarantined(w.db, w.t) == {"x.y"}
    w.db.kv_set("caretaker_slowdown", {"factor": 5.0, "until": w.t + 10})
    assert caretaker.slowdown(w.db, w.t)[0] == 1.0  # a factor is always clamped to [0.5, 1]
    w.db.kv_set("caretaker_slowdown", {"factor": 0.01, "until": w.t + 10})
    assert caretaker.slowdown(w.db, w.t)[0] == 0.5


# ------------------------------------------------------------------------------------------- 10, 12. the real agent
def test_orin_is_registered_planned_urgent_and_independent_of_the_society(tmp_path):
    cfg = make_config(tmp_path)
    db = open_database(tmp_path / "x.sqlite3")
    comps = build_components(cfg, db, planners=True)
    assert caretaker.KIND in comps.registry.kinds()
    assert caretaker.planner in [p for _i, p in comps.planners]
    agent = SimpleNamespace(config=cfg, scheduler=Scheduler(db))
    caretaker.planner(agent)
    row = db.one("SELECT priority FROM jobs WHERE kind = ?", (caretaker.KIND,))
    assert (
        row is not None and row["priority"] == caretaker.PRIORITY and caretaker.PRIORITY > 2.9
    )  # ahead of the bandit (URGENT)
    cfg.agents.caretaker = False
    db.execute("DELETE FROM jobs")
    caretaker.planner(agent)
    assert db.scalar("SELECT COUNT(*) FROM jobs") == 0  # switched off
    db.close()


def test_end_to_end_a_real_agent_runs_orin_and_shows_it(tmp_path):
    cfg = make_config(tmp_path)
    cfg.paths.run_dir = tmp_path / "run"
    cfg.paths.run_dir.mkdir()
    cfg.body.thermal_zone.write_text("74000\n")  # 74 °C: past Orin's hard limit, below the guard's throttle
    agent = build_agent(cfg)
    with agent.db.transaction():
        agent.scheduler.enqueue(caretaker.KIND, {}, priority=caretaker.PRIORITY)
    assert agent.run(until_idle=True) >= 1
    inc = agent.db.one("SELECT kind, state FROM incidents")
    assert inc is not None and inc["kind"] == "heat" and inc["state"] == "verifying"
    status = agent.db.kv_get("caretaker")
    assert status["mood"] == "mending" and status["open"][0]["title"] == "the Pi runs hot"
    agent.start()
    snapshot.planner(agent)
    st, _age = snapshot.read_status(snapshot.path_for(cfg))
    assert st is not None and st["caretaker"]["open"][0]["state"] == "verifying"
    f = face.Face()
    f.status = st
    assert f.mood() == "mending"
    checks = [c for c in doctor.run_checks(cfg, "http://x", health=lambda u: ("answered", 0.1, {})) if c.name == "Orin"]
    assert checks and "the Pi runs hot (fixed, checking it holds)" in checks[0].detail
    lines = caretaker.describe(st["caretaker"])
    assert "Orin the Caretaker (working on 1 incident(s)):" in lines and any(
        "slowed the agent down" in x for x in lines
    )
    events = feed.Feed(agent.db).poll(None)["events"]
    orin_lines = ["".join(t for t, _ in feed.render_event(e)) for e in events if e.get("what") == "caretaker"]
    assert orin_lines and any("Orin" in x and "slowed the agent down" in x for x in orin_lines)
    rows = caretaker.incident_rows(cfg, every=True)
    assert rows is not None and rows[0]["title"] == "the Pi runs hot"
    assert "the Pi runs hot [verifying]" in "\n".join(caretaker.incident_lines(rows))
    agent.db.close()


def test_polymath_caretaker_shows_orin(tmp_path, capsys):
    from polymath.interface import cli

    make_config(tmp_path)
    conf = str(tmp_path / "polymath.toml")
    assert cli.main(["--config", conf, "caretaker"]) == 0
    assert "Orin: not reporting" in capsys.readouterr().out
    assert cli.main(["--config", conf, "caretaker", "incidents"]) == 0
    out = capsys.readouterr().out
    assert "Orin: no incidents" in out or "#" in out
