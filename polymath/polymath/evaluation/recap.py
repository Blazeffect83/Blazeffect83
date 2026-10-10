"""The weekly recap: this week against last week, every Sunday morning.

``eval.recap`` compares the last seven days with the seven before:
* how much it read and learned;
* how its self-test accuracy moved;
* its predictions that came true;
* the mistakes it fixed;
* the new sites it approved;
* the agent that earned the most;
* the **most-improved topic** (the biggest jump in what it read about a topic);
* the best "did you know" facts, and how the disks held up.

Each line gives the change ("read 41,200 documents, up 12% on last week"). A topic grown from nothing says
"new this week". The feed shows it line by line, ``polymath recap`` prints it, and the dashboard has it too.
"""

from __future__ import annotations

import json
import time
from typing import Any

from polymath.body import wear
from polymath.core.config import Config
from polymath.core.db import Database
from polymath.core.jobs import JobContext, JobOutcome
from polymath.evaluation import surprise

WEEK = 7 * 86400.0
# what is counted per week: each query takes the window's start and end
COUNTS = {
    "documents": "SELECT COUNT(*) FROM documents WHERE fetched >= ? AND fetched < ? AND state != 'duplicate'",
    "facts": "SELECT COUNT(*) FROM triples WHERE created >= ? AND created < ? AND status != 'inferred'",
    "inferred": "SELECT COUNT(*) FROM triples WHERE created >= ? AND created < ? AND status = 'inferred'",
    "confirmed": "SELECT COUNT(*) FROM predictions WHERE state = 'confirmed' AND checked_at >= ? AND checked_at < ?",
    "refuted": "SELECT COUNT(*) FROM predictions WHERE state = 'refuted' AND checked_at >= ? AND checked_at < ?",
    "fixed": "SELECT COUNT(*) FROM remediation WHERE state = 'fixed' AND updated >= ? AND updated < ?",
    "sites": "SELECT COUNT(*) FROM sites WHERE status IN ('approved', 'probation') AND vetted_at >= ? "
    "AND vetted_at < ?",
}


def _count(db: Database, sql: str, lo: float, hi: float) -> int:
    return int(db.scalar(sql, (lo, hi), default=0) or 0)


def window(db: Database, lo: float, hi: float) -> dict[str, Any]:
    """What happened between ``lo`` and ``hi``."""
    quiz = db.one("SELECT SUM(n) AS n, SUM(correct) AS c FROM quizzes WHERE created >= ? AND created < ?", (lo, hi))
    best = db.one(
        "SELECT a.name AS name, SUM(r.amount) AS total FROM agent_rewards r JOIN agents a ON a.id = r.agent_id "
        "WHERE r.at >= ? AND r.at < ? GROUP BY a.id ORDER BY total DESC LIMIT 1",
        (lo, hi),
    )
    out: dict[str, Any] = {name: _count(db, sql, lo, hi) for name, sql in COUNTS.items()}
    return out | {
        "quiz_n": int(quiz["n"] or 0) if quiz else 0,
        "quiz_accuracy": (int(quiz["c"] or 0) / int(quiz["n"])) if quiz and quiz["n"] else None,
        "best_agent": {"name": str(best["name"]), "reward": round(float(best["total"]), 1)} if best else None,
    }


def topic_growth(db: Database, now: float) -> list[dict[str, Any]]:
    """Topics by how much more it read about them this week than last week."""
    rows = db.query(
        "SELECT t.name, SUM(d.fetched >= ?) AS this_week, SUM(d.fetched >= ? AND d.fetched < ?) AS last_week "
        "FROM doc_topics dt JOIN documents d ON d.id = dt.doc_id JOIN topics t ON t.id = dt.topic_id "
        "WHERE d.fetched >= ? GROUP BY t.id HAVING this_week > 0 ORDER BY this_week - last_week DESC LIMIT 5",
        (now - WEEK, now - 2 * WEEK, now - WEEK, now - 2 * WEEK),
    )
    return [{"topic": str(r["name"]), "this_week": int(r["this_week"]), "last_week": int(r["last_week"] or 0)}
            for r in rows]  # fmt: skip


