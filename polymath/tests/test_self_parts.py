"""Specialists for weak spots, the reading strategy, and phrasing learned from reading."""

from __future__ import annotations

import json
import math
import time
from typing import Any

import pytest

from polymath.agents import store
from polymath.drive import specialists, strategy
from polymath.drive.priority import unread_entities
from polymath.interface.tell import Teller
from polymath.memory.documents import Document, DocumentStore
from polymath.memory.graph import KnowledgeGraph
from polymath.perception import phrasing
from polymath.senses.net import FetchError

DAY = 86400.0


def answers(db, p: int, right: int, wrong: int, at: float) -> None:
    """Self-test answers about relation ``p``."""
    g = KnowledgeGraph(db)
    qid = int(db.execute("INSERT INTO quizzes(created, n, correct, accuracy, chance, details) VALUES(?, ?, ?, ?, 0.25, '{}')",
                         (at, right + wrong, right, right / max(1, right + wrong))).lastrowid or 0)  # fmt: skip
    for i in range(right + wrong):
        s = g.upsert_entity(f"Qs{p}_{at}_{i}", f"S{p}x{i}")
        o = g.upsert_entity(f"Qo{p}_{i}", f"O{p}x{i}")
        tid = g.add_triple(s, p, o=o, kind="wikidata", source="wikidata")[0]
        db.execute("INSERT INTO quiz_answers(quiz_id, triple_id, question, options, answer, chosen, correct, method, "
                   "confidence) VALUES(?,?,?,?,?,?,?,?,?)",
                   (qid, tid, "q", "[]", o, o, int(i < right), "related", 0.5))  # fmt: skip


# ------------------------------------------------------------------ specialists
def test_weak_spot_gets_a_specialist_that_retires_on_recovery(db):
    g = KnowledgeGraph(db)
    birth, capital = g.predicate("P19", "place of birth"), g.predicate("P36", "capital")
    now = time.time()
    answers(db, birth, 3, 12, now - DAY)  # 20 %: a weak spot
    answers(db, capital, 18, 2, now - DAY)  # 90 %
    weak, overall = specialists.weak_spots(db, now)
    assert [w[0] for w in weak] == [birth] and 0.5 < overall < 0.7
    out = specialists.step(db, now)
    assert out["spawned"] == ["fix-place of birth"]
    agent = specialists.specialists(db)[0]
    assert agent.kind == "predict" and agent.origin == "auto" and agent.scope["predicates"] == [birth]
    assert agent.name == store.get(db, agent.id).name  # type: ignore[union-attr]
    ch = db.one("SELECT * FROM self_changes WHERE action = 'spawned'")
    assert "only 20% of the time (15 answers" in ch["summary"]
    assert specialists.step(db, now)["spawned"] == []  # one specialist per weak spot
    # too young to judge, then the relation recovers after the spawn
    later = now + 3 * DAY
    db.execute("UPDATE agents SET created = ? WHERE id = ?", (now, agent.id))
    answers(db, birth, 8, 2, now + 2 * DAY)
    res = specialists.step(db, later)
    assert res["retired"] == [agent.name]
    retired = db.one("SELECT * FROM self_changes WHERE action = 'retired'")
    assert "went from 20% to 80%" in retired["summary"]
    assert specialists.specialists(db) == []
    assert specialists.step(db, later)["spawned"] == []  # retired this week: not re-spawned straight away


def test_specialist_gives_up_and_the_cap(db, monkeypatch):
    g = KnowledgeGraph(db)
    now = time.time()
    preds = [g.predicate(f"P{900 + i}", f"rel {i}") for i in range(5)]
    for p in preds:
        answers(db, p, 2, 12, now - DAY)
    assert len(specialists.step(db, now)["spawned"]) == specialists.MAX_ACTIVE  # at most three at once
    a = specialists.specialists(db)[0]
    db.execute("UPDATE agents SET created = ? WHERE id = ?", (now - 30 * DAY, a.id))
    res = specialists.step(db, now)
    assert a.name in res["retired"]
    assert "without success" in db.scalar("SELECT summary FROM self_changes WHERE action = 'retired'")
    out = specialists.specialists_job(type("C", (), {"db": db})())
    assert out.done


