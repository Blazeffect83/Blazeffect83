"""v0.5.1 "always on": offline mode, a dashboard that cannot hang, a feed that survives a dead dashboard, doctor."""

from __future__ import annotations

import http.server
import json
import socket
import sqlite3
import stat
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from polymath.body.systemd_notify import Notifier
from polymath.core import netstate
from polymath.core.app import build_components
from polymath.core.db import Database, open_database
from polymath.core.jobs import JobOutcome, JobRegistry
from polymath.core.loop import Agent
from polymath.drive import offline
from polymath.interface import cli, doctor, feed, snapshot
from polymath.interface.dashboard import DashboardData, answers, make_server, watchdog_loop
from polymath.interface.face import Face
from polymath.senses import connectivity
from polymath.senses.net import FetchError, HttpClient, reachable
from tests.conftest import make_config
from tests.test_feed import build
from tests.test_interface import beat, req


def dns_down() -> FetchError:
    return FetchError("DNS failure for dumps.wikimedia.org: [Errno -3] Temporary failure in name resolution",
                      transient=True)  # fmt: skip


def FakeHttp() -> HttpClient:
    """A real client that never connects: tests move ``responses`` and record failed connections by hand."""
    return HttpClient(env={})


# ------------------------------------------------------------------------------------------- net state
def test_network_errors_are_told_apart_from_server_errors():
    assert netstate.is_network_error(dns_down())
    assert netstate.is_network_error(FetchError("TimeoutError: timed out", transient=True))
    assert netstate.is_network_error(FetchError("ConnectionRefusedError: [Errno 111] Connection refused",
                                                transient=True))  # fmt: skip
    assert netstate.is_network_error(ConnectionResetError("reset by peer"))
    assert netstate.is_network_error(socket.gaierror(-3, "Temporary failure in name resolution"))
    assert netstate.is_network_error(OSError(101, "Network is unreachable"))
    # the server answered: the internet works, whatever the status
    assert not netstate.is_network_error(FetchError("HTTP 503 for x", status=503, transient=True))
    assert not netstate.is_network_error(FetchError("HTTP 404 for x", status=404))
    assert not netstate.is_network_error(ValueError("bad data"))
    assert not netstate.is_network_error(FetchError("too many redirects for x"))


def test_offline_after_two_failures_and_back_with_events(db):
    t = 1_000_000.0
    with db.transaction():
        assert netstate.get(db).online
        assert not netstate.failed(db, t, "dns failure")  # one failure can be a blip
        assert netstate.failed(db, t + 5, "dns failure")  # the second takes it offline
        assert not netstate.failed(db, t + 6, "dns failure")  # already offline: no second event
    st = netstate.get(db)
    assert not st.online and st.since == t + 5 and st.fails == 3
    with db.transaction():
        assert netstate.succeeded(db, t + 3605)
        assert not netstate.succeeded(db, t + 3606)  # nothing new: no event (and no write)
    rows = db.query("SELECT kind, text, detail FROM events ORDER BY id")
    assert [r["kind"] for r in rows] == ["net", "net"]
    assert "studying what I already have" in rows[0]["text"] and json.loads(rows[0]["detail"])["status"] == "offline"
    assert rows[1]["text"].startswith("back online after 60 min")
    assert netstate.get(db).online and netstate.get(db).fails == 0


def test_durations_read_naturally():
    assert netstate._duration(30) == "30 s"
    assert netstate._duration(600) == "10 min"
    assert netstate._duration(3 * 3600) == "3 h"
    assert netstate._duration(5400) == "1.5 h"
    assert netstate._duration(3 * 86400) == "3 days"


def test_corrupt_net_state_reads_as_online(db):
    db.kv_set("net", "garbage")
    assert netstate.get(db).online
    db.kv_set("net", {"online": False, "since": 5.0, "unknown_field": 1})
    assert not netstate.get(db).online


