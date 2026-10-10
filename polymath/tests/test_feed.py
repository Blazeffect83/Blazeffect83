"""Live learning feed: events since a cursor, caps, status, rendering, the terminal client, CLI and dashboard."""

from __future__ import annotations

import io
import json
import socket
import threading
import time
from typing import Any

import pytest

from polymath.agents import society, store
from polymath.core.app import build_components
from polymath.core.db import Database, open_database
from polymath.interface import cli, feed
from polymath.interface.dashboard import make_server
from polymath.interface.feed import (
    ACTIVITY,
    Feed,
    FeedUnavailable,
    Screen,
    format_cursor,
    parse_cursor,
    render_event,
    status_lines,
)
from polymath.memory.documents import Document, DocumentStore
from polymath.memory.graph import KnowledgeGraph
from tests.conftest import make_config
from tests.fixtures.kb import build
from tests.fixtures.typing import some


def text_of(segs) -> str:
    return "".join(t for t, _s in segs)


def kinds(events) -> list[str]:
    return [e["kind"] for e in events]


# ------------------------------------------------------------------ cursor
def test_cursor_round_trip_and_malformed():
    c = {"d": 12, "t": 3400, "c": 9}
    assert parse_cursor(format_cursor(c)) == c
    assert format_cursor(c) == "d12.t3400.c9"  # stream order, not dict order
    assert parse_cursor("d1.x5") == {"d": 1}  # a stream from a newer version is ignored
    for bad in ("", None, "d", "d-1", "D1", "d1..t2", "d1;DROP"):
        assert parse_cursor(bad) is None


# ------------------------------------------------------------------ server side
def test_first_poll_shows_recent_backlog_then_only_new(db):
    kb = build(db)
    f = Feed(db)
    first = f.poll(None)
    reads = [e for e in first["events"] if e["kind"] == "read"]
    assert len(reads) == 4 and reads[-1]["title"] == "Person4"  # newest documents, oldest first
    facts = [e for e in first["events"] if e["kind"] == "fact"]
    assert facts and all(e["how"] in {"Wikidata", "infobox"} for e in facts)
    born = {e["s"]: e for e in facts if e["p"] == "date of birth"}
    assert sorted(born) == [f"Person{j}" for j in range(5)]  # the five newest facts, capped at five
    assert born["Person4"]["o"] == "1914-05-05" and born["Person4"]["how"] == "infobox"
    assert born["Person4"]["why"] == "Person4"  # the infobox's article
    again = f.poll(first["cursor"])
    assert again["events"] == [] and again["cursor"] == first["cursor"]
    # something new arrives: exactly that is reported, once
    g = KnowledgeGraph(db)
    g.add_triple(kb.countries[3], kb.preds["country"], o=kb.countries[4], kind="wikidata", source="wikidata")
    nxt = f.poll(again["cursor"])
    assert kinds(nxt["events"]) == ["fact"]
    assert nxt["events"][0] | {"at": 0} == {
        "kind": "fact", "at": 0, "s": "Country03", "p": "country", "o": "Country04", "conf": 0.6,
        "how": "Wikidata", "why": "",
    }  # fmt: skip
    assert f.poll(nxt["cursor"])["events"] == []


