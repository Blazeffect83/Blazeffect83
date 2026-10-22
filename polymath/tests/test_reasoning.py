"""Phase 6: source reliability, confidence, contradictions, rule learning, forward chaining, type checks."""

from __future__ import annotations

import json
import threading
import time

from polymath.core.jobs import JobContext
from polymath.core.scheduler import Job, Scheduler
from polymath.memory.graph import KnowledgeGraph
from polymath.reasoning import jobs as rj
from polymath.reasoning.contradictions import compatible, detect, disputes, functional_predicates_all
from polymath.reasoning.inference import check_types, forward_chain, learn_rules
from polymath.reasoning.reliability import load_reliability, truth_discovery


def ctx_for(config, db, kind, payload=None):
    s = Scheduler(db)
    key = f"{kind}:{time.time_ns()}"
    jid, _ = s.enqueue(kind, payload or {}, key=key)
    return JobContext(
        config=config,
        db=db,
        scheduler=s,
        job=Job(jid, kind, key, payload or {}, None, 0, 0, 0, 5, 0),
        deadline=time.monotonic() + 60,
        stop_event=threading.Event(),
        services={},
    )


def countries_graph(db, n: int = 30):
    g = KnowledgeGraph(db)
    cap = g.predicate("P36", "capital")
    countries = [g.upsert_entity(f"Q{i}", f"Country{i}") for i in range(n)]
    cities = [g.upsert_entity(f"Q{1000 + i}", f"City{i}") for i in range(n)]
    for i in range(n):
        g.add_triple(countries[i], cap, o=cities[i], kind="wikidata", source="wikidata")
    return g, cap, countries, cities


def test_compatibility_of_values():
    t = lambda v: (0, json.dumps(v, sort_keys=True))  # noqa: E731
    assert compatible(t({"time": "1950"}), t({"time": "1950-05-03"}))
    assert not compatible(t({"time": "1950-05-03"}), t({"time": "1951-05-03"}))
    assert compatible(t({"amount": 100.0, "unit": "km"}), t({"amount": 103.0, "unit": "km"}))
    assert not compatible(t({"amount": 100.0, "unit": "km"}), t({"amount": 120.0, "unit": "km"}))
    assert compatible(t({"amount": 100.0, "unit": "km"}), t({"amount": 60.0, "unit": "mi"}))
    assert compatible(t({"text": "Paris "}), t({"text": "paris"})) and compatible((5, ""), (5, ""))
    assert not compatible((5, ""), (6, "")) and not compatible((0, "{bad"), (0, "{worse"))
    assert compatible((0, '"x"'), (0, '"x"'))


def test_functional_learning_and_contradictions(db):
    g, cap, countries, cities = countries_graph(db)
    assert cap in functional_predicates_all(db)
    # a text pattern claims a second capital for Country0 (weak) and Country1 (equally strong)
    g.add_triple(countries[0], cap, o=cities[5], confidence=0.3, kind="pattern", source="text", doc_id=1)
    db.execute("UPDATE triples SET confidence=0.95 WHERE s=? AND o=?", (countries[0], cities[0]))
    g.add_triple(countries[1], cap, o=cities[6], confidence=0.6, kind="pattern", source="text", doc_id=2)
    db.execute("UPDATE triples SET confidence=0.6 WHERE s=? AND o=?", (countries[1], cities[1]))
    res = detect(db, functional_predicates_all(db))
    assert res["conflicts"] == 2
    status = {(int(r["s"]), int(r["o"])): r["status"] for r in db.query("SELECT s, o, status FROM triples")}
    assert status[(countries[0], cities[0])] == "sourced" and status[(countries[0], cities[5])] == "disputed"
    assert status[(countries[1], cities[1])] == "disputed" and status[(countries[1], cities[6])] == "disputed"
    open_disputes = disputes(db)
    assert {d["subject"] for d in open_disputes} == {"Country0", "Country1"}
    # new evidence settles the second dispute
    db.execute("DELETE FROM triples WHERE s=? AND o=?", (countries[1], cities[6]))
    assert detect(db, functional_predicates_all(db))["settled"] == 1
    assert detect(db, set()) == {"groups_checked": 0, "conflicts": 0, "disputed": 0, "settled": 0}


