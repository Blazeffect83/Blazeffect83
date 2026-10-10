"""The agent society: directives, scopes, every skill and how it is verified, rewards, allocation, evolution, CLI."""

from __future__ import annotations

import json
import random
import threading
import time

import pytest

from polymath.agents import directives, skills, society, store
from polymath.agents.store import REWARDS
from polymath.core.jobs import JobContext
from polymath.core.scheduler import Job, Scheduler
from polymath.evaluation.quiz import mark_holdout
from polymath.memory.documents import Document, DocumentStore
from polymath.memory.text_index import TextIndex
from tests.fixtures.kb import build


def ctx_for(config, db, kind="agents.step", payload=None):
    s = Scheduler(db)
    jid, _ = s.enqueue(kind, payload or {}, key=f"{kind}:{time.time_ns()}")
    return JobContext(
        config=config, db=db, scheduler=s, job=Job(jid, kind, f"k{jid}", payload or {}, None, 0, 0, 0, 5, 0),
        deadline=time.monotonic() + 30, stop_event=threading.Event(), services={},
    )  # fmt: skip


def runtime(config, db, agent, seed=1):
    return skills.Runtime(db, Scheduler(db), config, {}, store.get(db, agent.id), random.Random(seed))


# ------------------------------------------------------------------ directives
@pytest.mark.parametrize(
    ("text", "kind", "subject", "qualifier"),
    [
        ("research black holes", "research", "black holes", None),
        ("Please become an expert on Roman history.", "research", "Roman history", None),
        ("learn about volcanoes", "research", "volcanoes", None),
        ("Volcanoes", "research", "Volcanoes", None),
        ("watch news about SpaceX", "watch", "SpaceX", None),
        ("keep an eye on Mars", "watch", "Mars", None),
        ("fact-check populations", "verify", "populations", None),
        ("predict capitals", "predict", "capitals", None),
        ("fill in the country of cities", "predict", "country", "cities"),
        ("answer questions about chemistry", "answer", "chemistry", None),
    ],
)
def test_directive_parsing(text, kind, subject, qualifier):
    d = directives.parse(text)
    assert (d.kind, d.subject, d.qualifier) == (kind, subject, qualifier) and d.text == text.strip()


def test_singular():
    words = ["countries", "capitals", "cities", "churches", "boxes", "classes", "bus", "series", "Alps"]
    assert [directives.singular(w) for w in words] == [
        "country", "capital", "city", "church", "box", "class", "bus", "series", "Alp"]  # fmt: skip


def test_describe_every_kind():
    assert directives.describe("predict", "capital", "countries") == "predict the capital of countries"
    assert {directives.describe(k, "x").split()[0] for k in directives.KINDS} == {
        "become", "watch", "fact-check", "predict", "answer"}  # fmt: skip


def test_spawn_resolves_scope_and_names_uniquely(db):
    kb = build(db)
    a = society.spawn(db, "research geography")
    assert a.kind == "research" and a.name == "research-geography" and kb.topics["Geography"] in a.scope["topics"]
    scope = set(store.scope_entities(db, a.id))
    assert set(kb.cities) <= scope  # the topic's documents link the capitals
    p = society.spawn(db, "predict capitals")
    assert p.scope["predicates"] == [kb.preds["capital"]] and set(kb.countries) <= set(store.scope_entities(db, p.id))
    again = society.spawn(db, "research geography", name="Geo Expert")
    assert again.name == "geo-expert" and society.spawn(db, "research geography").name == "research-geography-2"
    w = society.spawn(db, "watch Country03")
    assert kb.countries[3] in w.scope["entities"] and kb.cities[3] in store.scope_entities(db, w.id)


def test_levels():
    assert [store.level_for(x) for x in (0, 4.9, 5, 15, 35, 75)] == [1, 1, 2, 3, 4, 5]