def test_busy_streams_are_capped_and_counted(db, monkeypatch):
    kb = build(db, n=10)
    f = Feed(db)
    cur = f.poll(None)["cursor"]
    docs = DocumentStore(db)
    for i in range(30):
        docs.add(Document("feeds", f"item{i}", f"News item {i}", f"Body of news item {i} " * 5, "CC BY 4.0"))
    g = KnowledgeGraph(db)
    unnamed = [g.stub(f"Q{900_000 + i}") for i in range(40)]
    for i, o in enumerate(unnamed):  # facts about things it has not read about yet: counted, not shown
        g.add_triple(kb.countries[i % 10], kb.preds["country"], o=o, kind="wikidata", source="wikidata")
    for i in range(8):
        g.add_triple(kb.cities[i], kb.preds["capital"], o=kb.countries[i], kind="rule", source="rule",
                     status="inferred", detail=f"inverse 77: #{i} + #{i + 1}")  # fmt: skip
    events = f.poll(cur)["events"]
    reads = [e for e in events if e["kind"] == "read"]
    assert [e["title"] for e in reads] == [f"News item {i}" for i in range(26, 30)]
    more = {e["what"]: e for e in events if e["kind"] == "more"}
    assert more["documents"]["n"] == 26
    inferred = [e for e in events if e["kind"] == "inferred"]
    assert len(inferred) == feed.CAPS["inferred"] and all(e["how"] == "inverse rule" for e in inferred)
    assert not [e for e in events if e["kind"] == "fact"]  # every sourced fact had an unnamed object
    assert more["facts"]["n"] == 48 - 3 and more["facts"]["detail"] == "5 inferred"
    # huge spans are estimated, not counted row by row
    monkeypatch.setattr(feed, "EXACT_COUNT_SPAN", 5)
    est = {e["what"]: e for e in f.poll(cur)["events"] if e["kind"] == "more"}
    assert est["documents"]["n"] == 26 and est["facts"]["n"] == 48 - 3 and est["facts"]["detail"] == ""


def test_text_facts_quote_the_sentence_and_literals_render(db):
    kb = build(db, n=4)
    f = Feed(db)
    cur = f.poll(None)["cursor"]
    g = KnowledgeGraph(db)
    g.add_triple(kb.people[0], kb.preds["country"], o=kb.countries[1], kind="pattern", source="text",
                 doc_id=kb.docs["Country01"], detail="pattern 'was born in' (0.82): Person0 was born in Country01 long ago.")  # fmt: skip
    g.add_triple(kb.countries[2], kb.preds["population"], value={"amount": 2_500_000, "unit": None},
                 kind="infobox", source="wikipedia", doc_id=kb.docs["Country02"])  # fmt: skip
    g.add_triple(kb.countries[3], kb.preds["capital"], o=kb.cities[0], kind="wikidata", source="other",
                 status="disputed")  # fmt: skip
    ev = {e["s"]: e for e in f.poll(cur)["events"] if e["kind"] in {"fact", "disputed"}}
    assert ev["Person0"]["how"] == "read in text" and ev["Person0"]["why"] == "Person0 was born in Country01 long ago."
    assert ev["Country02"]["o"] == "2,500,000" and ev["Country02"]["why"] == "Country02"
    assert ev["Country03"]["kind"] == "disputed"


