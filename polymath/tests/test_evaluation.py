"""Phase 8: hold-out marking, sibling-distractor quizzes, nightly report, evaluation jobs."""

from __future__ import annotations

import json
import random
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from polymath.core.jobs import JobContext
from polymath.core.scheduler import Job, Scheduler
from polymath.evaluation import jobs as ej
from polymath.evaluation.quiz import OPTIONS, is_holdout, make_question, mark_holdout, phrase_question, run_quiz
from polymath.evaluation.report import collect, to_markdown, write_report
from polymath.interface.answer import Answerer
from polymath.reasoning.link_prediction import LinkPredictor
from tests.fixtures.kb import build
from tests.fixtures.typing import some


def ctx_for(config, db, kind, payload=None, services=None):
    s = Scheduler(db)
    jid, _ = s.enqueue(kind, payload or {}, key=f"{kind}:{time.time_ns()}")
    return JobContext(
        config=config,
        db=db,
        scheduler=s,
        job=Job(jid, kind, f"k{jid}", payload or {}, None, 0, 0, 0, 5, 0),
        deadline=time.monotonic() + 60,
        stop_event=threading.Event(),
        services=services or {},
    )


def test_holdout_is_deterministic_and_close_to_fraction():
    picks = [is_holdout(i, 0.05) for i in range(20_000)]
    assert picks == [is_holdout(i, 0.05) for i in range(20_000)]
    assert 0.04 < sum(picks) / len(picks) < 0.06
    assert not any(is_holdout(i, 0.0) for i in range(1000)) and all(is_holdout(i, 1.0) for i in range(1000))


def test_mark_holdout_only_wikidata_entity_facts_and_is_incremental(db):
    kb = build(db)
    n = mark_holdout(db, 0.5)
    held = db.query("SELECT t.id, t.o, t.p FROM triples t WHERE holdout=1")
    assert n == len(held) > 10
    assert all(int(r["o"]) != 0 for r in held)  # literal facts (population) are never held out
    assert kb.preds["birth"] not in {int(r["p"]) for r in held}  # infobox facts are not Wikidata facts
    assert all(is_holdout(int(r["id"]), 0.5) for r in held)
    assert mark_holdout(db, 0.5) == 0  # cursor: nothing new to look at
    s, k = kb.countries[0], kb.cities[1]
    kb.graph.add_triple(s, kb.preds["country"], o=k, kind="wikidata", source="wikidata")
    assert db.kv_get("holdout_cursor") > 0 and mark_holdout(db, 1.0) == 1


@pytest.mark.parametrize(
    ("label", "subject", "question"),
    [
        ("capital", "France", "What is the capital of France?"),
        ("capital of", "Belgorod", "What is Belgorod the capital of?"),
        ("instance of", "Paris", "What is Paris an instance of?"),
        ("subclass of", "cat", "What is cat a subclass of?"),
        ("part of", "Bavaria", "What is Bavaria part of?"),
        ("followed by", "1789", "What is 1789 followed by?"),
        (
            "located in the administrative territorial entity",
            "Pangkal Pinang",
            "What is Pangkal Pinang located in the administrative territorial entity?",
        ),
        ("follows", "1789", "What does 1789 follow?"),
        ("shares border with", "China", "What does China share border with?"),
        ("diplomatic relation", "Portugal", "What is the diplomatic relation of Portugal?"),
        ("  ", "X", "What is related to X?"),
    ],
)
def test_questions_read_naturally(label, subject, question):
    assert phrase_question(label, subject) == question


def test_question_uses_sibling_distractors_of_the_same_type(db):
    kb = build(db)
    p31 = kb.preds["p31"]
    t = db.one("SELECT id, s, p, o FROM triples WHERE s=? AND p=?", (kb.countries[3], kb.preds["capital"]))
    q = make_question(db, t, random.Random(1), p31)
    assert q is not None and q["answer"] == kb.cities[3] and len(q["options"]) == OPTIONS
    assert set(q["options"]) <= set(kb.cities)  # siblings: other capitals, all cities
    assert q["question"] == "What is the capital of Country03?" and q["predicate_label"] == "capital"
    assert [x["label"] for x in q["labels"]] == [
        db.scalar("SELECT label FROM entities WHERE id=?", (o,)) for o in q["options"]
    ]
    # a predicate with too few distinct objects cannot make a 4-option question
    lonely = db.one("SELECT id, s, p, o FROM triples WHERE p=? LIMIT 1", (p31,))
    assert make_question(db, lonely, random.Random(1), p31) is None


