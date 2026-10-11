"""Rules earn their trust: conclusions checked against what sources later said (reasoning.rule_audit)."""

from __future__ import annotations

import time

from polymath.memory.graph import KnowledgeGraph
from polymath.reasoning import inference, rule_audit
from polymath.reasoning.rule_audit import factor, rule_key


def setup(db, n_good: int, n_bad: int):
    """An inverse rule capital(P36) ↔ capital of(P1376) with conclusions; some later confirmed, some contradicted."""
    g = KnowledgeGraph(db)
    cap, cap_of = g.predicate("P36", "capital"), g.predicate("P1376", "capital of")
    db.execute("INSERT INTO rules(kind, p, q, confidence, support, updated) VALUES('inverse', ?, ?, 0.9, 50, ?)",
               (cap, cap_of, time.time()))  # fmt: skip
    out = []
    for i in range(n_good + n_bad):
        country = g.upsert_entity(f"Q{100 + i}", f"Land{i}")
        city = g.upsert_entity(f"Q{500 + i}", f"Town{i}")
        prem = g.add_triple(city, cap_of, o=country, kind="wikidata", source="wikidata", confidence=0.9)[0]
        concl = g.add_triple(country, cap, o=city, status="inferred", confidence=0.6, kind="rule", source="rule",
                             detail=f"inverse {cap_of}->{cap}: #{prem}")[0]  # fmt: skip
        if i < n_good:  # a source later states the same fact
            g.add_triple(country, cap, o=city, kind="wikidata", source="wikidata", confidence=0.9)
        else:  # a source states another capital
            other = g.upsert_entity(f"Q{900 + i}", f"Realcity{i}")
            g.add_triple(country, cap, o=other, kind="wikidata", source="wikidata", confidence=0.9)
        out.append(concl)
    # capital is single-valued: many subjects with one value
    for i in range(80):
        g.add_triple(g.upsert_entity(f"Q{2000 + i}", f"Extra{i}"), cap, o=g.upsert_entity(f"Q{3000 + i}", f"C{i}"),
                     kind="wikidata", source="wikidata")  # fmt: skip
    return g, cap, cap_of, out


def test_a_good_rule_earns_trust_and_wrong_conclusions_are_withdrawn(db):
    _g, cap, cap_of, concl = setup(db, n_good=12, n_bad=2)
    res = rule_audit.audit(db)
    key = rule_key("inverse", cap_of, cap)
    row = db.one("SELECT * FROM rule_trust WHERE kind = 'inverse' AND p = ? AND q = ?", key[1:])
    assert row["confirmed"] == 12 and row["refuted"] == 2 and row["state"] == "active"
    assert abs(row["trust"] - 13 / 16) < 1e-3
    assert res["removed"] == 2 and not db.scalar("SELECT 1 FROM triples WHERE id = ?", (concl[-1],))
    assert db.scalar("SELECT 1 FROM triples WHERE id = ?", (concl[0],))  # a confirmed one stays (now sourced too)
    ch = db.one("SELECT * FROM self_changes WHERE action = 'corrected'")
    assert "Withdrew 2 conclusions" in ch["summary"] and "e.g. Land13 → capital → Town13" in ch["summary"]
    assert db.kv_get("rule_audit_buckets") == {"0.60": [12, 2]}
    assert factor(rule_audit.trust_of(db)[key]) == 1.0  # trusted: full confidence