# ------------------------------------------------------------------------------------------- the loop offline
def offline_agent(config: Any, db: Database, calls: list[str], *, net_raises: BaseException | None = None) -> Agent:
    http = FakeHttp()
    reg = JobRegistry()

    def net_job(ctx: Any) -> JobOutcome:
        calls.append("net")
        if net_raises is not None:
            raise net_raises
        http.responses += 1
        return JobOutcome(done=True, value=1.0)

    def study_job(ctx: Any) -> JobOutcome:
        calls.append("study")
        return JobOutcome(done=True, value=0.5)

    reg.register("fetch.thing", net_job, "needs the internet", network=True)
    reg.register("study.thing", study_job, "works from disk")
    return Agent(config, db, reg, notifier=Notifier({}), services={"http": http})


def test_network_job_is_deferred_not_failed_and_never_dies(config, db):
    calls: list[str] = []
    agent = offline_agent(config, db, calls, net_raises=dns_down())
    agent.start()
    jid, _ = agent.scheduler.enqueue("fetch.thing", {}, key="f1", priority=5.0, max_attempts=2)
    for _ in range(2):
        rec = agent.cycle()
        assert rec.status == "interrupted"
        db.execute("UPDATE jobs SET not_before = 0 WHERE id = ?", (jid,))  # make it ready again at once
        netstate_now = netstate.get(db)
        if netstate_now.online:  # first failure: still online, the job is retried
            continue
    job = db.one("SELECT state, attempts, last_error FROM jobs WHERE id = ?", (jid,))
    assert job["state"] == "queued" and job["attempts"] == 0 and "DNS failure" in job["last_error"]
    assert not netstate.get(db).online
    # offline: the network job is held back however often the loop runs; the study job runs
    agent.scheduler.enqueue("study.thing", {}, key="s1", priority=0.1)
    for _ in range(3):
        agent.cycle()
    assert calls == ["net", "net", "study"]
    assert db.scalar("SELECT state FROM jobs WHERE id = ?", (jid,)) == "queued"


def test_back_online_releases_held_jobs(config, db):
    calls: list[str] = []
    agent = offline_agent(config, db, calls)
    agent.start()
    with db.transaction():
        netstate.failed(db, time.time(), "dns")
        netstate.failed(db, time.time(), "dns")
    agent.scheduler.enqueue("fetch.thing", {}, key="f2", priority=5.0)
    assert agent.cycle().status == "idle" and calls == []  # held back
    assert "fetch.thing" not in agent.observe().queue.ready_by_kind  # the policy never even sees it
    with db.transaction():
        netstate.succeeded(db, time.time())  # what a successful probe does
    assert agent.cycle().status == "done" and calls == ["net"]


def test_successful_network_slice_marks_online(config, db):
    calls: list[str] = []
    agent = offline_agent(config, db, calls)
    agent.start()
    db.kv_set("net", {"online": True, "fails": 1, "last_fail": time.time()})
    agent.scheduler.enqueue("fetch.thing", {}, key="f3")
    agent.cycle()
    assert netstate.get(db).fails == 0  # a server answered during the slice


def test_outage_noticed_even_when_jobs_cope_with_their_own_errors(config, db):
    """The crawler and the feed poller catch fetch errors themselves; failed connections to two or more different
    hosts in one slice, with no server answering, still count. One dead website never does."""
    http = FakeHttp()
    reg = JobRegistry()
    hosts: list[list[str]] = []

    def crawl(ctx: Any) -> JobOutcome:
        for h in hosts.pop(0):
            http._connection_failed(h)
        return JobOutcome(done=True)

    reg.register("crawl.thing", crawl, "copes with its own errors", network=True)
    agent = Agent(config, db, reg, notifier=Notifier({}), services={"http": http})
    agent.start()
    for i in range(4):  # one dead site, again and again: never offline
        hosts.append(["dead.example", "dead.example"])
        agent.scheduler.enqueue("crawl.thing", {}, key=f"one{i}")
        agent.cycle()
    assert netstate.get(db).online and netstate.get(db).fails == 0
    for i in range(2):  # every host fails: the internet is gone
        hosts.append(["a.example", "b.example", "c.example"])
        agent.scheduler.enqueue("crawl.thing", {}, key=f"all{i}")
        agent.cycle()
    st = netstate.get(db)
    assert not st.online and "no connection to a.example, b.example" in st.reason


