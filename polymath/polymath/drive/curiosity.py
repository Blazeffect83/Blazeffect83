"""The curiosity engine (v0.13.0): it is drawn to what it is unsure about, and rewarded for becoming sure.

**Uncertainty of a topic** (0 sure … 1 knows nothing), from the self-quiz answers about it (``quiz_answers``): half
how wrong it expects to be (1 − (correct + 1)/(total + 2)) and half how little it knows about its own accuracy (the
spread of the Beta posterior over it, against the widest possible spread). A topic never quizzed is fully
uncertain. More questions narrow the spread; learning raises the accuracy: both lower it.

**Drawn to it.** Topic priority (``drive.priority``) is multiplied by 1 + ``WEIGHT`` × uncertainty: the value of
information is highest where it knows least about its own knowledge.

**Rewarded for shrinking it.** Once a day (job ``drive.curiosity``) each quizzed topic's uncertainty is written
down (``topic_uncertainty``). Where it shrank since the last time, the agents' tasks of that period about things in
that topic share a reward of ``SCALE`` × the drop (``agents.store.reward``; the action that produced the task learns
from it), so agents learn to pick the work that makes them surer. Kept in ``kv curiosity``.
"""

from __future__ import annotations

import json
import math
import time
from typing import Any

from polymath.core.jobs import JobContext, JobOutcome

MAX_STD = math.sqrt(1 / 12)  # the spread of a uniform belief about an accuracy
WEIGHT = 0.5
SCALE = 5.0  # reward for a topic going from fully uncertain to certain (shared by its tasks)
MAX_TASKS = 200


def uncertainty(correct: int, total: int) -> float:
    a, b = correct + 1, total - correct + 1
    std = math.sqrt(a * b / ((a + b) ** 2 * (a + b + 1)))
    expected_error = 1 - a / (a + b)
    return round(min(1.0, 0.5 * expected_error / 0.5 + 0.5 * std / MAX_STD) if total else 1.0, 4)


def topic_quiz(db: Any) -> dict[int, tuple[int, int]]:
    return {
        int(r["topic_id"]): (int(r["c"] or 0), int(r["n"] or 0))
        for r in db.query(
            "SELECT topic_id, SUM(correct) AS c, COUNT(*) AS n FROM quiz_answers WHERE topic_id IS NOT NULL "
            "GROUP BY topic_id"
        )
    }


def curiosity_job(ctx: JobContext) -> JobOutcome:
    db, now = ctx.db, time.time()
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    last = dict(db.kv_get("curiosity") or {})
    since = float(last.get("at") or now - 86400)
    shrunk: dict[int, float] = {}
    for tid, (correct, total) in topic_quiz(db).items():
        u = uncertainty(correct, total)
        prev = db.scalar("SELECT u FROM topic_uncertainty WHERE topic_id = ? AND day < ? ORDER BY day DESC LIMIT 1",
                         (tid, day))  # fmt: skip
        db.execute("INSERT OR REPLACE INTO topic_uncertainty(topic_id, day, u) VALUES(?, ?, ?)", (tid, day, u))
        if prev is not None and float(prev) - u > 1e-4:
            shrunk[tid] = float(prev) - u
    rewarded = 0
    from polymath.agents import store

    for tid, drop in sorted(shrunk.items(), key=lambda kv: -kv[1]):
        tasks = db.query(
            "SELECT t.id FROM agent_tasks t WHERE t.created >= ? AND t.created < ? AND EXISTS "
            "(SELECT 1 FROM entities e JOIN doc_topics dt ON dt.doc_id = e.doc_id WHERE dt.topic_id = ? AND "
            "e.id = CAST(json_extract(t.payload, '$.s') AS INTEGER)) LIMIT ?",
            (since, now, tid, MAX_TASKS),
        )
        if not tasks:
            continue
        name = db.scalar("SELECT name FROM topics WHERE id = ?", (tid,), default=f"topic #{tid}")
        share = round(SCALE * drop / len(tasks), 4)
        for t in tasks:
            store.reward(db, int(t["id"]), share, f"curiosity: less unsure about {name} ({drop:.3f})", correct=None)
            rewarded += 1
    summary = {"at": now, "day": day, "topics": len(shrunk), "mean_drop": round(sum(shrunk.values()) /
               len(shrunk), 4) if shrunk else 0.0, "tasks_rewarded": rewarded}  # fmt: skip
    db.kv_set("curiosity", summary)
    return JobOutcome(done=True, value=0.05 + 0.1 * min(len(shrunk), 10) / 10, result=summary)


def most_uncertain(db: Any, k: int = 5) -> list[dict[str, Any]]:
    """The topics it is least sure about (for ``polymath why`` and the digest)."""
    day = db.scalar("SELECT MAX(day) FROM topic_uncertainty")
    rows = db.query("SELECT u.topic_id, t.name, u.u FROM topic_uncertainty u JOIN topics t ON t.id = u.topic_id "
                    "WHERE u.day = ? ORDER BY u.u DESC LIMIT ?", (day, k))  # fmt: skip
    return [json.loads(json.dumps(dict(r))) for r in rows]


def planner(agent: Any) -> None:
    from polymath.core.scheduler import local_phase

    agent.scheduler.ensure_recurring("drive.curiosity", 86400, priority=1.5,
                                     phase=local_phase((agent.config.body.backup_hour + 3) % 24))  # fmt: skip