# ------------------------------------------------------------------ quiz: immediate verification, once per fact
def test_quiz_rewards_once_and_penalises_wrong_answers(config, db, monkeypatch):
    build(db)
    mark_holdout(db, 0.4)
    a = society.spawn(db, "research geography")
    res = skills.quiz(runtime(config, db, a))
    assert res.tasks == store.DEFAULT_PARAMS["batch"] and res.reward == res.tasks * REWARDS["quiz_correct"]
    a = store.get(db, a.id)
    assert a.tasks_correct == res.tasks and a.level >= 2 and store.arm(db, a.id, "", "quiz").n == res.tasks
    asked = {r["target"] for r in db.query("SELECT target FROM agent_tasks WHERE agent_id=?", (a.id,))}
    res2 = skills.quiz(runtime(config, db, a))
    asked2 = {r["target"] for r in db.query("SELECT target FROM agent_tasks WHERE agent_id=?", (a.id,))}
    assert not (asked & (asked2 - asked)) and len(asked2) == len(asked) + res2.tasks  # never the same fact twice

    class Wrong:
        def predict(self, s, p, options):
            from polymath.reasoning.link_prediction import Prediction

            truth = db.scalar("SELECT o FROM triples WHERE s=? AND p=? AND holdout=1", (s, p))
            return Prediction(next(o for o in options if o != truth), 0.9, "text")

    rt = runtime(config, db, a)
    rt._predictor = Wrong()
    bad = skills.quiz(rt)
    if bad.tasks:
        assert bad.reward == pytest.approx(bad.tasks * REWARDS["quiz_wrong"])
        assert store.get(db, a.id).tasks_wrong == bad.tasks
    while skills.quiz(runtime(config, db, a)).tasks:
        pass
    final = skills.quiz(runtime(config, db, a))
    assert final.tasks == 0 and final.notes[0] in {
        "no unasked hidden facts in scope",
        "hidden facts in scope lack enough sibling answers to ask about",
    }


# ------------------------------------------------------------------ predictions: verified when the dumps deliver
def _continents(db, kb, missing):
    """Countries 0-19 use the euro and are in Europe, 20-39 use the yen and are in Asia; ``missing`` lack a continent."""
    g = kb.graph
    cont, cur = g.predicate("P30", "continent"), g.predicate("P38", "currency")
    europe, asia = g.upsert_entity("Q46", "Europe"), g.upsert_entity("Q48", "Asia")
    euro, yen = g.upsert_entity("Q4916", "euro"), g.upsert_entity("Q8146", "Japanese yen")
    for i, c in enumerate(kb.countries):
        g.add_triple(c, cur, o=euro if i < 20 else yen, kind="wikidata", source="wikidata")
        if c not in missing:
            g.add_triple(c, cont, o=europe if i < 20 else asia, kind="wikidata", source="wikidata")
    return cont, europe, asia


def test_open_predictions_are_judged_when_facts_arrive(config, db):
    kb = build(db)
    missing = [kb.countries[i] for i in (1, 2, 3, 25, 26, 27)]
    cont, europe, asia = _continents(db, kb, missing)
    a = society.spawn(db, "predict continents")
    assert a.scope["predicates"] == [cont]
    res = skills.predict(runtime(config, db, a))
    assert res.tasks == 6 and "6 predictions made" in res.notes[0]
    made = {
        json.loads(t["payload"])["s"]: (t["id"], json.loads(t["payload"])["choice"])
        for t in db.query("SELECT id, payload FROM agent_tasks WHERE agent_id=? AND kind='predict'", (a.id,))
    }
    assert set(made) == set(missing)  # cities (a type without continents) are never guessed for
    assert all(made[c][1] == (europe if kb.countries.index(c) < 20 else asia) for c in missing)  # learned from co-facts
    out = society.verify_job(ctx_for(config, db, "agents.verify")).result
    assert out == {"correct": 0, "wrong": 0, "expired": 0, "read_rewards": 0}  # nothing has arrived yet
    db.execute("UPDATE agent_tasks SET verify_after=0, created=created-10")
    kb.graph.add_triple(missing[0], cont, o=europe, kind="wikidata", source="wikidata")  # the dump agrees
    kb.graph.add_triple(missing[3], cont, o=europe, kind="wikidata", source="wikidata")  # the dump disagrees
    out = society.verify_job(ctx_for(config, db, "agents.verify")).result
    assert out["correct"] == 1 and out["wrong"] == 1
    a = store.get(db, a.id)
    assert a.reward_total == pytest.approx(REWARDS["predict_correct"] + REWARDS["predict_wrong"])
    assert {r["state"] for r in db.query("SELECT state FROM agent_tasks WHERE id IN (?, ?)",
                                         (made[missing[0]][0], made[missing[3]][0]))} == {"correct", "wrong"}  # fmt: skip
    db.execute("UPDATE agent_tasks SET verify_after=0, created=0 WHERE state='done'")
    assert society.verify_job(ctx_for(config, db, "agents.verify")).result["expired"] == 4
    assert skills.predict(runtime(config, db, a)).tasks == 0  # each missing fact is predicted once