def test_specialists_leave_your_agents_alone(db):
    from polymath.agents import society

    g = KnowledgeGraph(db)
    p = g.predicate("P19", "place of birth")
    answers(db, p, 2, 12, time.time() - DAY)
    mine = society.spawn(db, "research volcanoes")
    specialists.step(db)
    assert store.get(db, mine.id).status == "active" and mine.origin == "user"  # type: ignore[union-attr]
    from polymath.core.scheduler import Scheduler

    agent = type("A", (), {"db": db, "scheduler": Scheduler(db)})()
    specialists.planner(agent)
    assert db.scalar("SELECT COUNT(*) FROM jobs WHERE kind = 'self.specialists'") == 1


# ------------------------------------------------------------------ reading strategy
def docs_and_facts(db, source: str, n_docs: int, n_facts: int, cpu: float, kind: str, at: float) -> None:
    store_ = DocumentStore(db)
    g = KnowledgeGraph(db)
    p = g.predicate("P1", "related")
    for i in range(n_docs):
        did, _ = store_.add(Document(source, f"{source}{i}", f"{source} {i}", f"text {source} {i} " * 10, "CC0"))
        db.execute("UPDATE documents SET fetched = ? WHERE id = ?", (at, did))
        if i < n_facts:
            a, b = (
                g.upsert_entity(f"Q{source}{i}a", f"A{source}{i}"),
                g.upsert_entity(f"Q{source}{i}b", f"B{source}{i}"),
            )
            g.add_triple(a, p, o=b, kind="infobox", source="wikipedia", doc_id=did)
    db.execute("INSERT INTO cycles(started, ended, action, status, cpu_seconds) VALUES(?, ?, ?, 'done', ?)",
               (at, at, kind, cpu))  # fmt: skip


def test_reading_weights_follow_what_teaches_most(db):
    now = time.time()
    docs_and_facts(db, "pubmed", 40, 40, 120.0, "pubmed.ingest", now - DAY)  # 20 facts per CPU-minute
    docs_and_facts(db, "gutenberg", 40, 2, 120.0, "gutenberg.books", now - DAY)  # 1 per CPU-minute
    docs_and_facts(db, "stackexchange", 30, 15, 120.0, "stackexchange.ingest", now - DAY)
    res = strategy.step(db, now)
    w = res["weights"]
    assert w["pubmed"] > 1.0 > w["gutenberg"] and w["gutenberg"] >= 0.5 and w["pubmed"] <= 2.0
    assert strategy.weight(db, "pubmed") == w["pubmed"] and strategy.weight(db, "web") == 1.0
    summary = db.scalar("SELECT summary FROM self_changes WHERE area = 'reading'")
    assert summary.startswith("Reading more pubmed") and "less gutenberg" in summary
    again = strategy.step(db, now)
    assert again["changed"] == []  # no change, no new changelog line
    assert db.scalar("SELECT COUNT(*) FROM self_changes WHERE area = 'reading'") == 1
    assert strategy.strategy_job(type("C", (), {"db": db})()).done


def link_docs(db, source: str, share: float) -> None:
    """Link a share of a source's documents to a known thing (what reading them taught)."""
    e = KnowledgeGraph(db).upsert_entity("Qlinked", "Linked")
    rows = db.query("SELECT id FROM documents WHERE source = ? ORDER BY id", (source,))
    for r in rows[: int(len(rows) * share)]:
        db.execute("INSERT INTO doc_entities(doc_id, entity_id, count, score) VALUES(?, ?, 1, 1.0)", (r["id"], e))


