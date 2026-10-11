"""Curiosity: which topic should the agent learn about next?

priority(t) = gap(t) × importance(t) × novelty(t) × (1 − recent_effort(t))

* **gap** — how much the agent does *not* know: 1 − quiz accuracy when the
  topic has been quizzed (Laplace-smoothed), otherwise a structural proxy —
  the share of the topic's linked entities whose own article has not been read.
* **importance** — summed PageRank of the topic's entities, scaled to [0, 1]
  by the most important topic.
* **novelty** — 1 / (1 + recent pursuits), pursuits decaying with a 7-day half-life.
* **recent effort** — share of the last 24 h of targeted CPU time spent on the topic.

Every factor and its inputs are stored with the priority so ``polymath why``
can explain the choice.
"""

from __future__ import annotations

import json
import math
import time
from typing import Any

from polymath.core.db import Database

HALF_LIFE = 7 * 86400.0
MIN_DOCS = 3


def compute_priorities(db: Database, *, limit_topics: int = 20_000) -> dict[str, Any]:
    now = time.time()
    topics = db.query(
        "SELECT id, name, n_total FROM topics WHERE n_total >= ? ORDER BY n_total DESC LIMIT ?",
        (MIN_DOCS, limit_topics),
    )
    if not topics:
        return {"topics": 0}
    effort_rows = db.query(
        "SELECT topic_id, SUM(cpu_seconds) AS c FROM topic_effort WHERE at > ? GROUP BY topic_id", (now - 86400,)
    )
    effort = {int(r["topic_id"]): float(r["c"]) for r in effort_rows}
    effort_total = sum(effort.values()) or 1.0
    pursuits = {
        int(r["topic_id"]): r
        for r in db.query("SELECT topic_id, COUNT(*) AS n, MAX(at) AS last FROM topic_effort GROUP BY topic_id")
    }
    quiz = (
        {
            int(r["topic_id"]): (int(r["correct"]), int(r["total"]))
            for r in db.query(
                "SELECT topic_id, SUM(correct) AS correct, COUNT(*) AS total FROM quiz_answers "
                "WHERE topic_id IS NOT NULL GROUP BY topic_id"
            )
        }
        if _has_table(db, "quiz_answers")
        else {}
    )
    from polymath.drive.learn import INTEREST_BOOST, interests

    wanted = interests(db)
    from polymath.drive.strategy import topic_factor

    yields = topic_factor(db)  # facts learned per CPU-second on a topic, against the median (drive.strategy)
    rows = []
    raw_importance: dict[int, float] = {}
    gaps: dict[int, tuple[float, dict[str, Any]]] = {}
    for t in topics:
        tid = int(t["id"])
        stats = db.one(
            "SELECT COALESCE(SUM(e.pagerank), 0) AS pr, COUNT(DISTINCT de.entity_id) AS linked, "
            "COUNT(DISTINCT CASE WHEN e.doc_id IS NULL THEN de.entity_id END) AS unread "
            "FROM doc_topics dt JOIN doc_entities de ON de.doc_id = dt.doc_id JOIN entities e ON e.id = de.entity_id "
            "WHERE dt.topic_id = ?",
            (tid,),
        )
        assert stats is not None
        raw_importance[tid] = float(stats["pr"])
        linked, unread = int(stats["linked"]), int(stats["unread"])
        if tid in quiz:
            correct, total = quiz[tid]
            gap = 1 - (correct + 1) / (total + 2)
            gaps[tid] = (gap, {"source": "quiz", "correct": correct, "total": total})
        else:
            gap = (unread + 1) / (linked + 2)
            gaps[tid] = (gap, {"source": "unread_share", "linked": linked, "unread": unread})
    max_imp = max(raw_importance.values()) or 1.0
    for t in topics:
        tid = int(t["id"])
        importance = raw_importance[tid] / max_imp
        p = pursuits.get(tid)
        decayed = float(p["n"]) * 0.5 ** ((now - float(p["last"])) / HALF_LIFE) if p else 0.0
        novelty = 1.0 / (1.0 + decayed)
        eff = effort.get(tid, 0.0) / effort_total
        gap, gap_ev = gaps[tid]
        boost = INTEREST_BOOST if tid in wanted else 1.0
        teaches = yields.get(tid, 1.0)
        priority = gap * importance * novelty * (1.0 - eff) * boost * teaches
        evidence = {
            "gap": gap_ev,
            "importance_pagerank": round(raw_importance[tid], 3),
            "pursuits_decayed": round(decayed, 3),
            "effort_share_24h": round(eff, 3),
            "user_interest_boost": boost,
            "yield_factor": teaches,
            "docs": int(t["n_total"]),
        }
        rows.append((tid, gap, importance, novelty, eff, priority, json.dumps(evidence), now))
    db.executemany(
        "INSERT INTO topic_priority(topic_id, gap, importance, novelty, effort, priority, evidence, updated) "
        "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(topic_id) DO UPDATE SET gap=excluded.gap, importance=excluded.importance,"
        " novelty=excluded.novelty, effort=excluded.effort, priority=excluded.priority, evidence=excluded.evidence, "
        "updated=excluded.updated",
        rows,
    )
    top = sorted(rows, key=lambda r: -r[5])[:5]
    return {"topics": len(rows), "top": [{"topic_id": r[0], "priority": round(r[5], 4)} for r in top]}