def test_a_bad_rule_is_demoted_and_its_facts_removed(db):
    g, cap, cap_of, concl = setup(db, n_good=2, n_bad=10)
    # an unchecked conclusion only this rule supports
    lone = g.upsert_entity("Q7777", "Lonely")
    lone_city = g.upsert_entity("Q7778", "Lonetown")
    prem = g.add_triple(lone_city, cap_of, o=lone, kind="wikidata", source="wikidata")[0]
    unchecked = g.add_triple(lone, cap, o=lone_city, status="inferred", confidence=0.6, kind="rule", source="rule",
                             detail=f"inverse {cap_of}->{cap}: #{prem}")[0]  # fmt: skip
    res = rule_audit.audit(db)
    key = rule_key("inverse", cap, cap_of)
    assert res["demoted"] == ["“capital” ↔ “capital of”"]
    assert db.scalar("SELECT state FROM rule_trust WHERE p = ? AND q = ?", key[1:]) == "demoted"
    assert not db.scalar("SELECT 1 FROM triples WHERE id = ?", (unchecked,))
    assert db.scalar("SELECT 1 FROM triples WHERE id = ?", (concl[0],))  # confirmed by a source: kept
    summary = db.scalar("SELECT summary FROM self_changes WHERE action = 'demoted'")
    assert "Stopped trusting the rule “capital” ↔ “capital of”" in summary and "10 were contradicted" in summary
    assert factor(rule_audit.trust_of(db)[key]) == 0.0
    # inference no longer uses it
    db.kv_set("inference_cursor", 0)
    assert inference.forward_chain(db) == {"new": 0, "examined": 0}
    # what it removed is remembered: the mistakes as refuted, the unchecked rest as demoted
    reasons = dict(db.query("SELECT reason, COUNT(*) FROM withdrawn GROUP BY reason"))  # type: ignore[arg-type]
    assert reasons == {"refuted": 10, "demoted": 1}
    # deleting its mistakes does not clean its record: they still count against it
    rule_audit.audit(db)
    assert db.scalar("SELECT refuted FROM rule_trust WHERE p = ?", (key[1],)) == 10
    assert db.scalar("SELECT state FROM rule_trust WHERE p = ?", (key[1],)) == "demoted"
    # evidence turns around (sources confirm many of its conclusions): trusted again
    for i in range(30):
        country = g.upsert_entity(f"Q{4000 + i}", f"New{i}")
        city = g.upsert_entity(f"Q{4100 + i}", f"Newtown{i}")
        p2 = g.add_triple(city, cap_of, o=country, kind="wikidata", source="wikidata")[0]
        g.add_triple(country, cap, o=city, status="inferred", confidence=0.6, kind="rule", source="rule",
                     detail=f"inverse {cap_of}->{cap}: #{p2}")  # fmt: skip
        g.add_triple(country, cap, o=city, kind="wikidata", source="wikidata")
    db.kv_set("inference_cursor", 999_999)
    rule_audit.audit(db)
    assert db.scalar("SELECT state FROM rule_trust WHERE p = ?", (key[1],)) == "active"
    assert db.scalar("SELECT COUNT(*) FROM self_changes WHERE action = 'promoted'") == 1
    # its unchecked conclusions may come back; the refuted ones never do
    assert dict(db.query("SELECT reason, COUNT(*) FROM withdrawn GROUP BY reason")) == {"refuted": 10}  # type: ignore[arg-type]
    assert db.kv_get("inference_cursor") == 0
    inference.forward_chain(db)
    assert db.scalar("SELECT 1 FROM triples WHERE s = ? AND p = ? AND o = ?", (lone, cap, lone_city))
    bad = db.query("SELECT s, p, o FROM withdrawn")
    assert not any(db.scalar("SELECT 1 FROM triples WHERE s = ? AND p = ? AND o = ?", (r["s"], r["p"], r["o"]))
                   for r in bad)  # fmt: skip


def test_a_refuted_conclusion_is_never_derived_again(db):
    _g, cap, cap_of, _concl = setup(db, n_good=12, n_bad=2)
    rule_audit.audit(db)
    gone = db.one("SELECT s, p, o FROM withdrawn")
    assert gone is not None and rule_audit.is_withdrawn(db, gone["s"], gone["p"], gone["o"])
    db.kv_set("inference_cursor", 0)
    inference.forward_chain(db)  # the premise is still there, but the conclusion stays withdrawn
    assert not db.scalar("SELECT 1 FROM triples WHERE s = ? AND p = ? AND o = ?", (gone["s"], gone["p"], gone["o"]))
    # and it keeps counting against the rule on the next audit
    rule_audit.audit(db)
    assert db.scalar("SELECT refuted FROM rule_trust WHERE p = ?", (min(cap, cap_of),)) == 2


