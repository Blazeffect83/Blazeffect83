"""Reasoning jobs: rule learning, forward chaining, contradictions, source reliability."""

from __future__ import annotations

import json
import time
from typing import Any

from polymath.core.db import Database
from polymath.core.jobs import JobContext, JobOutcome
from polymath.reasoning.contradictions import detect, functional_predicates_all
from polymath.reasoning.inference import check_types, forward_chain, learn_rules
from polymath.reasoning.reliability import truth_discovery


def _log(db: Database, kind: str, started: float, result: dict[str, Any]) -> None:
    db.execute(
        "INSERT INTO reasoning_runs(kind, started, ended, result) VALUES(?,?,?,?)",
        (kind, started, time.time(), json.dumps(result, default=str)),
    )
    db.execute("DELETE FROM reasoning_runs WHERE id NOT IN (SELECT id FROM reasoning_runs ORDER BY id DESC LIMIT 500)")


def rules_job(ctx: JobContext) -> JobOutcome:
    t0 = time.time()
    res = learn_rules(ctx.db)
    _log(ctx.db, "rules", t0, res)
    return JobOutcome(done=True, value=0.2 * sum(res.values()), result=res)


def infer_job(ctx: JobContext) -> JobOutcome:
    t0 = time.time()
    res = forward_chain(ctx.db, max_new=int(ctx.job.payload.get("max_new", 20_000)), tick=ctx.tick)
    res.update(check_types(ctx.db))
    _log(ctx.db, "infer", t0, res)
    return JobOutcome(done=True, value=0.02 * res["new"], result=res)


def contradictions_job(ctx: JobContext) -> JobOutcome:
    t0 = time.time()
    res = detect(ctx.db, functional_predicates_all(ctx.db))
    _log(ctx.db, "contradictions", t0, res)
    return JobOutcome(done=True, value=0.05 * res["conflicts"], result=res)


def reliability_job(ctx: JobContext) -> JobOutcome:
    t0 = time.time()
    res = truth_discovery(ctx.db, functional=functional_predicates_all(ctx.db))
    _log(ctx.db, "reliability", t0, res)
    return JobOutcome(done=True, value=0.3, result=res)


def planner(agent: Any) -> None:
    s = agent.scheduler
    if not agent.db.scalar("SELECT 1 FROM triples LIMIT 1"):
        return
    s.ensure_recurring("reason.rules", 86400, priority=1.1)
    s.ensure_recurring("reason.reliability", 6 * 3600, priority=1.0)
    s.ensure_recurring("reason.contradictions", 3600, priority=1.3)
    s.ensure_recurring("reason.infer", 1800, priority=1.2)
