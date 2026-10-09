"""Forward-chaining inference with learned rules (semi-naive evaluation).

Rules are learned from the agent's own data:

* **Property metadata** read from the Wikidata dump: a property entity that is an
  instance of "transitive property" (Q18647515) or "symmetric property"
  (Q18647518), or that names an inverse (P1696).
* **Statistical evidence** in the graph: transitive if ≥ 50 % of observed
  a→b→c chains are closed (a→c); symmetric/inverse if ≥ 80 % of facts already have
  their mirror image. Rule confidence is the observed ratio (metadata rules are
  confirmed by data when there is enough of it).
* **Type constraints**: the dominant ``instance of`` class of a predicate's
  subjects (domain) and objects (range), when ≥ 70 % share it.

Inference is semi-naive: only facts newer than the last run's cursor (plus facts
derived in this run) are joined against the graph, so each run costs work
proportional to what changed. Derived facts are ``inferred`` with provenance
naming the rule and premise triples; confidence = rule confidence × weakest
premise × 0.95 per step, and derivation stops below 0.3. Holdout (quiz) facts are
never used as premises. Type constraints flag pattern-extracted facts that
violate them as ``disputed``.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any

from polymath.core.db import Database
from polymath.memory.graph import KnowledgeGraph

TRANSITIVE_CLASS = "Q18647515"
SYMMETRIC_CLASS = "Q18647518"
INVERSE_PROP = "P1696"
INSTANCE_OF = "P31"
MIN_CONF = 0.3
STEP_DECAY = 0.95


def _pred_id(db: Database, key: str) -> int | None:
    v = db.scalar("SELECT id FROM predicates WHERE key=?", (key,))
    return None if v is None else int(v)


def _upsert_rule(db: Database, kind: str, p: int, q: int | None, conf: float, support: int) -> None:
    db.execute(
        "INSERT INTO rules(kind, p, q, confidence, support, updated) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(kind, p, q) DO UPDATE SET confidence=excluded.confidence, support=excluded.support, "
        "updated=excluded.updated",
        (kind, p, q, round(conf, 4), support, time.time()),
    )


def learn_rules(db: Database, *, min_support: int = 20, sample: int = 5000) -> dict[str, int]:
    graph = KnowledgeGraph(db)
    db.execute("DELETE FROM rules")
    found = {"transitive": 0, "symmetric": 0, "inverse": 0, "domain": 0, "range": 0}
    p31 = _pred_id(db, INSTANCE_OF)
    p_inv = _pred_id(db, INVERSE_PROP)
    meta_transitive: set[int] = set()
    meta_symmetric: set[int] = set()
    meta_inverse: list[tuple[int, int]] = []
    for prop in db.query("SELECT e.id, e.key FROM entities e WHERE e.kind='property'"):
        pid = _pred_id(db, str(prop["key"]))
        if pid is None:
            continue
        if p31 is not None:
            classes = set()
            for o in graph.objects(int(prop["id"]), p31):
                ent = graph.entity(o) if isinstance(o, int) else None
                if ent is not None:
                    classes.add(ent.key)
            if TRANSITIVE_CLASS in classes:
                meta_transitive.add(pid)
            if SYMMETRIC_CLASS in classes:
                meta_symmetric.add(pid)
        if p_inv is not None:
            for o in graph.objects(int(prop["id"]), p_inv):
                if isinstance(o, int) and (e := graph.entity(o)) is not None and (q := _pred_id(db, e.key)):
                    meta_inverse.append((pid, q))
    preds = [
        int(r["p"])
        for r in db.query(
            "SELECT p, COUNT(*) AS n FROM triples WHERE o != 0 AND holdout=0 AND status!='disputed' GROUP BY p "
            "HAVING n >= ?",
            (min_support,),
        )
    ]
    for p in preds:
        # transitivity: closed chains / chains
        chains = db.query(
            "SELECT t1.s AS a, t2.o AS c FROM triples t1 JOIN triples t2 ON t2.s=t1.o AND t2.p=t1.p "
            "WHERE t1.p=? AND t1.holdout=0 AND t2.holdout=0 AND t1.o != 0 AND t2.o != 0 AND t1.s != t2.o LIMIT ?",
            (p, sample),
        )
        if chains:
            closed = sum(
                1
                for c in chains
                if db.scalar("SELECT 1 FROM triples WHERE s=? AND p=? AND o=? AND holdout=0", (c["a"], p, c["c"]))
            )
            ratio = closed / len(chains)
            if len(chains) >= min_support and ratio >= 0.5 and p not in meta_transitive:
                _upsert_rule(db, "transitive", p, None, ratio, len(chains))
                found["transitive"] += 1
        # symmetry
        facts = db.query("SELECT s, o FROM triples WHERE p=? AND o != 0 AND holdout=0 LIMIT ?", (p, sample))
        mirrored = sum(
            1
            for f in facts
            if db.scalar("SELECT 1 FROM triples WHERE s=? AND p=? AND o=? AND holdout=0", (f["o"], p, f["s"]))
        )
        ratio = mirrored / max(1, len(facts))
        if p not in meta_symmetric and len(facts) >= min_support and ratio >= 0.8:
            _upsert_rule(db, "symmetric", p, None, ratio, len(facts))
            found["symmetric"] += 1
    # Declared by the property itself (Wikidata never stores closures or mirrors): the declaration is the
    # evidence, so these hold for every predicate that has facts, however few.
    for kind, declared in (("transitive", meta_transitive), ("symmetric", meta_symmetric)):
        for p in sorted(declared):
            support = int(db.scalar("SELECT COUNT(*) FROM triples WHERE p=? AND o != 0 AND holdout=0", (p,), 0))
            if support:
                _upsert_rule(db, kind, p, None, 0.9, support)
                found[kind] += 1
    for p, q in meta_inverse:  # declared by the property itself (P1696), so trusted like the declarations above
        support = int(db.scalar("SELECT COUNT(*) FROM triples WHERE p=? AND o != 0 AND holdout=0", (p,), 0))
        _upsert_rule(db, "inverse", p, q, 0.9, support)
        found["inverse"] += 1
    if p31 is not None:
        for p in preds:
            if p == p31:
                continue
            for kind, col in (("domain", "s"), ("range", "o")):
                rows = db.query(
                    f"SELECT ty.o AS cls, COUNT(DISTINCT t.{col}) AS n FROM triples t JOIN triples ty "
                    f"ON ty.s=t.{col} AND ty.p=? WHERE t.p=? AND t.o != 0 GROUP BY ty.o ORDER BY n DESC LIMIT 1",
                    (p31, p),
                )
                total = int(
                    db.scalar(
                        f"SELECT COUNT(DISTINCT t.{col}) FROM triples t WHERE t.p=? AND t.o != 0 AND "
                        f"EXISTS (SELECT 1 FROM triples ty WHERE ty.s=t.{col} AND ty.p=?)",
                        (p, p31),
                        0,
                    )
                )
                if rows and total >= min_support and int(rows[0]["n"]) / total >= 0.7:
                    _upsert_rule(db, kind, p, int(rows[0]["cls"]), int(rows[0]["n"]) / total, total)
                    found[kind] += 1
    return found


def forward_chain(db: Database, *, max_new: int = 50_000, tick: Any = None) -> dict[str, int]:
    graph = KnowledgeGraph(db)
    rules = db.query("SELECT kind, p, q, confidence FROM rules WHERE kind IN ('transitive','inverse','symmetric')")
    if not rules:
        return {"new": 0, "examined": 0}
    trans = {int(r["p"]): float(r["confidence"]) for r in rules if r["kind"] == "transitive"}
    sym = {int(r["p"]): float(r["confidence"]) for r in rules if r["kind"] == "symmetric"}
    inv: dict[int, list[tuple[int, float]]] = {}
    for r in rules:
        if r["kind"] == "inverse":
            inv.setdefault(int(r["p"]), []).append((int(r["q"]), float(r["confidence"])))
            inv.setdefault(int(r["q"]), []).append((int(r["p"]), float(r["confidence"])))
    relevant = set(trans) | set(sym) | set(inv)
    cursor = int(db.kv_get("inference_cursor", 0))
    q = ",".join("?" * len(relevant))
    delta: deque[tuple[int, int, int, int, float]] = deque(
        (int(r["id"]), int(r["s"]), int(r["p"]), int(r["o"]), float(r["confidence"]))
        for r in db.query(
            "SELECT id, s, p, o, confidence FROM triples WHERE id > ? AND o != 0 AND holdout=0 "
            f"AND status != 'disputed' "
            f"AND p IN ({q}) ORDER BY id LIMIT ?",
            [cursor, *relevant, max_new],
        )
    )
    max_seen = max((d[0] for d in delta), default=cursor)
    new = examined = 0

    def derive(s: int, p: int, o: int, conf: float, rule: str, premises: list[int]) -> None:
        nonlocal new
        if s == o or conf < MIN_CONF or new >= max_new:
            return
        existing = db.one("SELECT id FROM triples WHERE s=? AND p=? AND o=? AND value=''", (s, p, o))
        if existing is not None:
            return
        tid, _added = graph.add_triple(
            s,
            p,
            o=o,
            status="inferred",
            confidence=round(conf, 4),
            kind="rule",
            source="rule",
            detail=f"{rule}: " + " + ".join(f"#{x}" for x in premises),
        )
        new += 1
        delta.append((tid, s, p, o, conf))

    while delta and new < max_new:
        tid, s, p, o, conf = delta.popleft()
        examined += 1
        if p in sym:
            derive(o, p, s, sym[p] * conf * STEP_DECAY, f"symmetric {p}", [tid])
        for q2, rc in inv.get(p, []):
            derive(o, q2, s, rc * conf * STEP_DECAY, f"inverse {p}->{q2}", [tid])
        if p in trans:
            rc = trans[p]
            for r in db.query(
                "SELECT id, o, confidence FROM triples WHERE s=? AND p=? AND o != 0 AND holdout=0 "
                "AND status != 'disputed' LIMIT 200",
                (o, p),
            ):  # (s p o) + (o p x) => (s p x)
                derive(
                    s,
                    p,
                    int(r["o"]),
                    rc * min(conf, float(r["confidence"])) * STEP_DECAY,
                    f"transitive {p}",
                    [tid, int(r["id"])],
                )
            for r in db.query(
                "SELECT id, s, confidence FROM triples WHERE o=? AND p=? AND holdout=0 "
                "AND status != 'disputed' LIMIT 200",
                (s, p),
            ):  # (x p s) + (s p o) => (x p o)
                derive(
                    int(r["s"]),
                    p,
                    o,
                    rc * min(conf, float(r["confidence"])) * STEP_DECAY,
                    f"transitive {p}",
                    [int(r["id"]), tid],
                )
        if tick and examined % 500 == 0:
            tick()
    db.kv_set("inference_cursor", max_seen)
    return {"new": new, "examined": examined, "cursor": max_seen}


def check_types(db: Database, *, limit: int = 20_000) -> dict[str, int]:
    """Flag pattern-extracted facts whose subject/object type contradicts a learned domain/range."""
    p31 = _pred_id(db, INSTANCE_OF)
    if p31 is None:
        return {"checked": 0, "violations": 0}
    checked = violations = 0
    for rule in db.query("SELECT kind, p, q FROM rules WHERE kind IN ('domain','range')"):
        col = "s" if rule["kind"] == "domain" else "o"
        rows = db.query(
            f"SELECT t.id, t.{col} AS e FROM triples t WHERE t.p=? AND t.o != 0 AND t.status='sourced' AND "
            "EXISTS (SELECT 1 FROM provenance v WHERE v.triple_id=t.id AND v.kind='pattern') AND NOT EXISTS "
            "(SELECT 1 FROM provenance v WHERE v.triple_id=t.id AND v.kind != 'pattern') LIMIT ?",
            (rule["p"], limit),
        )
        for r in rows:
            types = {int(x["o"]) for x in db.query("SELECT o FROM triples WHERE s=? AND p=? AND o != 0", (r["e"], p31))}
            checked += 1
            if types and int(rule["q"]) not in types:
                db.execute("UPDATE triples SET status='disputed', confidence=confidence*0.5 WHERE id=?", (r["id"],))
                violations += 1
    return {"checked": checked, "violations": violations}