def test_one_record_listing_several_values_is_not_a_dispute(db):
    g, _cap, countries, _cities = countries_graph(db)
    country = g.predicate("P17", "country")
    langs = [g.upsert_entity(f"Q{500 + i}", f"Lang{i}") for i in range(25)]
    for i, lang in enumerate(langs):  # learned single-valued: almost every language has one country …
        g.add_triple(lang, country, o=countries[i], kind="wikidata", source="wikidata")
    assert country in functional_predicates_all(db)
    for c in countries[1:6]:  # … but Wikidata itself lists five countries for this one
        g.add_triple(langs[0], country, o=c, kind="wikidata", source="wikidata")
    g.add_triple(langs[1], country, o=countries[9], kind="infobox", source="wikipedia", doc_id=7)  # a real conflict
    res = detect(db, functional_predicates_all(db))
    status = {(int(r["s"]), int(r["o"])): r["status"] for r in db.query("SELECT s, o, status FROM triples")}
    assert all(status[(langs[0], c)] == "sourced" for c in countries[:6])
    assert status[(langs[1], countries[9])] == "disputed" and res["conflicts"] == 1
    db.execute("UPDATE triples SET status='disputed' WHERE s=?", (langs[0],))  # a stale dispute is settled
    assert detect(db, functional_predicates_all(db))["settled"] >= 6


def test_literal_conflicts(db):
    g = KnowledgeGraph(db)
    born = g.predicate("P569", "date of birth")
    people = [g.upsert_entity(f"Q{i}", f"P{i}") for i in range(25)]
    for i, p in enumerate(people):
        g.add_triple(p, born, value={"time": f"19{50 + i % 40:02d}-01-01"}, kind="wikidata", source="wikidata")
    g.add_triple(people[0], born, value={"time": "1950"}, kind="infobox", source="wikipedia")  # same, less precise
    g.add_triple(people[1], born, value={"time": "1977-03-03"}, kind="infobox", source="wikipedia")
    res = detect(db, functional_predicates_all(db))
    assert res["conflicts"] == 1


def test_truth_discovery_learns_reliability(db):
    g, cap, countries, cities = countries_graph(db, 40)
    # "web" agrees with wikidata on 20 facts; "spam" asserts 20 conflicting capitals
    for i in range(20):
        g.add_triple(countries[i], cap, o=cities[i], kind="pattern", source="web", doc_id=i + 1)
        g.add_triple(countries[i], cap, o=cities[(i + 7) % 40], kind="pattern", source="spam", doc_id=100 + i)
    res = truth_discovery(db, functional={cap})
    rel = load_reliability(db)
    assert rel["web"] > rel["spam"] and rel["wikidata"] > rel["spam"]
    assert res["facts"] > 40
    good = db.scalar("SELECT confidence FROM triples WHERE s=? AND o=?", (countries[0], cities[0]))
    bad = db.scalar("SELECT confidence FROM triples WHERE s=? AND o=?", (countries[0], cities[7]))
    assert good > bad