def test_ordinary_errors_still_fail_normally(config, db):
    calls: list[str] = []
    agent = offline_agent(config, db, calls, net_raises=ValueError("broken parser"))
    agent.start()
    jid, _ = agent.scheduler.enqueue("fetch.thing", {}, key="f4")
    assert agent.cycle().status == "failed"
    assert db.scalar("SELECT attempts FROM jobs WHERE id = ?", (jid,)) == 1
    assert netstate.get(db).online and netstate.get(db).fails == 0


def test_network_error_after_an_answer_is_not_an_outage(config, db):
    """A server answered earlier in the slice: a later connection error is that site's, not the internet's."""
    http = FakeHttp()
    reg = JobRegistry()

    def half(ctx: Any) -> JobOutcome:
        http.responses += 1
        raise dns_down()

    reg.register("half.thing", half, "answers, then fails", network=True)
    agent = Agent(config, db, reg, notifier=Notifier({}), services={"http": http})
    agent.start()
    jid, _ = agent.scheduler.enqueue("half.thing", {}, key="h1")
    assert agent.cycle().status == "failed"
    assert db.scalar("SELECT attempts FROM jobs WHERE id = ?", (jid,)) == 1 and netstate.get(db).fails == 0


def test_registered_network_kinds(config, db):
    reg = build_components(config, db, planners=False).registry
    assert reg.network_kinds() == {
        "sources.plan", "dump.download", "wikipedia.part", "wikidata.dump", "wikidata.propindex",
        "wikidata.properties", "gutenberg.books", "feeds.poll", "crawl.step", "wikipedia.titles",
        "web.blocklists", "web.vet",
    }  # fmt: skip
    assert reg.get("net.probe") is not None and not reg.get("net.probe").network  # it must run while offline
    for kind in offline.STUDY:
        assert reg.get(kind) is not None and not reg.get(kind).network, kind


# ------------------------------------------------------------------------------------------- probe
def test_probe_spacing():
    assert [connectivity.probe_interval(n) for n in range(1, 7)] == [60, 120, 240, 480, 600, 600]


class _Answering(http.server.BaseHTTPRequestHandler):
    status = 200

    def do_HEAD(self) -> None:
        self.send_response(self.status)
        self.end_headers()

    def log_message(self, *a: Any) -> None:
        pass


@pytest.fixture()
def local_server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Answering)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_reachable(local_server):
    client = HttpClient(allow_private=True, env={})
    url = f"http://127.0.0.1:{local_server.server_address[1]}/"
    assert reachable(client, url) == (True, "")
    assert client.responses == 1 and client.requests_made == 1
    _Answering.status = 404
    try:
        assert reachable(client, url)[0]  # an error status still proves the way out is open
    finally:
        _Answering.status = 200
    ok, why = reachable(client, f"http://127.0.0.1:{free_port()}/", timeout=2)
    assert not ok and "refused" in why.lower()
    assert client.responses == 2 and client.connection_failures == 1 and client.failed_since(0) == {"127.0.0.1"}
    ok, why = reachable(HttpClient(env={}), "http://127.0.0.1/")  # private addresses stay blocked
    assert not ok and "non-public" in why


