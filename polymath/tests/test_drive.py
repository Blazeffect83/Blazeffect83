"""Phase 7: PageRank, curiosity priorities, targeted reading, the Thompson-sampling bandit, user-requested learning."""

from __future__ import annotations

import json
import random
import threading
import time
from types import SimpleNamespace

import numpy as np

from polymath.core import inbox
from polymath.core.app import build_agent
from polymath.core.jobs import JobContext, JobRegistry, noop_handler
from polymath.core.loop import BodyState, Decision, Observation
from polymath.core.scheduler import Job, QueueStats, Scheduler
from polymath.drive import jobs as dj
from polymath.drive.bandit import URGENT, ArmStats, BanditPolicy
from polymath.drive.learn import INTEREST_BOOST, interests, learn_job
from polymath.drive.pagerank import entity_pagerank, sparse_pagerank
from polymath.drive.priority import compute_priorities, explain_topic, top_topics, unread_entities, weakest_topics
from polymath.memory.documents import DocumentStore
from polymath.senses.crawler import Crawler
from tests.fixtures.kb import build
from tests.fixtures.typing import some
from tests.webserver import client


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


def dense_pagerank(src, dst, n, d=0.85, iters=200):
    m = np.zeros((n, n))
    out = np.bincount(src, minlength=n)
    for a, b in zip(src, dst, strict=True):
        m[b, a] += 1.0 / out[a]
    m[:, out == 0] = 1.0 / n  # dangling nodes link everywhere
    r = np.full(n, 1.0 / n)
    for _ in range(iters):
        r = (1 - d) / n + d * m @ r
    return r


def test_sparse_pagerank_matches_dense_reference():
    rng = np.random.default_rng(0)
    n = 60
    src = rng.integers(0, n, 300)
    dst = rng.integers(0, n, 300)
    keep = src != dst
    src, dst = src[keep], dst[keep]
    src[:5] = 0  # node 59 may be dangling; also make a hub
    pr = sparse_pagerank(src, dst, n, iters=200, tol=1e-14)
    assert abs(pr.sum() - 1) < 1e-9
    np.testing.assert_allclose(pr, dense_pagerank(src, dst, n), atol=1e-8)
    star = sparse_pagerank(np.array([1, 2, 3, 4]), np.array([0, 0, 0, 0]), 5)
    assert star.argmax() == 0 and star[0] > 3 * star[1]
    assert sparse_pagerank(np.array([], dtype=np.int64), np.array([], dtype=np.int64), 0).size == 0


def test_entity_pagerank_stores_scaled_scores(db):
    assert entity_pagerank(db) == {"entities": 0}
    kb = build(db)
    res = entity_pagerank(db)
    assert res["entities"] == db.scalar("SELECT COUNT(*) FROM entities") and res["edges"] > 0
    pr = {int(r["id"]): float(r["pagerank"]) for r in db.query("SELECT id, pagerank FROM entities")}
    assert abs(sum(pr.values()) / len(pr) - 1.0) < 1e-6  # 1.0 = average importance
    country_cls = int(db.scalar("SELECT id FROM entities WHERE key='Q6256'"))
    assert pr[country_cls] == max(pr.values())  # every country is an instance of it
    assert pr[kb.cities[0]] > pr[kb.people[0]]  # people have no entity links
    db.execute("UPDATE triples SET holdout=1")
    db.execute("DELETE FROM doc_entities")
    assert entity_pagerank(db)["edges"] == 0


