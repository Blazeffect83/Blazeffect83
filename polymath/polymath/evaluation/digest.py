"""The daily digest: "what I learned today", in plain words.

``eval.digest`` runs once a day (``learning.digest_hour``, local time) and stores a digest covering the time
since the previous one (or the last 24 hours): what it read, how much it learned, the most notable new facts,
what it got wrong and what it fixed (learning from mistakes), how its self-test accuracy is trending, which new
sites passed or failed vetting, and what it wants to learn next. The live feed, the dashboard and
``polymath digest`` show it.
"""

from __future__ import annotations

import json
import time
from typing import Any

from polymath.body import wear
from polymath.core.config import Config
from polymath.core.db import Database
from polymath.core.jobs import JobContext, JobOutcome
from polymath.evaluation import remedy, surprise
from polymath.interface.answer import render_value
from polymath.reasoning import predictions

HIGHLIGHTS = 5


def _label(db: Database, e: int) -> str:
    return str(db.scalar("SELECT label FROM entities WHERE id=?", (e,), default="?"))


def totals(db: Database) -> dict[str, int]:
    return {
        "documents": int(db.scalar("SELECT COUNT(*) FROM documents WHERE state != 'duplicate'", default=0)),
        "entities": int(db.scalar("SELECT COUNT(*) FROM entities WHERE kind = 'item'", default=0)),
        "facts": int(db.scalar("SELECT COUNT(*) FROM triples", default=0)),
    }


def quiz_trend(db: Database, now: float, days: int = 7) -> dict[str, Any]:
    """Accuracy over the last day versus the days before (what 'getting better' means here)."""
    rows = db.query(
        "SELECT created, n, correct FROM quizzes WHERE created >= ? ORDER BY created", (now - (days + 1) * 86400,)
    )
    today = [r for r in rows if r["created"] >= now - 86400]
    before = [r for r in rows if r["created"] < now - 86400]

    def acc(rs: list[Any]) -> float | None:
        n = sum(int(r["n"]) for r in rs)
        return None if n == 0 else sum(int(r["correct"]) for r in rs) / n

    return {"today": acc(today), "before": acc(before), "days": days}