def test_probe_job_and_planner(config, db, monkeypatch):
    calls: list[str] = []
    agent = offline_agent(config, db, calls)
    agent.registry.register("net.probe", connectivity.probe_job, "probe")
    agent.start()
    connectivity.planner(agent)
    assert not db.scalar("SELECT 1 FROM jobs WHERE kind = 'net.probe'")  # online: no probing
    t0 = time.time() - 61
    db.kv_set("net", {"online": False, "since": t0, "fails": 2})
    connectivity.planner(agent)
    assert db.scalar("SELECT COUNT(*) FROM jobs WHERE kind = 'net.probe'") == 1
    monkeypatch.setattr(connectivity, "reachable", lambda client, url: (False, "DNS failure"))
    agent.cycle()
    st = netstate.get(db)
    assert not st.online and st.probes == 1 and st.last_probe > t0
    connectivity.planner(agent)  # the next probe waits 2 minutes
    assert db.scalar("SELECT COUNT(*) FROM jobs WHERE kind = 'net.probe' AND state = 'queued'") == 0
    monkeypatch.setattr(connectivity, "reachable", lambda client, url: (True, ""))
    agent.scheduler.enqueue("net.probe", {}, key="manual")
    agent.cycle()
    assert netstate.get(db).online
    assert db.query("SELECT text FROM events WHERE kind = 'net'")[-1]["text"].startswith("back online after")


# ------------------------------------------------------------------------------------------- offline study
def test_offline_study_rotation(config, db):
    agent = Agent(config, db, build_components(config, db, planners=False).registry, notifier=Notifier({}))
    offline.planner(agent)
    assert not db.scalar("SELECT 1 FROM jobs")  # online: the normal schedules are enough
    db.kv_set("net", {"online": False, "since": time.time()})
    offline.planner(agent)
    kinds = {r["kind"] for r in db.query("SELECT kind FROM jobs WHERE key LIKE 'offline:%'")}
    assert kinds == set(offline.STUDY)
    offline.planner(agent)  # never two of one kind
    assert db.scalar("SELECT COUNT(*) FROM jobs") == len(offline.STUDY)
    db.execute("UPDATE jobs SET state = 'done', updated = ? WHERE kind = 'reason.rules'", (time.time(),))
    offline.planner(agent)  # it ran a moment ago: not again until its offline interval passes
    assert db.scalar("SELECT COUNT(*) FROM jobs WHERE kind = 'reason.rules'") == 1


def test_offline_agent_keeps_working_end_to_end(tmp_path):
    """A real agent with every job and planner, offline: it never claims a network job, and does other work."""
    cfg = make_config(tmp_path)
    cfg.loop.planners = True
    db = open_database(cfg.paths.db_path)
    with db.transaction():
        build(db, n=6)
        netstate.failed(db, time.time(), "dns")
        netstate.failed(db, time.time(), "dns")
    comps = build_components(cfg, db)
    agent = Agent(cfg, db, comps.registry, notifier=Notifier({}), services=comps.services, planners=comps.planners)
    agent.start()
    ran = []
    for _ in range(25):
        rec = agent.cycle()
        if rec.kind:
            ran.append(rec.kind)
    assert ran, "an offline agent must still find work"
    assert not set(ran) & comps.registry.network_kinds()
    assert db.scalar("SELECT 1 FROM jobs WHERE key LIKE 'offline:%'")
    db.close()


# ------------------------------------------------------------------------------------------- snapshot
def test_snapshot_written_only_with_a_runtime_dir(tmp_path):
    cfg = make_config(tmp_path)
    cfg.paths.run_dir = tmp_path / "run"
    db = open_database(cfg.paths.db_path)
    with db.transaction():
        build(db, n=3)
    agent = Agent(cfg, db, JobRegistry(), notifier=Notifier({}))
    snapshot.planner(agent)
    assert not cfg.paths.run_dir.exists()  # never created by the agent (systemd makes it)
    cfg.paths.run_dir.mkdir()
    snapshot.planner(agent)
    target = snapshot.path_for(cfg)
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    snap = json.loads(target.read_text())
    counts = snap["status"]["counts"]
    assert snap["seq"] == len(snap["events"]) > 0 and counts["approx"] is True  # no full scans: row numbers
    assert counts["documents"] == db.scalar("SELECT MAX(id) FROM documents") and counts["facts"] > 0
    first = snap["seq"]
    snapshot.planner(agent)  # nothing new
    assert json.loads(target.read_text())["seq"] == first
    with db.transaction():
        db.execute("INSERT INTO events(at, kind, text) VALUES(?, 'net', 'the internet is gone')", (time.time(),))
    snapshot.planner(agent)
    assert json.loads(target.read_text())["seq"] == first + 1

    src = snapshot.source(target)
    data = src(None)
    assert data["cursor"] == f"s{first + 1}" and data["events"] and "seq" not in data["events"][0]
    assert src(data["cursor"])["events"] == []
    assert [e["text"] for e in src(f"s{first}")["events"]] == ["the internet is gone"]
    old = snapshot.source(target, clock=lambda: time.time() + 3600)(None)
    assert old["status"]["online"] is False  # nobody refreshes it: the agent is not running
    db.close()