def test_predictions_abstain_below_the_confidence_threshold(config, db):
    kb = build(db)
    db.execute("DELETE FROM triples WHERE p=? AND s=?", (kb.preds["capital"], kb.countries[0]))
    a = society.spawn(db, "predict capitals")
    a.params["min_conf"] = 0.99
    store.save_params(db, a.id, a.params)
    res = skills.predict(runtime(config, db, a))
    assert res.tasks == 0 and "abstained" in res.notes[0]


# ------------------------------------------------------------------ disputes: verified by a hidden copy or settled evidence
def test_disputes_judged_against_hidden_truth_and_settled_evidence(config, db):
    kb = build(db)
    g, cap = kb.graph, kb.preds["capital"]
    true_t = int(db.scalar("SELECT id FROM triples WHERE s=? AND p=?", (kb.countries[1], cap)))
    fake, _ = g.add_triple(
        kb.countries[1],
        cap,
        o=kb.cities[30],
        kind="pattern",
        source="text",
        confidence=0.4,
        doc_id=kb.docs["Country30"],
    )
    db.execute("UPDATE triples SET status='disputed' WHERE s=? AND p=?", (kb.countries[1], cap))
    true2 = int(db.scalar("SELECT id FROM triples WHERE s=? AND p=?", (kb.countries[2], cap)))
    fake2, _ = g.add_triple(
        kb.countries[2],
        cap,
        o=kb.cities[31],
        kind="pattern",
        source="text",
        confidence=0.4,
        doc_id=kb.docs["Country31"],
    )
    db.execute("UPDATE triples SET status='disputed' WHERE s=? AND p=?", (kb.countries[2], cap))
    a = society.spawn(db, "fact-check capitals")
    res = skills.dispute(runtime(config, db, a))
    assert res.tasks == 2
    chosen = {json.loads(r["payload"])["s"]: json.loads(r["payload"])["choice"]
              for r in db.query("SELECT payload FROM agent_tasks WHERE kind='dispute'")}  # fmt: skip
    assert chosen[kb.countries[1]] == true_t and chosen[kb.countries[2]] == true2  # text says Capitol01 is the capital
    assert society.verify_job(ctx_for(config, db, "agents.verify")).result["correct"] == 0  # still disputed
    db.execute("UPDATE triples SET status='sourced', confidence=0.95 WHERE id IN (?, ?)", (true_t, true2))
    db.execute("DELETE FROM triples WHERE id IN (?, ?)", (fake, fake2))
    db.execute("UPDATE agent_tasks SET verify_after=0")
    out = society.verify_job(ctx_for(config, db, "agents.verify")).result
    assert out["correct"] == 2 and store.get(db, a.id).reward_total == pytest.approx(2 * REWARDS["dispute_correct"])
    assert skills.dispute(runtime(config, db, a)).notes == ["no open disputes in scope"]


