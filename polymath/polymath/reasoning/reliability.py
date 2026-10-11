"""Learned source reliability and evidence-combining confidence (iterative truth discovery).

Sources are provenance origins: ``wikidata``, ``wikipedia`` (infoboxes),
``text`` (learned relation patterns), ``rule`` (inference) …

Alternating fixed point (TruthFinder-style):

* fact confidence  c(f) = 1 − Π_{s ∈ S(f)} (1 − r(s)·w(s, f))   (noisy-OR over
  independent sources; w is the provenance weight, e.g. a pattern's confidence),
  then, for a *functional* (subject, predicate), the competing objects' confidences
  are renormalised so they cannot all be near 1;
* source reliability r(s) = (Σ_{f ∈ F(s)} c(f) + α) / (|F(s)| + α + β) — a Beta
  prior keeps sources with little evidence near 0.5·(α/(α+β)) … here α=4, β=1.

Holdout facts (reserved for self-evaluation) never contribute evidence.
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Any

from polymath.core.db import Database

ALPHA, BETA = 4.0, 1.0
PRIOR = {"wikidata": 0.9, "wikipedia": 0.8, "text": 0.6, "rule": 0.7}


def load_reliability(db: Database) -> dict[str, float]:
    rel = dict(PRIOR)
    for r in db.query("SELECT source, reliability FROM source_reliability"):
        rel[str(r["source"])] = float(r["reliability"])
    return rel


def truth_discovery(
    db: Database, *, functional: set[int], iterations: int = 10, max_facts: int = 2_000_000
) -> dict[str, Any]:
    rows = db.query(
        "SELECT v.triple_id, v.source, MAX(v.weight) AS w, t.s, t.p, t.o, t.value FROM provenance v "
        "JOIN triples t ON t.id=v.triple_id WHERE t.holdout=0 AND v.kind != 'rule' "
        "GROUP BY v.triple_id, v.source LIMIT ?",
        (max_facts,),
    )
    support: dict[int, list[tuple[str, float]]] = defaultdict(list)
    group_of: dict[int, tuple[int, int]] = {}
    for r in rows:
        tid = int(r["triple_id"])
        support[tid].append((str(r["source"]), min(1.0, float(r["w"]))))
        if int(r["p"]) in functional:
            group_of[tid] = (int(r["s"]), int(r["p"]))
    groups: dict[tuple[int, int], list[int]] = defaultdict(list)
    for tid, g in group_of.items():
        groups[g].append(tid)
    rel = load_reliability(db)
    conf: dict[int, float] = {}
    for _ in range(iterations):
        for tid, srcs in support.items():
            miss = 1.0
            for s, w in srcs:
                miss *= 1.0 - rel.get(s, 0.5) * w
            conf[tid] = 1.0 - miss
        for members in groups.values():  # competing values of a functional relation share belief
            if len(members) > 1:
                total = sum(conf[m] for m in members)
                if total > 1.0:
                    for m in members:
                        conf[m] = conf[m] / total
        acc: dict[str, list[float]] = defaultdict(list)
        for tid, srcs in support.items():
            for s, _w in srcs:
                acc[s].append(conf[tid])
        new_rel = {s: (sum(v) + ALPHA) / (len(v) + ALPHA + BETA) for s, v in acc.items()}
        delta = max((abs(new_rel[s] - rel.get(s, 0.5)) for s in new_rel), default=0.0)
        rel.update(new_rel)
        if delta < 1e-4:
            break
    now = time.time()
    counts: dict[str, int] = defaultdict(int)
    for srcs in support.values():
        for s, _w in srcs:
            counts[s] += 1
    db.executemany(
        "INSERT INTO source_reliability(source, reliability, n_facts, updated) VALUES(?,?,?,?) "
        "ON CONFLICT(source) DO UPDATE SET reliability=excluded.reliability, n_facts=excluded.n_facts, "
        "updated=excluded.updated",
        [(s, rel[s], counts[s], now) for s in counts],
    )
    db.executemany(
        "UPDATE triples SET confidence=? WHERE id=? AND status != 'inferred'",
        [(round(c, 4), tid) for tid, c in conf.items()],
    )
    return {"facts": len(conf), "sources": {s: round(rel[s], 4) for s in counts}}
