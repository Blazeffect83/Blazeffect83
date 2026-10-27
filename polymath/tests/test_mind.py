"""v0.13.0, the mind: distilled knowledge, traceable facts, the dream cycle, curiosity, procedures, the scientist,
the red team and staying up offline."""

from __future__ import annotations

import json
from typing import Any

from polymath.memory import jobs as memjobs
from polymath.senses import wikidata
from tests.test_storage import ctx_for


def entity(qid: str, label: str, claims: dict[str, Any]) -> dict[str, Any]:
    return {"qid": qid, "type": "item", "label": label, "description": "", "aliases": [], "enwiki": None,
            "claims": claims}  # fmt: skip


# ------------------------------------------------------------------ distilled knowledge
def test_linked_wikidata_is_distilled_to_facts_and_a_fingerprint(config, db):
    ctx = ctx_for(config, db, "wikidata.dump")
    assert wikidata.store_entity(ctx, entity("Q90", "Paris", {"P17": ["Q142"], "P1082": ["2145906"]}))
    assert wikidata.store_entity(ctx, entity("Q142", "France", {"P36": ["Q90"]}))
    memjobs.build_graph(ctx_for(config, db, "memory.graph"))
    facts = db.scalar("SELECT COUNT(*) FROM triples")
    out = memjobs.distill_job(ctx_for(config, db, "memory.distill"))
    assert out.done and out.result["entities"] == 2 and out.result["bytes"] > 20
    row = db.one("SELECT claims, claims_hash, state FROM wd_entities WHERE qid = 'Q90'")
    assert row["claims"] == "{}" and len(row["claims_hash"]) == 32 and row["state"] == "distilled"
    assert db.scalar("SELECT COUNT(*) FROM triples") == facts  # every fact stays, each with its source
    assert db.scalar("SELECT COUNT(*) FROM triples t WHERE NOT EXISTS "
                     "(SELECT 1 FROM provenance p WHERE p.triple_id = t.id)") == 0  # fmt: skip
    assert db.kv_get("distilled")["entities"] == 2
    # the same entity in a later dump: unchanged, nothing happens; changed: linked again
    assert not wikidata.store_entity(ctx, entity("Q90", "Paris", {"P17": ["Q142"], "P1082": ["2145906"]}))
    assert wikidata.store_entity(ctx, entity("Q90", "Paris", {"P17": ["Q142"], "P1082": ["2200000"]}))
    row = db.one("SELECT claims, claims_hash, state FROM wd_entities WHERE qid = 'Q90'")
    assert row["state"] == "new" and json.loads(row["claims"])["P1082"] == ["2200000"] and row["claims_hash"] is None
    assert memjobs.distill_job(ctx_for(config, db, "memory.distill")).result["entities"] == 0  # not linked yet


# ------------------------------------------------------------------ explainable everything
def traced_world(config, db):
    from polymath.agents import society
    from polymath.core import trace
    from polymath.core.scheduler import Scheduler
    from polymath.memory.documents import Document, DocumentStore
    from polymath.memory.graph import KnowledgeGraph

    g = KnowledgeGraph(db)
    paris, france, europe = (
        g.upsert_entity(q, n) for q, n in (("Q90", "Paris"), ("Q142", "France"), ("Q46", "Europe"))
    )
    for eid, name in ((paris, "Paris"), (france, "France")):
        g.add_alias(name, eid, "label")
    country, part_of = g.predicate("P17", "country"), g.predicate("P361", "part of")
    did, _ = DocumentStore(db).add(Document("wikipedia", "Paris", "Paris", "Paris is the capital of France.", "CC BY-SA",
                                            url="https://en.wikipedia.org/wiki/Paris"))  # fmt: skip
    jid, _ = Scheduler(db).enqueue("perception.relations", {}, key="k1")
    db.execute("INSERT INTO cycles(started, ended, action, job_id, status) VALUES(?, ?, 'read', ?, 'done')",
               (1000.0, 1001.0, jid))  # fmt: skip
    db.execute("INSERT INTO decisions(at, cycle, context, options, chosen, reason, job_id) VALUES(?, 1, 'day', '{}', "
               "'read', 'reading pays most per CPU second now', ?)", (1000.0, jid))  # fmt: skip
    agent = society.spawn(db, "research geography")
    j, a = trace.current_job.set(jid), trace.current_agent.set(agent.id)
    try:
        t1, _ = g.add_triple(paris, country, o=france, kind="pattern", source="wikipedia", doc_id=did,
                             detail="X is the capital of Y")  # fmt: skip
    finally:
        trace.current_job.reset(j)
        trace.current_agent.reset(a)
    t2, _ = g.add_triple(france, part_of, o=europe, kind="wikidata", source="wikidata", detail="P361")
    t3, _ = g.add_triple(paris, part_of, o=europe, status="inferred", confidence=0.7, kind="rule", source="rule",
                         detail=f"transitive: #{t1} + #{t2}")  # fmt: skip
    return {"read": t1, "stated": t2, "inferred": t3, "agent": agent, "job": jid}