def test_priorities_gap_importance_novelty_effort_and_boost(db):
    kb = build(db)
    entity_pagerank(db)
    res = compute_priorities(db)
    assert res["topics"] == 2
    geo = some(explain_topic(db, kb.topics["Geography"]))
    # every linked capital is unread → structural gap (40 + 1) / (40 + 2)
    assert geo["evidence"]["gap"] == {"source": "unread_share", "linked": 40, "unread": 40}
    assert abs(geo["gap"] - 41 / 42) < 1e-9 and geo["importance"] == 1.0 and geo["novelty"] == 1.0
    assert geo["formula"].startswith("priority ") and "× importance 1.000" in geo["formula"]
    hist = some(explain_topic(db, kb.topics["History"]))
    assert hist["importance"] == 0.0 and hist["priority"] == 0.0  # no linked entities → nothing to gain
    assert top_topics(db, 1)[0]["name"] == "Geography"
    assert weakest_topics(db, 5)[0]["name"] == "Geography"  # History is below the importance floor
    assert explain_topic(db, 99999) is None
    # quiz results replace the structural proxy
    db.execute("INSERT INTO quizzes(created, n, correct, accuracy, chance, details) VALUES(0, 4, 3, .75, .25, '{}')")
    for ok in (1, 1, 1, 0):
        db.execute(
            "INSERT INTO quiz_answers(quiz_id, triple_id, question, options, answer, chosen, correct, method, "
            "confidence, topic_id) VALUES(1, 1, 'q', '[]', 1, 1, ?, 'text', 1, ?)",
            (ok, kb.topics["Geography"]),
        )
    db.execute(
        "INSERT INTO topic_effort(topic_id, cpu_seconds, at) VALUES(?, 30, ?)", (kb.topics["Geography"], time.time())
    )
    db.execute(
        "INSERT INTO topic_effort(topic_id, cpu_seconds, at) VALUES(?, 10, ?)", (kb.topics["History"], time.time())
    )
    compute_priorities(db)
    geo = some(explain_topic(db, kb.topics["Geography"]))
    assert geo["evidence"]["gap"]["source"] == "quiz" and abs(geo["gap"] - (1 - 4 / 6)) < 1e-9
    assert abs(geo["effort"] - 0.75) < 1e-9 and abs(geo["novelty"] - 0.5) < 1e-3
    expect = geo["gap"] * geo["importance"] * geo["novelty"] * (1 - geo["effort"])
    assert abs(geo["priority"] - expect) < 1e-9
    db.kv_set("user_interests", {str(kb.topics["Geography"]): time.time() + 60, "777": time.time() - 1})
    assert interests(db) == {kb.topics["Geography"]: db.kv_get("user_interests")[str(kb.topics["Geography"])]}
    compute_priorities(db)
    boosted = some(explain_topic(db, kb.topics["Geography"]))
    assert abs(boosted["priority"] - INTEREST_BOOST * expect) < 1e-9
    assert boosted["evidence"]["user_interest_boost"] == INTEREST_BOOST


def test_priorities_job_pursues_unread_entities_from_the_dump_index(config, db):
    kb = build(db)
    entity_pagerank(db)
    for i in range(0, 40, 2):  # half of the capitals are in the part of the dump indexed so far
        db.execute(
            "INSERT INTO wiki_index(lang, title, dump_url, stream, next) VALUES('en', ?, 'u', 0, 1)",
            (f"Capitol{i:02d}",),
        )
    assert len(unread_entities(db, kb.topics["Geography"], 100)) == 40
    out = dj.priorities_job(ctx_for(config, db, "drive.priorities"))
    assert out.result["pursued"] == [
        {"topic": "Geography", "titles": dj.TITLES_PER_TOPIC, "indexed": out.result["pursued"][0]["indexed"]}
    ]
    job = db.one("SELECT payload, priority FROM jobs WHERE kind='wikipedia.titles'")
    assert json.loads(job["payload"])["topic_id"] == kb.topics["Geography"] and job["priority"] == 1.8
    again = dj.priorities_job(ctx_for(config, db, "drive.priorities"))
    assert again.result["pursued"] == []  # same hour: idempotent key
    assert dj.pagerank_job(ctx_for(config, db, "drive.pagerank")).result["entities"] > 0

    agent = SimpleNamespace(db=db, scheduler=Scheduler(db))
    dj.planner(agent)
    assert {"drive.pagerank", "drive.priorities"} <= {r["kind"] for r in db.query("SELECT kind FROM jobs")}


def obs(db, mode="normal", hour_ts=None):
    st = Scheduler(db).stats()
    return Observation(now=hour_ts or time.time(), body=BodyState(mode=mode), queue=st, cycle=1)