def collect(db: Database, now: float | None = None, cfg: Config | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    return {
        "week": time.strftime("%G-W%V", time.localtime(now)),
        "until": now,
        "this": window(db, now - WEEK, now),
        "last": window(db, now - 2 * WEEK, now - WEEK),
        "topics": topic_growth(db, now),
        "didyouknow": surprise.recent(db, now - WEEK, 3),
        "wear": wear.sd_summary(db, cfg, now) if cfg is not None else None,
        "facts_total": int(db.scalar("SELECT COUNT(*) FROM triples", default=0)),
    }


def change(new: float, old: float) -> str:
    if old <= 0:
        return "new this week" if new > 0 else "none either week"
    pct = (new - old) / old * 100
    if abs(pct) < 1:
        return "about the same as last week"
    return f"{'up' if pct > 0 else 'down'} {abs(pct):.0f}% on last week"


def lines(d: dict[str, Any]) -> list[str]:
    a, b = d["this"], d["last"]
    out = [
        f"Read {a['documents']:,} documents ({change(a['documents'], b['documents'])}).",
        f"Learned {a['facts']:,} facts ({change(a['facts'], b['facts'])}) and worked out {a['inferred']:,} by "
        f"reasoning; it now knows {d['facts_total']:,} in all.",
    ]
    if a["quiz_accuracy"] is not None:
        text = f"Self-test accuracy: {a['quiz_accuracy']:.0%}"
        if b["quiz_accuracy"] is not None:
            delta = (a["quiz_accuracy"] - b["quiz_accuracy"]) * 100
            text += f" ({'up' if delta >= 0 else 'down'} {abs(delta):.0f} points on last week)"
        out.append(text + ".")
    if a["confirmed"] or a["refuted"]:
        out.append(f"Predictions settled: {a['confirmed']} came true, {a['refuted']} were wrong.")
    if a["fixed"]:
        out.append(f"Fixed {a['fixed']} earlier mistakes by reading up ({change(a['fixed'], b['fixed'])}).")
    if a["sites"]:
        out.append(f"Approved {a['sites']} new sites to learn from.")
    if d["topics"]:
        top = d["topics"][0]
        grew = (f"{top['this_week']:,} documents, up from {top['last_week']:,}" if top["last_week"]
                else f"{top['this_week']:,} documents, new this week")  # fmt: skip
        out.append(f"Most improved topic: {top['topic']} ({grew}).")
    if a["best_agent"]:
        out.append(f"Agent of the week: {a['best_agent']['name']} (+{a['best_agent']['reward']} verified reward).")
    out += list(d.get("didyouknow") or [])[:2]
    w = d.get("wear")
    if w:
        out.append(wear.describe(w) + ".")
    return out


def write_recap(db: Database, now: float | None = None, cfg: Config | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    data = collect(db, now, cfg)
    db.execute(
        "INSERT INTO recaps(week, created, data) VALUES(?,?,?) "
        "ON CONFLICT(week) DO UPDATE SET created = excluded.created, data = excluded.data",
        (data["week"], now, json.dumps(data, default=str)),
    )
    return data


def recap_job(ctx: JobContext) -> JobOutcome:
    d = write_recap(ctx.db, cfg=ctx.config)
    return JobOutcome(done=True, value=0.1, result={"week": d["week"], "lines": len(lines(d))})


def latest(db: Database) -> dict[str, Any] | None:
    r = db.one("SELECT week, created, data FROM recaps ORDER BY created DESC LIMIT 1")
    if r is None:
        return None
    data = json.loads(r["data"])
    return {"week": str(r["week"]), "created": float(r["created"]), "data": data, "lines": lines(data)}


def planner(agent: Any) -> None:
    from polymath.core.scheduler import local_weekly_phase

    if agent.db.scalar("SELECT 1 FROM documents LIMIT 1"):
        hour = (agent.config.learning.digest_hour + 1) % 24
        agent.scheduler.ensure_recurring("eval.recap", WEEK, priority=2.0, phase=local_weekly_phase(6, hour))