def test_papers_that_teach_through_links_are_not_starved(db):
    """Sources that add no facts but whose documents link to known things keep a fair share; idle ones drop."""
    now = time.time()
    docs_and_facts(db, "wikipedia", 40, 40, 60.0, "wikipedia.part", now - DAY)  # 40 facts per CPU-minute
    docs_and_facts(db, "openalex", 40, 0, 60.0, "openalex.ingest", now - DAY)
    docs_and_facts(db, "pubmed", 40, 0, 60.0, "pubmed.ingest", now - DAY)
    docs_and_facts(db, "feed", 40, 0, 60.0, "feeds.poll", now - DAY)  # nothing linked: taught nothing
    docs_and_facts(db, "web", 40, 0, 60.0, "crawl.step", now - DAY)
    link_docs(db, "openalex", 1.0)  # 20 points per CPU-minute
    link_docs(db, "pubmed", 0.5)  # 10
    res = strategy.step(db, now)
    ys, w = res["sources"], res["weights"]
    assert ys["openalex"]["linked"] == 40 and ys["openalex"]["per_cpu_min"] == pytest.approx(20.0)
    assert w["openalex"] == pytest.approx(1.0)  # the median of the sources that taught anything
    assert w["wikipedia"] == pytest.approx(math.sqrt(2), abs=1e-3) and w["pubmed"] == pytest.approx(
        math.sqrt(0.5), abs=1e-3
    )
    assert w["feed"] == w["web"] == 0.5
    summary = db.scalar("SELECT summary FROM self_changes WHERE area = 'reading'")
    assert "more wikipedia (40 per CPU-minute)" in summary and "less feed" in summary and "web" in summary
    assert "half a point per document linked" in summary
    assert strategy.rate(1494.746) == "1,495" and strategy.rate(12.54) == "12.5" and strategy.rate(0.234) == "0.234"


def test_a_long_reading_change_lists_the_top_three(db):
    now = time.time()
    for i, src in enumerate(("wikipedia", "openalex", "pubmed", "gutenberg", "stackexchange")):
        docs_and_facts(db, src, 40, 40 if i == 0 else 0, 60.0, strategy.INGEST[src][0], now - DAY)
    summary_res = strategy.step(db, now)
    assert sorted(summary_res["changed"]) == ["gutenberg", "openalex", "pubmed", "stackexchange"]
    summary = db.scalar("SELECT summary FROM self_changes WHERE area = 'reading'")
    assert summary.count("per CPU-minute") == 3 and "and 1 more" in summary


def test_reading_weights_steer_planning(db, config):
    from polymath.core.jobs import JobContext
    from polymath.core.scheduler import Scheduler
    from polymath.senses import sources

    db.kv_set("reading_weights", {"gutenberg": 2.0})
    db.kv_set("topic_yield", {"7": 1.5})
    assert strategy.topic_factor(db) == {7: 1.5}

    class NoNet:
        def get(self, *a: Any, **k: Any) -> Any:
            raise FetchError("offline")

    ctx = JobContext(config=config, db=db, scheduler=Scheduler(db), job=None,  # type: ignore[arg-type]
                     deadline=time.monotonic() + 30, stop_event=__import__("threading").Event(),
                     services={"http": NoNet()})  # fmt: skip
    sources.plan_sources(ctx)
    prio = db.scalar("SELECT priority FROM jobs WHERE key LIKE 'download:gutenberg-catalog:%'")
    assert prio == 1.0  # 0.5 × weight 2


def test_topic_yield_and_basics_first(db):
    now = time.time()
    store_ = DocumentStore(db)
    g = KnowledgeGraph(db)
    p = g.predicate("P1", "related")
    t1 = int(
        db.execute("INSERT INTO topics(name, kind, n_docs, n_total) VALUES('Rich', 'category', 5, 5)").lastrowid or 0
    )
    t2 = int(
        db.execute("INSERT INTO topics(name, kind, n_docs, n_total) VALUES('Poor', 'category', 5, 5)").lastrowid or 0
    )
    common = g.upsert_entity("Q900", "Atom", wiki_title="Atom")
    rare = g.upsert_entity("Q901", "Quark", wiki_title="Quark")
    db.execute("UPDATE entities SET pagerank = 0.5 WHERE id IN (?, ?)", (common, rare))
    for i in range(6):
        did, _ = store_.add(Document("wikipedia", f"r{i}", f"R{i}", "physics text " * 20, "CC0"))
        db.execute("INSERT INTO doc_topics(doc_id, topic_id, weight) VALUES(?,?,1)", (did, t1 if i < 4 else t2))
        db.execute("INSERT INTO doc_entities(doc_id, entity_id, count, score) VALUES(?,?,1,1)", (did, common))
        if i == 0:
            db.execute("INSERT INTO doc_entities(doc_id, entity_id, count, score) VALUES(?,?,1,1)", (did, rare))
        if i < 4:
            for j in range(3):
                a, b = g.upsert_entity(f"Qa{i}{j}", f"A{i}{j}"), g.upsert_entity(f"Qb{i}{j}", f"B{i}{j}")
                g.add_triple(a, p, o=b, kind="infobox", source="wikipedia", doc_id=did)
    for t in (t1, t2):
        db.execute("INSERT INTO topic_effort(topic_id, cpu_seconds, at) VALUES(?, 30, ?)", (t, now - 3600))
    y = strategy.topic_yield(db, now)
    assert y[t1] > y[t2] and 0.5 <= y[t2] <= y[t1] <= 1.5
    # basics first: the article its documents refer to most comes first
    assert [title for _e, title in unread_entities(db, t1)] == ["Atom", "Quark"]
    db.execute("DELETE FROM topic_effort")
    assert strategy.topic_yield(db, now) == {}  # no targeted reading measured: no factors