def test_reasoning_quiz_agents_requests_reports_backups_failures(db, config):
    kb = build(db, n=6)
    f = Feed(db)
    cur = f.poll(None)["cursor"]
    now = time.time()
    db.execute(
        "INSERT INTO rules(kind, p, q, confidence, support, updated) VALUES('inverse', ?, ?, 0.98, 400, ?)",
        (kb.preds["capital"], kb.preds["country"], now),
    )
    db.execute("INSERT INTO rules(kind, p, q, confidence, support, updated) VALUES('domain', ?, ?, 0.9, 300, ?)",
               (kb.preds["capital"], kb.countries[0], now))  # fmt: skip
    db.execute("INSERT INTO rules(kind, p, q, confidence, support, updated) VALUES('symmetric', ?, NULL, 0.9, 200, ?)",
               (kb.preds["country"], now))  # fmt: skip
    for kind, res in (
        ("rules", {"inverse": 1, "transitive": 0, "domain": 1, "symmetric": 1}),
        ("infer", {"new": 1234, "examined": 5000, "violations": 3}),
        ("contradictions", {"groups_checked": 9, "conflicts": 2, "disputed": 1, "settled": 1}),
        ("reliability", {"facts": 10, "sources": {"wikidata": 0.9, "text": 0.6}}),
    ):
        db.execute("INSERT INTO reasoning_runs(kind, started, ended, result) VALUES(?,?,?,?)",
                   (kind, now - 2, now, json.dumps(res)))  # fmt: skip
    db.execute("INSERT INTO reasoning_runs(kind, started, ended, result) VALUES('other', ?, ?, 'not json')", (now, now))
    qid = db.execute(
        "INSERT INTO quizzes(created, n, correct, accuracy, chance, details) VALUES(?, 2, 1, 0.5, 0.25, '{}')", (now,)
    ).lastrowid
    opts = json.dumps([{"entity": kb.cities[0], "label": "Capitol00"}, {"entity": kb.cities[1], "label": "Capitol01"}])
    for chosen in (kb.cities[0], kb.cities[1]):
        db.execute(
            "INSERT INTO quiz_answers(quiz_id, triple_id, question, options, answer, chosen, correct, method, "
            "confidence) VALUES(?, 1, 'What is the capital of Country00?', ?, ?, ?, ?, 'graph', 0.8)",
            (qid, opts, kb.cities[0], chosen, int(chosen == kb.cities[0])),
        )
    a = society.spawn(db, "verify capitals of countries", name="caps")
    tid = store.open_task(db, some(store.get(db, a.id)), kind="user", action="answer", target="t1",
                          payload={"text": "What is the capital of Country02?"})  # fmt: skip
    store.reward(db, some(tid), 2.0, "you said correct", correct=True)
    db.execute("INSERT INTO jobs(kind, key, payload, state, created, updated) VALUES('drive.learn', 'l1', ?, 'queued', ?, ?)",
               (json.dumps({"query": "black holes"}), now, now))  # fmt: skip
    db.execute("INSERT INTO reports(created, day, path, summary) VALUES(?, '2026-10-10', '/r.md', ?)",
               (now, json.dumps({"quiz": {"accuracy": 0.8}, "documents": 1234})))  # fmt: skip
    db.execute("INSERT INTO backups(created, path, bytes, ok, detail) VALUES(?, '/b.xz', 123456789, 1, 'ok')", (now,))
    db.execute("INSERT INTO backups(created, path, bytes, ok, detail) VALUES(?, '', 0, 0, 'disk full')", (now,))
    db.execute(
        "INSERT INTO cycles(started, ended, action, status, detail) VALUES(?, ?, 'wikipedia.part', 'failed', ?)",
        (now, now, json.dumps({"error": "TimeoutError: read timed out", "state": "queued"})),
    )
    out = f.poll(cur)
    ev = out["events"]
    by: dict[str, list[dict[str, Any]]] = {}
    for e in ev:
        by.setdefault(e["kind"], []).append(e)
    rules = next(e for e in by["reason"] if e["what"] == "rules")
    assert rules["examples"] == ["capital ⇄ country", "whatever has a capital is a Country00", "country goes both ways"]
    texts = [text_of(render_event(e)) for e in ev]
    joined = "\n".join(texts)
    for needle in (
        "learned rules: 1 inverse, 1 domain, 1 symmetric  e.g. capital ⇄ country",
        "inferred 1,234 new facts from 5,000 examined, flagged 3 type violations",
        "checked 9 claims: 2 conflicts, 1 disputed, 1 settled",
        "source trust: wikidata 0.90, text 0.60",
        "✓ What is the capital of Country00? → Capitol00",
        "✗ What is the capital of Country00? → Capitol01  (truth: Capitol00)",
        "self-test: 1/2 = 50%  (guessing would score 25%)",
        "new agent caps (spawned): verify capitals of countries",
        "caps  +2.00  you said correct  · level 1  What is the capital of Country02?",
        "asked to learn: black holes",
        "nightly report 2026-10-10 · 1,234 documents · quiz 80%  /r.md",
        "verified backup saved (123.5 MB)",
        "backup failed: disk full",
        "wikipedia.part: TimeoutError: read timed out (will retry)",
        "other: {}",
    ):
        assert needle in joined, needle
    assert [e["at"] for e in ev] == sorted(e["at"] for e in ev)
    assert f.poll(out["cursor"])["events"] == []