def registry():
    reg = JobRegistry()
    reg.register("read.a", noop_handler, "", action="read")
    reg.register("read.b", noop_handler, "", action="read")
    reg.register("learn.x", noop_handler, "", action="learn", heavy=True)
    reg.register("crawl.y", noop_handler, "", action="crawl")
    return reg


def test_arm_stats_welford():
    a = ArmStats()
    xs = [1.0, 2.0, 4.0, 7.0]
    for x in xs:
        a.update(x)
    assert a.n == 4 and abs(a.mean - np.mean(xs)) < 1e-12 and abs(a.var - np.var(xs, ddof=1)) < 1e-12
    assert ArmStats().var == 1.0


def test_bandit_groups_contexts_urgent_and_heavy(db):
    pol = BanditPolicy(db, rng=random.Random(1))
    reg = registry()
    assert pol.decide(Observation(time.time(), BodyState(), QueueStats(), 0), reg).action == "any"
    s = Scheduler(db)
    for k in ("read.a", "read.b", "learn.x", "crawl.y"):
        s.enqueue(k, {}, key=k)
    s.enqueue("unknown.kind", {}, key="u")
    d = pol.decide(obs(db), reg)
    assert d.action in {"read", "learn", "crawl"} and set(d.scores) == {"read", "learn", "crawl"}
    assert d.context.startswith("normal|") and d.context.endswith("|clear")
    if d.action == "read":
        assert d.kinds == ["read.a", "read.b"]
    row = db.one("SELECT * FROM decisions ORDER BY id DESC LIMIT 1")
    assert row["chosen"] == d.action and set(json.loads(row["options"])) == set(d.scores)
    throttled = pol.decide(obs(db, "throttle"), reg)
    assert "learn" not in throttled.scores and throttled.context.startswith("throttle|")
    s.enqueue("crawl.y", {}, key="urgent", priority=URGENT + 0.1)
    u = pol.decide(obs(db), reg)
    assert u.action == "urgent" and u.kinds == ["crawl.y"]
    night = time.mktime((2026, 1, 1, 3, 0, 0, 0, 0, -1))
    assert "|night|" in BanditPolicy.context_of(Observation(night, BodyState(), QueueStats(), 0), True)
    assert BanditPolicy.context_of(Observation(night + 12 * 3600, BodyState(), QueueStats(), 0), True).endswith(
        "day|backlog"
    )


def test_bandit_only_heavy_while_yielding_is_idle(db):
    reg = registry()
    Scheduler(db).enqueue("learn.x", {}, key="h")
    d = BanditPolicy(db).decide(obs(db, "yield"), reg)
    assert d.action == "idle" and d.kinds == []


def test_bandit_learns_to_prefer_the_productive_arm(db):
    pol = BanditPolicy(db, rng=random.Random(7))
    reg = registry()
    s = Scheduler(db)
    s.enqueue("read.a", {}, key="r")
    s.enqueue("crawl.y", {}, key="c")
    job = Job(1, "read.a", "r", {"topic_id": 5}, None, 0, 0, 0, 5, 0)
    picks = []
    for _ in range(300):
        d = pol.decide(obs(db), reg)
        picks.append(d.action)
        pol.learn(d, job, reward=5.0 if d.action == "read" else 0.2, cpu=0.1, value=1.0)
    assert picks[-100:].count("read") > 85  # converges on the arm with higher value per CPU second
    arms = {a["action"]: a for a in pol.arms()}
    assert arms["read"]["mean"] > arms["crawl"]["mean"] and arms["read"]["n"] > arms["crawl"]["n"]
    last = db.one("SELECT * FROM decisions ORDER BY id DESC LIMIT 1")
    assert last["reward"] is not None and last["job_id"] == 1
    assert db.scalar("SELECT SUM(cpu_seconds) FROM topic_effort WHERE topic_id=5") > 0
    pol.learn(Decision(kinds=None, action="urgent"), job, 1.0, 0.1, 1.0)  # overrides teach nothing
    assert sum(a["n"] for a in pol.arms()) == 300
    db.execute("UPDATE decisions SET at=0")
    pol.learn(pol.decide(obs(db), reg), job, 1.0, 0.1, 1.0)
    assert db.scalar("SELECT COUNT(*) FROM decisions") == 1  # 30-day retention