# ------------------------------------------------------------------ reading and watching
def test_read_requests_rewarded_when_articles_arrive(config, db):
    kb = build(db)
    a = society.spawn(db, "research geography")
    assert skills.read(runtime(config, db, a)).notes == ["nothing unread in the dump index for this scope"]
    for i in range(5):
        db.execute("INSERT INTO wiki_index(lang, title, dump_url, stream, next) VALUES('en', ?, 'u', 0, 1)",
                   (f"Capitol{i:02d}",))  # fmt: skip
    res = skills.read(runtime(config, db, a))
    assert res.tasks == 1 and db.scalar("SELECT COUNT(*) FROM jobs WHERE kind='wikipedia.titles'") == 1
    db.execute("UPDATE agent_tasks SET verify_after=0")
    assert society.verify_job(ctx_for(config, db, "agents.verify")).result["read_rewards"] == 0
    store_ = DocumentStore(db)
    for i in range(3):
        store_.add(Document("wikipedia", f"en:Capitol{i:02d}", f"Capitol{i:02d}", f"Capitol{i:02d} is a city. " * 20,
                            "CC BY-SA 4.0"))  # fmt: skip
    db.execute("UPDATE agent_tasks SET verify_after=0")
    assert society.verify_job(ctx_for(config, db, "agents.verify")).result["read_rewards"] == 1
    assert store.get(db, a.id).reward_total == pytest.approx(3 * REWARDS["read_doc"])
    assert kb.cities


def test_watch_digest_verified_by_entity_links(config, db):
    kb = build(db)
    a = society.spawn(db, "watch Country05")
    assert skills.scan(runtime(config, db, a)).notes == ["nothing new has been read yet"]
    st, ti = DocumentStore(db), TextIndex(db)
    linked, _ = st.add(Document("feed", "n1", "Capitol05 news", "Big news from Capitol05 today about its port. " * 5,
                                "publisher"))  # fmt: skip
    keyword, _ = st.add(Document("feed", "n2", "Country05 mention", "A note about Country05 trade talks. " * 5, "pub"))
    other, _ = st.add(Document("feed", "n3", "Weather", "Rain everywhere else this week. " * 5, "pub"))
    for d, title in ((linked, "Capitol05 news"), (keyword, "Country05 mention"), (other, "Weather")):
        ti.index(d, title, st.get(d).text)
    db.execute("UPDATE documents SET state='perceived' WHERE id IN (?,?,?)", (linked, keyword, other))
    db.execute("INSERT INTO doc_entities(doc_id, entity_id, count, score) VALUES(?,?,3,5)", (linked, kb.cities[5]))
    res = skills.scan(runtime(config, db, a))
    digest = {json.loads(r["payload"])["doc_id"]: r["state"]
              for r in db.query("SELECT payload, state FROM agent_tasks WHERE kind='digest'")}  # fmt: skip
    assert digest == {linked: "correct", keyword: "done"}  # keyword-only waits for your verdict
    assert res.reward == REWARDS["digest_doc"] and store.get(db, a.id).params["cursor"] >= other
    assert skills.scan(runtime(config, db, a)).notes == ["nothing new has been read yet"]


# ------------------------------------------------------------------ self-improvement
def test_calibrate_learns_scope_weights(config, db):
    build(db)
    a = society.spawn(db, "research geography")
    res = skills.calibrate(runtime(config, db, a))
    assert res.tasks == 1 and 0 <= res.reward <= 1
    t = db.one("SELECT payload, state FROM agent_tasks WHERE kind='calibrate'")
    p = json.loads(t["payload"])
    assert p["samples"] >= 10 and p["after"] >= p["before"] and t["state"] == "done"
    if p["after"] - p["before"] > 0.005:
        assert store.get(db, a.id).params["weights"]
    assert skills.calibrate(runtime(config, db, a)).tasks == 0  # once per hour
    lone = society.spawn(db, "watch nothingatall")
    assert "visible facts in scope" in skills.calibrate(runtime(config, db, lone)).notes[0]