def test_agent_reward_says_what_the_task_was(db):
    kb = build(db, n=6)
    f = Feed(db)
    a = society.spawn(db, "predict capitals", name="pred")
    ag = some(store.get(db, a.id))
    cases: list[tuple[str, dict[str, Any], str]] = [
        ("quiz", {"question": "What is the capital of Country01?", "options": [{"entity": kb.cities[1], "label": "Capitol01"}],
                  "chosen": kb.cities[1]}, "What is the capital of Country01? → Capitol01"),
        ("digest", {"title": "A news story"}, "A news story"),
        ("read", {"titles": ["A", "B", "C", "D"]}, "A, B, C"),
        ("predict", {"s": kb.countries[2], "p": kb.preds["capital"], "choice": kb.cities[2]}, "Country02 → capital → Capitol02"),
    ]  # fmt: skip
    t = db.one("SELECT id FROM triples WHERE s=? AND p=?", (kb.countries[3], kb.preds["capital"]))
    cases.append(("dispute", {"choice": int(t["id"])}, "Country03 → capital → Capitol03"))
    cases.append(("dispute", {"choice": 10**9}, ""))
    cases.append(("calibrate", {}, ""))
    for i, (kind, payload, want) in enumerate(cases):
        assert f._task_text(kind, json.dumps(payload)) == want, kind
        store.open_task(db, ag, kind=kind, action=kind, target=f"x{i}", payload=payload)
    assert f._task_text("quiz", "not json") == "" and f._task_text("quiz", "[1]") == ""
    assert f._task_text("quiz", json.dumps({"options": [{"bad": 1}]})) == ""


def test_cursor_ahead_of_data_resets_and_new_streams_start_now(db):
    build(db, n=4)
    f = Feed(db)
    heads = parse_cursor(f.poll(None)["cursor"])
    assert heads is not None
    ahead = format_cursor({k: v + 1000 for k, v in heads.items()})  # e.g. a backup was restored
    assert f.poll(ahead)["events"] == [] and parse_cursor(f.poll(ahead)["cursor"]) == heads
    assert f.poll("d0")["events"] and all(e["kind"] in {"read", "more"} for e in f.poll("d0")["events"])


def test_status_reports_state_activity_counts_and_curiosity(db, tmp_path):
    kb = build(db, n=5)
    f = Feed(db, pulse=tmp_path / "pulse", stale=60)
    st = f.status()
    assert st["online"] is False and st["state"] == "not started" and st["activity"] == ""
    assert st["counts"]["documents"] == 10 and st["counts"]["facts"] == db.scalar("SELECT COUNT(*) FROM triples")
    db.kv_set("heartbeat", {"ts": time.time(), "state": "running", "cycle": 7})
    now = time.time()
    db.execute(
        "INSERT INTO cycles(started, ended, action, status) VALUES(?, ?, 'reason.infer', 'continue')", (now, now)
    )
    db.execute("INSERT INTO vitals(at, temp_c, load1, mem_mb, disk_free_gb, data_gb, mode) VALUES(?, 71.26, 1, 1, 1, 1, 'normal')",
               (now,))  # fmt: skip
    db.execute("INSERT INTO topic_priority(topic_id, gap, importance, novelty, effort, priority, evidence, updated) "
               "VALUES(?, 1, 1, 1, 0, 0.9, '{}', ?)", (kb.topics["History"], now))  # fmt: skip
    db.execute(
        "INSERT INTO quizzes(created, n, correct, accuracy, chance, details) VALUES(?, 4, 3, 0.75, 0.25, '{}')", (now,)
    )
    docs_before = st["counts"]["documents"]
    DocumentStore(db).add(Document("feeds", "late", "Late news", "Late news body " * 5, "CC BY 4.0"))
    st = f.status()
    assert st["counts"]["quiz"]["accuracy"] == 0.75  # small tables: always current
    assert st["counts"]["documents"] == docs_before  # full-table counts: cached for COUNTS_TTL
    f._counts_at -= feed.COUNTS_TTL
    assert f.status()["counts"]["documents"] == docs_before + 1
    assert st["online"] and st["cycle"] == 7 and st["activity"] == "inferring new facts" and st["temp_c"] == 71.3
    assert st["curious"] == ["History"] and st["mode"] == "normal"
    db.execute("INSERT INTO cycles(started, ended, action, status) VALUES(?, ?, 'noop', 'idle')", (now, now))
    db.kv_set("heartbeat", {"ts": time.time() - 3600, "state": "paused", "cycle": 8})
    db.kv_set("paused_reason", "CPU 83.0 °C: cooling down below 77 °C")
    (tmp_path / "pulse").touch()  # a long slice: the pulse file proves the agent is alive
    st = f.status()
    assert st["online"] and st["activity"] == "waiting for work" and st["paused_reason"].startswith("CPU 83")
    db.kv_set("heartbeat", {"ts": time.time(), "state": "stopped"})
    assert f.status()["online"] is False


