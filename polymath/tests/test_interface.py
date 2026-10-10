"""Phase 9: answering engine with citations, CLI commands, and the read-only dashboard (real HTTP server)."""

from __future__ import annotations

import http.client
import json
import os
import threading
import time

import pytest

from polymath.core import inbox
from polymath.core.db import Database, open_database
from polymath.interface import cli
from polymath.interface.answer import Answer, Answerer, Citation, Statement, render_value
from polymath.interface.dashboard import ASK_PER_MINUTE, MAX_BODY, RateLimiter, make_server
from tests.conftest import make_config
from tests.fixtures.kb import build
from tests.fixtures.typing import some


# ------------------------------------------------------------------ answering
@pytest.mark.parametrize(
    ("q", "subj", "rel", "kind"),
    [
        ("What is the capital of France?", "France", "capital", "rel"),
        ("who was the author of The Hobbit", "The Hobbit", "author", "rel"),
        ("When was Ada Lovelace born?", "Ada Lovelace", "born", "when"),
        ("Where is the Eiffel Tower located?", "the Eiffel Tower", "located", "where"),
        ("Where is Paris", "Paris", None, "where"),
        ("Who is Marie Curie?", "Marie Curie", None, "describe"),
        ("Tell me about black holes", "black holes", None, "describe"),
        ("France's population", "France", "population", "rel"),
        ("How many moons does Jupiter have?", "Jupiter", "moons", "rel"),
        ("photosynthesis in desert plants", None, None, "search"),
    ],
)
def test_question_parsing(q, subj, rel, kind):
    assert Answerer.parse(q) == (subj, rel, kind)


def test_answers_cite_their_sources(db):
    kb = build(db)
    a = Answerer(db)
    cap = a.ask("What is the capital of Country03?")
    assert cap.subject == "Country03" and cap.statements[0].text == "The capital of Country03 is Capitol03."
    assert cap.statements[0].kind == "fact" and cap.statements[0].citations[0].license == "CC0 1.0"
    assert "[1] Wikidata — https://www.wikidata.org/ — CC0 1.0" in cap.render()
    alias = a.ask("What is the capital city of Country04?")  # Wikidata property alias → P36
    assert alias.statements[0].text == "The capital of Country04 is Capitol04."
    born = a.ask("When was Person2 born?")
    assert born.statements[0].text == "The date of birth of Person2 is 1912-05-03."
    assert born.statements[0].citations == [Citation("Person2", None, "CC BY-SA 4.0", "wikipedia")]
    pop = a.ask("Country07's population")
    assert pop.statements[0].text == "The population of Country07 is 1,000,007."
    where = a.ask("Where is Capitol05?")
    assert where.statements[0].text == "The country of Capitol05 is Country05."
    who = a.ask("Who is Country01?")
    assert [s.kind for s in who.statements] == ["description", "passage"]
    assert who.statements[1].citations[0].url == "https://en.wikipedia.org/wiki/Country01"
    about = a.ask("Tell me about Person1")
    assert about.statements[0].text.endswith("born long ago.") and ".." not in about.statements[0].text
    city = a.ask("Who is Capitol09?")  # no article, no description: falls back to its facts
    assert {s.text for s in city.statements} >= {"Capitol09 — country: Country09."}
    hist = a.ask("What do historians write about kingdoms?")
    assert hist.statements and all(s.kind == "passage" for s in hist.statements)
    assert kb.docs  # (fixture sanity)


def test_answers_admit_ignorance_instead_of_guessing(db):
    build(db)
    a = Answerer(db)
    unknown = a.ask("What is the meaning of zorblax?")
    assert unknown.statements == [] and "could not identify “zorblax”" in unknown.note
    assert unknown.render().startswith("I don't know yet.")
    gdp = a.ask("What is the GDP of Country03?")  # known subject, unknown relation: no off-topic passage
    assert gdp.statements == [] and "have not learned its GDP" in gdp.note.replace("gdp", "GDP")
    where = a.ask("Where is Country01?")
    assert where.statements == [] and where.note == "I know about Country01 but have not learned where it is yet."
    vague = a.ask("is the of and")
    assert vague.statements == [] and vague.confidence == 0.0


def test_disputed_and_inferred_facts_are_labelled(db):
    kb = build(db)
    g = kb.graph
    tid, _ = g.add_triple(
        kb.countries[2],
        kb.preds["capital"],
        o=kb.cities[9],
        kind="pattern",
        source="text",
        doc_id=kb.docs["Country02"],
        confidence=0.4,
    )
    g.set_status(tid, "disputed")
    db.execute("UPDATE triples SET status='disputed' WHERE s=? AND p=?", (kb.countries[2], kb.preds["capital"]))
    rule, _ = g.add_triple(
        kb.countries[2],
        kb.preds["country"],
        o=kb.countries[2],
        status="inferred",
        kind="rule",
        source="rule",
        detail="P17 is reflexive for states",
    )
    ans = Answerer(db).ask("What is the capital of Country02?")
    assert {s.kind for s in ans.statements} == {"disputed"} and "(disputed)" in ans.render()
    assert any(c.title == "Country02" for s in ans.statements for c in s.citations)  # text evidence cites its doc
    inferred = Answerer(db).facts(some(Answerer(db).find_entity("Country02")), [kb.preds["country"]])
    assert inferred[0].kind == "inferred" and inferred[0].citations[0].source == "rule"
    assert rule > 0