# ------------------------------------------------------------------ your tasks and your verdicts
def test_user_tasks_answers_and_feedback_rewards(config, db):
    build(db)
    a = society.spawn(db, "answer questions about geography")
    t1 = society.give_task(db, a.name, "What is the capital of Country07?")
    t2 = society.give_task(db, a.name, "What is the capital of Country08?")
    assert skills.answer_pending(runtime(config, db, a)) == 2
    r1 = json.loads(db.scalar("SELECT result FROM agent_tasks WHERE id=?", (t1,)))
    assert "The capital of Country07 is Capitol07." in r1["rendered"]
    assert society.feedback(db, t1, "correct", "spot on") == {"task": t1, "reward": REWARDS["user_correct"],
                                                              "state": "correct"}  # fmt: skip
    assert society.feedback(db, t1, "wrong")["unchanged"] is True  # a settled task stays settled
    society.feedback(db, t2, "wrong")
    a = store.get(db, a.id)
    assert a.reward_total == REWARDS["user_correct"] + REWARDS["user_wrong"] and a.xp == REWARDS["user_correct"]
    assert (a.tasks_correct, a.tasks_wrong) == (1, 1)
    reasons = [r["reason"] for r in db.query("SELECT reason FROM agent_rewards ORDER BY id")]
    assert reasons == ["you: correct — spot on", "you: wrong"]
    with pytest.raises(KeyError):
        society.give_task(db, "nobody", "x")
    with pytest.raises(KeyError):
        society.feedback(db, 999999, "correct")


# ------------------------------------------------------------------ allocation and learning to choose
def test_allocation_favours_productive_agents_and_your_questions(config, db):
    build(db)
    good = society.spawn(db, "research geography", name="good")
    poor = society.spawn(db, "research geography", name="poor")
    db.execute("UPDATE agents SET reward_total=40, steps=40, level=3 WHERE id=?", (good.id,))
    db.execute("UPDATE agents SET reward_total=-10, steps=40 WHERE id=?", (poor.id,))
    rng = random.Random(3)
    picks = [society.choose_agent(db, store.active(db), rng).name for _ in range(200)]
    assert picks.count("good") > 190
    society.give_task(db, "poor", "What is the capital of Country01?")
    assert society.choose_agent(db, store.active(db), rng).name == "poor"  # your question jumps the queue
    assert society.choose_agent(db, [], rng) is None


def test_agents_learn_which_actions_pay(config, db):
    build(db)
    a = society.spawn(db, "research geography")
    for _ in range(30):
        store.learn(db, a.id, "", "quiz", 1.0)
        store.learn(db, a.id, "", "read", -0.05)
        store.learn(db, a.id, "", "predict", 0.1)
        store.learn(db, a.id, "", "calibrate", 0.0)
    rng = random.Random(1)
    picks = [society.choose_action(db, store.get(db, a.id), rng) for _ in range(200)]
    assert picks.count("quiz") > 180


def test_step_job_gives_each_agent_a_turn(config, db):
    build(db)
    mark_holdout(db, 0.3)
    names = {society.spawn(db, d).name for d in ("research geography", "predict capitals", "fact-check capitals")}
    out = society.step_job(ctx_for(config, db))
    turns = out.result["turns"]
    assert {t["agent"] for t in turns} == names and len(turns) == 3
    assert all(store.get(db, n).params is not None for n in names)
    assert db.scalar("SELECT SUM(steps) FROM agents") == 3 and out.value >= 0
    db.execute("UPDATE agents SET scope_at=0")
    society.step_job(ctx_for(config, db))  # stale scopes are refreshed on the next turn
    assert db.scalar("SELECT MIN(scope_at) FROM agents") > 0