def test_every_job_kind_has_an_activity_label(config, db):
    kinds_registered = set(build_components(config, db, planners=False).registry.kinds())
    assert kinds_registered <= set(ACTIVITY), sorted(kinds_registered - set(ACTIVITY))
    assert set(ACTIVITY) <= kinds_registered, sorted(set(ACTIVITY) - kinds_registered)  # no stale labels


# ------------------------------------------------------------------ rendering
def test_status_lines_and_states():
    base = {"online": True, "state": "running", "cycle": 1234, "activity": "reading Wikipedia articles",
            "mode": "normal", "temp_c": 61.0, "counts": {"documents": 12_345, "entities": 2_000_000, "facts": 3_456_789,
            "inferred": 19_917, "disputed": 12, "rules": 270, "agents": 6, "quiz": {"accuracy": 0.815, "chance": 0.25}}}  # fmt: skip
    l1, l2 = (text_of(x) for x in status_lines(base))
    assert "● learning" in l1 and "cycle 1,234" in l1 and "now: reading Wikipedia articles" in l1
    assert l2 == (" 12,345 docs · 2.00M things · 3.46M facts (19,917 inferred, 12 disputed) · 270 rules"
                  " · quiz 82% · 6 agents · 61 °C")  # fmt: skip
    hot = text_of(status_lines(base | {"mode": "throttle", "temp_c": 76.0})[0])
    assert "throttled" in hot
    paused = text_of(status_lines(base | {"state": "paused", "mode": "pause", "paused_reason": "disk"})[0])
    assert "paused" in paused and "(disk)" in paused
    assert "agent offline" in text_of(status_lines(base | {"online": False})[0])
    assert "agent stopped" in text_of(status_lines(base | {"online": False, "state": "stopped"})[0])
    assert "waiting: x" in text_of(status_lines(None, offline="waiting: x")[0])
    assert feed.compact(1_234_567_890) == "1.23B" and feed.compact(999) == "999"
    assert [feed.human_bytes(n) for n in (2_500, 3_400_000, 7_100_000_000)] == ["2 kB", "3.4 MB", "7.10 GB"]


def test_screen_paint_truncates_and_falls_back_to_ascii():
    class Out(io.StringIO):
        encoding = "ascii"

    s = Screen(Out(), color=True, fancy=True)
    s.cols = 20
    painted = s.paint([("Paris", "bold"), (" → capital of → ", "dim"), ("France and more text", "")])
    plain = painted.replace("\033[1m", "").replace("\033[2m", "").replace("\033[0m", "")
    assert len(plain) == 20 and plain.endswith(".") and "->" in plain and "\033[1mParis" in painted
    u = Screen(io.StringIO(), color=False, fancy=False)
    assert u.paint([("✓ → ·", "bold")]) == "✓ → ·"  # in-memory stream: unicode, no colour, no truncation


def test_unknown_event_kind_still_renders():
    assert "mystery" in text_of(render_event({"kind": "mystery", "at": 0, "x": 1}))