def test_rule_learning_from_property_metadata_and_statistics(db):
    g = KnowledgeGraph(db)
    p31 = g.predicate("P31", "instance of")
    p279 = g.predicate("P279", "subclass of")
    p47 = g.predicate("P47", "shares border with")
    p40 = g.predicate("P40", "child")
    p22 = g.predicate("P22", "father")
    p1696 = g.predicate("P1696", "inverse property")
    # property entities carry their own metadata, read from the Wikidata dump
    prop279 = g.upsert_entity("P279", "subclass of", kind="property")
    g.add_triple(prop279, p31, o=g.stub("Q18647515"), kind="wikidata", source="wikidata")
    prop40 = g.upsert_entity("P40", "child", kind="property")
    g.add_triple(prop40, p1696, o=g.upsert_entity("P22", "father", kind="property"), kind="wikidata", source="wikidata")
    classes = [g.upsert_entity(f"Q{100 + i}", f"Class{i}") for i in range(30)]
    for i in range(29):  # a chain of subclasses
        g.add_triple(classes[i], p279, o=classes[i + 1], kind="wikidata", source="wikidata")
    countries = [g.upsert_entity(f"Q{200 + i}", f"Country{i}") for i in range(30)]
    country_cls = g.upsert_entity("Q6256", "country")
    for i in range(30):  # symmetric borders (all mirrored)
        g.add_triple(countries[i], p47, o=countries[(i + 1) % 30], kind="wikidata", source="wikidata")
        g.add_triple(countries[(i + 1) % 30], p47, o=countries[i], kind="wikidata", source="wikidata")
        g.add_triple(countries[i], p31, o=country_cls, kind="wikidata", source="wikidata")
    parents = [g.upsert_entity(f"Q{300 + i}", f"Parent{i}") for i in range(25)]
    kids = [g.upsert_entity(f"Q{400 + i}", f"Kid{i}") for i in range(25)]
    for i in range(25):
        g.add_triple(parents[i], p40, o=kids[i], kind="wikidata", source="wikidata")
        if i < 10:
            g.add_triple(kids[i], p22, o=parents[i], kind="wikidata", source="wikidata")
    found = learn_rules(db)
    rules = {(r["kind"], r["p"], r["q"]) for r in db.query("SELECT kind, p, q FROM rules")}
    assert ("transitive", p279, None) in rules and ("symmetric", p47, None) in rules
    assert ("inverse", p40, p22) in rules and ("domain", p47, country_cls) in rules
    assert found["transitive"] >= 1
    res = forward_chain(db)
    assert res["new"] > 0
    assert (
        db.scalar("SELECT status FROM triples WHERE s=? AND p=? AND o=?", (classes[0], p279, classes[2])) == "inferred"
    )
    assert db.scalar("SELECT 1 FROM triples WHERE s=? AND p=? AND o=?", (kids[20], p22, parents[20])) == 1
    prov = db.one("SELECT detail FROM provenance WHERE kind='rule' LIMIT 1")
    assert prov and "#" in prov["detail"]
    deep = db.scalar("SELECT confidence FROM triples WHERE s=? AND p=? AND o=?", (classes[0], p279, classes[8]))
    assert deep is None or deep < 0.9  # confidence decays with derivation depth
    assert forward_chain(db)["new"] == 0  # semi-naive: nothing new without new facts
    # holdout facts are never premises
    db.execute("UPDATE triples SET holdout=1 WHERE s=? AND p=?", (parents[24], p40))
    db.execute("DELETE FROM triples WHERE s=? AND p=?", (kids[24], p22))
    db.kv_set("inference_cursor", 0)
    forward_chain(db)
    assert db.scalar("SELECT 1 FROM triples WHERE s=? AND p=?", (kids[24], p22)) is None


def test_type_constraints_flag_bad_extractions(db):
    g = KnowledgeGraph(db)
    p31 = g.predicate("P31", "instance of")
    cap = g.predicate("P36", "capital")
    country, city, person = (
        g.upsert_entity(k, lab) for k, lab in (("Q6256", "country"), ("Q515", "city"), ("Q5", "human"))
    )
    for i in range(25):
        c = g.upsert_entity(f"Q{i}", f"C{i}")
        t = g.upsert_entity(f"Q{100 + i}", f"T{i}")
        g.add_triple(c, p31, o=country, kind="wikidata", source="wikidata")
        g.add_triple(t, p31, o=city, kind="wikidata", source="wikidata")
        g.add_triple(c, cap, o=t, kind="wikidata", source="wikidata")
    bob = g.upsert_entity("Q999", "Bob")
    g.add_triple(bob, p31, o=person, kind="wikidata", source="wikidata")
    bad, _ = g.add_triple(bob, cap, o=g.by_key("Q100").id, kind="pattern", source="text", doc_id=5)  # type: ignore[union-attr]
    learn_rules(db)
    res = check_types(db)
    assert res["violations"] == 1 and db.scalar("SELECT status FROM triples WHERE id=?", (bad,)) == "disputed"


def test_reasoning_jobs_and_planner(config, db):
    from polymath.body.systemd_notify import Notifier
    from polymath.core.app import build_components
    from polymath.core.loop import Agent

    countries_graph(db)
    for kind, fn in (
        ("reason.rules", rj.rules_job),
        ("reason.infer", rj.infer_job),
        ("reason.contradictions", rj.contradictions_job),
        ("reason.reliability", rj.reliability_job),
    ):
        out = fn(ctx_for(config, db, kind))
        assert out.done, kind
    assert db.scalar("SELECT COUNT(*) FROM reasoning_runs") == 4
    comps = build_components(config, db)
    agent = Agent(config, db, comps.registry, services=comps.services, notifier=Notifier({}))
    rj.planner(agent)
    kinds = {r["kind"] for r in db.query("SELECT kind FROM jobs WHERE state='queued'")}
    assert {"reason.rules", "reason.infer", "reason.contradictions", "reason.reliability"} <= kinds