def _has_table(db: Database, name: str) -> bool:
    return bool(db.scalar("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)))


def top_topics(db: Database, k: int = 10) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in db.query(
            "SELECT p.*, t.name FROM topic_priority p JOIN topics t ON t.id = p.topic_id "
            "ORDER BY p.priority DESC LIMIT ?",
            (k,),
        )
    ]


def weakest_topics(db: Database, k: int = 10) -> list[dict[str, Any]]:
    """Topics with the largest knowledge gap among reasonably important ones."""
    return [
        dict(r)
        for r in db.query(
            "SELECT p.*, t.name FROM topic_priority p JOIN topics t ON t.id = p.topic_id WHERE p.importance >= 0.01 "
            "ORDER BY p.gap DESC, p.importance DESC LIMIT ?",
            (k,),
        )
    ]


def unread_entities(db: Database, topic_id: int, k: int = 20) -> list[tuple[int, str]]:
    """Unread articles a topic's documents point to, basics first: the ones its documents refer to most often,
    weighted by importance (PageRank), are the concepts everything else in the topic builds on."""
    return [
        (int(r["id"]), str(r["wiki_title"]))
        for r in db.query(
            "SELECT e.id, e.wiki_title, MAX(e.pagerank) AS pr, COUNT(DISTINCT de.doc_id) AS refs FROM doc_topics dt "
            "JOIN doc_entities de ON de.doc_id = dt.doc_id JOIN entities e ON e.id = de.entity_id WHERE "
            "dt.topic_id = ? AND e.doc_id IS NULL AND e.wiki_title IS NOT NULL GROUP BY e.id "
            "ORDER BY refs * (pr + 1e-6) DESC, pr DESC LIMIT ?",
            (topic_id, k),
        )
    ]


def record_effort(db: Database, topic_id: int, cpu: float) -> None:
    db.execute("INSERT INTO topic_effort(topic_id, cpu_seconds, at) VALUES(?,?,?)", (topic_id, cpu, time.time()))
    db.execute("DELETE FROM topic_effort WHERE at < ?", (time.time() - 60 * 86400,))


def explain_topic(db: Database, topic_id: int) -> dict[str, Any] | None:
    r = db.one(
        "SELECT p.*, t.name FROM topic_priority p JOIN topics t ON t.id=p.topic_id WHERE p.topic_id=?", (topic_id,)
    )
    if r is None:
        return None
    out = dict(r)
    out["evidence"] = json.loads(out["evidence"])
    out["formula"] = (
        f"priority {out['priority']:.4f} = gap {out['gap']:.3f} × importance {out['importance']:.3f} × "
        f"novelty {out['novelty']:.3f} × (1 − effort {out['effort']:.3f})"
    )
    return out


_ = math
