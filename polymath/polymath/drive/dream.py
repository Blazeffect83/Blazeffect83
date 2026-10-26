"""The dream cycle (v0.13.0): while the Pi is quiet at night, Polymath goes over its day and wakes up sharper.

Job ``dream.night``, daily two hours after the backup (``body.backup_hour + 2``), in phases (one or more slices):

1. **Before.** A fixed set of held-out quiz questions (facts hidden from itself, seeded by the dream's number) is
   answered and scored, without being recorded as a quiz.
2. **Replay.** The day's work is gone over again: contradictions settled, impossible facts caught, rules judged by
   how their conclusions held up, new conclusions drawn (the same reasoning jobs that run by day, run together).
3. **Consolidate.** A fact read in several independent sources becomes surer: its confidence rises to the noisy-OR
   of its evidence (1 − Π(1 − weight × 0.9)), never above 0.99.
4. **Prune.** Weak beliefs go: inferred facts below 30 % confidence, older than two weeks, that no source ever
   backed. Each is written down in ``withdrawn`` (so it is never drawn again) and removed.
5. **Plan (what if).** From its own record it builds a small model of its work: for each kind of work, the value
   it brings per CPU second (the bandit's experience, by day), the share of CPU it took in the last day, and how
   much it heats the Pi (``body.heat``). It simulates the coming day under alternatives (as now; more of each of the
   three most valuable kinds; less of the hottest) and keeps the one with the highest expected value that stays
   clear of the throttle point. The plan nudges the bandit for a day (``kv dream_plan``: a bonus of at most
   ±0.25 of a prior SD per kind of work), never orders it.
6. **After.** The same questions again: the morning report says what changed, with the score before and after.

The morning report goes to the feed (an event ``dream``), the ``dreams`` table and ``polymath dream``.
"""

from __future__ import annotations

import json
import math
import random
import time
from typing import Any

from polymath.core.jobs import JobContext, JobOutcome

QUESTIONS = 60
PRUNE_BELOW = 0.3
PRUNE_AGE_S = 14 * 86400.0
PRUNE_MAX = 2000
CONSOLIDATE_MAX = 5000
PLAN_HOURS = 24.0
SHIFT = 0.2  # an alternative moves this share of CPU
MAX_NUDGE = 0.25  # of a prior SD, either way
PHASES = ("before", "replay", "consolidate", "prune", "plan", "after")


# ------------------------------------------------------------------ measuring
def question_set(db: Any, seed: int, size: int = QUESTIONS) -> list[dict[str, Any]]:
    from polymath.evaluation.quiz import make_question, questionable_sql

    rng = random.Random(seed)
    p31 = db.scalar("SELECT id FROM predicates WHERE key='P31'")
    held = db.query(f"{questionable_sql(holdout=True)} ORDER BY t.id LIMIT ?", (size * 20,))
    rng.shuffle(held)
    out = []
    for t in held:
        q = make_question(db, t, rng, int(p31) if p31 is not None else None)
        if q is not None:
            out.append({k: q[k] for k in ("triple_id", "subject", "predicate", "answer", "options")})
        if len(out) >= size:
            break
    return out


def score(ctx: JobContext, questions: list[dict[str, Any]]) -> dict[str, Any]:
    """How many of ``questions`` it gets right now (not recorded as a quiz)."""
    if not questions:
        return {"n": 0, "correct": 0, "accuracy": None}
    from polymath.evaluation.jobs import predictor_for

    predictor = predictor_for(ctx)
    correct = 0
    for q in questions:
        correct += int(predictor.predict(q["subject"], q["predicate"], q["options"]).best == q["answer"])
        ctx.tick()
    return {"n": len(questions), "correct": correct, "accuracy": round(correct / len(questions), 4)}


def counts(db: Any) -> dict[str, int]:
    return {
        "facts": int(db.scalar("SELECT COUNT(*) FROM triples", default=0) or 0),
        "inferred": int(db.scalar("SELECT COUNT(*) FROM triples WHERE status='inferred'", default=0) or 0),
        "disputed": int(db.scalar("SELECT COUNT(*) FROM triples WHERE status='disputed'", default=0) or 0),
    }