def test_snapshot_source_errors(tmp_path):
    with pytest.raises(feed.FeedUnavailable, match="no agent snapshot"):
        snapshot.source(tmp_path / "none.json")(None)
    (tmp_path / "bad.json").write_text("{not json")
    with pytest.raises(feed.FeedUnavailable, match="cannot read"):
        snapshot.source(tmp_path / "bad.json")(None)
    assert snapshot.read_status(tmp_path / "none.json") == (None, None)


def test_snapshot_failure_never_disturbs_the_agent(tmp_path, monkeypatch, caplog):
    cfg = make_config(tmp_path)
    cfg.paths.run_dir = tmp_path / "run"
    cfg.paths.run_dir.mkdir()
    db = open_database(cfg.paths.db_path)
    agent = Agent(cfg, db, JobRegistry(), notifier=Notifier({}))
    monkeypatch.setattr(snapshot, "write", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    snapshot.planner(agent)
    snapshot.planner(agent)  # logged once, not on every refresh
    assert sum("snapshot not written" in r.getMessage() for r in caplog.records) == 1
    db.close()


# ------------------------------------------------------------------------------------------- resilient feed
def test_resilient_source_falls_back_and_comes_back():
    now = [0.0]
    primary_up = [True]
    ev1 = {"kind": "read", "at": 1.0, "title": "Lisbon"}
    ev2 = {"kind": "read", "at": 2.0, "title": "Porto"}

    def primary(cursor):
        if not primary_up[0]:
            raise feed.FeedUnavailable("no answer from http://127.0.0.1:8765")
        return {"cursor": "p1", "events": [ev1, ev2], "status": {"online": True}}

    def fallback(cursor):
        return {"cursor": "s1", "events": [ev2, {"kind": "read", "at": 3.0, "title": "Faro"}], "status": {}}

    src = feed.resilient_source(primary, [("the snapshot", fallback)], hint="restart it", clock=lambda: now[0])
    assert [e.get("title") for e in src(None)["events"]] == ["Lisbon", "Porto"]
    primary_up[0] = False
    out = src("p1")
    texts = [e.get("text") or e.get("title") for e in out["events"]]
    assert texts[0] == "no answer from http://127.0.0.1:8765: showing the snapshot instead · fix: restart it"
    assert texts[1:] == ["Faro"]  # Porto was shown already
    now[0] = 10.0  # the dashboard is not asked again yet: no 5 s wait on every poll
    calls = []
    src2 = feed.resilient_source(lambda c: calls.append(c) or primary(c), [("the snapshot", fallback)],
                                 clock=lambda: now[0])  # fmt: skip
    src2(None)
    src2("x")
    assert len(calls) == 1
    primary_up[0] = True
    now[0] = 10.0 + feed.PRIMARY_RETRY_S
    back = src("s1")
    assert back["events"][0]["text"] == "the dashboard answers again: back to the normal feed"


def test_resilient_source_all_down():
    def down(name):
        def f(cursor):
            raise feed.FeedUnavailable(f"{name} is down")

        return f

    src = feed.resilient_source(down("dashboard"), [("db", down("database")), ("snap", down("snapshot"))],
                                hint="polymath doctor")  # fmt: skip
    with pytest.raises(feed.FeedUnavailable) as exc:
        src(None)
    assert str(exc.value) == ("dashboard is down; db: database is down; snap: snapshot is down "
                              "· fix: polymath doctor")  # fmt: skip


def test_cli_feed_uses_the_snapshot_when_dashboard_and_database_are_unreadable(tmp_path, capsys, monkeypatch):
    cfg_file = tmp_path / "polymath.toml"
    make_config(tmp_path)  # writes polymath.toml
    run = tmp_path / "run"
    run.mkdir()
    snapshot.write(run / "feed.json", {"written": time.time(), "seq": 1, "status": {"online": True,
                   "state": "running", "activity": "reasoning"},
                   "events": [{"kind": "note", "what": "net", "status": "offline", "text": "no internet", "at": 1.0,
                               "seq": 1}]})  # fmt: skip
    monkeypatch.setenv("POLYMATH_RUN_DIR", str(run))
    rc = cli.main(["--config", str(cfg_file), "feed", "--url", f"http://127.0.0.1:{free_port()}", "--once",
                   "--plain", "--no-color"])  # fmt: skip
    out = capsys.readouterr().out
    assert rc == 0 and "showing the agent's own snapshot instead" in out and "no internet" in out


# ------------------------------------------------------------------------------------------- feed display
def test_header_never_shows_false_zero_counts_and_shows_offline():
    st = {"online": True, "state": "running", "activity": "reasoning", "counts": {"rules": 4},
          "net": {"online": False, "since": time.time() - 600}}  # fmt: skip
    lines = feed.status_lines(st, width=0)
    flat = ["".join(t for t, _ in line) for line in lines]
    assert "learning offline" in flat[0]
    assert "no internet since" in flat[1] and "studying what it has" in flat[1]
    assert "counting what it knows" in flat[2] and "0 facts" not in flat[2]
    st["counts"] = {"documents": 5, "entities": 3, "facts": 9, "rules": 4, "approx": True}
    assert "~9 facts" in "".join(t for t, _ in feed.status_lines(st, width=0)[2])
    st["counts"] = {"documents": 5, "entities": 3, "facts": 9, "inferred": 1, "disputed": 0, "rules": 4}
    st["net"] = {"online": True}
    flat = ["".join(t for t, _ in line) for line in feed.status_lines(st, width=0)]
    assert "learning" in flat[0] and "offline" not in flat[0] and "9 facts" in flat[2]


def test_net_notes_render_and_move_the_face():
    segs = feed.render_event({"kind": "note", "what": "net", "status": "offline", "text": "gone", "at": time.time()})
    assert any("offline" in t for t, _ in segs)
    face = Face()
    face.update({"online": True, "state": "running", "net": {"online": False}})
    assert face.mood(time.time()) == "studying"
    face.see({"kind": "note", "what": "net", "status": "online"}, now=100.0)
    assert face.mood(101.0) == "happy"


def test_feed_counts_sources(kb_db):
    assert "facts" in feed.Feed(kb_db).status()["counts"]
    assert "facts" not in feed.Feed(kb_db, heavy_counts=False).status()["counts"]
    st = feed.Feed(kb_db, counts_source=lambda: {"facts": 7}).status()
    assert st["counts"]["facts"] == 7 and "rules" in st["counts"]
    assert "facts" not in feed.Feed(kb_db, counts_source=lambda: None).status()["counts"]
    assert feed.Feed(kb_db).status()["net"] == {"online": True}


@pytest.fixture()
def kb_db(tmp_path):
    cfg = make_config(tmp_path)
    db = open_database(cfg.paths.db_path)
    with db.transaction():
        build(db, n=4)
    yield db
    db.close()


# ------------------------------------------------------------------------------------------- dashboard
def test_limit_queries_interrupts_and_lifts(tmp_path):
    db = open_database(tmp_path / "x.sqlite3")
    slow = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT COUNT(*) FROM c"
    db.limit_queries(0.2)
    t0 = time.monotonic()
    with pytest.raises(sqlite3.OperationalError, match="interrupted"):
        db.scalar(slow)
    assert time.monotonic() - t0 < 2
    db.limit_queries(None)
    assert db.scalar("SELECT COUNT(*) FROM (WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c "
                     "WHERE x < 300000) SELECT x FROM c)") == 300000  # fmt: skip
    db.close()


@pytest.fixture()
def dashboard(tmp_path):
    cfg = make_config(tmp_path)
    db = open_database(cfg.paths.db_path)
    with db.transaction():
        build(db, n=4)
    db.close()
    beat(cfg)
    server = make_server(cfg, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield cfg, server
    server.shutdown()
    server.server_close()


def test_a_slow_question_never_blocks_the_feed(dashboard, monkeypatch):
    """Loading the answerer used to hold the lock every request needed: one question froze the feed."""
    _cfg, server = dashboard
    port = server.server_address[1]
    from polymath.perception import entities

    started = threading.Event()

    def slow_load(self):
        started.set()
        time.sleep(3)
        return None

    monkeypatch.setattr(entities.AliasIndex, "load", slow_load)
    asker = threading.Thread(target=req, args=(port, "POST", "/api/ask", json.dumps({"question": "what is x"}),
                                               {"Content-Type": "application/json"}), daemon=True)  # fmt: skip
    asker.start()
    assert started.wait(5)
    t0 = time.monotonic()
    status, _h, body = req(port, "GET", "/api/feed")
    assert status == 200 and time.monotonic() - t0 < 1.5 and json.loads(body)["events"]
    asker.join(10)


def test_a_slow_page_is_stopped_not_left_hanging(dashboard, monkeypatch):
    _cfg, server = dashboard
    port = server.server_address[1]
    from polymath.interface import dashboard as dash

    monkeypatch.setattr(dash, "DEFAULT_LIMIT_S", 0.3)

    def slow_overview(self):
        self.db().scalar("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT COUNT(*) FROM c")
        return {}

    monkeypatch.setattr(DashboardData, "overview", slow_overview)
    t0 = time.monotonic()
    status, _h, body = req(port, "GET", "/api/overview")
    assert status == 503 and "busy" in json.loads(body)["error"] and time.monotonic() - t0 < 3
    status, _h, _b = req(port, "GET", "/api/feed")  # the same worker's connection works again afterwards
    assert status == 200


def test_counts_come_from_the_background(dashboard):
    _cfg, server = dashboard
    port = server.server_address[1]
    server.data.count_now()
    st = json.loads(req(port, "GET", "/api/feed")[2])["status"]
    assert st["counts"]["facts"] > 0 and st["counts"]["documents"] > 0


def test_many_requests_at_once(dashboard):
    _cfg, server = dashboard
    port = server.server_address[1]
    results: list[int] = []

    def hit():
        results.append(req(port, "GET", "/api/feed")[0])

    threads = [threading.Thread(target=hit) for _ in range(30)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert results == [200] * 30


def test_watchdog_pings_only_while_the_server_answers(dashboard):
    _cfg, server = dashboard
    sent: list[str] = []

    class FakeNotifier:
        ping_every = 0.05

        def watchdog(self, force: bool = False) -> bool:
            sent.append("WATCHDOG")
            return True

    stop = threading.Event()
    t = threading.Thread(target=watchdog_loop, args=(server, FakeNotifier(), stop), daemon=True)
    t.start()
    time.sleep(0.5)
    assert sent
    stop.set()
    t.join(2)
    assert not answers(f"http://127.0.0.1:{free_port()}/health", timeout=1)


def test_health_answers_even_without_the_agent(dashboard):
    cfg, server = dashboard
    beat(cfg, age=3600)
    status, _h, body = req(server.server_address[1], "GET", "/health")
    assert status == 503 and json.loads(body)["ok"] is False
    assert answers(f"http://127.0.0.1:{server.server_address[1]}/health")  # a 503 still counts as answering


# ------------------------------------------------------------------------------------------- doctor
def test_doctor_checks(tmp_path):
    cfg = make_config(tmp_path)
    cfg.paths.run_dir = tmp_path / "run"
    cfg.paths.data_dir.mkdir(parents=True, exist_ok=True)

    def fine(url):
        return "answered", 0.2, {"ok": True}

    checks = {c.name: c for c in doctor.run_checks(cfg, "http://x", health=fine)}
    assert checks["dashboard"].level == doctor.OK and checks["agent"].level == doctor.WARN  # no snapshot yet
    cfg.paths.run_dir.mkdir()
    snapshot.write(snapshot.path_for(cfg), {"written": time.time(), "status": {"activity": "reasoning",
                   "net": {"online": False, "since": time.time() - 60}}})  # fmt: skip
    checks = {c.name: c for c in doctor.run_checks(cfg, "http://x", health=lambda u: ("refused", 0.0, None))}
    assert checks["agent"].level == doctor.OK and checks["internet"].level == doctor.WARN
    assert checks["dashboard"].level == doctor.BAD and "restart polymath-dashboard" in checks["dashboard"].fix
    checks = {c.name: c for c in doctor.run_checks(cfg, "http://x", health=lambda u: ("timeout", 6.0, None))}
    assert "hung or overloaded" in checks["dashboard"].detail
    snapshot.write(snapshot.path_for(cfg), {"written": time.time() - 900, "status": {}})
    checks = {c.name: c for c in doctor.run_checks(cfg, "http://x", health=fine)}
    assert checks["agent"].level == doctor.BAD and "restart polymath" in checks["agent"].fix
    (cfg.paths.data_dir / "MOVED-TO-DRIVE.json").write_text(json.dumps({"name": "Samsung T7"}))
    checks = {c.name: c for c in doctor.run_checks(cfg, "http://x", health=fine)}
    assert checks["brain"].level == doctor.BAD and "Samsung T7" in checks["brain"].detail
    lines = doctor.render(list(checks.values()), ascii_only=True)
    assert lines[-1].endswith("to fix (above)") and any(line.startswith("XX brain") for line in lines)
    assert doctor.render([doctor.Check("x", doctor.OK, "fine")])[-1] == "everything is working"


def test_doctor_http_probe_and_cli(tmp_path, capsys, local_server):
    cfg_file = tmp_path / "polymath.toml"
    make_config(tmp_path)
    kind, _secs, _b = doctor.http_health(f"http://127.0.0.1:{free_port()}")
    assert kind == "refused"
    rc = cli.main(["--config", str(cfg_file), "doctor", "--url", f"http://127.0.0.1:{free_port()}"])
    out = capsys.readouterr().out
    assert rc == 1 and "dashboard" in out and "systemctl restart polymath-dashboard" in out
    rc = cli.main(["--config", str(cfg_file), "doctor", "--json", "--url", f"http://127.0.0.1:{free_port()}"])
    assert rc == 1 and {"name", "level", "detail", "fix"} <= set(json.loads(capsys.readouterr().out)[0])


# ------------------------------------------------------------------------------------------- deployment
ROOT = Path(__file__).resolve().parent.parent


def test_units_restart_forever_and_watch_the_dashboard():
    agent = (ROOT / "deploy/polymath.service").read_text()
    dash = (ROOT / "deploy/polymath-dashboard.service").read_text()
    for unit in (agent, dash):
        assert "StartLimitIntervalSec=0" in unit and "Restart=always" in unit and "RestartMaxDelaySec=" in unit
    assert "RuntimeDirectory=polymath" in agent and "RuntimeDirectoryMode=0755" in agent
    assert "Type=notify" in dash and "WatchdogSec=60" in dash and "MemoryMax=1200M" in dash
    cli_wrapper = (ROOT / "deploy/polymath-cli").read_text()
    assert "feed|doctor|" in cli_wrapper