# ------------------------------------------------------------------ evolution
def test_evolution_forks_judges_and_adopts(config, db):
    build(db)
    config.agents.min_tasks_to_judge = 5
    parent = society.spawn(db, "research geography", name="geo")
    db.execute("UPDATE agents SET reward_total=20, steps=20, tasks_correct=15, tasks_wrong=3 WHERE id=?", (parent.id,))
    out = society.evolve_job(ctx_for(config, db, "agents.evolve")).result
    assert out["forked"] == ["geo-g1"]
    child = store.get(db, "geo-g1")
    assert child.parent == parent.id and child.origin == "evolved" and child.generation == 1
    lo_hi = society.MUTATIONS
    for key, (lo, hi, _s, _h) in lo_hi.items():
        assert lo <= child.params[key] <= hi
    assert society.evolve_job(ctx_for(config, db, "agents.evolve")).result["forked"] == []  # one child at a time
    # the child does better: the user's agent adopts its genes and keeps its name
    db.execute("UPDATE agents SET reward_total=200, steps=60, tasks_correct=50, tasks_wrong=2 WHERE id=?", (child.id,))
    out = society.evolve_job(ctx_for(config, db, "agents.evolve")).result
    assert out["adopted"] == ["geo←geo-g1"] and store.get(db, "geo-g1").status == "retired"
    assert store.get(db, "geo").params["min_conf"] == child.params["min_conf"]
    # the improved parent immediately forks again; this time the child does worse and retires
    assert len(out["forked"]) == 1
    kid = store.get(db, out["forked"][0])
    db.execute("UPDATE agents SET reward_total=-30, steps=60, tasks_correct=5, tasks_wrong=40 WHERE id=?", (kid.id,))
    out = society.evolve_job(ctx_for(config, db, "agents.evolve")).result
    assert kid.name in out["retired"] and store.get(db, "geo").status == "active"  # yours is never auto-retired


def test_evolution_respects_the_population_cap_and_flags_struggling(config, db):
    build(db)
    config.agents.min_tasks_to_judge = 5
    config.agents.max_agents = 2
    a = society.spawn(db, "research geography", name="a")
    b = society.spawn(db, "predict capitals", name="b")
    db.execute("UPDATE agents SET reward_total=20, steps=10, tasks_correct=10 WHERE id=?", (a.id,))
    db.execute("UPDATE agents SET reward_total=-5, steps=20, tasks_correct=2, tasks_wrong=10 WHERE id=?", (b.id,))
    out = society.evolve_job(ctx_for(config, db, "agents.evolve")).result
    assert out["forked"] == [] and out["struggling"] == ["b"]
    config.agents.evolve = False
    config.agents.max_agents = 10
    assert society.evolve_job(ctx_for(config, db, "agents.evolve")).result["forked"] == []


def test_mutation_bounds():
    rng = random.Random(0)
    p = {**store.DEFAULT_PARAMS, "weights": {"text": 1.0, "graph": 3.0}, "cursor": 99}
    for _ in range(200):
        c = society.mutate(p, rng)
        assert 0.1 <= c["min_conf"] <= 0.9 and 2 <= c["batch"] <= 20 and isinstance(c["batch"], int)
        assert "cursor" not in c and all(w >= 0 for w in c["weights"].values())


# ------------------------------------------------------------------ commands, planner, loop
def test_command_job_and_planner(config, db):
    build(db)
    out = society.command_job(ctx_for(config, db, "agents.command", {"op": "spawn", "directive": "verify capitals"}))
    assert out.result == {"agent": "verify-capitals", "kind": "verify"}
    tid = society.command_job(ctx_for(config, db, "agents.command",
                                      {"op": "task", "agent": "verify-capitals", "text": "q?"})).result["task"]  # fmt: skip
    fb = society.command_job(ctx_for(config, db, "agents.command", {"op": "feedback", "task": tid, "verdict": "good"}))
    assert fb.result["state"] == "correct"
    for op, status in (("pause", "paused"), ("resume", "active"), ("retire", "retired")):
        society.command_job(ctx_for(config, db, "agents.command", {"op": op, "agent": "verify-capitals"}))
        assert store.get(db, "verify-capitals").status == status
    assert "error" in society.command_job(ctx_for(config, db, "agents.command", {"op": "pause", "agent": "x"})).result
    assert "error" in society.command_job(ctx_for(config, db, "agents.command", {"op": "dance"})).result

    class A:
        pass

    agent = A()
    agent.db, agent.scheduler, agent.config = db, Scheduler(db), config
    society.planner(agent)
    assert not db.scalar("SELECT 1 FROM jobs WHERE kind='agents.step'")  # nobody active
    society.spawn(db, "research geography")
    society.planner(agent)
    kinds = {r["kind"] for r in db.query("SELECT kind FROM jobs WHERE state='queued'")}
    assert {"agents.step", "agents.verify", "agents.evolve"} <= kinds