def collect(db: Database, since: float, now: float | None = None, cfg: Config | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    docs = {
        str(r["source"]): int(r["n"])
        for r in db.query(
            "SELECT source, COUNT(*) AS n FROM documents WHERE fetched >= ? AND state != 'duplicate' GROUP BY source "
            "ORDER BY n DESC",
            (since,),
        )
    }
    facts = {
        str(r["status"]): int(r["n"])
        for r in db.query("SELECT status, COUNT(*) AS n FROM triples WHERE created >= ? GROUP BY status", (since,))
    }
    topics = [
        str(r["name"])
        for r in db.query(
            "SELECT t.name, COUNT(*) AS n FROM doc_topics dt JOIN documents d ON d.id = dt.doc_id JOIN topics t "
            "ON t.id = dt.topic_id WHERE d.fetched >= ? GROUP BY t.id ORDER BY n DESC LIMIT 5",
            (since,),
        )
    ]
    highlights = []
    seen: set[int] = set()
    for r in db.query(
        "SELECT t.s, t.o, t.value, t.status, p.label AS pl FROM triples t JOIN entities e ON e.id = t.s "
        "JOIN predicates p ON p.id = t.p WHERE t.created >= ? AND t.holdout = 0 "
        "AND t.status IN ('sourced', 'inferred') "
        "AND e.kind = 'item' AND e.label NOT GLOB 'Q[0-9]*' ORDER BY e.pagerank DESC, t.id DESC LIMIT 200",
        (since,),
    ):
        if int(r["s"]) in seen:
            continue
        obj = render_value(db, int(r["o"]), str(r["value"]))
        if "not read yet" in obj:
            continue
        seen.add(int(r["s"]))
        highlights.append(
            {"s": _label(db, int(r["s"])), "p": str(r["pl"]), "o": obj, "inferred": r["status"] == "inferred"}
        )
        if len(highlights) >= HIGHLIGHTS:
            break
    wrong = [
        {"question": str(r["question"]), "chosen": str(r["chosen_label"]), "truth": str(r["truth_label"])}
        for r in db.query(
            "SELECT a.question, ec.label AS chosen_label, et.label AS truth_label FROM quiz_answers a "
            "JOIN quizzes z ON z.id = a.quiz_id JOIN entities ec ON ec.id = a.chosen "
            "JOIN entities et ON et.id = a.answer "
            "WHERE z.created >= ? AND a.correct = 0 ORDER BY a.id DESC LIMIT 3",
            (since,),
        )
    ]
    wrong_n = int(
        db.scalar(
            "SELECT COUNT(*) FROM quiz_answers a JOIN quizzes z ON z.id = a.quiz_id "
            "WHERE z.created >= ? AND a.correct = 0",
            (since,),
            default=0,
        )
    )
    sites = {
        str(r["status"]): int(r["n"])
        for r in db.query(
            "SELECT status, COUNT(*) AS n FROM sites WHERE vetted_at >= ? AND status != 'retry' GROUP BY status",
            (since,),
        )
    }
    site_examples = [
        {"site": str(r["site"]), "status": str(r["status"]), "reason": str(r["reason"])}
        for r in db.query(
            "SELECT site, status, reason FROM sites WHERE vetted_at >= ? AND status != 'retry' "
            "ORDER BY citations DESC LIMIT 4",
            (since,),
        )
    ]
    settled = disputed = 0
    for r in db.query("SELECT result FROM reasoning_runs WHERE kind = 'contradictions' AND ended >= ?", (since,)):
        try:
            res = json.loads(r["result"])
        except ValueError:
            continue
        settled += int(res.get("settled", 0))
        disputed += int(res.get("disputed", 0))
    rewards = db.one(
        "SELECT a.name AS name, SUM(r.amount) AS total FROM agent_rewards r JOIN agents a ON a.id = r.agent_id "
        "WHERE r.at >= ? GROUP BY a.id ORDER BY total DESC LIMIT 1",
        (since,),
    )
    curious = [
        str(r["name"])
        for r in db.query(
            "SELECT t.name FROM topic_priority tp JOIN topics t ON t.id = tp.topic_id WHERE tp.priority > 0 "
            "ORDER BY tp.priority DESC LIMIT 3"
        )
    ]
    return {
        "since": since,
        "until": now,
        "documents": docs,
        "facts": facts,
        "totals": totals(db),
        "topics": topics,
        "highlights": highlights,
        "quiz": quiz_trend(db, now),
        "mistakes": {"count": wrong_n, "examples": wrong},
        "relearning": remedy.stats(db, since),
        "sites": {"counts": sites, "examples": site_examples},
        "disputes": {"settled": settled, "disputed": disputed},
        "best_agent": {"name": rewards["name"], "reward": round(float(rewards["total"]), 1)} if rewards else None,
        "curious": curious,
        "wear": wear.sd_summary(db, cfg, now) if cfg is not None else None,
        "didyouknow": surprise.recent(db, since, 2),
        "predictions": predictions.scoreboard(db)
        | {
            "confirmed_today": int(
                db.scalar(
                    "SELECT COUNT(*) FROM predictions WHERE state = 'confirmed' AND checked_at >= ?",
                    (since,),
                    default=0,
                )
            )
        },
    }


def lines(d: dict[str, Any]) -> list[str]:
    """The digest as short plain-text lines (feed, dashboard, CLI)."""
    out = []
    docs = d.get("documents") or {}
    n_docs = sum(docs.values())
    parts = ", ".join(f"{n:,} {src}" for src, n in list(docs.items())[:4])
    out.append(f"Read {n_docs:,} documents" + (f" ({parts})" if parts else "") + ".")
    f = d.get("facts") or {}
    learned = f.get("sourced", 0) + f.get("disputed", 0)
    out.append(f"Learned {learned:,} new facts and worked out {f.get('inferred', 0):,} more by reasoning.")
    if d.get("topics"):
        out.append("Mostly about: " + ", ".join(d["topics"]) + ".")
    for h in d.get("highlights") or []:
        out.append(f"{'Worked out' if h['inferred'] else 'New'}: {h['s']} → {h['p']} → {h['o']}")
    q = d.get("quiz") or {}
    if q.get("today") is not None:
        trend = ""
        if q.get("before") is not None:
            delta = (q["today"] - q["before"]) * 100
            trend = f" ({'up' if delta >= 0 else 'down'} {abs(delta):.0f} points from the {q['days']} days before)"
        out.append(f"Self-test accuracy: {q['today']:.0%}{trend}.")
    m = d.get("mistakes") or {}
    rl = d.get("relearning") or {}
    if m.get("count") or rl.get("fixed"):
        out.append(
            f"Got {m.get('count', 0)} self-test questions wrong; fixed {rl.get('fixed', 0)} earlier mistakes by "
            f"reading up ({rl.get('open', 0)} still being worked on)."
        )
    for x in rl.get("fixed_examples") or []:
        out.append(f"Now knows: {x['question']} → {x['answer']}")
    s = (d.get("sites") or {}).get("counts") or {}
    if s:
        out.append("New sites vetted: " + ", ".join(f"{n} {k}" for k, n in sorted(s.items())) + ".")
    ds = d.get("disputes") or {}
    if ds.get("settled") or ds.get("disputed"):
        out.append(
            f"Checked conflicting claims: {ds.get('settled', 0)} settled, {ds.get('disputed', 0)} still disputed."
        )
    if d.get("best_agent"):
        out.append(f"Best agent today: {d['best_agent']['name']} (+{d['best_agent']['reward']} verified reward).")
    if d.get("curious"):
        out.append("Wants to learn next: " + ", ".join(d["curious"]) + ".")
    out += list(d.get("didyouknow") or [])
    pr = d.get("predictions") or {}
    if pr.get("made"):
        today = f" {pr['confirmed_today']} came true today." if pr.get("confirmed_today") else ""
        out.append(predictions.describe(pr) + today)
    w = d.get("wear")
    if w and w.get("measured_days", 0) >= 0.9:
        verdict = {"ok": "within budget", "high": "over budget", "critical": "far over budget"}.get(
            str(w.get("status")), ""
        )
        out.append(wear.describe(w) + (f" ({verdict})." if verdict else "."))
    return out


def to_markdown(d: dict[str, Any], day: str) -> str:
    return f"# What I learned — {day}\n\n" + "\n".join(f"- {line}" for line in lines(d)) + "\n"


def write_digest(db: Database, now: float | None = None, cfg: Config | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    last = db.scalar("SELECT MAX(created) FROM digests")
    since = float(last) if last else now - 86400
    data = collect(db, since, now, cfg)
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    db.execute(
        "INSERT INTO digests(day, created, data) VALUES(?,?,?) "
        "ON CONFLICT(day) DO UPDATE SET created=excluded.created, data=excluded.data",
        (day, now, json.dumps(data, default=str)),
    )
    return {"day": day, **data}


def digest_job(ctx: JobContext) -> JobOutcome:
    d = write_digest(ctx.db, cfg=ctx.config)
    return JobOutcome(done=True, value=0.1, result={"day": d["day"], "lines": len(lines(d))})


def latest(db: Database) -> dict[str, Any] | None:
    r = db.one("SELECT day, created, data FROM digests ORDER BY created DESC LIMIT 1")
    if r is None:
        return None
    data = json.loads(r["data"])
    return {"day": str(r["day"]), "created": float(r["created"]), "data": data, "lines": lines(data)}