# ------------------------------------------------------------------ phases
def replay(ctx: JobContext) -> dict[str, Any]:
    from polymath.reasoning import jobs as rjobs
    from polymath.reasoning import rule_audit, sanity

    out: dict[str, Any] = {}
    for name, handler in (("contradictions", rjobs.contradictions_job), ("sanity", sanity.sanity_job),
                          ("audit", rule_audit.audit_job), ("infer", rjobs.infer_job)):  # fmt: skip
        try:
            out[name] = handler(ctx).result
        except Exception as exc:  # one phase failing never spoils the night
            out[name] = {"error": f"{type(exc).__name__}: {exc}"}
        ctx.tick()
    return out


def consolidate(db: Any) -> dict[str, Any]:
    rows = db.query(
        "SELECT t.id, t.confidence, GROUP_CONCAT(p.weight) AS w FROM triples t JOIN provenance p ON p.triple_id = t.id "
        "WHERE t.status = 'sourced' AND t.n_sources >= 2 AND t.confidence < 0.99 GROUP BY t.id "
        "HAVING COUNT(DISTINCT p.source || ':' || p.doc_id) >= 2 LIMIT ?",
        (CONSOLIDATE_MAX,),
    )
    raised = 0
    for r in rows:
        miss = 1.0
        for w in str(r["w"] or "").split(","):
            with_w = min(1.0, max(0.0, float(w or 0)))
            miss *= 1.0 - 0.9 * with_w
        conf = round(min(0.99, 1.0 - miss), 4)
        if conf > float(r["confidence"]) + 1e-4:
            db.execute("UPDATE triples SET confidence = ?, updated = ? WHERE id = ?", (conf, time.time(), r["id"]))
            raised += 1
    return {"surer": raised}


def prune(db: Any, now: float) -> dict[str, Any]:
    rows = db.query(
        "SELECT t.id, t.s, t.p, t.o FROM triples t WHERE t.status = 'inferred' AND t.confidence < ? AND t.created < ? "
        "AND t.holdout = 0 AND t.o != 0 AND NOT EXISTS (SELECT 1 FROM provenance p WHERE p.triple_id = t.id "
        "AND p.kind != 'rule') LIMIT ?",
        (PRUNE_BELOW, now - PRUNE_AGE_S, PRUNE_MAX),
    )
    for r in rows:
        db.execute(
            "INSERT OR IGNORE INTO withdrawn(s, p, o, kind, rule_p, reason, at) VALUES(?, ?, ?, 'dream', ?, ?, ?)",
            (r["s"], r["p"], r["o"], r["p"], "pruned in a dream: weak and never backed by a source", now),
        )
        db.execute("DELETE FROM triples WHERE id = ?", (r["id"],))
    return {"pruned": len(rows)}


def world_model(db: Any, now: float) -> dict[str, dict[str, float]]:
    """Per kind of work: value per CPU second (by day), its share of the last day's CPU, and how much it heats."""
    model: dict[str, dict[str, float]] = {}
    for r in db.query("SELECT action, SUM(n * mean) / SUM(n) AS v FROM bandit_arms WHERE context LIKE '%|day|%' "
                      "AND n > 0 GROUP BY action"):  # fmt: skip
        model.setdefault(str(r["action"]), {})["value"] = float(r["v"] or 0.0)
    cpu = db.query("SELECT action, SUM(cpu_seconds) AS c FROM cycles WHERE started > ? AND action IS NOT NULL "
                   "AND action != '' GROUP BY action", (now - 86400,))  # fmt: skip
    total = sum(float(r["c"] or 0) for r in cpu) or 0.0
    for r in cpu:
        model.setdefault(str(r["action"]), {})["share"] = float(r["c"] or 0) / total if total else 0.0
    heat = (db.kv_get("heat") or {}).get("kinds") or {}
    kinds = db.query(
        "SELECT DISTINCT c.action, j.kind FROM cycles c JOIN jobs j ON j.id = c.job_id WHERE c.started > ?",
        (now - 7 * 86400,),
    )
    per: dict[str, list[float]] = {}
    for r in kinds:
        h = heat.get(str(r["kind"]))
        if h and int(h[1]) >= 3:
            per.setdefault(str(r["action"]), []).append(float(h[0]))
    for action, rates in per.items():
        model.setdefault(action, {})["heat"] = sum(rates) / len(rates)
    for m in model.values():
        m.setdefault("value", 0.0)
        m.setdefault("share", 0.0)
        m.setdefault("heat", 0.0)
    return model