def test_render_value_kinds(db):
    kb = build(db)
    assert render_value(db, kb.cities[0], "") == "Capitol00"
    assert render_value(db, kb.graph.stub("Q239"), "") == "Q239 (an entity whose name I have not read yet)"
    assert render_value(db, 0, json.dumps({"time": "1969-07-20"})) == "1969-07-20"
    assert render_value(db, 0, json.dumps({"amount": 2.5, "unit": "km"})) == "2.5 km"
    assert render_value(db, 0, json.dumps({"amount": 12, "unit": "Q6256"})) == "12 country"
    assert render_value(db, 0, json.dumps({"text": "hello"})) == "hello"
    assert render_value(db, 0, json.dumps({"lat": 48.85661, "lon": 2.35222})) == "48.8566, 2.3522"
    assert render_value(db, 0, json.dumps("plain")) == "plain"


def test_answer_serialisation_round_trip():
    c = Citation("T", "https://x", "CC0", "web")
    a = Answer("q?", "s", None, [Statement("x.", 0.5, "fact", [c, c]), Statement("y.", 0.25, "inferred", [c])], 0.5)
    d = a.to_dict()
    assert d["statements"][0]["citations"][0] == {"title": "T", "url": "https://x", "license": "CC0", "source": "web"}
    assert a.render().splitlines() == [
        "- x. [1, 1] (confidence 0.50)",
        "- y. (inferred) [1] (confidence 0.25)",
        "",
        "[1] T — https://x — CC0",
    ]


# ------------------------------------------------------------------------ CLI
@pytest.fixture()
def kb_config(tmp_path):
    cfg = make_config(tmp_path)
    db = open_database(cfg.paths.db_path)
    with db.transaction():
        build(db)
    db.close()
    return cfg, str(tmp_path / "polymath.toml")


def run(capsys, *argv):
    rc = cli.main(list(argv))
    return rc, capsys.readouterr().out


def test_cli_ask_status_topics_why_report(kb_config, capsys):
    cfg, path = kb_config
    rc, out = run(capsys, "--config", path, "ask", "What", "is", "the", "capital", "of", "Country03?")
    assert rc == 0 and "The capital of Country03 is Capitol03." in out and "CC0 1.0" in out
    rc, out = run(capsys, "--config", path, "ask", "--json", "What is the meaning of zorblax?")
    assert rc == 1 and json.loads(out)["statements"] == []
    rc, out = run(capsys, "--config", path, "status")
    st = json.loads(out)
    assert st["knowledge"]["documents"] == 45 and st["knowledge"]["triples"]["sourced"] > 100
    rc, out = run(capsys, "--config", path, "topics")
    assert "no topic priorities yet" in out
    db = Database(cfg.paths.db_path)
    from polymath.drive.pagerank import entity_pagerank
    from polymath.drive.priority import compute_priorities

    with db.transaction():
        entity_pagerank(db)
        compute_priorities(db)
        db.execute(
            "INSERT INTO decisions(at, cycle, context, options, chosen, reason, reward) "
            "VALUES(?, 1, 'normal|day|clear', '{}', 'read', 'Thompson sample 1.2 highest of 3', 0.5)",
            (time.time(),),
        )
    db.close()
    rc, out = run(capsys, "--config", path, "topics", "--weakest", "-n", "3")
    assert rc == 0 and out.splitlines()[1].startswith("Geography")
    rc, out = run(capsys, "--config", path, "topics", "--json")
    assert json.loads(out)[0]["name"] == "Geography"
    rc, out = run(capsys, "--config", path, "why", "Geography")
    assert "Topic “Geography”: priority" in out and "gap:" in out and "Recent decisions:" in out
    assert "Thompson sample 1.2" in out
    rc, out = run(capsys, "--config", path, "why", "--json")
    assert json.loads(out)["decisions"][0]["chosen"] == "read"
    rc, out = run(capsys, "--config", path, "why", "What is the capital of Country05?")
    assert "The capital of Country05 is Capitol05." in out  # provenance of a fact
    rc, out = run(capsys, "--config", path, "report", "--hours", "1")
    assert rc == 0 and out.startswith("# Polymath nightly report") and "| wikipedia | 45 | 45 |" in out
    rc, out = run(capsys, "--config", path, "report", "--json")
    assert json.loads(out)["documents"]["wikipedia"]["total"] == 45
    rc, out = run(capsys, "--config", path, "report", "--write")
    assert json.loads(out)["path"].endswith(".md")


