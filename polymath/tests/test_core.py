"""Phase 1: config, logging, database, scheduler, sd_notify, agent loop."""

from __future__ import annotations

import io
import json
import logging
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from polymath.body.sensors import (
    disk_usage,
    read_meminfo,
    read_temperature,
    read_vitals,
    same_filesystem_as_root,
)
from polymath.body.systemd_notify import Notifier, notify
from polymath.core.app import StorageError, build_agent, build_components, check_storage
from polymath.core.config import ConfigError, load_config, to_dict
from polymath.core.db import Database, open_database
from polymath.core.jobs import Interrupted, JobOutcome, JobRegistry, PermanentError, noop_handler
from polymath.core.logging import JsonFormatter, get_logger, setup_logging
from polymath.core.loop import Agent, BodyState, Decision, PriorityPolicy
from polymath.core.scheduler import Scheduler
from polymath.interface import cli
from tests.conftest import ROOT, make_config

# ----------------------------------------------------------------- config


def test_defaults_without_file():
    default = load_config(None, env={})  # /etc/polymath/polymath.toml is absent on the build host
    assert default.paths.data_dir == Path("/srv/polymath")
    assert default.dashboard.port == 8765
    assert default.senses.crawl_rate <= 1.0


def test_config_file_and_env_override(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text('log_level = "DEBUG"\n[loop]\njob_time_budget = 5\n[senses]\nfeeds = ["https://a/b"]\n')
    cfg = load_config(f, env={"POLYMATH_DATA_DIR": str(tmp_path / "d")})
    assert cfg.loop.job_time_budget == 5.0 and isinstance(cfg.loop.job_time_budget, float)
    assert cfg.senses.feeds == ["https://a/b"]
    assert cfg.paths.data_dir == tmp_path / "d"
    assert to_dict(cfg)["paths"]["data_dir"] == str(tmp_path / "d")


@pytest.mark.parametrize(
    "text, msg",
    [
        ("[loop]\nbogus = 1\n", "unknown key"),
        ("[loop]\njob_time_budget = 'x'\n", "expected a number"),
        ("[loop]\njob_time_budget = 500\n", "watchdog"),
        ("[senses]\ncrawl_rate = 3\n", "crawl_rate"),
        ("[body]\npause_celsius = 70\n", "pause_celsius"),
        ("[senses]\nfeeds = 'x'\n", "expected a list"),
        ("[paths]\ndata_dir = 3\n", "path string"),
        ("loop = 3\n", "expected a table"),
        ("[loop]\nmax_crashes = true\n", "expected int"),
        ("log_level = 'LOUD'\n", "log_level"),
        ("[learning]\nembedding_dim = 4\n", "embedding_dim"),
        ("[body]\nbackup_keep = 0\n", "backup_keep"),
        ("not toml ===", "Expected"),
    ],
)
def test_config_rejects_bad_values(tmp_path, text, msg):
    f = tmp_path / "c.toml"
    f.write_text(text)
    with pytest.raises(ConfigError, match=msg):
        load_config(f, env={})


def test_explicit_missing_config_is_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml", env={})
    with pytest.raises(ConfigError, match="not found"):
        load_config(None, env={"POLYMATH_CONFIG": str(tmp_path / "nope.toml")})


def test_example_config_is_valid():
    cfg = load_config(ROOT / "config" / "polymath.toml", env={})
    assert cfg.paths.data_dir == Path("/srv/polymath")
    assert cfg.paths.require_separate_mount is True


# ---------------------------------------------------------------- logging


def test_json_logging_includes_extra_and_exceptions():
    buf = io.StringIO()
    setup_logging("DEBUG", stream=buf)
    lg = get_logger("t")
    lg.info("hello %s", "x", extra={"job": 7})
    try:
        raise ValueError("boom")
    except ValueError:
        lg.exception("failed")
    lines = [json.loads(line) for line in buf.getvalue().splitlines()]
    assert lines[0]["msg"] == "hello x" and lines[0]["job"] == 7 and lines[0]["logger"] == "polymath.t"
    assert "ValueError: boom" in lines[1]["exc"]
    assert isinstance(JsonFormatter().format(logging.makeLogRecord({"msg": "m"})), str)
    setup_logging("WARNING")


# --------------------------------------------------------------- database


def test_migrations_wal_and_idempotent(config):
    db = Database(config.paths.db_path)
    applied = db.migrate()
    assert applied and applied[0] == "0001_core.sql"
    assert db.migrate() == []
    assert db.scalar("PRAGMA journal_mode") == "wal"
    assert db.schema_version() >= 1
    db.close()
    assert not db.is_open
    with pytest.raises(sqlite3.ProgrammingError):
        db.execute("SELECT 1")


def test_nested_transactions_and_kv(db):
    with db.transaction():
        db.kv_set("a", {"x": 1})
        with pytest.raises(RuntimeError), db.transaction():
            db.kv_set("b", 2)
            raise RuntimeError
        assert db.kv_get("b") is None
    assert db.kv_get("a") == {"x": 1}
    assert db.kv_get("missing", 5) == 5
    with pytest.raises(RuntimeError), db.transaction():
        db.kv_set("c", 1)
        raise RuntimeError
    assert db.kv_get("c") is None


def test_readonly_connection(config, db):
    db.kv_set("k", 1)
    ro = Database(config.paths.db_path, readonly=True)
    assert ro.kv_get("k") == 1
    with pytest.raises(sqlite3.OperationalError):
        ro.kv_set("k", 2)
    ro.close()
    with pytest.raises(FileNotFoundError):
        Database(config.paths.data_dir / "nope.sqlite3", readonly=True)


# -------------------------------------------------------------- scheduler


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_enqueue_idempotent_and_priority(db):
    s = Scheduler(db)
    a, created = s.enqueue("noop", {"n": 1}, priority=1)
    assert created
    a2, created2 = s.enqueue("noop", {"n": 1}, priority=9)
    assert a == a2 and not created2
    b, _ = s.enqueue("noop", {"n": 2}, priority=5)
    c, _ = s.enqueue("other", {}, key="explicit", priority=3)
    job = s.claim()
    assert job and job.id == b
    assert s.claim(["other"]).id == c  # type: ignore[union-attr]
    assert s.claim([]) is None
    assert s.get(b)["state"] == "running"  # type: ignore[index]
    assert s.get(999) is None


def test_not_before_backoff_and_dead(db):
    clock = Clock()
    s = Scheduler(db, clock=clock, retry_base=10, retry_cap=100)
    jid, _ = s.enqueue("k", {}, max_attempts=3, not_before=clock.t + 50)
    assert s.claim() is None and s.stats().ready == 0
    assert s.next_wakeup() == clock.t + 50
    clock.t += 50
    job = s.claim()
    assert job is not None
    assert s.fail(job, "e1") == "queued"
    row = s.get(jid)
    assert row and 7.5 <= row["not_before"] - clock.t <= 12.5 and row["attempts"] == 1
    clock.t += 1000
    job = s.claim()
    assert job and job.attempts == 1
    assert s.fail(job, "e2") == "queued"
    clock.t += 1000
    job = s.claim()
    assert job and s.fail(job, "e3") == "dead"
    assert s.stats().dead == 1
    assert s.backoff(30) <= 100 * 1.25


def test_permanent_failure_and_checkpoint_resets_attempts(db):
    clock = Clock()
    s = Scheduler(db, clock=clock)
    jid, _ = s.enqueue("k", {})
    job = s.claim()
    assert job
    s.fail(job, "x")
    clock.t += 10_000
    job = s.claim()
    assert job and job.attempts == 1
    s.checkpoint(job, {"offset": 10}, cpu=0.5, value=2)
    job = s.claim()
    assert job and job.checkpoint == {"offset": 10} and job.attempts == 0 and job.slices == 1
    s.complete(job, result={"ok": True}, cpu=0.5, value=1)
    row = s.get(jid)
    assert row and row["state"] == "done" and row["value"] == 3 and row["cpu_seconds"] == 1.0
    s.enqueue("k", {"b": 1})
    job2 = s.claim()
    assert job2 and s.fail(job2, "bad", retryable=False) == "dead"


def test_recover_requeues_then_dead_letters_crashers(db):
    s = Scheduler(db, max_crashes=2)
    jid, _ = s.enqueue("k", {})
    assert s.claim()
    assert s.recover() == {"requeued": 1, "dead": 0}
    assert s.claim()
    assert s.recover() == {"requeued": 0, "dead": 1}
    assert s.get(jid)["state"] == "dead"  # type: ignore[index]


def test_release_recurring_and_purge(db):
    clock = Clock()
    s = Scheduler(db, clock=clock)
    a, created = s.ensure_recurring("nightly", 100)
    assert created
    assert s.ensure_recurring("nightly", 100) == (a, False)
    clock.t += 100
    b, created = s.ensure_recurring("nightly", 100)
    assert created and b != a
    job = s.claim()
    assert job
    s.release(job, delay=5)
    assert s.get(job.id)["state"] == "queued"  # type: ignore[index]
    clock.t += 5
    job = s.claim()
    assert job
    s.complete(job)
    clock.t += 1000
    assert s.purge_done(500) == 1


# ------------------------------------------------------------- sd_notify


def _dgram_listener(path: str) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(path)
    sock.settimeout(2)
    return sock


def test_sd_notify_real_socket(tmp_path):
    path = str(tmp_path / "notify.sock")
    listener = _dgram_listener(path)
    env = {"NOTIFY_SOCKET": path, "WATCHDOG_USEC": "120000000", "WATCHDOG_PID": str(os.getpid())}
    n = Notifier(env)
    assert n.active and n.watchdog_interval == 120.0 and n.ping_every == 30.0
    assert n.ready("hi")
    assert listener.recv(1024) == b"READY=1\nSTATUS=hi"
    assert n.watchdog(force=True)
    assert listener.recv(1024) == b"WATCHDOG=1"
    assert not n.watchdog()  # rate limited
    assert n.status("x" * 500)
    assert len(listener.recv(1024)) == len("STATUS=") + 200
    assert n.stopping()
    assert listener.recv(1024) == b"STOPPING=1"
    assert n.sent == ["READY", "WATCHDOG", "STATUS", "STOPPING"]
    listener.close()


def test_sd_notify_abstract_and_absent(tmp_path):
    name = f"polymath-test-{os.getpid()}"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    listener.bind("\0" + name)
    listener.settimeout(2)
    assert notify("READY=1", {"NOTIFY_SOCKET": "@" + name})
    assert listener.recv(64) == b"READY=1"
    listener.close()
    assert not notify("READY=1", {})
    assert not notify("READY=1", {"NOTIFY_SOCKET": "vsock:2:3"})
    assert not notify("READY=1", {"NOTIFY_SOCKET": str(tmp_path / "missing.sock")})
    other_pid = Notifier({"NOTIFY_SOCKET": "/x", "WATCHDOG_USEC": "1000000", "WATCHDOG_PID": "1"})
    assert other_pid.watchdog_interval == 0.0
    assert not Notifier({}).ready()


# ----------------------------------------------------------------- sensors


def test_sensors(tmp_path):
    zone = tmp_path / "temp"
    zone.write_text("61250\n")
    assert read_temperature(zone) == 61.25
    assert read_temperature(tmp_path / "none") is None
    mem = tmp_path / "meminfo"
    mem.write_text("MemTotal: 100 kB\nMemAvailable: 2048 kB\n")
    assert read_meminfo(mem) == 2.0
    assert read_meminfo(tmp_path / "nomem") is None
    (tmp_path / "m2").write_text("MemTotal: 1 kB\n")
    assert read_meminfo(tmp_path / "m2") is None
    free, _used = disk_usage(tmp_path / "does" / "not" / "exist")
    assert free is not None and free > 0
    v = read_vitals(zone, tmp_path)
    assert v.temp_c == 61.25 and v.cpus >= 1
    assert same_filesystem_as_root(Path("/")) is True


def test_storage_guard(tmp_path, config):
    check_storage(config)  # require_separate_mount disabled in tests
    assert config.paths.raw_dir.is_dir()
    config.paths.require_separate_mount = True
    config.paths.data_dir = Path("/") / "tmp" / f"pm-guard-{os.getpid()}"
    if same_filesystem_as_root(config.paths.data_dir):
        with pytest.raises(StorageError, match="root filesystem"):
            check_storage(config)


# -------------------------------------------------------------------- loop


def _agent(config, db, registry=None, **kw):
    registry = registry or build_components(config, db).registry
    return Agent(config, db, registry, notifier=Notifier({}), **kw)


def test_hundred_noop_cycles(config, db):
    agent = _agent(config, db)
    for i in range(100):
        agent.scheduler.enqueue("noop", {"i": i, "value": 1})
    t0 = time.perf_counter()
    ran = agent.run(max_cycles=100)
    elapsed = time.perf_counter() - t0
    assert ran == 100
    assert db.scalar("SELECT COUNT(*) FROM noop_log") == 100
    assert db.scalar("SELECT COUNT(*) FROM cycles WHERE status='done'") == 100
    assert db.scalar("SELECT COUNT(DISTINCT cycle) FROM noop_log") == 100
    assert agent.scheduler.stats().done == 100
    assert db.kv_get("heartbeat")["state"] == "stopped"
    assert elapsed < 30
    # Rewards are value per CPU second and positive for positive value.
    assert db.scalar("SELECT MIN(reward) FROM cycles") > 0


def test_until_idle_and_idle_heartbeat(config, db):
    agent = _agent(config, db)
    agent.scheduler.enqueue("noop", {})
    assert agent.run(until_idle=True) == 1
    assert agent.last and agent.last.status == "idle"


def test_failures_retry_then_succeed(config, db):
    reg = JobRegistry()
    calls = {"n": 0}

    def flaky(ctx):
        calls["n"] += 1
        ctx.db.kv_set(f"partial{calls['n']}", 1)  # must be rolled back on failure
        if calls["n"] < 3:
            raise OSError("transient")
        return JobOutcome(done=True, value=5)

    reg.register("flaky", flaky, "test")
    agent = _agent(config, db, reg)
    agent.scheduler.enqueue("flaky", {})
    statuses = []
    for _ in range(3):
        db.execute("UPDATE jobs SET not_before=0")
        statuses.append(agent.cycle().status)
    assert statuses == ["failed", "failed", "done"]
    assert db.kv_get("partial1") is None and db.kv_get("partial2") is None and db.kv_get("partial3") == 1
    assert db.scalar("SELECT COUNT(*) FROM cycles WHERE status='failed'") == 2


def test_permanent_unknown_and_bad_return(config, db):
    reg = JobRegistry()
    reg.register("perm", lambda ctx: (_ for _ in ()).throw(PermanentError("bad payload")), "t")
    reg.register("bad", lambda ctx: "nope", "t")  # type: ignore[arg-type,return-value]
    with pytest.raises(ValueError):
        reg.register("bad", noop_handler, "dup")
    agent = _agent(config, db, reg)
    s = agent.scheduler
    p, _ = s.enqueue("perm", {})
    u, _ = s.enqueue("ghost", {})
    b, _ = s.enqueue("bad", {})
    for _ in range(3):
        agent.cycle()
    assert s.get(p)["state"] == "dead" and "bad payload" in s.get(p)["last_error"]  # type: ignore[index]
    assert s.get(u)["state"] == "dead"  # type: ignore[index]
    assert s.get(b)["state"] == "queued" and "TypeError" in s.get(b)["last_error"]  # type: ignore[index]


def test_checkpointed_continuation(config, db):
    reg = JobRegistry()

    def counter(ctx):
        n = (ctx.job.checkpoint or {}).get("n", 0) + 1
        ctx.db.execute("INSERT INTO noop_log(job_key, cycle, at) VALUES(?,?,?)", (f"step{n}", n, 0))
        if n < 5:
            return JobOutcome(done=False, checkpoint={"n": n}, value=1)
        return JobOutcome(done=True, value=1, result={"n": n})

    reg.register("count", counter, "t")
    agent = _agent(config, db, reg)
    jid, _ = agent.scheduler.enqueue("count", {})
    assert agent.run(until_idle=True) == 5
    row = agent.scheduler.get(jid)
    assert row and row["state"] == "done" and row["slices"] == 5 and json.loads(row["result"]) == {"n": 5}


def test_interrupt_releases_job_and_rolls_back(config, db):
    reg = JobRegistry()

    def long(ctx):
        ctx.db.kv_set("half", 1)
        agent.stop()
        ctx.check()
        raise AssertionError("unreachable")

    reg.register("long", long, "t")
    agent = _agent(config, db, reg)
    jid, _ = agent.scheduler.enqueue("long", {})
    agent.start()
    rec = agent.cycle()
    assert rec.status == "interrupted"
    assert db.kv_get("half") is None
    assert agent.scheduler.get(jid)["state"] == "queued"  # type: ignore[index]
    with pytest.raises(Interrupted):
        raise Interrupted


class FakeBody:
    def __init__(self, mode: str) -> None:
        self.mode = mode

    def observe(self) -> BodyState:
        return BodyState(mode=self.mode, intensity=0.5, reasons=[f"mode {self.mode}"])


def test_pause_and_yield_modes(config, db):
    reg = JobRegistry()
    reg.register("noop", noop_handler, "light")
    reg.register("heavy", noop_handler, "heavy", heavy=True)
    agent = _agent(config, db, reg, body=FakeBody("pause"))
    agent.scheduler.enqueue("heavy", {}, priority=10)
    agent.scheduler.enqueue("noop", {}, priority=1)
    assert agent.cycle().status == "paused"
    assert db.kv_get("paused_reason") == "mode pause"
    agent.body = FakeBody("yield")
    rec = agent.cycle()
    assert rec.status == "done" and rec.kind == "noop"  # heavy job skipped while yielding
    assert agent.cycle().status == "idle"
    agent.body = FakeBody("normal")
    assert agent.cycle().kind == "heavy"
    d = PriorityPolicy().decide(agent.observe(), reg)
    assert isinstance(d, Decision)


def test_planners_run_on_interval_and_errors_are_contained(config, db):
    calls = []

    def planner(ag):
        calls.append(1)
        ag.scheduler.enqueue("noop", {"from": "planner"})

    def broken(ag):
        raise RuntimeError("planner bug")

    agent = _agent(config, db, planners=[(3600, planner), (3600, broken)])
    agent.cycle()
    agent.cycle()
    assert len(calls) == 1
    assert db.scalar("SELECT COUNT(*) FROM noop_log") == 1


def test_sigterm_in_process(config, db):
    agent = _agent(config, db)
    agent.install_signal_handlers()
    try:
        threading.Timer(0.3, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
        t0 = time.time()
        agent.run()
        assert time.time() - t0 < 5
        assert db.kv_get("heartbeat")["state"] == "stopped"
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGINT, signal.default_int_handler)


# --------------------------------------------------- process-level crash tests


def _env(config_path: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("POLYMATH")}
    env["POLYMATH_CONFIG"] = str(config_path)
    env["PYTHONPATH"] = str(ROOT)
    env.update(extra or {})
    return env


def test_kill9_mid_run_then_resume_exactly_once(tmp_path):
    cfg = make_config(tmp_path)
    cfg_path = tmp_path / "polymath.toml"
    db = open_database(cfg.paths.db_path)
    s = Scheduler(db)
    for i in range(100):
        s.enqueue("noop", {"i": i, "sleep": 0.02})
    db.close()
    proc = subprocess.Popen(
        [sys.executable, "-m", "polymath", "run", "--until-idle"],
        env=_env(cfg_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    probe = Database(cfg.paths.db_path, readonly=True)
    deadline = time.time() + 30
    while time.time() < deadline and probe.scalar("SELECT COUNT(*) FROM noop_log", default=0) < 30:
        time.sleep(0.05)
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait(10)
    done_before = probe.scalar("SELECT COUNT(*) FROM noop_log")
    assert 30 <= done_before < 100
    probe.close()

    out = subprocess.run(
        [sys.executable, "-m", "polymath", "run", "--until-idle"],
        env=_env(cfg_path),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert out.returncode == 0, out.stderr
    db = Database(cfg.paths.db_path)
    assert db.scalar("SELECT COUNT(*) FROM noop_log") == 100  # every job exactly once
    assert db.scalar("SELECT COUNT(*) FROM jobs WHERE state='done'") == 100
    assert db.scalar("SELECT COUNT(*) FROM cycles") == db.scalar("SELECT COUNT(DISTINCT id) FROM cycles")
    assert db.scalar("PRAGMA integrity_check") == "ok"
    db.close()


def test_sigterm_subprocess_graceful_with_systemd_notify(tmp_path):
    make_config(tmp_path)
    cfg_path = tmp_path / "polymath.toml"
    sock_path = str(tmp_path / "n.sock")
    listener = _dgram_listener(sock_path)
    proc = subprocess.Popen(
        [sys.executable, "-m", "polymath", "run"],
        env=_env(cfg_path, {"NOTIFY_SOCKET": sock_path}),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    first = listener.recv(1024)
    assert first.startswith(b"READY=1")
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(15) == 0
    msgs = []
    listener.settimeout(0.5)
    try:
        while True:
            msgs.append(listener.recv(1024))
    except TimeoutError:
        pass
    assert b"STOPPING=1" in msgs
    listener.close()


# --------------------------------------------------------------------- CLI


def test_cli_init_enqueue_run_status(tmp_path, capsys):
    make_config(tmp_path)
    cfg = str(tmp_path / "polymath.toml")
    assert cli.main(["--config", cfg, "init"]) == 0
    assert cli.main(["--config", cfg, "enqueue", "noop", "--payload", '{"a":1}']) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["created"] is True
    assert cli.main(["--config", cfg, "run", "--until-idle"]) == 0
    assert json.loads(capsys.readouterr().out)["job_cycles"] == 1
    assert cli.main(["--config", cfg, "status"]) == 0
    st = json.loads(capsys.readouterr().out)
    assert st["jobs"]["done"] == 1 and st["agent_state"] == "stopped"


def test_cli_errors(tmp_path, capsys):
    assert cli.main(["--config", str(tmp_path / "missing.toml"), "status"]) == 2
    make_config(tmp_path)
    assert cli.main(["--config", str(tmp_path / "polymath.toml"), "status"]) == 4
    bad = tmp_path / "bad.toml"
    bad.write_text(f'[paths]\ndata_dir = "{tmp_path}/x"\nrequire_separate_mount = true\n')
    if same_filesystem_as_root(tmp_path):
        assert cli.main(["--config", str(bad), "init"]) == 3


def test_build_agent(config):
    agent = build_agent(config)
    assert "noop" in agent.registry.kinds()
    agent.db.close()


# ------------------------------------------------------------------- inbox


def test_inbox_requests_are_ingested_by_the_agent(config, db):
    from polymath.core import inbox

    agent = _agent(config, db)
    p = inbox.submit(config.paths.data_dir, {"type": "enqueue", "kind": "noop", "payload": {"x": 1}, "key": "k1"})
    inbox.submit(config.paths.data_dir, {"type": "enqueue", "kind": "noop", "payload": {"x": 1}, "key": "k1"})
    (inbox.inbox_dir(config.paths.data_dir) / "zz-bad.json").write_text("{not json")
    (inbox.inbox_dir(config.paths.data_dir) / "zz-odd.json").write_text('{"type": "enqueue"}')
    with pytest.raises(ValueError):
        inbox.submit(config.paths.data_dir, {"type": "rm -rf"})
    assert p.exists()
    rec = agent.cycle()
    assert rec.status == "done" and rec.kind == "noop"
    assert db.scalar("SELECT COUNT(*) FROM jobs WHERE key='k1'") == 1  # idempotent
    assert not list(inbox.inbox_dir(config.paths.data_dir).glob("*.json"))
    assert inbox.ingest(db, agent.scheduler, config.paths.data_dir / "nowhere") == 0


def test_cli_enqueue_uses_inbox_while_agent_runs(tmp_path, capsys):
    make_config(tmp_path)
    cfg = load_config(tmp_path / "polymath.toml", env={})
    assert cli.main(["--config", str(tmp_path / "polymath.toml"), "init"]) == 0
    capsys.readouterr()
    db = Database(cfg.paths.db_path)
    db.kv_set("heartbeat", {"ts": time.time(), "cycle": 1, "state": "running"})
    db.close()
    assert cli.main(["--config", str(tmp_path / "polymath.toml"), "enqueue", "noop", "--key", "via"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["queued_via"] == "inbox" and (cfg.paths.data_dir / "inbox" / out["file"]).exists()