def simulate(model: dict[str, dict[str, float]], mix: dict[str, float], *, temp: float | None,
             throttle: float) -> dict[str, float]:  # fmt: skip
    """Expected value of a day spent in ``mix`` (shares of CPU), and the warming it would add (°C per minute)."""
    value = sum(share * model[a]["value"] for a, share in mix.items() if a in model)
    warming = sum(share * model[a]["heat"] for a, share in mix.items() if a in model)
    penalty = 0.0
    if temp is not None and temp + 10.0 * warming >= throttle - 3.0:  # ten minutes of it would reach the throttle
        penalty = abs(value) * 0.5 + 1.0
    return {"value": round(value, 4), "warming": round(warming, 4), "score": round(value - penalty, 4)}


def _shift(mix: dict[str, float], to: str | None, away: str | None) -> dict[str, float]:
    out = dict(mix)
    if away is not None:
        moved = out.get(away, 0.0) * SHIFT
        out[away] = out.get(away, 0.0) - moved
        others = [a for a in out if a != away]
        for a in others:
            out[a] += moved / len(others) if others else 0.0
    if to is not None:
        moved = SHIFT * (1.0 - out.get(to, 0.0))
        for a in out:
            if a != to:
                out[a] *= 1.0 - SHIFT
        out[to] = out.get(to, 0.0) + moved
    total = sum(out.values()) or 1.0
    return {a: v / total for a, v in out.items()}


def plan(db: Any, now: float, *, temp: float | None, throttle: float) -> dict[str, Any]:
    model = world_model(db, now)
    current = {a: m["share"] for a, m in model.items() if m["share"] > 0}
    if len(current) < 2:
        return {"note": "not enough of its own experience yet to plan a day", "model": model}
    alternatives: dict[str, dict[str, float]] = {"as now": current}
    for a in sorted(current, key=lambda x: -model[x]["value"])[:3]:
        alternatives[f"more {a}"] = _shift(current, a, None)
    hottest = max(current, key=lambda x: model[x]["heat"])
    if model[hottest]["heat"] > 0:
        alternatives[f"less {hottest}"] = _shift(current, None, hottest)
    results = {name: simulate(model, mix, temp=temp, throttle=throttle) for name, mix in alternatives.items()}
    best = max(results, key=lambda n: (results[n]["score"], n == "as now"))
    chosen = alternatives[best]
    nudges = {}
    for a, share in chosen.items():
        base = current.get(a, 0.0)
        ratio = share / base if base > 0 else 1.0
        nudges[a] = round(max(-MAX_NUDGE, min(MAX_NUDGE, math.log(ratio))) if ratio > 0 else 0.0, 4)
    db.kv_set("dream_plan", {"plan": best, "nudges": nudges, "until": now + PLAN_HOURS * 3600, "at": now})
    return {"chosen": best, "alternatives": results, "nudges": nudges,
            "model": {a: {k: round(v, 4) for k, v in m.items()} for a, m in model.items()}}  # fmt: skip


def nudge(db: Any, action: str, now: float) -> float:
    """The dream's plan for a kind of work (``drive.bandit``): a small bonus or malus, for one day."""
    p = db.kv_get("dream_plan")
    if not isinstance(p, dict) or float(p.get("until") or 0) < now:
        return 0.0
    return float((p.get("nudges") or {}).get(action, 0.0))