# ------------------------------------------------------------------ client loop
def fake_source(script):
    """Returns the scripted answers in order (an Exception instance is raised)."""
    calls = []

    def source(cursor):
        calls.append(cursor)
        item = script[min(len(calls) - 1, len(script) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    return source, calls


def status(**kw):
    return {"online": True, "state": "running", "cycle": 1, "activity": "x", "mode": "normal", "temp_c": 50.0,
            "curious": [], "counts": {}} | kw  # fmt: skip


def test_run_plain_prints_events_waits_and_announces_changes():
    ev = {"kind": "read", "at": time.time(), "source": "wikipedia", "title": "Paris", "chars": 1234}
    script = [
        FeedUnavailable("no answer from http://127.0.0.1:8765"),
        FeedUnavailable("no answer from http://127.0.0.1:8765"),
        {"cursor": "d1", "events": [ev], "status": status(curious=["Black holes"])},
        {"cursor": "d2", "events": [], "status": status(mode="throttle", temp_c=76.4, curious=["Black holes"])},
        {"cursor": "d3", "events": [], "status": status(online=False, state="running", curious=["Black holes"])},
        {"cursor": "d4", "events": [], "status": status(mode="pause", paused_reason="disk nearly full")},
    ]
    source, calls = fake_source(script)
    out = io.StringIO()
    slept: list[float] = []
    rc = feed.run(source, out=out, color=False, fancy=False, max_polls=6, sleep=slept.append, where="test")
    text = out.getvalue()
    assert rc == 0 and calls == [None, None, None, "d1", "d2", "d3"]
    assert text.count("waiting: no answer") == 1  # the same problem is reported once
    assert "connected" in text and "Paris" in text and "1,234 chars" in text
    assert "most curious about: Black holes" in text
    assert "running hot (CPU 76 °C): light work only" in text
    assert "agent went quiet (state: running)" in text and "paused: disk nearly full" in text
    assert "── " in text and "\033[" not in text  # a plain status line, no escape codes
    assert slept[:2] == [1.5, 3.0]  # backoff while waiting


def test_run_once_and_unavailable_exit_codes():
    source, _ = fake_source([FeedUnavailable("down")])
    assert feed.run(source, out=io.StringIO(), color=False, fancy=False, once=True, sleep=lambda s: None) == 1
    source, _ = fake_source([{"cursor": "d1", "events": [], "status": status()}])
    assert feed.run(source, out=io.StringIO(), color=False, fancy=False, once=True, sleep=lambda s: None) == 0


def test_run_fancy_pins_header_spreads_lines_and_restores_terminal(monkeypatch):
    monkeypatch.setattr("shutil.get_terminal_size", lambda fallback=None: __import__("os").terminal_size((80, 24)))
    evs = [{"kind": "request", "at": time.time(), "query": f"topic {i}"} for i in range(4)]
    source, _ = fake_source([{"cursor": "j1", "events": evs, "status": status()}])
    out = io.StringIO()
    slept: list[float] = []
    assert feed.run(source, out=out, color=True, fancy=True, once=True, sleep=slept.append) == 0
    text = out.getvalue()
    assert text.startswith("\033[?25l\033[2J\033[4;24r\033[24;1H")  # hide cursor, clear, scroll region below header
    assert "\0337\033[1;1H\033[2K" in text and "\0338" in text  # header drawn in place, cursor restored
    assert text.endswith("\033[r\033[?25h\033[24;1H\n")  # region reset, cursor shown
    assert len(slept) == 3 and all(s == 0.25 for s in slept)  # a batch is spread out


def test_run_ctrl_c_restores_terminal(monkeypatch):
    def boom(cursor):
        raise KeyboardInterrupt

    out = io.StringIO()
    assert feed.run(boom, out=out, color=True, fancy=True, sleep=lambda s: None) == 0
    assert out.getvalue().endswith("\n") and "\033[r" in out.getvalue()


def test_resize_redraws_region(monkeypatch):
    sizes = iter([(80, 24), (120, 40)])
    monkeypatch.setattr("shutil.get_terminal_size", lambda fallback=None: __import__("os").terminal_size(next(sizes)))
    out = io.StringIO()
    s = Screen(out, color=False, fancy=True)
    s.start()
    s._on_resize()
    s.header(status_lines(status()))
    assert "\033[4;40r" in out.getvalue() and s.cols == 120


def test_wants_color():
    class Tty(io.StringIO):
        def isatty(self):
            return True

    assert feed.wants_color(Tty(), {"TERM": "xterm-256color"})
    assert not feed.wants_color(Tty(), {"TERM": "dumb"})
    assert not feed.wants_color(Tty(), {"NO_COLOR": "1"})
    assert not feed.wants_color(io.StringIO(), {})


# ------------------------------------------------------------------ sources, dashboard, CLI
@pytest.fixture()
def kb_cfg(tmp_path):
    cfg = make_config(tmp_path)
    db = open_database(cfg.paths.db_path)
    with db.transaction():
        build(db, n=6)
    db.close()
    return cfg


def test_dashboard_api_feed_and_http_source(kb_cfg):
    server = make_server(kb_cfg, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        src = feed.http_source(f"http://127.0.0.1:{server.server_address[1]}/")
        first = src(None)
        assert any(e["kind"] == "read" for e in first["events"]) and first["status"]["counts"]["documents"] == 11
        db = Database(kb_cfg.paths.db_path)
        DocumentStore(db).add(Document("feeds", "n1", "Fresh news", "Fresh news body " * 4, "CC BY 4.0"))
        db.close()
        nxt = src(first["cursor"])
        assert [e["title"] for e in nxt["events"] if e["kind"] == "read"] == ["Fresh news"]
        assert src(nxt["cursor"])["events"] == []
        assert src("garbage!!")["events"]  # a malformed cursor starts over instead of failing
    finally:
        server.shutdown()
        server.server_close()


def test_http_source_errors(tmp_path):
    cfg = make_config(tmp_path)  # no database yet: the dashboard answers 503
    server = make_server(cfg, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        with pytest.raises(FeedUnavailable, match="not initialised"):
            feed.http_source(f"http://127.0.0.1:{server.server_address[1]}")(None)
    finally:
        server.shutdown()
        server.server_close()
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(FeedUnavailable, match="no answer"):
        feed.http_source(f"http://127.0.0.1:{port}", timeout=1)(None)


def test_http_source_rejects_unexpected_answers():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    port = srv.getsockname()[1]
    bodies = [b"not json", b"[1, 2]", b'{"x": 1}', None]

    def serve():
        for body in bodies:
            conn, _ = srv.accept()
            conn.recv(4096)
            if body is None:
                conn.sendall(b"HTTP/1.1 500 Oops\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            else:
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s" % (len(body), body)
                )
            conn.close()

    threading.Thread(target=serve, daemon=True).start()
    src = feed.http_source(f"http://127.0.0.1:{port}", timeout=5)
    for want in ("unexpected answer", "unexpected answer", "unexpected answer", "HTTP 500"):
        with pytest.raises(FeedUnavailable, match=want):
            src(None)
    srv.close()


def test_direct_source(kb_cfg, tmp_path):
    with pytest.raises(FeedUnavailable, match="not initialised"):
        feed.direct_source(tmp_path / "missing.sqlite3")(None)
    src = feed.direct_source(kb_cfg.paths.db_path)
    assert src(None)["status"]["counts"]["documents"] == 11
    bad = tmp_path / "bad.sqlite3"
    bad.write_bytes(b"this is not a database" * 100)
    with pytest.raises(FeedUnavailable, match="cannot read the database"):
        feed.direct_source(bad)(None)


def test_cli_feed_direct_once(kb_cfg, tmp_path, capsys):
    path = str(tmp_path / "polymath.toml")
    rc = cli.main(["--config", path, "feed", "--direct", "--once", "--plain", "--no-color"])
    out = capsys.readouterr().out
    assert rc == 0 and "live feed from" in out and "read     wikipedia" in out and "facts" in out
    assert cli.main(["--config", path, "feed", "--url", "http://127.0.0.1:9", "--once", "--plain"]) == 1


def test_dashboard_url(config):
    assert cli.dashboard_url(config) == "http://127.0.0.1:8765"
    config.dashboard.host, config.dashboard.port = "192.168.1.20", 9000
    assert cli.dashboard_url(config) == "http://192.168.1.20:9000"
    config.dashboard.host = "::1"
    assert cli.dashboard_url(config) == "http://[::1]:9000"
    config.dashboard.host = "::"
    assert cli.dashboard_url(config) == "http://127.0.0.1:9000"
