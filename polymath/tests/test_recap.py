"""The weekly recap (evaluation.recap): this week against last week."""

from __future__ import annotations

import json
import time

from polymath.core.scheduler import Scheduler, local_weekly_phase
from polymath.evaluation import recap as R
from polymath.interface import cli
from polymath.interface.face import Face
from polymath.interface.feed import Feed, render_event
from polymath.memory.documents import Document, DocumentStore
from polymath.memory.graph import KnowledgeGraph

DAY = 86400.0


def text_of(segs):
    return "".join(t for t, _ in segs)


def seed(db, now):
    store = DocumentStore(db)
    g = KnowledgeGraph(db)
    space = int(db.execute("INSERT INTO topics(name, kind, n_docs, n_total) VALUES('Astronomy', 'category', 0, 0)")
                .lastrowid or 0)  # fmt: skip
    art = int(db.execute("INSERT INTO topics(name, kind, n_docs, n_total) VALUES('Art', 'category', 0, 0)")
              .lastrowid or 0)  # fmt: skip
    for i in range(30):  # 20 this week (12 about astronomy), 10 last week (2 about astronomy)
        when = now - 60 - (i % 20) * 0.3 * DAY if i < 20 else now - 8 * DAY - i * 100
        did, _ = store.add(Document("web", f"d{i}", f"Doc {i}", f"text {i} " * 20, "CC0"))
        db.execute("UPDATE documents SET fetched = ? WHERE id = ?", (when, did))
        topic = space if (i < 12 or 20 <= i < 22) else art
        db.execute("INSERT INTO doc_topics(doc_id, topic_id, weight) VALUES(?,?,1)", (did, topic))
    p = g.predicate("P1", "related")
    for i in range(15):
        a, b = g.upsert_entity(f"Q{i}", f"A{i}"), g.upsert_entity(f"Q{100 + i}", f"B{i}")
        tid = g.add_triple(
            a, p, o=b, kind="wikidata", source="wikidata", status="inferred" if i % 5 == 0 else "sourced"
        )[0]
        db.execute("UPDATE triples SET created = ? WHERE id = ?", (now - (1 if i < 10 else 9) * DAY, tid))
    db.execute("INSERT INTO quizzes(created, n, correct, accuracy, chance, details) VALUES(?, 10, 8, 0.8, 0.25, '{}')",
               (now - DAY,))  # fmt: skip
    db.execute("INSERT INTO quizzes(created, n, correct, accuracy, chance, details) VALUES(?, 10, 6, 0.6, 0.25, '{}')",
               (now - 9 * DAY,))  # fmt: skip
    db.execute("INSERT INTO predictions(s, p, o, score, n_options, made_at, state, checked_at) "
               "VALUES(1, 1, 1, 0.8, 4, ?, 'confirmed', ?)", (now - 3 * DAY, now - DAY))  # fmt: skip
    db.execute("INSERT INTO events(at, kind, text) VALUES(?, 'didyouknow', 'Did you know? Something odd.')",
               (now - DAY,))  # fmt: skip


def test_week_against_week(db):
    now = time.time()
    seed(db, now)
    d = R.collect(db, now)
    assert d["this"]["documents"] == 20 and d["last"]["documents"] == 10
    assert d["this"]["facts"] == 8 and d["this"]["inferred"] == 2 and d["last"]["facts"] == 4
    assert d["topics"][0]["topic"] == "Astronomy" and d["topics"][0]["this_week"] == 12
    lines = R.lines(d)
    assert lines[0] == "Read 20 documents (up 100% on last week)."
    assert lines[1].startswith("Learned 8 facts (up 100% on last week) and worked out 2 by reasoning")
    assert "Self-test accuracy: 80% (up 20 points on last week)." in lines
    assert "Predictions settled: 1 came true, 0 were wrong." in lines
    assert "Most improved topic: Astronomy (12 documents, up from 2)." in lines
    assert "Did you know? Something odd." in lines
    assert R.change(5, 0) == "new this week" and R.change(0, 0) == "none either week"
    assert R.change(100, 100.5) == "about the same as last week" and R.change(50, 100) == "down 50% on last week"


def test_write_latest_feed_face_and_cli(db, config, tmp_path, capsys):
    now = time.time()
    seed(db, now)
    d = R.write_recap(db, now, cfg=config)
    latest = R.latest(db)
    assert latest is not None and latest["week"] == d["week"] == time.strftime("%G-W%V", time.localtime(now))
    R.write_recap(db, now + 60, cfg=config)  # the same week again: replaced, not duplicated
    assert db.scalar("SELECT COUNT(*) FROM recaps") == 1
    evs = [e for e in Feed(db).poll()["events"] if e["kind"] == "recap"]
    assert evs[0]["head"] and "the week in review" in text_of(render_event(evs[0]))
    assert any("Read 20 documents" in text_of(render_event(e)) for e in evs[1:])
    f = Face()
    f.update({"online": True, "state": "running", "mode": "normal", "action": "wikipedia.part"})
    f.see(evs[0], 1.0)
    assert f.mood(1.0) == "happy"
    db.conn.commit()
    path = str(tmp_path / "polymath.toml")
    assert cli.main(["--config", path, "recap"]) == 0
    assert "The week in review" in capsys.readouterr().out
    assert cli.main(["--config", path, "recap", "--now", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["week"].endswith("(so far)")
    db.execute("DELETE FROM recaps")
    db.conn.commit()
    assert cli.main(["--config", path, "recap"]) == 0
    assert "(so far)" in capsys.readouterr().out
    job = R.recap_job(type("Ctx", (), {"db": db, "config": config})())
    assert job.done and job.result["lines"] >= 3


def test_sunday_morning_schedule(db, config):
    phase = local_weekly_phase(6, 8, now=0)
    local = time.localtime(7 * DAY * 1000 + phase)  # every slot boundary is a Sunday, 08:00 local time
    assert local.tm_wday == 6 and local.tm_hour == 8

    from types import SimpleNamespace

    a = SimpleNamespace(db=db, scheduler=Scheduler(db), config=config)
    R.planner(a)
    assert db.scalar("SELECT COUNT(*) FROM jobs WHERE kind = 'eval.recap'") == 0  # nothing read yet
    DocumentStore(db).add(Document("web", "x", "X", "text " * 30, "CC0"))
    R.planner(a)
    assert db.scalar("SELECT COUNT(*) FROM jobs WHERE kind = 'eval.recap'") == 1
