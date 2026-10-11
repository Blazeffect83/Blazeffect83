"""Predictions: guessing real gaps, checking the guesses against facts read later, and the scoreboard."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from polymath.interface import cli
from polymath.interface.face import Face
from polymath.interface.feed import Feed, render_event
from polymath.memory.graph import KnowledgeGraph
from polymath.reasoning import predictions as P
from polymath.reasoning.link_prediction import LinkPredictor


class _Ctx:
    def __init__(self, db):
        self.db = db

    def tick(self):
        pass


def Ctx(db) -> Any:
    return _Ctx(db)


def text_of(segs):
    return "".join(t for t, _ in segs)


@pytest.fixture()
def world(db):
    g = KnowledgeGraph(db)
    p31 = g.predicate("P31", "instance of")
    continent = g.predicate("P30", "continent")
    member = g.predicate("P463", "member of")
    flag = g.predicate("P163", "flag")
    country = g.upsert_entity("Q6256", "country")
    europe, asia = g.upsert_entity("Q46", "Europe"), g.upsert_entity("Q48", "Asia")
    africa = g.upsert_entity("Q15", "Africa")
    eu, asean = g.upsert_entity("Q458", "European Union"), g.upsert_entity("Q7768", "ASEAN")

    def add(s, p, o):
        g.add_triple(s, p, o=o, kind="wikidata", source="wikidata", confidence=0.9)

    for i in range(40):
        c = g.upsert_entity(f"Q{900 + i}", f"Land{i}")
        add(c, p31, country)
        side = i % 3
        add(c, continent, (europe, asia, africa)[side])
        if side < 2:
            add(c, member, (eu, asean)[side])
        add(c, flag, g.upsert_entity(f"Q{800 + i}", f"flag of Land{i}"))
    gaps: dict[str, int] = {}
    for name, union in (("Newland", eu), ("Eastland", asean), ("Mystery", None)):
        c = g.upsert_entity(f"Q{700 + len(gaps)}", name)
        add(c, p31, country)
        if union:
            add(c, member, union)
        gaps[name] = c
    return {"g": g, "continent": continent, "member": member, "gaps": gaps, "europe": europe, "asia": asia,
            "flag": flag}  # fmt: skip


def test_relations_skip_housekeeping_and_cache(db, world):
    rels = P.relations(db, 1000.0)
    ps = {r["p"] for r in rels}
    assert world["continent"] in ps and world["flag"] not in ps  # flags: housekeeping, never guessed
    db.execute("DELETE FROM triples")
    assert P.relations(db, 1000.0 + 60) == rels  # cached for a day
    assert P.relations(db, 1000.0 + P.PREDS_EVERY + 1) == []
    db.execute("DELETE FROM predicates WHERE key = 'P31'")
    assert P.relations(db, 1000.0 + 3 * P.PREDS_EVERY) == []


def test_predict_check_and_scoreboard(db, world):
    g, cont, gaps = world["g"], world["continent"], world["gaps"]
    out = P.predict_job(Ctx(db), predictor=LinkPredictor(db))
    assert out.done and out.result["made"] >= 2
    guesses = {int(r["s"]): r for r in db.query("SELECT * FROM predictions WHERE p = ?", (cont,))}
    assert int(guesses[gaps["Newland"]]["o"]) == world["europe"]  # EU members are in Europe
    assert int(guesses[gaps["Eastland"]]["o"]) == world["asia"]
    assert gaps["Mystery"] not in guesses  # no evidence at all: no guess
    assert all(int(r["n_options"]) >= P.MIN_OPTIONS and r["method"] for r in guesses.values())
    again = P.predict_job(Ctx(db), predictor=LinkPredictor(db))
    assert again.result["made"] == 0  # each gap is guessed once
    # later it reads the truth: one guess right, one wrong; its own inference never settles a guess
    time.sleep(0.01)
    g.add_triple(gaps["Newland"], cont, o=world["europe"], kind="wikidata", source="wikidata", confidence=0.9)
    g.add_triple(gaps["Eastland"], cont, o=world["europe"], status="inferred", kind="rule", source="rule")
    res = P.check(db, time.time())
    assert res == {"confirmed": 1, "refuted": 0, "expired": 0}
    db.execute("UPDATE triples SET status = 'sourced' WHERE s = ? AND p = ?", (gaps["Eastland"], cont))
    assert P.check(db, time.time())["refuted"] == 1
    row = db.one("SELECT * FROM predictions WHERE s = ? AND p = ?", (gaps["Eastland"], cont))
    assert row["state"] == "refuted" and int(row["truth"]) == world["europe"]
    sb = P.scoreboard(db)
    assert sb["confirmed"] == 1 and sb["refuted"] == 1 and sb["accuracy"] == 0.5 and 0 < sb["chance"] <= 1 / 3
    assert sb["recent_hits"][0]["subject"] == "Newland" and sb["recent_hits"][0]["guess"] == "Europe"
    line = P.describe(sb)
    assert "1 confirmed, 1 wrong (50% right; guessing would be" in line
    notes = db.query("SELECT text, detail FROM events WHERE kind = 'prediction' ORDER BY id")
    assert "before reading it; now confirmed" in notes[0]["text"] and "but it is Europe" in notes[1]["text"]
    assert "confirmed" in notes[0]["detail"]


def test_expiry_and_empty_scoreboard(db, world):
    assert P.scoreboard(db)["accuracy"] is None and "still open" in P.describe(P.scoreboard(db))
    db.execute("INSERT INTO predictions(s, p, o, score, n_options, made_at) VALUES(1, 2, 3, 0.5, 4, ?)",
               (time.time() - (P.EXPIRE_DAYS + 1) * 86400,))  # fmt: skip
    assert P.check(db, time.time())["expired"] == 1
    sb = P.scoreboard(db)
    assert sb["expired"] == 1 and sb["accuracy"] is None and sb["chance"] is None


def test_unsure_and_unnamed_are_not_guessed(db, world):
    class Shrug:
        def predict(self, s, p, options):
            from polymath.reasoning.link_prediction import Prediction

            return Prediction(options[0], 0.2, "text", {o: 1 / len(options) for o in options})

    assert P.predict_job(Ctx(db), predictor=Shrug()).result["made"] == 0
    g = world["g"]
    stub = g.upsert_entity("Q4242", "Q4242")
    g.add_triple(world["gaps"]["Mystery"], world["member"], o=stub, kind="wikidata", source="wikidata")
    g.add_triple(g.upsert_entity("Q4243", "Other"), world["member"], o=stub, kind="wikidata", source="wikidata")
    assert stub not in P.options_for(db, world["gaps"]["Mystery"], world["member"])


def test_job_without_relations_and_the_default_predictor(db, monkeypatch):
    out = P.predict_job(Ctx(db))
    assert out.result == {"made": 0, "confirmed": 0, "refuted": 0, "expired": 0, "relations": 0}


def test_default_predictor_is_loaded_when_needed(db, world, monkeypatch):
    from polymath.evaluation import jobs as vjobs

    monkeypatch.setattr(vjobs, "predictor_for", lambda ctx: LinkPredictor(ctx.db))
    assert P.predict_job(Ctx(db)).result["made"] >= 2


def test_planner_feed_face_and_cli(db, world, config, tmp_path, capsys):
    class Agent:
        def __init__(self, db):
            from polymath.core.scheduler import Scheduler

            self.db, self.scheduler = db, Scheduler(db)

    a = Agent(db)
    P.planner(a)
    assert db.scalar("SELECT COUNT(*) FROM jobs WHERE kind = 'reason.predict'") == 1
    ev = {"kind": "note", "at": time.time(), "what": "prediction", "status": "confirmed", "text": "guessed X"}
    assert "predicted" in text_of(render_event(ev))
    f = Face()
    f.update({"online": True, "state": "running", "mode": "normal", "action": "wikipedia.part"})
    f.see(ev, 5.0)
    assert f.mood(5.0) == "proud"
    P.predict_job(Ctx(db), predictor=LinkPredictor(db))
    db.conn.commit()
    path = str(tmp_path / "polymath.toml")
    assert cli.main(["--config", path, "predictions"]) == 0
    out = capsys.readouterr().out
    assert "Predicted" in out and "open:" in out and "Newland → continent → Europe" in out
    assert cli.main(["--config", path, "predictions", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["made"] >= 2
    assert Feed(db).poll()["status"]  # the feed still works with predictions present


def test_digest_line(db, world):
    from polymath.evaluation import digest

    P.predict_job(Ctx(db), predictor=LinkPredictor(db))
    time.sleep(0.01)
    world["g"].add_triple(world["gaps"]["Newland"], world["continent"], o=world["europe"], kind="wikidata",
                          source="wikidata", confidence=0.9)  # fmt: skip
    P.check(db, time.time())
    lines = digest.lines(digest.collect(db, time.time() - 86400))
    assert any(line.startswith("Predicted") and "1 came true today" in line for line in lines)