def test_a_sparse_relation_is_not_judged_single_valued(db):
    """Few subjects, or a few with many values: no conclusion is called contradicted on that basis."""
    g = KnowledgeGraph(db)
    part = g.predicate("P710", "participant")
    for i in range(10):  # ten subjects, one value each: too few to call it single-valued
        g.add_triple(g.upsert_entity(f"Q{i}", f"E{i}"), part, o=g.upsert_entity(f"Q{100 + i}", f"P{i}"),
                     kind="wikidata", source="wikidata")  # fmt: skip
    assert part not in rule_audit.strict_functional(db)
    for i in range(10, 60):
        g.add_triple(g.upsert_entity(f"Q{i}", f"E{i}"), part, o=g.upsert_entity(f"Q{100 + i}", f"P{i}"),
                     kind="wikidata", source="wikidata")  # fmt: skip
    assert part in rule_audit.strict_functional(db)  # 60 subjects, all single-valued
    for i in range(4):  # 4 of 60 with a second value: 93 % single, below the bar
        g.add_triple(g.upsert_entity(f"Q{i}", f"E{i}"), part, o=g.upsert_entity(f"Q{900 + i}", f"X{i}"),
                     kind="wikidata", source="wikidata")  # fmt: skip
    assert part not in rule_audit.strict_functional(db)


def test_trust_factor_and_keys():
    assert rule_key("inverse", 9, 3) == ("inverse", 3, 9) == rule_key("inverse", 3, 9)
    assert rule_key("symmetric", 5, None) == ("symmetric", 5, 0)
    assert factor(None) == 1.0 and factor((0.2, 3, "active")) == 1.0  # too little evidence to judge
    assert abs(factor((0.6, 20, "active")) - 0.75) < 1e-9 and factor((0.9, 20, "active")) == 1.0
    assert rule_audit.prefixes(("transitive", 4, 0)) == ["transitive 4:%"]


def test_doubtful_rule_weakens_its_conclusions(db):
    g = KnowledgeGraph(db)
    border = g.predicate("P47", "shares border with")
    db.execute("INSERT INTO rules(kind, p, q, confidence, support, updated) VALUES('symmetric', ?, NULL, 0.9, 50, ?)",
               (border, time.time()))  # fmt: skip
    db.execute("INSERT INTO rule_trust(kind, p, q, checked, confirmed, refuted, trust, state, updated) "
               "VALUES('symmetric', ?, 0, 20, 11, 9, 0.55, 'active', ?)", (border, time.time()))  # fmt: skip
    a, b = g.upsert_entity("Q1", "A"), g.upsert_entity("Q2", "B")
    g.add_triple(a, border, o=b, kind="wikidata", source="wikidata", confidence=1.0)
    inference.forward_chain(db)
    conf = db.scalar("SELECT confidence FROM triples WHERE s = ? AND o = ? AND status = 'inferred'", (b, a))
    assert abs(conf - round(0.9 * (0.55 / 0.8) * 1.0 * 0.95, 4)) < 1e-4  # 0.9 × trust factor × premise × decay


def test_job_and_planner(db):
    from polymath.core.scheduler import Scheduler

    agent = type("A", (), {"db": db, "scheduler": Scheduler(db)})()
    rule_audit.planner(agent)
    assert db.scalar("SELECT COUNT(*) FROM jobs WHERE kind = 'reason.audit'") == 0  # nothing inferred yet
    setup(db, 3, 0)
    rule_audit.planner(agent)
    assert db.scalar("SELECT COUNT(*) FROM jobs WHERE kind = 'reason.audit'") == 1
    out = rule_audit.audit_job(type("C", (), {"db": db, "tick": staticmethod(lambda: None)})())
    assert out.done and out.result["rules"] == 1
