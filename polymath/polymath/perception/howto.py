"""How-to knowledge: ordered steps from Stack Exchange answers.

When an answer is read (:func:`polymath.senses.books_qa.se_document`), its first numbered list (``<ol>``) is kept as
steps (:func:`steps_from_html`); plain "1. … 2. …" lines count too (:func:`steps_from_text`). ``perception.howto``
files each answer with steps under its question in ``howto`` (with its score and whether it was accepted) and a
full-text index of the questions, so :mod:`polymath.qa.howto` can answer "How do I …?" with the steps and their
source.
"""

from __future__ import annotations

import html
import json
import re
import time
from typing import Any

from polymath.core.db import Database
from polymath.core.jobs import JobContext, JobOutcome

MIN_STEPS, MAX_STEPS, MAX_STEP_CHARS = 2, 15, 300
BATCH = 2000
_OL = re.compile(r"<ol\b[^>]*>(.*?)</ol>", re.S | re.I)
_LI = re.compile(r"<li\b[^>]*>(.*?)(?=<li\b|$)", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_NUMBERED = re.compile(r"^\s*(?:step\s*)?(\d{1,2})[.):]\s+(.+)$", re.I)


def _plain(fragment: str) -> str:
    text = _TAG.sub(" ", re.sub(r"(?is)<pre\b.*?</pre>", " [code] ", fragment))
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def steps_from_html(body: str) -> list[str]:
    """The items of the first numbered list with at least two steps."""
    for ol in _OL.finditer(body or ""):
        items = [_plain(li)[:MAX_STEP_CHARS] for li in _LI.findall(ol.group(1))]
        items = [s for s in items if len(s) >= 3]
        if len(items) >= MIN_STEPS:
            return items[:MAX_STEPS]
    return []


def steps_from_text(text: str) -> list[str]:
    """Consecutive lines numbered 1, 2, 3 … (or "Step 1: …")."""
    steps: list[str] = []
    for line in (text or "").splitlines():
        m = _NUMBERED.match(line)
        if m and int(m.group(1)) == len(steps) + 1:
            steps.append(m.group(2).strip()[:MAX_STEP_CHARS])
        elif steps and len(steps) >= MIN_STEPS:
            break
        elif m is None and line.strip() and steps:
            steps = []
    return steps[:MAX_STEPS] if len(steps) >= MIN_STEPS else []


def collect(db: Database, *, limit: int = BATCH) -> dict[str, int]:
    """File new Stack Exchange answers that carry steps (a cursor over document ids)."""
    cursor = int(db.kv_get("howto_cursor", 0) or 0)
    rows = db.query(
        "SELECT id, title, url, meta FROM documents WHERE source = 'stackexchange' AND id > ? AND state != 'duplicate' "
        "ORDER BY id LIMIT ?",
        (cursor, limit),
    )
    n = 0
    now = time.time()
    for r in rows:
        try:
            meta: dict[str, Any] = json.loads(str(r["meta"] or "{}"))
        except ValueError:
            continue
        steps = meta.get("steps") or []
        if meta.get("kind") != "answer" or len(steps) < MIN_STEPS or not str(r["title"]).strip():
            continue
        site = str(meta.get("site", ""))
        pid = str(r["url"] or "").rsplit("/", 1)[-1]
        accepted = bool(
            db.scalar(
                "SELECT 1 FROM documents WHERE source = 'stackexchange' AND external_id = ? "
                "AND json_extract(meta, '$.accepted') = ?",
                (f"{site}:{meta.get('parent')}", pid),
            )
        )
        db.execute(
            "INSERT OR REPLACE INTO howto(doc_id, question, steps, score, accepted, site, created) "
            "VALUES(?,?,?,?,?,?,?)",
            (int(r["id"]), str(r["title"]), json.dumps(steps, ensure_ascii=False), int(meta.get("score") or 0),
             int(accepted), site, now),
        )  # fmt: skip
        db.execute("DELETE FROM howto_fts WHERE doc_id = ?", (int(r["id"]),))
        db.execute("INSERT INTO howto_fts(question, doc_id) VALUES(?, ?)", (str(r["title"]), int(r["id"])))
        n += 1
    if rows:
        db.kv_set("howto_cursor", int(rows[-1]["id"]))
    return {"filed": n, "remaining": int(len(rows) == limit)}


def howto_job(ctx: JobContext) -> JobOutcome:
    total = 0
    while True:
        res = collect(ctx.db)
        total += res["filed"]
        ctx.tick()
        if not res["remaining"] or ctx.should_stop():
            break
    return JobOutcome(done=not res["remaining"], value=0.02 * total, result={"filed": total})


def planner(agent: Any) -> None:
    if agent.db.scalar("SELECT 1 FROM documents WHERE source = 'stackexchange' LIMIT 1"):
        agent.scheduler.ensure_recurring("perception.howto", 6 * 3600, priority=0.5)