def test_agent_uses_the_bandit_and_records_why(config):
    agent = build_agent(config, planners=False)
    agent.start()
    agent.registry.register("t.noop", noop_handler, "", action="test")
    agent.scheduler.enqueue("t.noop", {}, key="n1")
    rec = agent.cycle()
    assert rec.status == "done"
    assert some(agent.db.one("SELECT chosen FROM decisions"))["chosen"] == "test"
    assert some(agent.db.one("SELECT n FROM bandit_arms WHERE action='test'"))["n"] == 1
    agent.shutdown()
    agent.db.close()


def test_learn_job_topic_entity_and_unknown(config, db):
    kb = build(db)
    for i in range(10):
        db.execute(
            "INSERT INTO wiki_index(lang, title, dump_url, stream, next) VALUES('en', ?, 'u', 0, 1)",
            (f"Capitol{i:02d}",),
        )
    out = learn_job(ctx_for(config, db, "drive.learn", {"query": "Country03"}))
    acts = " ".join(out.result["actions"])
    assert "recognised “Country03” as Country03" in acts and "queued 1 Wikipedia articles" in acts
    job = db.one("SELECT payload, priority FROM jobs WHERE kind='wikipedia.titles'")
    assert json.loads(job["payload"])["titles"] == ["Capitol03"] and job["priority"] == 2.5
    geo = learn_job(ctx_for(config, db, "drive.learn", {"query": "Geography"}))
    assert "prioritising topics: Geography" in " ".join(geo.result["actions"])
    assert kb.topics["Geography"] in interests(db)
    none = learn_job(ctx_for(config, db, "drive.learn", {"query": "quantum chromodynamics"}))
    assert "I know nothing about" in none.result["actions"][0]
    assert db.kv_get("pending_interests") == ["quantum chromodynamics"]
    assert learn_job(ctx_for(config, db, "drive.learn", {"query": " "})).result["actions"][0].startswith("nothing")


def test_learn_job_url_allows_domain_and_queues_crawl(config, db):
    crawler = Crawler(db, client(), DocumentStore(db), user_agent="t", allow_domains=["example.org"])
    out = learn_job(
        ctx_for(config, db, "drive.learn", {"query": "https://Docs.Python.org/3/"}, services={"crawler": crawler})
    )
    acts = " ".join(out.result["actions"])
    assert "allowed crawling of docs.python.org" in acts and "queued https://docs.python.org/3/" in acts
    assert "docs.python.org" in crawler.allow and db.kv_get("user_allow_domains") == ["docs.python.org"]
    assert crawler.frontier.pending() == 1 and db.scalar("SELECT COUNT(*) FROM jobs WHERE kind='crawl.step'") == 1
    again = learn_job(
        ctx_for(config, db, "drive.learn", {"query": "https://docs.python.org/3/x"}, services={"crawler": crawler})
    )
    assert not any("allowed" in a for a in again.result["actions"])
    bad = learn_job(ctx_for(config, db, "drive.learn", {"query": "http://"}, services={"crawler": crawler}))
    assert "not a crawlable URL" in bad.result["actions"][0]
    agent = build_agent(config, planners=False)  # a restart keeps the user's permission
    assert "docs.python.org" in agent.services["crawler"].allow
    agent.db.close()


def test_inbox_learn_request_becomes_a_drive_job(config, db):
    inbox.submit(config.paths.data_dir, {"type": "learn", "query": "  black holes "})
    inbox.submit(config.paths.data_dir, {"type": "learn", "query": ""})
    assert inbox.ingest(db, Scheduler(db), config.paths.data_dir) == 2
    job = db.one("SELECT payload, priority FROM jobs WHERE kind='drive.learn'")
    assert json.loads(job["payload"]) == {"query": "black holes"} and job["priority"] == 3.5
    assert db.scalar("SELECT COUNT(*) FROM jobs WHERE kind='drive.learn'") == 1