def report_text(before: dict[str, Any], after: dict[str, Any], steps: dict[str, Any]) -> str:
    b, a = before.get("score") or {}, after.get("score") or {}
    lines = []
    if b.get("n"):
        delta = (a.get("accuracy") or 0) - (b.get("accuracy") or 0)
        lines.append(f"held-out questions: {b['correct']}/{b['n']} before, {a.get('correct')}/{a.get('n')} after "
                     f"({delta:+.1%})")  # fmt: skip
    else:
        lines.append("no held-out questions yet to measure itself on")
    rp = steps.get("replay") or {}
    infer = (rp.get("infer") or {}).get("new", (rp.get("infer") or {}).get("inferred", 0))
    lines.append(f"replayed the day: {infer} new conclusions, "
                 f"{(rp.get('sanity') or {}).get('caught', 0)} impossible facts caught, "
                 f"{(rp.get('audit') or {}).get('removed', 0)} conclusions of weak rules withdrawn")  # fmt: skip
    lines.append(f"{(steps.get('consolidate') or {}).get('surer', 0)} facts became surer (several sources agree); "
                 f"{(steps.get('prune') or {}).get('pruned', 0)} weak beliefs pruned")  # fmt: skip
    pl = steps.get("plan") or {}
    if pl.get("chosen"):
        lines.append(f"plan for today: {pl['chosen']} (simulated against {len(pl.get('alternatives') or {}) - 1} "
                     "other days)")  # fmt: skip
    elif pl.get("note"):
        lines.append(pl["note"])
    cb, ca = before.get("counts") or {}, after.get("counts") or {}
    if cb:
        lines.append(f"facts: {cb.get('facts', 0):,} → {ca.get('facts', 0):,}")
    return "\n".join(lines)


# ------------------------------------------------------------------ the job
def dream_job(ctx: JobContext) -> JobOutcome:
    from polymath.senses.openweb import event

    db, now = ctx.db, time.time()
    cp = dict(ctx.job.checkpoint or {})
    if not cp:
        cur = db.execute("INSERT INTO dreams(started) VALUES(?)", (now,))
        cp = {"id": int(cur.lastrowid or 0), "phase": 0, "steps": {}}
    did = int(cp["id"])
    steps: dict[str, Any] = dict(cp.get("steps") or {})
    while cp["phase"] < len(PHASES):
        phase = PHASES[cp["phase"]]
        if phase == "before":
            qs = question_set(db, seed=did)
            cp["questions"] = qs
            steps["before"] = {"score": score(ctx, qs), "counts": counts(db)}
            db.execute("UPDATE dreams SET before = ? WHERE id = ?", (json.dumps(steps["before"]), did))
        elif phase == "replay":
            steps["replay"] = replay(ctx)
        elif phase == "consolidate":
            steps["consolidate"] = consolidate(db)
        elif phase == "prune":
            steps["prune"] = prune(db, now)
        elif phase == "plan":
            obs = ctx.services.get("observation")
            temp = getattr(getattr(getattr(obs, "body", None), "vitals", None), "temp_c", None)
            steps["plan"] = plan(db, now, temp=temp, throttle=ctx.config.body.throttle_celsius)
        else:
            steps["after"] = {"score": score(ctx, cp.get("questions") or []), "counts": counts(db)}
        cp["phase"] += 1
        cp["steps"] = steps
        if cp["phase"] < len(PHASES) and ctx.should_stop():
            return JobOutcome(done=False, checkpoint=cp, result={"phase": PHASES[cp["phase"]]})
    text = report_text(steps.get("before") or {}, steps.get("after") or {}, steps)
    db.execute(
        "UPDATE dreams SET finished = ?, after = ?, steps = ?, plan = ?, report = ? WHERE id = ?",
        (time.time(), json.dumps(steps.get("after") or {}), json.dumps({k: v for k, v in steps.items()
         if k in {"replay", "consolidate", "prune"}}, default=str), json.dumps(steps.get("plan") or {}), text, did),
    )  # fmt: skip
    event(db, "dream", "while you slept: " + text.split("\n")[0], report=text)
    b = (steps.get("before") or {}).get("score") or {}
    a = (steps.get("after") or {}).get("score") or {}
    gain = (a.get("accuracy") or 0) - (b.get("accuracy") or 0) if b.get("n") else 0.0
    return JobOutcome(done=True, value=0.2 + max(0.0, gain), result={"dream": did, "report": text})


def latest(db: Any) -> dict[str, Any] | None:
    row = db.one("SELECT * FROM dreams WHERE finished IS NOT NULL ORDER BY id DESC LIMIT 1")
    return dict(row) if row is not None else None


def planner(agent: Any) -> None:
    from polymath.core import standby
    from polymath.core.scheduler import local_phase

    if standby.is_standby(agent.config):
        return
    hour = (agent.config.body.backup_hour + 2) % 24
    agent.scheduler.ensure_recurring("dream.night", 86400, priority=2.0, phase=local_phase(hour))
