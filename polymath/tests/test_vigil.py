"""v0.8.2: busy, not offline. The vigil, slow-step reports, and the efficiency fixes found while measuring."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from polymath.agents.caretaker import Orin, docs_since
from polymath.agents.faults import for_issue
from polymath.body.systemd_notify import Notifier
from polymath.core import loop as loop_mod
from polymath.core import vigil
from polymath.core.app import build_components
from polymath.core.db import open_database
from polymath.core.jobs import JobContext, JobOutcome
from polymath.core.loop import Agent
from polymath.interface import cli, feed
from polymath.interface.dashboard import DashboardData
from polymath.interface.feed import Feed, status_lines
from polymath.memory import text_index
from polymath.memory.documents import Document, DocumentStore
from polymath.memory.text_index import TextIndex
from tests.conftest import make_config


def text_of(segs) -> str:
    return "".join(t for t, _s in segs)


def hung(v: vigil.Vigil) -> bool:  # read through a call: the checker would keep an earlier assert's narrowing
    return v.hung


def running(v: vigil.Vigil) -> bool:
    return v._thread is not None


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


# ------------------------------------------------------------------ the vigil itself
def test_the_vigil_vouches_while_the_main_thread_makes_progress(tmp_path):
    mono, wall, pings, pulses = Clock(), Clock(), [], []
    v = vigil.Vigil(ping=lambda: pings.append(1), pulse=lambda: pulses.append(1), run_dir=tmp_path,
                    hard_s=600, clock=mono, wall=wall)  # fmt: skip
    v.enter("eval.quiz", 42)
    assert v.beat() and len(pings) == len(pulses) == 1
    data = json.loads((tmp_path / vigil.FILE).read_text())
    assert (data["what"], data["job"], data["since"]) == ("eval.quiz", 42, 1000.0)
    assert vigil.read(tmp_path, now=1010.0)["what"] == "eval.quiz"  # type: ignore[index]
    mono.t += 300
    v.progress()  # a tick: progress
    mono.t += 599
    assert v.beat() and not hung(v)
    assert v.section() == {"took": 899.0, "silent": 599.0}
    mono.t += 2  # 601 s without progress: hung
    assert not v.beat() and hung(v) and len(pings) == 2  # it stops vouching, so systemd restarts the agent
    assert not v.beat() and len(pings) == 2
    v.progress()  # it moved again after all
    assert v.beat() and not hung(v) and len(pings) == 3
    v.enter("")  # between sections: a fresh start
    assert v.section() == {"took": 0.0, "silent": 0.0} and v.state()["what"] == ""


def test_the_pulse_file_is_read_only_while_fresh_and_never_breaks(tmp_path):
    assert vigil.read(tmp_path) is None
    (tmp_path / vigil.FILE).write_text("{not json")
    assert vigil.read(tmp_path) is None
    (tmp_path / vigil.FILE).write_text("[1, 2]")
    assert vigil.read(tmp_path) is None
    (tmp_path / vigil.FILE).write_text(json.dumps({"at": "soon"}))
    assert vigil.read(tmp_path) is None
    (tmp_path / vigil.FILE).write_text(json.dumps({"at": 100.0, "what": "x"}))
    assert vigil.read(tmp_path, now=130.0, stale=60) == {"at": 100.0, "what": "x", "since": 130.0}
    assert vigil.read(tmp_path, now=200.0, stale=60) is None  # an old pulse: the agent is gone
    no_dir = vigil.Vigil(ping=lambda: None, pulse=lambda: None, run_dir=None)
    no_dir.write(no_dir.state())  # no runtime directory (development): nothing written, nothing raised
    gone = vigil.Vigil(ping=lambda: None, pulse=lambda: None, run_dir=tmp_path / "missing")
    assert gone.beat()


def test_the_vigil_thread_starts_beats_and_cleans_up(tmp_path):
    pings: list[int] = []
    v = vigil.Vigil(ping=lambda: pings.append(1), pulse=lambda: 1 / 0, run_dir=tmp_path, every=0.01)
    v.start()
    v.start()  # once is enough
    deadline = time.monotonic() + 5
    while not pings and time.monotonic() < deadline:
        time.sleep(0.01)
    v.stop()
    assert pings and not (tmp_path / vigil.FILE).exists()  # a failing pulse never stops the pings


def test_the_notifier_keeps_only_its_latest_messages():
    n = Notifier({"NOTIFY_SOCKET": "relative"})  # not a usable socket: nothing is sent
    assert n.sent.maxlen == 64
    n.sent.extend(["WATCHDOG"] * 500)
    assert len(n.sent) == 64


# ------------------------------------------------------------------ in the agent loop
def _agent(config, db) -> Agent:
    return Agent(config, db, build_components(config, db).registry, notifier=Notifier({}))


def test_a_silent_slow_step_is_vouched_for_recorded_and_reported(config, db, monkeypatch):
    config.paths.run_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(loop_mod, "SILENT_REPORT_S", 0.05)
    agent = _agent(config, db)
    seen: dict[str, Any] = {}

    def slow(ctx: JobContext) -> JobOutcome:
        seen["state"] = agent.vigil.state()
        time.sleep(0.12)  # one long step, no tick
        return JobOutcome(done=True)

    agent.registry.register("t.slow", slow, "a slow step")
    agent.scheduler.enqueue("t.slow", {}, key="slow1", priority=9)
    agent.start()
    assert running(agent.vigil)  # the vigil runs with the agent
    rec = agent.cycle()
    assert rec.status == "done" and seen["state"]["what"] == "t.slow" and seen["state"]["job"] is not None
    assert agent.vigil.state()["what"] == ""  # the section ended with the slice
    slow_steps = db.kv_get("slow_steps")
    assert slow_steps["t.slow"]["n"] == 1 and slow_steps["t.slow"]["silent"] >= 0.1
    issue = db.one("SELECT text, detail FROM events WHERE kind = 'issue' ORDER BY id DESC LIMIT 1")
    assert issue is not None and "t.slow went 0 s without a sign of life" in issue["text"]
    assert "job-slow" in json.loads(issue["detail"])["known"] and [f.id for f in for_issue("jobs/slow:t.slow")] == [
        "job-slow"
    ]
    agent.scheduler.enqueue("t.slow", {}, key="slow2", priority=9)
    agent.cycle()
    assert db.kv_get("slow_steps")["t.slow"]["n"] == 2  # recorded every time, reported once a day
    assert db.scalar("SELECT COUNT(*) FROM events WHERE kind='issue' AND text LIKE 't.slow%'") == 1
    agent.shutdown()
    assert not running(agent.vigil) and not (config.paths.run_dir / vigil.FILE).exists()


def test_a_ticking_step_and_a_quick_one_are_not_reported(config, db, monkeypatch):
    monkeypatch.setattr(loop_mod, "SILENT_REPORT_S", 0.05)
    agent = _agent(config, db)

    def ticking(ctx: JobContext) -> JobOutcome:
        for _ in range(6):
            time.sleep(0.02)
            ctx.tick()  # progress all along
        return JobOutcome(done=True)

    agent.registry.register("t.ticking", ticking, "ticks")
    agent.scheduler.enqueue("t.ticking", {}, key="tick1", priority=9)
    assert agent.cycle().status == "done"
    assert not db.kv_get("slow_steps")
    agent._slow_step("", {"took": 999.0, "silent": 999.0})  # no kind: nothing to say
    agent._slow_step("t.long", {"took": 400.0, "silent": 0.0})  # busy for minutes, ticking: reported as busy
    assert "kept the agent busy for 6.7 min" in str(db.scalar("SELECT text FROM events WHERE kind='issue'"))


def test_planners_and_the_checkpoint_are_sections_too(config, db):
    agent = _agent(config, db)
    seen: list[str] = []

    def planner(a: Any) -> None:
        seen.append(a.vigil.state()["what"])

    agent.planners = [(0.0, planner)]
    agent._planner_due = [0.0]
    agent.observe()
    assert seen and seen[0].startswith("planner tests.test_vigil.planner")
    assert agent.vigil.state()["what"] == ""


# ------------------------------------------------------------------ what the feed and the dashboard say
def _alive_but_busy(config, db, *, what: str, since_ago: float, hb_ago: float = 300.0) -> None:
    now = time.time()
    db.kv_set("heartbeat", {"ts": now - hb_ago, "state": "running", "cycle": 7})
    config.paths.run_dir.mkdir(parents=True, exist_ok=True)
    (config.paths.run_dir / vigil.FILE).write_text(json.dumps({"pid": 1, "at": now, "what": what, "job": 3,
                                                               "since": now - since_ago}))  # fmt: skip


def test_the_feed_says_busy_not_offline(config, db):
    _alive_but_busy(config, db, what="eval.quiz", since_ago=250)
    st = Feed(db, heavy_counts=False, run_dir=config.paths.run_dir).status()
    assert st["online"] and st["activity"] == "quizzing itself" and st["action"] == "eval.quiz"
    assert st["long_step"] == {"what": "eval.quiz", "seconds": 250}
    now = text_of(status_lines(st)[1])
    assert now == " NOW   Quizzing itself  (one long step: 4 min so far — busy, not stuck)"
    assert "● learning" in text_of(status_lines(st)[0])
    assert not Feed(db, heavy_counts=False).status()["online"]  # without the pulse it looked offline (v0.8.1)
    _alive_but_busy(config, db, what="checkpoint", since_ago=40)
    assert "tidying its database" in Feed(db, heavy_counts=False, run_dir=config.paths.run_dir).status()["activity"]
    _alive_but_busy(config, db, what="planner perception.jobs.planner", since_ago=5)
    st = Feed(db, heavy_counts=False, run_dir=config.paths.run_dir).status()
    assert st["activity"] == "planning its next work" and "long_step" not in st
    db.kv_set("heartbeat", {"ts": time.time() - 300, "state": "stopped"})
    assert not Feed(db, heavy_counts=False, run_dir=config.paths.run_dir).status()["online"]  # stopped is stopped
    assert feed._duration(45) == "45 s" and feed._duration(600) == "10 min"


def test_the_dashboard_and_the_cli_know_it_is_busy(config, db):
    _alive_but_busy(config, db, what="body.backup", since_ago=600)
    ok, health = DashboardData(config).health()
    assert ok and health["heartbeat_age_s"] < 5
    assert cli.agent_running(config)  # so the CLI hands work to its inbox instead of fighting for the lock
    (config.paths.run_dir / vigil.FILE).unlink()
    assert not cli.agent_running(config)
    db.kv_set("slow_steps", {"body.backup": {"took": 600.0, "silent": 90.0, "n": 1, "at": 0}})
    assert cli.status_dict(db)["slow_steps"]["body.backup"]["silent"] == 90.0


# ------------------------------------------------------------------ efficiency
def test_reading_rate_is_counted_without_scanning(db):
    assert docs_since(db, 0) == 0
    store = DocumentStore(db)
    for i in range(40):
        store.add(Document(source="web", external_id=f"d{i}", title=f"T{i}", text=f"text {i}", license="CC"))
    stamps = [1000.0 + 10 * i for i in range(40)]
    for i, t in enumerate(stamps, start=1):
        db.execute("UPDATE documents SET fetched=? WHERE id=?", (t, i))
    db.execute("DELETE FROM documents WHERE id IN (5, 6, 30)")  # gaps in the ids
    for since in (0.0, 1000.0, 1055.0, 1200.0, 1385.0, 1390.0, 1391.0, 9999.0):
        brute = db.scalar("SELECT COUNT(*) FROM documents WHERE fetched >= ?", (since,))
        assert docs_since(db, since) == brute, since


def test_orin_does_not_queue_work_already_on_its_way(tmp_path):
    cfg = make_config(tmp_path)
    db = open_database(cfg.paths.db_path)
    try:
        now = time.time()
        orin = Orin(cfg, db, clock=lambda: now)
        orin.scheduler.enqueue("body.backup", {}, key="body.backup@night", not_before=now + 600)
        assert orin.remedy("backup", {"title": "x", "key": "backup_stale"}) == "a backup is already on its way"
        assert db.scalar("SELECT COUNT(*) FROM jobs WHERE kind='body.backup'") == 1
        db.execute("UPDATE jobs SET not_before=? WHERE kind='body.backup'", (now + 20 * 3600,))  # due tomorrow
        assert orin.remedy("backup", {"title": "x", "key": "backup_stale"}) == "asked for a backup now"
        assert db.scalar("SELECT COUNT(*) FROM jobs WHERE kind='body.backup'") == 2
        orin.scheduler.enqueue("net.probe", {}, key="probe-running")
        db.execute("UPDATE jobs SET state='running' WHERE kind='net.probe'")
        assert orin.remedy("probe", {"title": "x", "key": "offline"}) == "an internet check is already on its way"
    finally:
        db.close()


def _docs(db: Any, n: int) -> DocumentStore:
    store = DocumentStore(db)
    for i in range(n):
        store.add(Document(source="web", external_id=f"w{i}", title=f"Walrus {i}", license="CC",
                           text=f"The walrus number {i} lives in the Arctic sea. " * 3))  # fmt: skip
    return store


@pytest.fixture()
def fresh_cache(monkeypatch):
    cache = text_index._TextCache()
    monkeypatch.setattr(text_index, "TEXTS", cache)
    return cache


def test_search_keeps_decoded_documents_and_counts_without_opening_them(db, fresh_cache, monkeypatch):
    store = _docs(db, 5)
    tindex = TextIndex(db, store)
    for r in db.query("SELECT id, title FROM documents"):
        doc = store.get(int(r["id"]))
        assert doc is not None
        tindex.index(int(r["id"]), str(r["title"]), doc.text)
    opened: list[int] = []
    real_get = DocumentStore.get

    def counting_get(self: DocumentStore, doc_id: int) -> Any:
        opened.append(doc_id)
        return real_get(self, doc_id)

    monkeypatch.setattr(DocumentStore, "get", counting_get)
    first = tindex.search("walrus arctic", limit=10)
    assert first and len(opened) == 5 and fresh_cache.misses == 5
    again = tindex.search("walrus arctic", limit=10)
    assert [h.text for h in again] == [h.text for h in first] and len(opened) == 5 and fresh_cache.hits == 5
    assert tindex.count("walrus arctic", limit=10) == len(first) and len(opened) == 5  # counting opens nothing
    assert tindex.count("", limit=10) == 0
    # a changed document is read again (its content hash is part of the key): never a stale passage
    doc_id = first[0].doc_id
    db.execute("UPDATE documents SET content_hash='changed' WHERE id=?", (doc_id,))
    tindex.search("walrus arctic", limit=10)
    assert opened.count(doc_id) == 2


def test_the_text_cache_stays_within_its_budget():
    c = text_index._TextCache(budget=1000)  # one entry may use at most an eighth: 125 characters
    c.put(("db", 1, "h", "lzma"), "x" * 120)
    for i in range(2, 9):
        c.put(("db", i, "h", "lzma"), "y" * 120)
    assert c.size == 960 and c.get(("db", 1, "h", "lzma")) == "x" * 120  # read: now the most recent
    c.put(("db", 9, "h", "lzma"), "z" * 126)  # too big for one entry: never kept
    assert c.get(("db", 9, "h", "lzma")) is None and c.size == 960
    c.put(("db", 10, "h", "lzma"), "w" * 100)  # over budget: the least recently used goes
    assert c.size <= 1000 and c.get(("db", 2, "h", "lzma")) is None and c.get(("db", 1, "h", "lzma")) is not None
    c.put(("db", 10, "h", "lzma"), "v" * 5)  # replaced, not counted twice
    assert c.get(("db", 10, "h", "lzma")) == "v" * 5 and c.size == 845


def test_embedding_training_reads_its_vocabulary_every_quarter_hour(config, db, monkeypatch):
    from polymath.perception import embed_jobs
    from polymath.perception.embeddings import Vocab

    calls: list[int] = []

    def from_db(cls: Any, db: Any, *, min_count: int, max_size: int) -> Vocab:
        calls.append(1)
        return Vocab(["a", "b"], __import__("numpy").array([2, 1]))

    monkeypatch.setattr(Vocab, "from_db", classmethod(from_db))
    ctx = JobContext(config=config, db=db, scheduler=None, job=None, deadline=0.0,  # type: ignore[arg-type]
                     stop_event=__import__("threading").Event(), services={})  # fmt: skip
    clock = [100.0]
    monkeypatch.setattr("polymath.perception.embed_jobs.time.monotonic", lambda: clock[0])
    v1 = embed_jobs._vocab(ctx)
    assert embed_jobs._vocab(ctx) is v1 and len(calls) == 1
    clock[0] += embed_jobs.VOCAB_TTL + 1
    assert embed_jobs._vocab(ctx) is not v1 and len(calls) == 2


def test_the_learning_lines_look_only_at_the_newest_documents(db):
    from polymath.interface import focus

    store = DocumentStore(db)
    store.add(Document(source="gutenberg", external_id="g1", title="Moby Dick", text="Call me Ishmael.", license="PD"))
    now = time.time()
    assert focus._newest_item(db, "gutenberg", now - 60, now) == "Moby Dick"
    for i in range(focus.NEWEST + 5):  # a burst of other reading pushes it out of the window it looks at
        store.add(Document(source="web", external_id=f"x{i}", title=f"Page {i}", text=f"page {i}", license="CC"))
    assert focus._newest_item(db, "gutenberg", now - 60, now) == ""
    assert focus._newest_item(db, "web", now - 60, now) == f"Page {focus.NEWEST + 4}"
