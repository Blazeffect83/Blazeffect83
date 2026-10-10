"""Learned phrasing: it writes the way the texts it read are written.

Relation learning (:mod:`polymath.perception.relations`) finds the words that sit between two things when a sentence
states a known fact: "Dürer *was born in* Nuremberg", "the film *was directed by* Sidney Lumet". ``perception.phrasing``
(daily) keeps, for each relation, the best of those phrasings that reads as a clean sentence:

* subject first;
* seen in at least ``MIN_SUPPORT`` sentences, with Snowball confidence ≥ ``MIN_CONFIDENCE``;
* one to five words, with a verb ("was …", "is …", or a participle such as "directed", "born");
* no numbers, articles, pronouns or punctuation in between, and nothing archaic ("lieth").

"Tell me about" (:mod:`polymath.interface.tell`) then writes "Lumet's film … was directed by …" instead of the plain
"Its director is …" for relations that have no hand-written sentence. Learned phrasings are kept even when the
pattern table is rebuilt, and each newly learned phrasing is announced in the changelog.
"""

from __future__ import annotations

import re
import time
from typing import Any

from polymath.core.db import Database
from polymath.core.jobs import JobContext, JobOutcome
from polymath.drive import changelog

MIN_SUPPORT = 3
MIN_CONFIDENCE = 0.7
MAX_WORDS = 5
AUX = {"is", "was", "are", "were", "has", "had", "have", "became", "becomes", "remains", "remained"}
PARTICIPLE = {"born", "known", "made", "built", "written", "drawn", "grown", "held", "led", "won", "taught", "sung",
              "set", "found", "located", "situated", "based", "buried", "named", "called"}  # fmt: skip
PRONOUNS = {"he", "she", "his", "her", "hers", "they", "their", "them", "it", "its", "him", "we", "our", "i", "you",
            "my", "your", "this", "that", "these", "those", "which", "who", "whom", "whose"}  # fmt: skip
WORD = re.compile(r"[a-z]+")


def readable(middle: str) -> bool:
    words = middle.split()
    if not 1 <= len(words) <= MAX_WORDS or not all(WORD.fullmatch(w) for w in words):
        return False  # numbers, articles (<det>), punctuation
    if any(w in PRONOUNS or w.endswith("eth") or (w.endswith("est") and w not in {"west", "best"}) for w in words):
        return False
    verb = words[0] in AUX or any((w.endswith("ed") and len(w) > 3) or w in PARTICIPLE for w in words)
    return verb and words[-1] not in AUX


def learn(db: Database, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    best: dict[int, tuple[str, int, float]] = {}
    for r in db.query(
        "SELECT predicate_id, middle, positive, confidence FROM patterns WHERE order_flag = 0 AND positive >= ? "
        "AND confidence >= ? ORDER BY positive DESC, confidence DESC",
        (MIN_SUPPORT, MIN_CONFIDENCE),
    ):
        p, middle = int(r["predicate_id"]), str(r["middle"])
        if p not in best and readable(middle):
            best[p] = (middle, int(r["positive"]), float(r["confidence"]))
    known = {int(r["predicate_id"]): (str(r["middle"]), int(r["support"])) for r in db.query("SELECT * FROM phrasings")}
    learned: list[str] = []
    for p, (middle, support, conf) in best.items():
        old = known.get(p)
        if old is not None and (old[0] == middle or old[1] >= support):
            db.execute("UPDATE phrasings SET updated = ? WHERE predicate_id = ?", (now, p))
            continue
        db.execute(
            "INSERT INTO phrasings(predicate_id, middle, support, confidence, learned_at, updated) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(predicate_id) DO UPDATE SET middle = excluded.middle, support = excluded.support, "
            "confidence = excluded.confidence, updated = excluded.updated",
            (p, middle, support, round(conf, 3), now, now),
        )
        label = str(db.scalar("SELECT label FROM predicates WHERE id = ?", (p,), default="?"))
        learned.append(f"“X {middle} Y” for {label}")
    if learned:
        more = f" and {len(learned) - 3} more" if len(learned) > 3 else ""
        changelog.record(
            db, "writing", "phrasings", "learned",
            f"Learned to write {len(learned)} new kind{'s' if len(learned) != 1 else ''} of sentence from what it "
            f"read: {', '.join(learned[:3])}{more}",
            detail={"learned": learned},
        )  # fmt: skip
    return {"candidates": len(best), "learned": len(learned), "total": len(known) + len(learned)}


def phrasings(db: Database) -> dict[int, str]:
    return {int(r["predicate_id"]): str(r["middle"]) for r in db.query("SELECT predicate_id, middle FROM phrasings")}


def phrasing_job(ctx: JobContext) -> JobOutcome:
    res = learn(ctx.db)
    return JobOutcome(done=True, value=0.02 + 0.05 * res["learned"], result=res)


def planner(agent: Any) -> None:
    if agent.db.scalar("SELECT 1 FROM patterns LIMIT 1"):
        agent.scheduler.ensure_recurring("perception.phrasing", 86400, priority=0.9)