def test_cli_learn_direct_or_via_inbox(kb_config, capsys):
    cfg, path = kb_config
    rc, out = run(capsys, "--config", path, "learn", "black", "holes")
    res = json.loads(out)
    assert rc == 0 and res["queued_via"] == "database" and res["created"]
    db = Database(cfg.paths.db_path)
    assert json.loads(some(db.one("SELECT payload FROM jobs WHERE kind='drive.learn'"))["payload"]) == {
        "query": "black holes"
    }
    db.kv_set("heartbeat", {"ts": time.time(), "state": "running", "cycle": 9})
    db.close()
    rc, out = run(capsys, "--config", path, "learn", "https://example.org/")
    assert json.loads(out)["queued_via"] == "inbox"
    files = list(inbox.inbox_dir(cfg.paths.data_dir).glob("*.json"))
    assert len(files) == 1 and json.loads(files[0].read_text()) == {"query": "https://example.org/", "type": "learn"}


def test_cli_errors(tmp_path, capsys):
    cfg = make_config(tmp_path)
    path = str(tmp_path / "polymath.toml")
    assert cli.main(["--config", path, "ask", "anything"]) == 4  # not initialised
    bad = tmp_path / "bad.toml"
    bad.write_text("[loop]\nidle_sleep = 'x'\n")
    assert cli.main(["--config", str(bad), "status"]) == 2
    assert not cfg.paths.db_path.exists()  # read-only commands never create the database
    assert cli.main(["--config", path, "learn", " "]) == 2


# ------------------------------------------------------------------ dashboard
@pytest.fixture()
def dash(kb_config):
    cfg, _ = kb_config
    server = make_server(cfg, "127.0.0.1", 0)
    t = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield cfg, server.server_address[1]
    server.shutdown()
    server.server_close()