# ------------------------------------------------------------------ learned phrasing
def test_readable_phrasings():
    assert phrasing.readable("was born in") and phrasing.readable("was directed by") and phrasing.readable("founded")
    for bad in (
        "lieth",
        "and",
        ",",
        "was <det>",
        "is located in <num>",
        "where he saw",
        "is",
        "was a leading figure of the",
    ):
        assert not phrasing.readable(bad), bad


def test_learn_phrasings_and_tell_uses_them(db):
    g = KnowledgeGraph(db)
    directed = g.predicate("P58", "screenwriter")
    birth = g.predicate("P19", "place of birth")
    human = g.upsert_entity("Q5", "human")
    rows = [(directed, "was written by", 0, 6, 0, 0.9), (directed, "and", 0, 9, 0, 0.9),
            (birth, "was born in", 0, 5, 0, 0.8), (birth, "lieth", 0, 7, 0, 0.9)]  # fmt: skip
    for r in rows:
        db.execute("INSERT INTO patterns(predicate_id, middle, order_flag, positive, negative, confidence) "
                   "VALUES(?,?,?,?,?,?)", r)  # fmt: skip
    res = phrasing.learn(db)
    assert res["learned"] == 2 and phrasing.phrasings(db) == {directed: "was written by", birth: "was born in"}
    summary = db.scalar("SELECT summary FROM self_changes WHERE area = 'writing'")
    assert "Learned to write 2 new kinds of sentence" in summary and "“X was written by Y” for screenwriter" in summary
    assert phrasing.learn(db)["learned"] == 0  # already known
    film = g.upsert_entity("Q900", "Twelve Angry Men", description="1957 film")
    lumet = g.upsert_entity("Q901", "Reginald Rose")
    g.add_triple(film, directed, o=lumet, kind="wikidata", source="wikidata")
    p = Teller(db).tell("Twelve Angry Men").paragraph
    assert "Twelve Angry Men was written by Reginald Rose." in p  # not "Its screenwriter is Reginald Rose"
    assert human  # (fixture sanity)


def test_phrasing_planner_and_job(db):
    from polymath.core.scheduler import Scheduler

    agent = type("A", (), {"db": db, "scheduler": Scheduler(db)})()
    phrasing.planner(agent)
    assert db.scalar("SELECT COUNT(*) FROM jobs WHERE kind = 'perception.phrasing'") == 0
    pid = KnowledgeGraph(db).predicate("P1", "related")
    db.execute("INSERT INTO patterns(predicate_id, middle, order_flag, positive, negative, confidence) "
               "VALUES(?, 'x', 0, 1, 0, 0.1)", (pid,))  # fmt: skip
    phrasing.planner(agent)
    assert db.scalar("SELECT COUNT(*) FROM jobs WHERE kind = 'perception.phrasing'") == 1
    assert phrasing.phrasing_job(type("C", (), {"db": db})()).result["learned"] == 0
    assert json.dumps(phrasing.phrasings(db)) == "{}"
