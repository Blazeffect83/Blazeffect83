"""Evaluation jobs: hold-out marking, quizzes, nightly report."""

from __future__ import annotations

import time
from typing import Any

from polymath.core.jobs import JobContext, JobOutcome
from polymath.core.scheduler import local_phase
from polymath.evaluation.quiz import mark_holdout, run_quiz
from polymath.evaluation.report import write_report
from polymath.reasoning.link_prediction import LinkPredictor, WordVectors, calibrate

CALIBRATE_EVERY = 24 * 3600.0


def holdout_job(ctx: JobContext) -> JobOutcome:
    n = mark_holdout(ctx.db, ctx.config.learning.holdout_fraction)
    return JobOutcome(done=True, value=0.01 * n, result={"held_out": n})


def predictor_for(ctx: JobContext) -> LinkPredictor:
    from polymath.perception.embed_jobs import load_model

    loaded = load_model(ctx)
    return LinkPredictor(ctx.db, vectors=WordVectors.from_model(loaded[0]) if loaded else None)


def quiz_job(ctx: JobContext) -> JobOutcome:
    """Job ``eval.quiz``: re-learn the evidence weights (daily, on visible facts), then quiz on held-out ones."""
    predictor = predictor_for(ctx)
    fit = ctx.db.kv_get("link_weights") or {}
    if time.time() - float(fit.get("at", 0)) > CALIBRATE_EVERY:
        fit = calibrate(ctx.db, predictor, size=ctx.config.learning.quiz_size * 2, tick=ctx.tick)
        fit["at"] = time.time()
        ctx.db.kv_set("link_weights", fit)
    res = run_quiz(ctx.db, predictor, size=ctx.config.learning.quiz_size, tick=ctx.tick)
    res["calibration"] = {k: v for k, v in fit.items() if k != "at"}
    ctx.db.kv_set("last_quiz", res)
    return JobOutcome(done=True, value=float(res.get("n", 0)) * 0.02, result=res)


def digest_job(ctx: JobContext) -> JobOutcome:
    from polymath.evaluation.digest import digest_job as run

    return run(ctx)


def remedy_job(ctx: JobContext) -> JobOutcome:
    from polymath.evaluation.remedy import remedy_job as run

    return run(ctx)


def report_job(ctx: JobContext) -> JobOutcome:
    res = write_report(ctx.db, ctx.config.paths.report_dir)
    return JobOutcome(done=True, value=0.2, result=res)


def planner(agent: Any) -> None:
    s = agent.scheduler
    if not agent.db.scalar("SELECT 1 FROM triples LIMIT 1"):
        return
    s.ensure_recurring("eval.holdout", 1800, priority=3.0)  # before reasoning can see new facts
    s.ensure_recurring("eval.quiz", 6 * 3600, priority=1.0)
    hour = agent.config.body.backup_hour
    s.ensure_recurring("eval.report", 86400, priority=3.0, phase=local_phase((hour + 1) % 24))
    s.ensure_recurring("eval.digest", 86400, priority=2.0, phase=local_phase(agent.config.learning.digest_hour))
    s.ensure_recurring("eval.remedy", 1800, priority=1.5)