def req(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, dict(r.getheaders()), data


def beat(cfg, age=0.0, state="running"):
    db = Database(cfg.paths.db_path)
    db.kv_set("heartbeat", {"ts": time.time() - age, "state": state, "cycle": 42})
    db.close()


def test_health_reflects_heartbeat(dash):
    cfg, port = dash
    status, _h, body = req(port, "GET", "/health")
    assert status == 503 and json.loads(body)["heartbeat_age_s"] is None
    beat(cfg)
    status, _h, body = req(port, "GET", "/health")
    assert status == 200 and json.loads(body)["cycle"] == 42
    beat(cfg, age=cfg.loop.heartbeat_stale + 5)
    assert req(port, "GET", "/health")[0] == 503
    pulse = cfg.paths.data_dir / "heartbeat"
    pulse.touch()  # a long job slice: the database heartbeat is old, but the agent's pulse file is fresh
    assert req(port, "GET", "/health")[0] == 200
    old = time.time() - cfg.loop.heartbeat_stale - 5
    os.utime(pulse, (old, old))
    assert req(port, "GET", "/health")[0] == 503
    pulse.touch()
    beat(cfg, state="stopped")
    assert req(port, "GET", "/health")[0] == 503  # a clean stop is never "healthy", whatever the pulse says
    assert req(port, "HEAD", "/health")[2] == b""


def test_health_without_database(tmp_path):
    cfg = make_config(tmp_path)
    server = make_server(cfg, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        port = server.server_address[1]
        status, _h, body = req(port, "GET", "/health")
        assert status == 503 and "database unavailable" in json.loads(body)["error"]
        assert req(port, "GET", "/api/overview")[0] == 503
        assert req(port, "GET", "/")[0] == 200  # the page itself still loads and shows the problem
    finally:
        server.shutdown()
        server.server_close()


def test_static_files_and_security_headers(dash):
    _cfg, port = dash
    status, h, body = req(port, "GET", "/")
    assert status == 200 and h["Content-Type"].startswith("text/html") and b"<canvas" in body
    assert "default-src 'self'" in h["Content-Security-Policy"] and h["X-Frame-Options"] == "DENY"
    assert h["X-Content-Type-Options"] == "nosniff" and h["Cache-Control"] == "no-store"
    assert h["Server"].startswith("Polymath") and "Python" not in h["Server"]
    for name, ctype in (("app.js", "text/javascript"), ("style.css", "text/css"), ("icon.svg", "image/svg+xml")):
        status, h, body = req(port, "GET", f"/static/{name}")
        assert status == 200 and h["Content-Type"].startswith(ctype) and body
    assert req(port, "GET", "/static/../polymath.toml")[0] == 404
    assert req(port, "GET", "/static/secret.py")[0] == 404
    assert req(port, "GET", "/nope")[0] == 404
    for asset in (b"http://", b"https://"):  # no CDNs / external resources anywhere in the UI
        for name in ("index.html", "app.js", "style.css"):
            body = req(port, "GET", f"/static/{name}" if name != "index.html" else "/")[2]
            assert asset not in body.replace(b"http://www.w3.org/2000/svg", b""), name


def test_api_endpoints(dash):
    cfg, port = dash
    beat(cfg)
    db = Database(cfg.paths.db_path)
    db.execute(
        "INSERT INTO vitals(at, temp_c, load1, mem_mb, disk_free_gb, data_gb, mode) "
        "VALUES(?, 51.5, 0.7, 900, 200, 1.2, 'normal')",
        (time.time(),),
    )
    db.execute(
        "INSERT INTO cycles(started, ended, action, status, cpu_seconds, wall_seconds, value, reward) "
        "VALUES(?, ?, 'read', 'done', 1.5, 2, 0.4, 0.2)",
        (time.time() - 3, time.time()),
    )
    db.close()
    o = json.loads(req(port, "GET", "/api/overview")[2])
    assert o["health"]["ok"] and o["documents_total"] == 45 and o["entities"] > 80
    assert o["vitals"]["temp_c"] == 51.5 and o["recent_cycles"][0]["action"] == "read"
    ts = json.loads(req(port, "GET", "/api/timeseries")[2])
    assert sum(n for _d, n in ts["documents_per_day"]["wikipedia"]) == 45
    assert ts["effort_24h"] == {"read": 1.5} and ts["vitals_24h"][0][1] == 51.5
    tp = json.loads(req(port, "GET", "/api/topics")[2])
    assert set(tp) == {"top", "weakest", "decisions"}
    kg = json.loads(req(port, "GET", "/api/knowledge?q=Country03")[2])
    facts = {(f["predicate"], f["other"]) for f in kg["entities"][0]["facts"]}
    assert kg["entities"][0]["label"] == "Country03" and ("capital", "Capitol03") in facts
    assert json.loads(req(port, "GET", "/api/knowledge?q=")[2])["entities"] == []


def test_ask_box_is_the_only_input(dash):
    _cfg, port = dash
    hdr = {"Content-Type": "application/json"}
    status, _h, body = req(port, "POST", "/api/ask", json.dumps({"question": "What is the capital of Country08?"}), hdr)
    out = json.loads(body)
    assert status == 200 and out["statements"][0]["text"] == "The capital of Country08 is Capitol08."
    assert "[1] Wikidata" in out["rendered"]
    assert req(port, "POST", "/api/ask", b"{not json", hdr)[0] == 400
    assert req(port, "POST", "/api/ask", json.dumps({"question": "  "}), hdr)[0] == 400
    assert req(port, "POST", "/api/ask", json.dumps([1]), hdr)[0] == 400
    assert req(port, "POST", "/api/ask", b"x" * (MAX_BODY + 1), hdr)[0] == 413
    assert req(port, "POST", "/api/ask", b"", hdr)[0] == 413
    for method, path in (("POST", "/api/learn"), ("PUT", "/api/ask"), ("DELETE", "/"), ("PATCH", "/api/overview")):
        status, _h, body = req(port, method, path, b"{}", hdr)
        assert status == 405 and b"read-only" in body


def test_ask_rate_limit(dash):
    _cfg, port = dash
    body = json.dumps({"question": "Who is Country01?"})
    codes = [
        req(port, "POST", "/api/ask", body, {"Content-Type": "application/json"})[0] for _ in range(ASK_PER_MINUTE + 2)
    ]
    assert codes[:ASK_PER_MINUTE] == [200] * ASK_PER_MINUTE and codes[-1] == 429


def test_rate_limiter_window(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    lim = RateLimiter(2)
    assert lim.allow("a") and lim.allow("a") and not lim.allow("a") and lim.allow("b")
    now[0] += 61
    assert lim.allow("a")


def test_dashboard_never_writes(dash):
    cfg, port = dash
    beat(cfg)
    db = Database(cfg.paths.db_path)
    before = db.scalar("SELECT total_changes()"), db.query("SELECT COUNT(*) AS n FROM jobs")[0]["n"]
    db.close()
    mtime = cfg.paths.db_path.stat().st_mtime_ns
    for p in ("/", "/health", "/api/overview", "/api/timeseries", "/api/topics", "/api/knowledge?q=Country01"):
        assert req(port, "GET", p)[0] == 200
    req(port, "POST", "/api/ask", json.dumps({"question": "Who is Country02?"}), {"Content-Type": "application/json"})
    assert cfg.paths.db_path.stat().st_mtime_ns == mtime
    db = Database(cfg.paths.db_path)
    assert db.query("SELECT COUNT(*) AS n FROM jobs")[0]["n"] == before[1]
    db.close()