def test_agents_run_inside_the_real_loop(config):
    from polymath.core.app import build_agent

    agent = build_agent(config, planners=False)
    agent.start()
    with agent.db.transaction():
        build(agent.db)
        mark_holdout(agent.db, 0.3)
        society.spawn(agent.db, "research geography")
    agent.scheduler.enqueue("agents.step", {}, key="s1", priority=5)
    rec = agent.cycle()
    assert rec.status == "done" and rec.kind == "agents.step"
    assert agent.db.scalar("SELECT steps FROM agents") == 1
    agent.shutdown()
    agent.db.close()


# ------------------------------------------------------------------ CLI and dashboard
def test_cli_agents_end_to_end(tmp_path, capsys):
    from polymath.core.db import Database, open_database
    from polymath.interface import cli
    from tests.conftest import make_config

    cfg = make_config(tmp_path)
    path = str(tmp_path / "polymath.toml")
    db = open_database(cfg.paths.db_path)
    with db.transaction():
        build(db)
    db.close()

    def run(*argv):
        rc = cli.main(["--config", path, "agents", *argv])
        return rc, capsys.readouterr()

    rc, io = run("list")
    assert rc == 0 and "no agents yet" in io.out
    rc, io = run("spawn", "research", "geography")
    assert rc == 0 and io.out.startswith("spawned research-geography: become an expert on geography")
    rc, io = run("task", "research-geography", "What", "is", "the", "capital", "of", "Country04?")
    assert rc == 0 and "The capital of Country04 is Capitol04." in io.out and "polymath agents feedback" in io.out
    task_id = int(io.out.split("#", 1)[1].split(":", 1)[0])
    rc, io = run("feedback", str(task_id), "correct", "nice")
    assert rc == 0 and json.loads(io.out)["reward"] == REWARDS["user_correct"]
    rc, io = run("list", "--json")
    row = json.loads(io.out)[0]
    assert row["name"] == "research-geography" and row["level"] == 1 and row["correct"] == 1 and row["scope"] > 0
    rc, io = run("show", "research-geography")
    assert (
        rc == 0
        and "become an expert on geography" in io.out
        and f"#{task_id}" in io.out
        and "→ - The capital" in io.out
    )
    rc, io = run("show", "research-geography", "--json")
    detail = json.loads(io.out)
    assert detail["purpose"] == "become an expert on geography" and detail["tasks"][0]["state"] == "correct"
    for op, status in (("pause", "paused"), ("resume", "active"), ("retire", "retired")):
        assert run(op, "research-geography")[0] == 0
        assert json.loads(run("list", "--json")[1].out)[0]["status"] == status
    assert run("show", "nobody")[0] == 2 and run("pause", "nobody")[0] == 2
    assert run("task", "nobody", "x")[0] == 2
    # while the agent runs, commands go through its inbox
    db = Database(cfg.paths.db_path)
    db.kv_set("heartbeat", {"ts": time.time(), "state": "running", "cycle": 1})
    db.close()
    rc, io = run("spawn", "watch", "Country09")
    assert rc == 0 and json.loads(io.out)["queued_via"] == "inbox"
    files = list((cfg.paths.data_dir / "inbox").glob("*.json"))
    req = json.loads(files[0].read_text())
    assert req["kind"] == "agents.command" and req["payload"] == {"op": "spawn", "directive": "watch Country09",
                                                                  "name": None}  # fmt: skip


