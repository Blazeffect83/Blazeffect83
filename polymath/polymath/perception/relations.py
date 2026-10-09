"""Bootstrapped relation learning: DIPRE patterns with Snowball confidence.

1. **Seeds**: known triples (Wikidata/infobox) between two entities that co-occur
   in a sentence label that sentence's context with the predicate and argument order.
2. **Patterns**: the normalised token sequence *between* the entities (DIPRE's
   "middle"), numbers and articles generalised.
3. **Confidence** (Snowball): every pattern is applied to every context with
   that middle; extractions matching known facts are positive, extractions that
   contradict a functional predicate are negative.
   conf = P / (P + N + 0.1·U + 1).
4. **Extraction**: contexts matched by patterns with confidence ≥ τ produce
   triples; a tuple's confidence is 1 − Π(1 − conf(p)) over the patterns that
   found it. New facts become seeds for the next round.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from polymath.core.db import Database
from polymath.memory.graph import KnowledgeGraph
from polymath.perception.tokenize import tokenize

MAX_MIDDLE = 8
MIN_POSITIVE = 2
MIN_CONFIDENCE = 0.7
_DET = {"a", "an", "the"}


def normalize_middle(text: str) -> str:
    toks: list[str] = []
    depth = 0
    for t in tokenize(text):
        if t.text in "([":
            depth += 1
            continue
        if t.text in ")]":
            depth = max(0, depth - 1)
            continue
        if depth:
            continue
        if t.kind == "num":
            toks.append("<num>")
        elif t.kind == "word":
            w = t.text.lower().rstrip(".")
            toks.append("<det>" if w in _DET else w)
        elif t.text in {",", "'s", "-", "–", ":"}:
            toks.append(t.text)
    return " ".join(toks[: MAX_MIDDLE + 1]) if 1 <= len(toks) <= MAX_MIDDLE else ""


@dataclass
class Context:
    id: int
    e1: int
    e2: int
    doc_id: int
    middle: str
    sentence: str


def functional_predicates(db: Database, threshold: float = 0.8, min_subjects: int = 20) -> set[int]:
    """Predicates where (almost) every subject has a single entity object — learned from the graph."""
    out = set()
    for r in db.query(
        "SELECT p, COUNT(*) AS subjects, SUM(n=1) AS single FROM (SELECT p, s, COUNT(*) AS n FROM triples "
        "WHERE o != 0 AND status != 'disputed' GROUP BY p, s) GROUP BY p HAVING subjects >= ?",
        (min_subjects,),
    ):
        if int(r["single"]) / int(r["subjects"]) >= threshold:
            out.add(int(r["p"]))
    return out


def learn_and_extract(db: Database, *, max_contexts: int = 200_000, tick: Any = None) -> dict[str, int]:
    graph = KnowledgeGraph(db)
    rows = db.query(
        "SELECT id, e1, e2, doc_id, middle, sentence FROM pair_contexts ORDER BY id DESC LIMIT ?", (max_contexts,)
    )
    contexts = [
        Context(int(r["id"]), int(r["e1"]), int(r["e2"]), int(r["doc_id"]), str(r["middle"]), str(r["sentence"]))
        for r in rows
        if r["middle"]
    ]
    if not contexts:
        return {"contexts": 0, "patterns": 0, "extracted": 0}
    by_middle: dict[str, list[Context]] = defaultdict(list)
    for c in contexts:
        by_middle[c.middle].append(c)
    # known entity-entity facts touching these pairs (non-pattern evidence only → no self-reinforcement)
    pairs = {(c.e1, c.e2) for c in contexts} | {(c.e2, c.e1) for c in contexts}
    known: dict[tuple[int, int], set[int]] = defaultdict(set)
    subj_objs: dict[tuple[int, int], set[int]] = defaultdict(set)
    ents = sorted({e for p in pairs for e in p})
    for i in range(0, len(ents), 500):
        part = ents[i : i + 500]
        q = ",".join("?" * len(part))
        for r in db.query(
            f"SELECT t.s, t.p, t.o FROM triples t WHERE t.s IN ({q}) AND t.o != 0 AND t.status != 'disputed' "
            "AND EXISTS (SELECT 1 FROM provenance v WHERE v.triple_id=t.id AND v.kind != 'pattern')",
            part,
        ):
            s, p, o = int(r["s"]), int(r["p"]), int(r["o"])
            subj_objs[(s, p)].add(o)
            if (s, o) in pairs:
                known[(s, o)].add(p)
        if tick:
            tick()
    functional = functional_predicates(db)
    # 1) seed-labelled contexts → candidate patterns
    candidates: set[tuple[int, str, int]] = set()
    for c in contexts:
        for p in known.get((c.e1, c.e2), ()):
            candidates.add((p, c.middle, 0))
        for p in known.get((c.e2, c.e1), ()):
            candidates.add((p, c.middle, 1))
    # 2) Snowball confidence of each pattern over all contexts sharing its middle
    stats: dict[tuple[int, str, int], list[int]] = {}
    for p, middle, order in candidates:
        pos = neg = unk = 0
        for c in by_middle[middle]:
            s, o = (c.e1, c.e2) if order == 0 else (c.e2, c.e1)
            if p in known.get((s, o), ()):
                pos += 1
            elif p in functional and subj_objs.get((s, p)):
                neg += 1
            else:
                unk += 1
        stats[(p, middle, order)] = [pos, neg, unk]
    db.execute("DELETE FROM patterns")
    good: dict[tuple[str, int], list[tuple[int, float]]] = defaultdict(list)
    for (p, middle, order), (pos, neg, unk) in stats.items():
        conf = pos / (pos + neg + 0.1 * unk + 1.0)
        db.execute(
            "INSERT INTO patterns(predicate_id, middle, order_flag, positive, negative, unknown, confidence) "
            "VALUES(?,?,?,?,?,?,?)",
            (p, middle, order, pos, neg, unk, conf),
        )
        if pos >= MIN_POSITIVE and conf >= MIN_CONFIDENCE:
            good[(middle, order)].append((p, conf))
    # 3) extraction
    tuple_conf: dict[tuple[int, int, int], list[tuple[float, Context, str]]] = defaultdict(list)
    for (middle, order), preds in good.items():
        for c in by_middle[middle]:
            s, o = (c.e1, c.e2) if order == 0 else (c.e2, c.e1)
            for p, conf in preds:
                if p not in known.get((s, o), ()):
                    tuple_conf[(s, p, o)].append((conf, c, middle))
    extracted = 0
    for (s, p, o), evid in tuple_conf.items():
        miss = 1.0
        for conf, _c, _m in evid:
            miss *= 1 - conf
        tconf = 1 - miss
        if tconf < MIN_CONFIDENCE:
            continue
        for conf, c, middle in evid[:5]:
            _tid, added = graph.add_triple(
                s,
                p,
                o=o,
                confidence=round(0.85 * tconf, 4),
                kind="pattern",
                source="text",
                doc_id=c.doc_id,
                detail=f"pattern '{middle}' ({conf:.2f}): {c.sentence[:300]}",
                weight=conf,
            )
            extracted += added
    # contexts older than a week are dropped: their patterns have been learned from
    db.execute("DELETE FROM pair_contexts WHERE created < ?", (time.time() - 7 * 86400,))
    return {
        "contexts": len(contexts),
        "patterns": len(stats),
        "confident_patterns": sum(len(v) for v in good.values()),
        "extracted": extracted,
    }
