"""Phase 10: Minecraft ping, thermal / disk / player guard, vitals, backups with rotation and restore, eviction."""

from __future__ import annotations

import json
import lzma
import os
import socket
import sqlite3
import struct
import threading
import time
from collections import namedtuple

import pytest

from polymath.body import maintenance as mt
from polymath.body import minecraft as mc
from polymath.body.guard import Guard, data_bytes
from polymath.body.sensors import Vitals
from polymath.core.app import build_agent
from polymath.core.jobs import JobContext, noop_handler
from polymath.core.scheduler import Job, Scheduler, local_phase
from polymath.memory.documents import Document, DocumentStore
from polymath.memory.graph import KnowledgeGraph
from polymath.memory.text_index import TextIndex


# ------------------------------------------------------------------ Minecraft
def read_varint(conn):
    result = 0
    for shift in range(0, 35, 7):
        b = conn.recv(1)[0]
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result
    raise ValueError


class FakeMinecraft:
    """Speaks the server side of Server List Ping."""

    def __init__(self, response: bytes | None = None, online: int = 2) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.online = online
        self.response = response
        self.handshakes: list[tuple[int, str, int, int]] = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                try:
                    length = read_varint(conn)
                    data = b""
                    while len(data) < length:
                        data += conn.recv(length - len(data))
                    pos = 0
                    pid, pos = mc._decode_varint(data, pos)
                    proto, pos = mc._decode_varint(data, pos)
                    n, pos = mc._decode_varint(data, pos)
                    host = data[pos : pos + n].decode()
                    pos += n
                    port = struct.unpack(">H", data[pos : pos + 2])[0]
                    nxt, _ = mc._decode_varint(data, pos + 2)
                    self.handshakes.append((pid, host, port, nxt))
                    assert proto == mc.PROTOCOL_ANY and nxt == 1
                    read_varint(conn)
                    conn.recv(1)  # status request: length 1, id 0
                    if self.response is not None:
                        conn.sendall(self.response)
                        continue
                    body = json.dumps(
                        {"version": {"name": "Paper 1.21"}, "players": {"online": self.online, "max": 20}}
                    ).encode()
                    payload = mc._varint(0) + mc._varint(len(body)) + body
                    conn.sendall(mc._varint(len(payload)) + payload)
                except (OSError, IndexError, AssertionError):
                    continue

    def close(self) -> None:
        self.sock.close()


def test_varint_round_trip():
    for v in (0, 1, 127, 128, 255, 25565, 2**31 - 1):
        assert mc._decode_varint(mc._varint(v), 0) == (v, len(mc._varint(v)))
    assert mc._varint(-1) == b"\xff\xff\xff\xff\x0f"
    with pytest.raises(ValueError):
        mc._decode_varint(b"\x80\x80", 0)


def test_server_list_ping():
    srv = FakeMinecraft(online=3)
    try:
        st = mc.ping("127.0.0.1", srv.port)
        assert st == mc.ServerStatus(3, 20, "Paper 1.21")
        assert srv.handshakes[-1] == (0, "127.0.0.1", srv.port, 1)
    finally:
        srv.close()
    bad = FakeMinecraft(response=mc._varint(5) + b"\x00\x03abc")  # not JSON
    try:
        assert mc.ping("127.0.0.1", bad.port) is None
    finally:
        bad.close()
    wrong = FakeMinecraft(response=mc._varint(2) + b"\x01\x00")  # wrong packet id
    try:
        assert mc.ping("127.0.0.1", wrong.port) is None
    finally:
        wrong.close()
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    assert mc.ping("127.0.0.1", port, timeout=0.5) is None  # server down: nobody to yield to


# ---------------------------------------------------------------------- guard
class Clock:
    def __init__(self) -> None:
        self.t = 1_790_000_000.0

    def __call__(self) -> float:
        return self.t


def guard_for(config, db, temps, *, free=500.0, players=None):
    clock = Clock()
    state = {"temp": temps, "free": free}
    calls = []

    def sensors():
        return Vitals(state["temp"], 0.5, 4, 1000.0, state["free"], 10.0)

    def pinger(host, port):
        calls.append((host, port))
        return None if players is None else mc.ServerStatus(players, 20, "x")

    config.body.minecraft_check = players is not None
    g = Guard(config, db, clock=clock, pinger=pinger, sensors=sensors)
    return g, clock, state, calls


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


def test_players_yield_and_poll_interval(config, db):
    g, clock, _state, calls = guard_for(config, db, 50.0, players=2)
    st = g.observe()
    assert st.mode == "yield" and st.players_online == 2 and "2 player(s)" in st.reasons[0]
    clock.t += 10
    g.observe()
    assert len(calls) == 1  # cached between polls
    clock.t += config.body.minecraft_poll_seconds
    g.observe()
    assert len(calls) == 2 and calls[0] == (config.body.minecraft_host, config.body.minecraft_port)
    g0, *_ = guard_for(config, db, 50.0, players=0)
    assert g0.observe().mode == "normal"


def test_disk_pressure_requests_eviction_and_allows_it_while_paused(config, db):
    config.body.disk_min_free_gb = 10.0
    g, clock, state, _ = guard_for(config, db, 50.0, free=8.0)
    st = g.observe()
    assert st.mode == "yield" and st.allow == []
    assert db.one("SELECT kind, priority FROM jobs")["kind"] == "body.evict"
    state["free"] = 3.0
    st = g.observe()
    assert st.mode == "pause" and st.allow == ["body.evict"]
    assert db.scalar("SELECT COUNT(*) FROM jobs WHERE kind='body.evict'") == 1  # one request per hour
    state["temp"] = 90.0
    assert g.observe().allow == []  # too hot: nothing runs, not even eviction
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


def test_paused_agent_runs_only_maintenance(config):
    agent = build_agent(config, planners=False)
    agent.start()
    hits = []
    agent.registry.register("body.test", lambda ctx: (hits.append(1), noop_handler(ctx))[1], "t")
    agent.scheduler.enqueue("t.other", {}, key="o", priority=9)
    agent.scheduler.enqueue("body.test", {}, key="b")
    from polymath.core.loop import BodyState

    agent.body.observe = lambda: BodyState(mode="pause", intensity=0, reasons=["disk"], allow=["body.test"])
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
    monkeypatch.setattr(mt.shutil, "disk_usage", lambda _p: usage(100, 99, 1))
    ctx, _ = ctx_for(config, db, "body.backup")
    out = mt.backup_job(ctx)
    assert out.result["ok"] is False and "skipped" in out.result["detail"]
    assert db.one("SELECT ok, detail FROM backups")["ok"] == 0 and not mt.backups(config.paths.backup_dir)


def test_backup_planner(config, db):
    class A:
        pass

    agent = A()
    agent.config, agent.scheduler = config, Scheduler(db)
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
    exact, _ = store.add(Document("web", "dup", "Copy", store.get(ids["yak"]).text, "CC"))
    assert db.one("SELECT state, codec FROM documents WHERE id=?", (exact,))["codec"] == "evicted"  # never stored
    near, _ = store.add(Document("web", "near", "Near copy", store.get(ids["yak"]).text + " (mirror)", "CC"))
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
    assert store.get(ids["walrus"]).text == ""
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
