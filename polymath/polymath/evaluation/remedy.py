"""Learning from mistakes: every wrong self-test answer is gone back to.

``eval.remedy`` collects wrong answers from the self-quiz and from the agents' quiz tasks. For each one it:

1. **reads** about it — the Wikipedia articles of the subject, the true answer and the wrong choice
   (``wikipedia.titles``); what it reads becomes text evidence the link predictor uses;
2. **re-tests** the same question after ``learning.relearn_delay_hours``, with the same options;
3. if it is still wrong, reads again and re-tests, up to ``learning.relearn_attempts`` times — then records
   honestly that it could not learn it yet (``still_wrong``).

The hidden fact itself stays hidden: the re-test can only succeed through what it read, so a fix is real
learning, not a peek at the answer.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from polymath.core.db import Database
from polymath.core.jobs import JobContext, JobOutcome
from polymath.senses.openweb import event

BATCH = 20


def collect(db: Database) -> int:
    """Turn new wrong answers (self-quiz and agents) into remediation items. Returns how many were added."""
    now = time.time()
    added = 0
    q_cursor = int(db.kv_get("remedy_quiz_cursor") or 0)
    rows = db.query(
        "SELECT a.id, a.triple_id, a.question, a.options, a.answer, a.chosen FROM quiz_answers a "
        "WHERE a.id > ? AND a.correct = 0 ORDER BY a.id LIMIT 500",
        (q_cursor,),
    )
    for r in rows:
        try:
            options = [int(o["entity"]) for o in json.loads(r["options"])]
        except (ValueError, KeyError, TypeError):
            continue
        added += _add(
            db, "quiz", int(r["triple_id"]), str(r["question"]), options, int(r["answer"]), int(r["chosen"]), now
        )
    if rows:
        db.kv_set("remedy_quiz_cursor", int(rows[-1]["id"]))
    a_cursor = int(db.kv_get("remedy_agent_cursor") or 0)
    rows = db.query(
        "SELECT id, target, payload FROM agent_tasks WHERE id > ? AND kind = 'quiz' AND state = 'wrong' "
        "ORDER BY id LIMIT 500",
        (a_cursor,),
    )
    for r in rows:
        try:
            pl = json.loads(r["payload"])
            triple = int(str(r["target"]).split(":", 1)[1])
            options = [int(o["entity"]) for o in pl.get("options", [])]
            chosen = int(pl.get("chosen") or 0)
        except (ValueError, KeyError, TypeError, IndexError):
            continue
        t = db.one("SELECT o FROM triples WHERE id=?", (triple,))
        if t is None or not options:
            continue
        added += _add(db, "agent", triple, str(pl.get("question", "")), options, int(t["o"]), chosen, now)
    if rows:
        db.kv_set("remedy_agent_cursor", int(rows[-1]["id"]))
    return added


def _add(
    db: Database, origin: str, triple: int, question: str, options: list[int], answer: int, wrong: int, now: float
) -> int:
    t = db.one("SELECT s, p FROM triples WHERE id=?", (triple,))
    if t is None:
        return 0
    cur = db.execute(
        "INSERT OR IGNORE INTO remediation(origin, triple_id, subject, predicate, answer, wrong, options, question, "
        "state, created, next_at, updated) VALUES(?,?,?,?,?,?,?,?,'pending',?,?,?)",
        (origin, triple, int(t["s"]), int(t["p"]), answer, wrong, json.dumps(options), question, now, now, now),
    )
    return int(cur.rowcount or 0)


def _label(db: Database, entity: int) -> str:
    return str(db.scalar("SELECT label FROM entities WHERE id=?", (entity,), default="?"))


def _titles(db: Database, entities: list[int]) -> list[str]:
    out = []
    for e in entities:
        r = db.one("SELECT wiki_title, label FROM entities WHERE id=?", (e,))
        if r is not None:
            out.append(str(r["wiki_title"] or r["label"]))
    return [t for t in dict.fromkeys(out) if t and not re.fullmatch(r"[QP]\d+", t)]  # unnamed stubs cannot be read


def read_about(ctx: JobContext, item: Any) -> list[str]:
    """Queue the articles worth reading for this mistake (more of them on later attempts)."""
    entities = [int(item["subject"]), int(item["answer"])] + (
        [int(item["wrong"])] if int(item["attempts"]) >= 1 else []
    )
    titles = _titles(ctx.db, entities)
    if titles:
        ctx.scheduler.enqueue(
            "wikipedia.titles",
            {"titles": titles},
            key=f"relearn:{item['id']}:{int(item['attempts'])}",
            priority=2.6,
        )
    return titles


def remedy_job(ctx: JobContext, predictor: Any = None) -> JobOutcome:
    """Job ``eval.remedy``: collect new mistakes, read about due ones, re-test the ones whose reading is done."""
    cfg = ctx.config.learning
    added = collect(ctx.db)
    now = time.time()
    due = ctx.db.query(
        "SELECT * FROM remediation WHERE state IN ('pending', 'reading') AND next_at <= ? ORDER BY next_at LIMIT ?",
        (now, BATCH),
    )
    out = {"new": added, "reading": 0, "fixed": 0, "still_wrong": 0, "retry": 0}
    for item in due:
        if item["state"] == "pending":
            titles = read_about(ctx, item)
            ctx.db.execute(
                "UPDATE remediation SET state='reading', next_at=?, updated=?, detail=? WHERE id=?",
                (now + cfg.relearn_delay_hours * 3600, now, json.dumps({"read": titles}), item["id"]),
            )
            event(
                ctx.db,
                "relearn",
                f"reading about a mistake: {item['question']} (said "
                f"{_label(ctx.db, int(item['wrong']))}, truth {_label(ctx.db, int(item['answer']))})",
                read=titles,
            )
            out["reading"] += 1
            continue
        if predictor is None:
            from polymath.evaluation.jobs import predictor_for

            predictor = predictor_for(ctx)
        options = [int(o) for o in json.loads(item["options"])]
        pred = predictor.predict(int(item["subject"]), int(item["predicate"]), options)
        attempts = int(item["attempts"]) + 1
        if pred.best == int(item["answer"]):
            ctx.db.execute(
                "UPDATE remediation SET state='fixed', attempts=?, updated=? WHERE id=?", (attempts, now, item["id"])
            )
            event(
                ctx.db,
                "fixed",
                f"learned it: {item['question']} → {_label(ctx.db, int(item['answer']))} "
                f"(after {attempts} re-test{'s' if attempts != 1 else ''})",
                attempts=attempts,
            )
            out["fixed"] += 1
        elif attempts >= cfg.relearn_attempts:
            ctx.db.execute(
                "UPDATE remediation SET state='still_wrong', attempts=?, updated=? WHERE id=?",
                (attempts, now, item["id"]),
            )
            event(
                ctx.db,
                "relearn",
                f"still wrong after {attempts} tries: {item['question']} — will learn it as more is read",
                attempts=attempts,
            )
            out["still_wrong"] += 1
        else:
            ctx.db.execute("UPDATE remediation SET attempts=?, updated=? WHERE id=?", (attempts, now, item["id"]))
            fresh = ctx.db.one("SELECT * FROM remediation WHERE id=?", (item["id"],))
            titles = read_about(ctx, fresh if fresh is not None else item)
            ctx.db.execute(
                "UPDATE remediation SET next_at=?, detail=? WHERE id=?",
                (now + cfg.relearn_delay_hours * 3600, json.dumps({"read": titles}), item["id"]),
            )
            out["retry"] += 1
        ctx.tick()
    return JobOutcome(done=True, value=0.1 * out["fixed"] + 0.01 * len(due), result=out)


def stats(db: Database, since: float) -> dict[str, Any]:
    by = {str(r["state"]): int(r["n"]) for r in db.query("SELECT state, COUNT(*) AS n FROM remediation GROUP BY state")}
    fixed_recent = [
        {"question": str(r["question"]), "answer": _label(db, int(r["answer"])), "attempts": int(r["attempts"])}
        for r in db.query(
            "SELECT question, answer, attempts FROM remediation WHERE state='fixed' AND updated >= ? "
            "ORDER BY updated DESC LIMIT 3",
            (since,),
        )
    ]
    fixed_n = int(
        db.scalar("SELECT COUNT(*) FROM remediation WHERE state='fixed' AND updated >= ?", (since,), default=0)
    )
    return {
        "open": by.get("pending", 0) + by.get("reading", 0),
        "fixed_total": by.get("fixed", 0),
        "still_wrong_total": by.get("still_wrong", 0),
        "fixed": fixed_n,
        "fixed_examples": fixed_recent,
    }
