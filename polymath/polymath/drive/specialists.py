"""Specialists it spawns for itself: when the self-test shows a weak spot, an agent is created to fix it.

``self.specialists`` (every 6 hours) reads the self-test answers of the last ``WINDOW_DAYS`` days, relation by
relation. A relation is a **weak spot** when it has at least ``MIN_ANSWERS`` answers and its accuracy is below 50 %, or
20 points below the overall accuracy. For each weak spot (at most ``MAX_ACTIVE`` at a time) it spawns a *predict*
agent scoped to that relation, for example "fix birth places". The agent works like any other: it quizzes itself,
re-learns its own evidence weights for the relation, makes predictions, and is paid only for verified results.

It retires the specialist when the relation recovers (accuracy since the spawn at least 70 %, or within 5 points of
overall and 10 points better than before, over ``MIN_ANSWERS_AFTER`` answers), or after ``GIVE_UP_DAYS`` without
recovering. Either way the changelog records the accuracy before and after. Your own agents are never touched, and
specialists are marked ``auto``.
"""

from __future__ import annotations

import time
from typing import Any

from polymath.agents import store
from polymath.core.db import Database
from polymath.core.jobs import JobContext, JobOutcome
from polymath.drive import changelog

WINDOW_DAYS = 14
MIN_ANSWERS = 12
MIN_ANSWERS_AFTER = 8
MAX_ACTIVE = 3
GIVE_UP_DAYS = 21
MIN_AGE_DAYS = 2
RECOVERED = 0.7
DAY = 86400.0


def by_relation(db: Database, since: float) -> dict[int, tuple[int, int]]:
    """(right, total) self-test answers per relation since ``since``."""
    return {
        int(r["p"]): (int(r["right"] or 0), int(r["n"]))
        for r in db.query(
            "SELECT t.p, SUM(a.correct) AS right, COUNT(*) AS n FROM quiz_answers a JOIN quizzes z ON z.id = a.quiz_id "
            "JOIN triples t ON t.id = a.triple_id WHERE z.created >= ? GROUP BY t.p",
            (since,),
        )
    }


def weak_spots(db: Database, now: float) -> tuple[list[tuple[int, float, int]], float]:
    rel = by_relation(db, now - WINDOW_DAYS * DAY)
    total = sum(n for _r, n in rel.values())
    overall = sum(r for r, _n in rel.values()) / total if total else 0.0
    weak = [(p, r / n, n) for p, (r, n) in rel.items() if n >= MIN_ANSWERS and (r / n < 0.5 or r / n < overall - 0.2)]
    return sorted(weak, key=lambda x: (x[1], -x[2])), overall


def plabel(db: Database, p: int) -> str:
    return str(db.scalar("SELECT label FROM predicates WHERE id = ?", (p,), default=f"relation {p}"))


def specialists(db: Database) -> list[store.Agent]:
    return [a for a in store.active(db) if a.origin == "auto"]


def step(db: Database, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    out: dict[str, Any] = {"spawned": [], "retired": []}
    weak, overall = weak_spots(db, now)
    # 1) retire the ones whose relation recovered, or that did not help in time
    for a in specialists(db):
        p = int((a.scope.get("predicates") or [0])[0])
        born = float(db.scalar("SELECT created FROM agents WHERE id = ?", (a.id,), default=now))
        if now - born < MIN_AGE_DAYS * DAY:
            continue
        right, n = by_relation(db, born).get(p, (0, 0))
        acc = right / n if n else 0.0
        before = float(a.params.get("weak_accuracy", 0.0))
        name = plabel(db, p)
        if n >= MIN_ANSWERS_AFTER and (acc >= RECOVERED or (acc >= overall - 0.05 and acc >= before + 0.1)):
            store.set_status(db, a.id, "retired", f"{name} recovered: {acc:.0%}")
            changelog.record(
                db, "specialists", a.name, "retired",
                f"Retired its specialist {a.name}: “{name}” went from {before:.0%} to {acc:.0%} right on the self-test",
                before=before, after=acc,
            )  # fmt: skip
            out["retired"].append(a.name)
        elif now - born > GIVE_UP_DAYS * DAY:
            store.set_status(db, a.id, "retired", f"{name} did not recover in {GIVE_UP_DAYS} days")
            changelog.record(
                db, "specialists", a.name, "retired",
                f"Retired its specialist {a.name} without success: “{name}” is at {acc:.0%} ({n} answers) after "
                f"{GIVE_UP_DAYS} days, from {before:.0%}; it will try again if it stays weak",
                before=before, after=acc,
            )  # fmt: skip
            out["retired"].append(a.name)
    # 2) spawn for the weakest relations that have no specialist yet
    covered = {int((a.scope.get("predicates") or [0])[0]) for a in specialists(db)}
    recent = {
        int(r["p"])
        for r in db.query(
            "SELECT CAST(json_extract(scope, '$.predicates[0]') AS INTEGER) AS p FROM agents WHERE origin = 'auto' "
            "AND updated >= ?",
            (now - 7 * DAY,),
        )
        if r["p"] is not None
    }  # a specialist retired this week is not re-spawned straight away
    for p, acc, n in weak:
        if len(specialists(db)) >= MAX_ACTIVE:
            break
        if p in covered or p in recent:
            continue
        name = plabel(db, p)
        aid = store.create(
            db, kind="predict", directive=f"fix {name}", subject=name,
            scope={"query": name, "predicates": [p], "entities": [], "topics": [], "keywords": []},
            name=f"fix-{name}", params={"weak_accuracy": round(acc, 3), "explore": 1.2}, origin="auto",
        )  # fmt: skip
        agent = store.get(db, aid)
        if agent is not None:
            store.refresh_scope(db, agent)
        changelog.record(
            db, "specialists", f"fix-{name}", "spawned",
            f"Spawned a specialist for a weak spot: it gets “{name}” right only {acc:.0%} of the time ({n} answers, "
            f"overall {overall:.0%})",
            before=acc, detail={"predicate": p, "answers": n, "overall": overall},
        )  # fmt: skip
        out["spawned"].append(f"fix-{name}")
    return out


def specialists_job(ctx: JobContext) -> JobOutcome:
    res = step(ctx.db)
    return JobOutcome(done=True, value=0.1 * (len(res["spawned"]) + len(res["retired"])) + 0.01, result=res)


def planner(agent: Any) -> None:
    if agent.db.scalar("SELECT 1 FROM quiz_answers LIMIT 1"):
        agent.scheduler.ensure_recurring("self.specialists", 6 * 3600, priority=1.0)