def test_dashboard_agents_api(config, db):
    import http.client

    from polymath.interface.dashboard import make_server

    build(db)
    a = society.spawn(db, "research geography")
    tid = society.give_task(db, a.name, "q")
    society.feedback(db, tid, "correct")
    server = make_server(config, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        c.request("GET", "/api/agents")
        body = json.loads(c.getresponse().read())
        assert body["agents"][0]["name"] == "research-geography" and body["rewards"][0]["amount"] == 2.0
        c.request("GET", "/")
        assert b'id="t-agents"' in c.getresponse().read()
    finally:
        server.shutdown()
        server.server_close()


def test_dispute_verdicts_respect_multi_valued_relations(config, db):
    kb = build(db)
    g = kb.graph
    border = g.predicate("P47", "shares border with")
    for i in range(30):  # a multi-valued relation: every country borders two others
        for j in (1, 2):
            g.add_triple(kb.countries[i], border, o=kb.countries[(i + j) % 40], kind="wikidata", source="wikidata")
    s = kb.countries[0]
    claim, _ = g.add_triple(s, border, o=kb.countries[5], kind="pattern", source="text", doc_id=kb.docs["Country00"])
    hidden = int(db.scalar("SELECT id FROM triples WHERE s=? AND p=? AND o=?", (s, border, kb.countries[1])))
    db.execute("UPDATE triples SET holdout=1 WHERE id=?", (hidden,))
    a = society.spawn(db, "fact-check borders")
    t = store.open_task(db, store.get(db, a.id), kind="dispute", action="dispute", target="d1",
                        payload={"s": s, "p": border, "choice": claim}, verify_after=0)  # fmt: skip
    row = db.one("SELECT * FROM agent_tasks WHERE id=?", (t,))
    # the hidden copy is a *different* border: that does not make the chosen border wrong
    db.execute("UPDATE triples SET status='disputed' WHERE id=?", (claim,))
    assert society._verify_dispute(db, row, json.loads(row["payload"])) is None  # still open
    db.execute("UPDATE triples SET status='sourced' WHERE id=?", (claim,))
    assert society._verify_dispute(db, row, json.loads(row["payload"])) is True  # accepted
    db.execute("DELETE FROM triples WHERE id=?", (claim,))
    assert society._verify_dispute(db, row, json.loads(row["payload"])) is False  # retracted
    match = int(db.scalar("SELECT id FROM triples WHERE s=? AND p=? AND o=?", (s, border, kb.countries[2])))
    assert society._verify_dispute(db, row, {"s": s, "p": border, "choice": match}) is True  # still sourced, settled
    cap = kb.preds["capital"]  # single-valued: a hidden capital that differs means the choice was wrong
    db.execute("UPDATE triples SET holdout=1 WHERE s=? AND p=?", (kb.countries[3], cap))
    wrong, _ = g.add_triple(kb.countries[3], cap, o=kb.cities[9], kind="pattern", source="text",
                            doc_id=kb.docs["Country09"])  # fmt: skip
    assert society._verify_dispute(db, row, {"s": kb.countries[3], "p": cap, "choice": wrong}) is False


def test_predictions_only_for_subjects_whose_type_has_the_relation(db):
    kb = build(db)
    missing = [kb.countries[1]]
    cont, _europe, _asia = _continents(db, kb, missing)
    cache: dict = {}
    st = skills.typical(db, kb.countries[1], cont, cache)  # countries usually have a continent
    assert st is not None and st.share > 0.8 and set(st.options) == {_europe, _asia} and st.reflexive == 0
    assert skills.typical(db, kb.cities[1], cont, cache) is None  # cities in this graph never do
    assert skills.typical(db, kb.people[0], cont, cache) is None  # no type known: no guess


def test_reflexive_regularities_are_learned(config, db):
    kb = build(db)
    g = kb.graph
    country = kb.preds["country"]
    for c in kb.countries[:30]:  # the "country" of a country is itself
        g.add_triple(c, country, o=c, kind="wikidata", source="wikidata")
    a = society.spawn(db, "predict the country of countries")
    res = skills.predict(runtime(config, db, a))
    made = {json.loads(t["payload"])["s"]: json.loads(t["payload"]) for t in db.query(
        "SELECT payload FROM agent_tasks WHERE kind='predict'")}  # fmt: skip
    assert res.tasks > 0 and all(p["choice"] == s and p["evidence"] == "pattern" for s, p in made.items())
    assert set(made) <= set(kb.countries[30:])