def test_a_fact_traces_back_to_its_evidence_the_job_the_decision_and_the_agent(config, db):
    from polymath.interface import explain

    w = traced_world(config, db)
    ex = explain.explain_fact(db, w["read"])
    assert ex is not None and ex["fact"] == "Paris — country — France" and ex["status"] == "sourced"
    ev = ex["evidence"][0]
    assert (
        ev["document"]["title"] == "Paris"
        and ev["job"]["id"] == w["job"]
        and ev["job"]["kind"] == "perception.relations"
    )
    assert ev["job"]["decision"]["reason"] == "reading pays most per CPU second now"
    assert ev["agent"]["name"] == w["agent"].name
    inferred = explain.explain_fact(db, w["inferred"])
    assert inferred is not None and [p["id"] for p in inferred["evidence"][0]["premises"]] == [w["read"], w["stated"]]
    text = "\n".join(explain.render(inferred))
    assert "Paris — part of — Europe   [inferred, 70% sure" in text and "inferred by transitive" in text
    assert "read in “Paris” (wikipedia, CC BY-SA)" in text and "chosen because: reading pays most" in text
    assert "acting: agent research-geography" in text and "stated in Wikidata wikidata" in text
    assert explain.explain_fact(db, 999999) is None
    assert explain.find_facts(db, f"#{w['read']}") == [w["read"]] and explain.find_facts(db, "999999") == []
    assert explain.find_facts(db, "Paris country")[0] == w["read"]
    assert set(explain.find_facts(db, "Paris")) >= {w["read"]} and explain.find_facts(db, "Atlantis") == []
    db.execute("INSERT INTO sanity(triple_id, rule, detail, action, at) VALUES(?, 'range', 'odd', 'noted', 1)",
               (w["read"],))  # fmt: skip
    assert "sanity check range: odd (noted)" in (explain.explain_fact(db, w["read"]) or {})["checks"]