def test_quiz_beats_chance_from_text_and_records_answers(db):
    build(db)
    mark_holdout(db, 0.3)
    res = run_quiz(db, LinkPredictor(db), size=20, seed=3)
    assert res["n"] >= 10 and res["chance"] == 0.25
    assert res["accuracy"] > 0.6  # the articles state the capitals; held-out facts are invisible to the graph
    assert set(res["by_method"]) <= {"graph", "text", "related", "association", "embedding", "none"}
    assert res["evidence_alone"] and res["weights"]["graph"] > 0
    rows = db.query("SELECT * FROM quiz_answers WHERE quiz_id=?", (res["quiz_id"],))
    assert len(rows) == res["n"] and sum(r["correct"] for r in rows) == res["correct"]
    assert any(r["topic_id"] for r in rows)  # subjects' articles carry topics → feeds the drive's gap
    stored = db.one("SELECT * FROM quizzes WHERE id=?", (res["quiz_id"],))
    assert stored["accuracy"] == res["correct"] / res["n"] and json.loads(stored["details"])["by_method"]


def test_held_out_facts_are_invisible_but_recoverable(db):
    kb = build(db)
    db.execute("UPDATE triples SET holdout=1 WHERE s=? AND p=?", (kb.countries[5], kb.preds["capital"]))
    a = Answerer(db)
    assert "Capitol05" not in " ".join(s.text for s in a.facts(some(a.find_entity("Country05")), [kb.preds["capital"]]))
    options = [kb.cities[1], kb.cities[5], kb.cities[7], kb.cities[9]]
    pred = LinkPredictor(db).predict(kb.countries[5], kb.preds["capital"], options)
    assert "graph" not in pred.by_method  # the hidden fact itself is not evidence
    assert pred.best == kb.cities[5] and pred.method in {"related", "text"}  # recovered from other knowledge
    best, conf, _method = a.score_options(kb.countries[5], kb.preds["capital"], options)
    assert best == kb.cities[5] and conf > 0.25
    db.execute("UPDATE triples SET holdout=0")
    pred = LinkPredictor(db).predict(kb.countries[5], kb.preds["capital"], options)
    assert pred.best == kb.cities[5] and pred.method == "graph" and pred.confidence > 0.9


def test_quiz_without_material(db):
    assert run_quiz(db, LinkPredictor(db), size=5)["n"] == 0


def test_report_collect_markdown_and_upsert(config, db):
    build(db)
    mark_holdout(db, 0.3)
    run_quiz(db, LinkPredictor(db), size=10, seed=1)
    db.execute(
        "INSERT INTO cycles(started, ended, action, status, cpu_seconds, wall_seconds, value, reward, "
        "detail) VALUES(?, ?, 'read', 'done', 2.5, 3, 1.0, 0.4, '')",
        (time.time() - 5, time.time()),
    )
    db.execute("INSERT INTO source_reliability(source, reliability, n_facts, updated) VALUES('wikidata', 0.97, 10, 0)")
    db.kv_set("linker_model", {"validation": {"precision": 0.9, "recall": 0.8}})
    db.kv_set(
        "embedding_quality",
        {"relation_hit@10": {"hit_rate": 0.3, "random_baseline": 0.001}, "alias_similarity": {"auc": 0.81}},
    )
    r = collect(db, time.time() - 3600)
    assert r["documents"]["wikipedia"]["total"] == 45 and r["graph"]["holdout"] > 0
    assert r["quiz"]["latest"]["n"] > 0 and r["effort"]["read"]["cycles"] == 1
    md = to_markdown(r)
    for needle in (
        "# Polymath nightly report",
        "Latest quiz",
        "chance 25%",
        "precision 0.9",
        "alias-pair AUC 0.81",
        "| read | 1 | 2.5 |",
        "wikidata: 0.97",
    ):
        assert needle in md, needle
    out = write_report(db, config.paths.report_dir)
    again = write_report(db, config.paths.report_dir)
    assert out["path"] == again["path"] and db.scalar("SELECT COUNT(*) FROM reports") == 1
    day_json = json.loads(Path(out["path"].replace(".md", ".json")).read_text())
    assert day_json["quiz"]["latest"]["n"] > 0


def test_markdown_without_quiz(db):
    md = to_markdown(collect(db, 0))
    assert "No quiz yet" in md and "(none yet)" in md


def test_evaluation_jobs_and_planner(config, db):
    build(db)
    assert ej.holdout_job(ctx_for(config, db, "eval.holdout")).result["held_out"] > 0
    out = ej.quiz_job(ctx_for(config, db, "eval.quiz"))
    assert out.done and out.result["n"] > 0 and db.kv_get("last_quiz")["n"] == out.result["n"]
    rep = ej.report_job(ctx_for(config, db, "eval.report"))
    assert rep.result["path"].endswith(".md")

    agent = SimpleNamespace(db=db, scheduler=Scheduler(db), config=config)
    ej.planner(agent)
    kinds = {r["kind"] for r in db.query("SELECT kind FROM jobs WHERE state='queued'")}
    assert {"eval.holdout", "eval.quiz", "eval.report"} <= kinds