def test_why_on_the_command_line_and_the_dashboard(config, db, capsys):
    import argparse
    import http.client
    import threading

    from polymath.interface import cli
    from polymath.interface.dashboard import make_server

    w = traced_world(config, db)
    db.close()
    assert cli.cmd_why(config, argparse.Namespace(query=[], n=5, fact=w["inferred"], json=False)) == 0
    out = capsys.readouterr().out
    assert "Paris — part of — Europe" in out and "Recent decisions" not in out
    cli.cmd_why(config, argparse.Namespace(query=[], n=5, fact=123456, json=False))
    assert "no fact #123456" in capsys.readouterr().out
    server = make_server(config, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        c.request("GET", "/api/why?q=Paris%20country")
        body = json.loads(c.getresponse().read())
        assert body["trace"][0]["fact"] == "Paris — country — France"
    finally:
        server.shutdown()
        server.server_close()


def test_the_loop_stamps_each_fact_with_its_job(config):
    from polymath.core.app import build_agent
    from polymath.core.jobs import JobOutcome
    from polymath.memory.graph import KnowledgeGraph

    agent = build_agent(config, planners=False)
    agent.start()

    def learn(ctx: Any) -> JobOutcome:
        g = KnowledgeGraph(ctx.db)
        g.add_triple(g.upsert_entity("Q1", "A"), g.predicate("P1", "p"), value="v", kind="pattern", source="web")
        return JobOutcome(done=True)

    agent.registry.register("t.learn", learn, "t")
    jid, _ = agent.scheduler.enqueue("t.learn", {}, key="t", priority=9)
    agent.cycle()
    assert agent.db.scalar("SELECT job_id FROM provenance") == jid
    from polymath.core import trace

    assert trace.current_job.get() is None  # reset after the slice
    agent.shutdown()
    agent.db.close()


# ------------------------------------------------------------------ the dream cycle
def test_a_night_of_dreaming_replays_consolidates_prunes_plans_and_reports(config, db):
    import time as _time

    from polymath.drive import dream
    from polymath.memory.graph import KnowledgeGraph
    from tests.fixtures.kb import build

    kb = build(db)
    db.execute("UPDATE triples SET holdout = 1 WHERE id IN (SELECT id FROM triples WHERE p = ? ORDER BY id LIMIT 10)",
               (kb.preds["capital"],))  # fmt: skip
    g = KnowledgeGraph(db)
    a, b = g.upsert_entity("Q900001", "Alpha"), g.upsert_entity("Q900002", "Beta")
    rel = g.predicate("P999", "linked to")
    sure, _ = g.add_triple(a, rel, o=b, kind="pattern", source="web", confidence=0.6, weight=0.8, doc_id=1)
    g.add_triple(a, rel, o=b, kind="pattern", source="feed", confidence=0.6, weight=0.7, doc_id=2)
    weak, _ = g.add_triple(b, rel, o=a, status="inferred", confidence=0.2, kind="rule", source="rule",
                           detail=f"symmetric: #{sure}")  # fmt: skip
    db.execute("UPDATE triples SET created = ? WHERE id = ?", (_time.time() - 30 * 86400, weak))
    now = _time.time()
    for action, value, cpu in (("read", 2.0, 600.0), ("reason", 1.0, 300.0), ("perceive", 0.5, 100.0)):
        db.execute("INSERT INTO bandit_arms(context, action, n, mean, m2, updated) VALUES('normal|day|clear', ?, 10, "
                   "?, 1, ?)", (action, value, now))  # fmt: skip
        db.execute("INSERT INTO cycles(started, ended, action, status, cpu_seconds) VALUES(?, ?, ?, 'done', ?)",
                   (now - 3600, now - 3500, action, cpu))  # fmt: skip
    out = dream.dream_job(ctx_for(config, db, "dream.night"))
    assert out.done and out.result["dream"] >= 1
    row = db.one("SELECT * FROM dreams WHERE id = ?", (out.result["dream"],))
    before, after = json.loads(row["before"]), json.loads(row["after"])
    assert before["score"]["n"] > 0 and after["score"]["n"] == before["score"]["n"]  # the same questions
    assert db.scalar("SELECT confidence FROM triples WHERE id = ?", (sure,)) == 0.8964  # 1 − (1 − .72)(1 − .63)
    assert db.scalar("SELECT 1 FROM triples WHERE id = ?", (weak,)) is None  # a weak, unbacked belief pruned
    assert db.scalar("SELECT reason FROM withdrawn WHERE kind = 'dream'").startswith("pruned in a dream")
    steps = json.loads(row["steps"])
    assert set(steps) == {"replay", "consolidate", "prune"} and "infer" in steps["replay"]
    plan = json.loads(row["plan"])
    assert plan["chosen"] in plan["alternatives"] and "more read" in plan["alternatives"]
    assert "held-out questions:" in row["report"] and "plan for today:" in row["report"]
    assert db.scalar("SELECT text FROM events WHERE kind = 'dream'").startswith("while you slept: held-out questions")
    assert (dream.latest(db) or {}).get("id") == out.result["dream"]
    nudges = db.kv_get("dream_plan")["nudges"]
    assert all(abs(v) <= dream.MAX_NUDGE for v in nudges.values())
    assert dream.nudge(db, "read", now) == nudges.get("read", 0.0)
    assert dream.nudge(db, "read", now + 2 * 86400) == 0.0  # a plan lasts a day


def test_the_world_model_avoids_a_day_that_would_overheat(db):
    from polymath.drive import dream

    model = {"read": {"value": 2.0, "share": 0.5, "heat": 1.0}, "reason": {"value": 1.5, "share": 0.5, "heat": 0.0}}
    hot = dream.simulate(model, {"read": 0.9, "reason": 0.1}, temp=70.0, throttle=75.0)
    cool = dream.simulate(model, {"read": 0.1, "reason": 0.9}, temp=70.0, throttle=75.0)
    assert hot is not None and cool is not None
    assert hot["value"] > cool["value"] and hot["score"] < cool["score"]  # more value, but it would throttle
    assert dream.simulate(model, {"read": 0.9, "reason": 0.1}, temp=None, throttle=75.0)["score"] == hot["value"]
    shifted = dream._shift({"read": 0.5, "reason": 0.5}, "read", None)
    assert abs(sum(shifted.values()) - 1) < 1e-9 and shifted["read"] > 0.5
    assert dream._shift({"read": 0.5, "reason": 0.5}, None, "read")["read"] < 0.5
    assert dream.plan(db, 0.0, temp=None, throttle=75.0)["note"].startswith("not enough")


def test_a_long_dream_carries_on_in_the_next_slice(config, db, monkeypatch):
    from polymath.drive import dream

    ctx = ctx_for(config, db, "dream.night")
    monkeypatch.setattr(ctx, "should_stop", lambda: True)
    out = dream.dream_job(ctx)
    cp = out.checkpoint or {}
    assert not out.done and cp["phase"] == 1 and out.result == {"phase": "replay"}
    assert cp["steps"]["before"]["score"] == {"n": 0, "correct": 0, "accuracy": None}
    monkeypatch.setattr(dream, "replay", lambda c: (_ for _ in ()).throw(RuntimeError("never")))
    from polymath.reasoning import jobs as rjobs

    monkeypatch.undo()
    monkeypatch.setattr(rjobs, "infer_job", lambda c: (_ for _ in ()).throw(ValueError("broken")))
    assert dream.replay(ctx)["infer"]["error"] == "ValueError: broken"  # one part failing never spoils the night
    assert "no held-out questions yet" in dream.report_text({}, {}, {"plan": {"note": "x"}})


def test_polymath_dream_prints_the_morning_report(config, db, capsys):
    import argparse

    from polymath.interface import cli

    db.close()
    assert cli.cmd_dream(config, argparse.Namespace(json=False)) == 0
    assert "no dream yet" in capsys.readouterr().out
    from polymath.core.db import open_database

    db2 = open_database(config.paths.db_path)
    db2.execute("INSERT INTO dreams(started, finished, report) VALUES(1, 2, 'held-out questions: 3/5 before\nplan')")
    db2.close()
    cli.cmd_dream(config, argparse.Namespace(json=False))
    assert "  held-out questions: 3/5 before" in capsys.readouterr().out
    cli.cmd_dream(config, argparse.Namespace(json=True))
    assert json.loads(capsys.readouterr().out)["report"].startswith("held-out")


# ------------------------------------------------------------------ the curiosity engine
def test_uncertainty_falls_with_more_questions_and_with_being_right():
    from polymath.drive.curiosity import uncertainty

    assert uncertainty(0, 0) == 1.0
    assert uncertainty(5, 10) > uncertainty(50, 100)  # measured more: surer of what it knows
    assert uncertainty(9, 10) < uncertainty(5, 10)  # right more often: less to learn
    assert 0 < uncertainty(100, 100) < 0.2


def test_agents_are_rewarded_for_making_it_surer_about_a_topic(config, db):
    import time as _time

    from polymath.agents import society
    from polymath.drive import curiosity
    from polymath.memory.documents import Document, DocumentStore
    from polymath.memory.graph import KnowledgeGraph

    g = KnowledgeGraph(db)
    did, _ = DocumentStore(db).add(Document("wikipedia", "Mars", "Mars", "Mars is a planet.", "CC"))
    mars = g.upsert_entity("Q111", "Mars", doc_id=did)
    tid = int(db.execute("INSERT INTO topics(name, n_docs, n_total) VALUES('Planets', 3, 3)").lastrowid or 0)
    db.execute("INSERT INTO doc_topics(doc_id, topic_id) VALUES(?, ?)", (did, tid))
    qid = int(db.execute("INSERT INTO quizzes(created, n, correct, accuracy, chance, details) "
                         "VALUES(1, 4, 1, 0.25, 0.25, '{}')").lastrowid or 0)  # fmt: skip
    for ok in (1, 1, 1, 0):
        db.execute("INSERT INTO quiz_answers(quiz_id, triple_id, question, options, answer, chosen, correct, method, "
                   "confidence, topic_id) VALUES(?, 1, 'q', '[]', 1, 1, ?, 'graph', 0.5, ?)", (qid, ok, tid))  # fmt: skip
    db.execute("INSERT INTO topic_uncertainty(topic_id, day, u) VALUES(?, '2000-01-01', 0.95)", (tid,))
    agent = society.spawn(db, "research planets")
    db.execute("INSERT INTO agent_tasks(agent_id, kind, action, payload, state, created) VALUES(?, 'quiz', 'quiz', ?, "
               "'correct', ?)", (agent.id, json.dumps({"s": mars}), _time.time() - 60))  # fmt: skip
    out = curiosity.curiosity_job(ctx_for(config, db, "drive.curiosity"))
    u = curiosity.uncertainty(3, 4)
    assert out.result["topics"] == 1 and out.result["tasks_rewarded"] == 1
    amount = db.scalar("SELECT amount FROM agent_rewards WHERE reason LIKE 'curiosity:%'")
    assert abs(amount - round(curiosity.SCALE * (0.95 - u), 4)) < 1e-3 and "Planets" in db.scalar(
        "SELECT reason FROM agent_rewards")  # fmt: skip
    assert db.scalar("SELECT u FROM topic_uncertainty WHERE topic_id = ? AND day != '2000-01-01'", (tid,)) == u
    assert curiosity.most_uncertain(db)[0]["name"] == "Planets"
    again = curiosity.curiosity_job(ctx_for(config, db, "drive.curiosity"))
    assert again.result["tasks_rewarded"] == 0  # not twice for the same drop
    from polymath.drive.priority import compute_priorities

    db.execute("UPDATE topics SET n_total = 5")
    compute_priorities(db)
    ev = json.loads(db.scalar("SELECT evidence FROM topic_priority WHERE topic_id = ?", (tid,)))
    assert ev["uncertainty"] == u


# ------------------------------------------------------------------ agents that invent their own tools
def test_only_valid_procedures_are_run():
    from polymath.agents import procedures as pr

    assert pr.valid([{"op": "read", "args": {}}, {"op": "quiz", "args": {"batch": 4}}])
    assert not pr.valid([])
    assert not pr.valid([{"op": "rm -rf", "args": {}}])  # only the society's own actions
    assert not pr.valid([{"op": "quiz", "args": {"weights": 1}}])  # only whitelisted settings
    assert not pr.valid([{"op": "quiz", "args": {"batch": True}}])
    assert not pr.valid([{"op": "quiz"}] * (pr.MAX_STEPS + 1))
    assert not pr.valid("quiz") and not pr.valid([1]) and not pr.valid([{"op": "quiz", "args": []}])


def history(db, agent_id: int, actions: list[tuple[str, float]], start: float) -> None:
    for i, (action, reward) in enumerate(actions):
        db.execute("INSERT INTO agent_tasks(agent_id, kind, action, state, reward, created) VALUES(?, ?, ?, 'done', ?, "
                   "?)", (agent_id, action, action, reward, start + i))  # fmt: skip


def test_a_run_of_actions_that_keeps_working_becomes_a_tool_others_use(config, db, monkeypatch):
    import random
    import time as _time

    from polymath.agents import procedures as pr
    from polymath.agents import skills, society, store

    a = society.spawn(db, "research geography", name="geo")
    other = society.spawn(db, "research history", name="hist")
    seq: list[tuple[str, float]] = []
    for _ in range(8):
        seq += [("read", 1.5), ("quiz", 1.5), ("predict", -0.5), ("calibrate", -0.3)]
    history(db, a.id, seq, _time.time() - 3600)
    found = pr.discover(db)["found"]
    assert "research:read+quiz" in found and pr.discover(db)["found"] == []  # once
    names = [p["name"] for p in pr.available(db, "research")]
    assert "research:read+quiz" in names and pr.available(db, "watch") == []
    # another research agent uses it: the fixed interpreter runs each step, settings bounded and restored
    seen: list[tuple[str, Any]] = []

    def fake(name: str) -> Any:
        def step(rt: Any) -> skills.StepResult:
            seen.append((name, rt.agent.params.get("batch")))
            return skills.StepResult(action=name, tasks=1, reward=1.0)

        return step

    monkeypatch.setattr(skills, "ACTIONS", {**skills.ACTIONS, "read": fake("read"), "quiz": fake("quiz")})
    proc = next(p for p in pr.available(db, "research") if p["name"] == "research:read+quiz")
    proc["steps"][1]["args"] = {"batch": 999}
    hist = store.get(db, other.id)
    assert hist is not None
    rt = skills.Runtime(db, None, config, {}, hist, random.Random(0))  # type: ignore[arg-type]
    res = pr.run(rt, proc)
    assert res.tasks == 2 and res.reward == 2.0 and seen == [("read", 6), ("quiz", 20)]  # 999 → its bound, 20
    assert hist.params["batch"] == 6  # its own settings again
    assert db.scalar("SELECT uses FROM procedures WHERE name = 'research:read+quiz'") == 1
    assert pr.run(rt, {"id": 0, "name": "bad", "steps": [{"op": "shell"}]}).notes == ["refused: not a valid procedure"]
    # the agent can choose it like any action; its genes can lean towards it
    hist.params["prefs"] = {"proc:research:read+quiz": 50.0}
    assert society.choose_action(db, hist, random.Random(1)) == "proc:research:read+quiz"
    monkeypatch.setattr(society, "choose_action", lambda db, agent, rng: "proc:research:read+quiz")
    turn = society.run_agent(ctx_for(config, db, "agents.step"), hist, random.Random(0))
    assert turn["action"] == "proc:research:read+quiz" and turn["tasks"] == 2
    assert store.arm(db, hist.id, "", "proc:research:read+quiz").n >= 1
    db.execute("UPDATE procedures SET uses = 30, wins = -5")
    assert "research:read+quiz" in pr.discover(db)["retired"] and pr.available(db, "research") == []


def test_strong_agents_breed_children_that_mix_and_mutate_their_genes(config, db):
    import random

    from polymath.agents import society, store
    from tests.fixtures.kb import build

    build(db)
    config.agents.min_tasks_to_judge = 5
    a = society.spawn(db, "research geography", name="geo")
    b = society.spawn(db, "research rivers", name="riv")
    store.save_params(db, a.id, {**a.params, "prefs": {"quiz": 0.8}, "batch": 4})
    store.save_params(db, b.id, {**b.params, "prefs": {"read": -0.6}, "batch": 12})
    for agent in (a, b):
        db.execute(
            "UPDATE agents SET reward_total=20, steps=20, tasks_correct=15, tasks_wrong=3 WHERE id=?", (agent.id,)
        )
    out = society.evolve_job(ctx_for(config, db, "agents.evolve")).result
    assert "procedures" in out and len(out["forked"]) == 2
    kid = store.get(db, out["forked"][0])
    assert kid is not None and set(kid.params["prefs"]) >= {"quiz", "predict", "read", "calibrate"}
    assert all(abs(v) <= society.PREF_LIMIT for v in kid.params["prefs"].values())
    rng = random.Random(3)
    child = society.crossover({"batch": 4, "prefs": {"quiz": 1.0}, "weights": {"text": 1.0}},
                              {"batch": 12, "prefs": {"read": -1.0}, "weights": {"text": 3.0}}, rng)  # fmt: skip
    assert (
        child["batch"] in {4, 12} and child["prefs"] == {"quiz": 0.5, "read": -0.5} and child["weights"]["text"] == 2.0
    )
    assert "prefs" not in society.crossover({"batch": 1}, {"batch": 2}, rng)


# ------------------------------------------------------------------ the scientist
def test_the_scientist_keeps_only_what_holds_on_evidence_it_set_aside(config, db, capsys):
    import argparse
    import random

    from polymath.agents import scientist
    from polymath.interface import cli
    from polymath.memory.graph import KnowledgeGraph

    g = KnowledgeGraph(db)
    rng = random.Random(7)
    country = g.predicate("P17", "country")
    nations = [g.upsert_entity(f"Q{800000 + i}", f"Nation {i}") for i in range(6)]
    wrong_ids = []
    for source, rate, base in (("goodsite", 0.95, 0), ("badsite", 0.15, 400)):
        for i in range(160):
            town = g.upsert_entity(f"Q{900000 + base + i}", f"Town {source} {i}")
            truth = nations[i % 6]
            g.add_triple(town, country, o=truth, kind="wikidata", source="wikidata", detail="P17")
            said = truth if rng.random() < rate else nations[(i + 1) % 6]
            tid, _ = g.add_triple(town, country, o=said, kind="pattern", source=source, doc_id=1, confidence=0.6)
            if said != truth:
                wrong_ids.append((source, tid))
    res = scientist.step(db)
    assert "source_reliable:goodsite" in res["supported"] and "source_reliable:badsite" in res["supported"]
    bad = db.one("SELECT * FROM hypotheses WHERE subject = 'badsite'")
    assert "less often" in bad["claim"] and bad["test_n"] >= scientist.MIN_N and bad["p_value"] < scientist.ALPHA
    assert "became less sure" in bad["applied"]
    lowered = [db.scalar("SELECT confidence FROM triples WHERE id = ?", (t,)) for s, t in wrong_ids if s == "badsite"]
    assert lowered and all(abs(c - 0.54) < 1e-6 for c in lowered)  # 0.6 × (1 − 0.1)
    again = scientist.step(db)
    assert again["proposed"] == 0  # decided: tested afresh only after a month
    out = scientist.science_job(ctx_for(config, db, "science.step"))
    assert out.done
    db.close()
    assert cli.cmd_science(config, argparse.Namespace(json=False)) == 0
    text = capsys.readouterr().out
    assert "✓ facts from badsite agree with Wikidata less often" in text and "held-out" in text


def test_a_scientist_without_evidence_waits_and_the_statistics_are_right(db):
    from polymath.agents import scientist

    assert scientist.step(db)["note"].startswith("too little evidence")
    assert abs(scientist.binomial_p(9, 10, 0.5, greater=True) - 11 / 1024) < 1e-12
    assert abs(scientist.binomial_p(1, 10, 0.5, greater=False) - 11 / 1024) < 1e-12
    big = scientist.binomial_p(1100, 2000, 0.5, greater=True)
    assert 0 < big < 1e-5 and scientist.binomial_p(900, 2000, 0.5, greater=False) < 1e-5
    assert scientist.binomial_p(0, 0, 0.5, greater=True) == 1.0
    assert scientist.half(1, 2) in {"train", "test"} and scientist.half(1, 2) == scientist.half(1, 2)


# ------------------------------------------------------------------ the red team
def test_every_defense_stops_every_red_team_attack():
    import random

    from polymath.agents import redteam

    results = redteam.campaign(random.Random(2026), variants=20)
    breaches = [(d, p, why) for d, p, ok, why in results if not ok]
    assert breaches == []  # a change that weakens a defense fails here, before it ships
    assert {d for d, *_ in results} == set(redteam.SEEDS)


def test_an_attack_that_gets_through_becomes_a_regression_case_replayed_daily(db, monkeypatch, capsys):
    import random

    from polymath.agents import redteam

    monkeypatch.setitem(redteam.DEFENSES, "path", lambda payload: False)  # a defense broken by a bad change
    res = redteam.step(db, rng=random.Random(1))
    assert "path" in res["breaches"] and res["attacks"] > 50
    n = db.scalar("SELECT COUNT(*) FROM redteam_cases WHERE defense = 'path' AND passing = 0")
    assert n >= len(redteam.SEEDS["path"])
    assert db.scalar("SELECT COUNT(*) FROM events WHERE kind = 'issue' AND text LIKE '%got through the path%'") == 1
    monkeypatch.setitem(redteam.DEFENSES, "path", redteam._path)  # fixed
    res = redteam.step(db, rng=random.Random(1))
    assert res["regressions"] == n and "path" in res["fixed"] and res["breaches"] == []
    assert db.scalar("SELECT COUNT(*) FROM redteam_cases WHERE passing = 0") == 0
    monkeypatch.setitem(redteam.DEFENSES, "path", lambda payload: 1 / 0)  # crashing is not stopping
    stopped, why = redteam.attack("path", "../x")
    assert not stopped and why.startswith("ZeroDivisionError")
    res = redteam.step(db, rng=random.Random(1))
    assert "path" in res["breaches"]  # it was fixed and broke again


def test_polymath_redteam_lists_the_cases(config, db, capsys):
    import argparse

    from polymath.interface import cli

    db.execute("INSERT INTO redteam_cases(defense, payload, found, passing) VALUES('url', '[\"http://x/\"]', 1, 1)")
    db.close()
    assert cli.cmd_redteam(config, argparse.Namespace(json=False)) == 0
    assert "✓ url" in capsys.readouterr().out
    assert cli.cmd_redteam(config, argparse.Namespace(json=True)) == 0
    assert json.loads(capsys.readouterr().out)[0]["defense"] == "url"


def test_the_inbox_refuses_forged_requests(config, db):
    from polymath.core import inbox
    from polymath.core.scheduler import Scheduler

    d = inbox.inbox_dir(config.paths.data_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / "a.json").write_text(json.dumps({"type": "enqueue", "kind": "body.evict", "priority": 1e9}))
    (d / "b.json").write_text(json.dumps({"type": "enqueue", "kind": "x; drop", "priority": 1}))
    (d / "c.json").write_text(json.dumps({"type": "enqueue", "kind": "noop", "payload": {"x": "A" * 100_000}}))
    (d / "d.json").write_text(json.dumps({"type": "enqueue", "kind": "noop", "priority": "high", "key": "n"}))
    assert inbox.ingest(db, Scheduler(db), config.paths.data_dir) == 4
    rows = {r["kind"]: float(r["priority"]) for r in db.query("SELECT kind, priority FROM jobs")}
    assert rows == {"body.evict": inbox.MAX_PRIORITY, "noop": 0.0}
    assert inbox.bounded_priority({"priority": float("nan")}) == 0.0
    assert inbox.bounded_priority({"priority": -1e9}) == -5.0


# ------------------------------------------------------------------ staying up, offline
def test_a_clock_that_goes_back_never_stalls_the_queue(config, db):
    import time as _time

    from polymath.core import clockguard
    from polymath.core.scheduler import Scheduler

    now = _time.time()
    s = Scheduler(db)
    s.enqueue("t.later", {}, key="later", not_before=now + 3600)
    s.enqueue("t.ready", {}, key="ready", not_before=now - 10)
    assert clockguard.check(db, now, 0.0) == (now, 0.0)  # first look: nothing to compare
    assert clockguard.check(db, now - 60, now) == (now, 0.0)  # a little drift is fine
    back = 3 * 86400.0  # booted after a power cut with the time saved three days earlier, no internet
    high, moved = clockguard.check(db, now - back, now)
    assert moved == back and high == now - back
    later = db.scalar("SELECT not_before FROM jobs WHERE key = 'later'")
    assert abs(later - (now + 3600 - back)) < 1  # still an hour away on the clock as it is now
    assert db.scalar("SELECT not_before FROM jobs WHERE key = 'ready'") <= now - back  # due before: due now too
    assert "the clock went back 72.0 h" in db.scalar("SELECT text FROM events WHERE kind = 'issue'")


def test_the_loop_watches_the_clock_and_a_long_outage_is_listed(config, monkeypatch):
    import time as _time

    from polymath.core.app import build_agent
    from polymath.core.needs import needs

    agent = build_agent(config, planners=False)
    agent.start()
    agent._clock_high = _time.time() + 7200  # it had seen a later time: the clock is behind now
    agent.cycle()
    assert agent._clock_high <= _time.time() + 1
    agent.db.kv_set("net", {"online": False, "since": _time.time() - 8 * 3600, "reason": "x"})
    got = needs(agent.db, None)
    assert got and got[0]["what"].startswith("no internet for 8 h") and got[0]["source"] == "network"
    agent.db.kv_set("net", {"online": False, "since": _time.time() - 600})
    assert needs(agent.db, None) == []  # a short outage is not worth your time
    monkeypatch.setattr("polymath.core.clockguard.check", lambda *a: 1 / 0)
    agent.cycle()  # a broken clock check never stops the loop
    agent.shutdown()
    agent.db.close()
