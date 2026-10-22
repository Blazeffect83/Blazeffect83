""" "Did you know?": facts that surprised its own model, shared in the feed, the digest and the weekly recap.

``eval.surprise`` looks at facts that arrived since its last run (at most ``EXAMINE``) and keeps two kinds:

* **against expectation.** Hide the fact and ask the link predictor which object it would have expected. When
  the predictor confidently expected something else (``EXPECT`` or more) and the true answer looked unlikely to
  it, the fact is surprising *to this agent*: "Australia's capital is Canberra, although everything else it
  knew pointed to Sydney";
* **extremes.** A number in the top or bottom ``EXTREME`` share of the same relation among things of the same
  kind: "Vatican City has the smallest population of the 190 countries it knows".

Subjects it considers important (PageRank) come first, housekeeping relations (identifiers, images, categories)
never count, and a fact is shared once. At most ``PER_RUN`` facts are shared per run, so the feed stays
interesting rather than noisy.
"""

from __future__ import annotations

import json
import math
import re
import time
from typing import Any

from polymath.core.db import Database
from polymath.core.jobs import JobContext, JobOutcome
from polymath.interface.answer import render_value
from polymath.senses.openweb import event

EXAMINE = 300
MAX_PREDICT = 40  # hidden-fact predictions per run (the expensive part)
PER_RUN = 3
EXPECT = 0.6  # the predictor's confidence in a different answer
TRUE_AT_MOST = 0.2  # …while it gave the true answer at most this much
EXTREME = 0.02
MIN_PEERS = 30
SEEN_KEEP = 2000
OPTIONS = 6
DULL = re.compile(
    r"categor|flag|coat of arms|image|logo|seal|signature|icon|map|gallery|commons|id\b|identifier|code|"
    r"number of (?:out|edits)|template|wikimedia|different from|described by|ranking|propert|subclass|"
    r"instance of|said to be",
    re.I,
)
NAMED = "AND e.label NOT GLOB 'Q[0-9]*'"


def _label(db: Database, e: int) -> str:
    return str(db.scalar("SELECT label FROM entities WHERE id=?", (e,), default="?"))


def _named(db: Database, e: int) -> bool:
    return not re.fullmatch(r"[QP]\d+", _label(db, e))


def plural(noun: str) -> str:
    words = noun.split(" ")
    w = words[-1]
    if re.search(r"[^aeiou]y$", w):
        w = w[:-1] + "ies"
    elif re.search(r"(s|x|z|ch|sh)$", w):
        w += "es"
    else:
        w += "s"
    return " ".join([*words[:-1], w])


def _amount(value: str, unit: str | None = "*") -> float | None:
    """The number of a quantity literal (only when its unit is ``unit``, unless ``unit`` is ``"*"``)."""
    try:
        v = json.loads(value)
        if not isinstance(v, dict) or "amount" not in v or (unit != "*" and v.get("unit") != unit):
            return None
        return float(v["amount"])
    except (ValueError, TypeError, KeyError):
        return None


def _unit(value: str) -> str | None:
    try:
        v = json.loads(value)
        return v.get("unit") if isinstance(v, dict) else None
    except ValueError:
        return None


def candidates(db: Database, cursor: int) -> list[Any]:
    """New, visible, sourced facts about named subjects, most important subjects first."""
    return db.query(
        "SELECT t.id, t.s, t.p, t.o, t.value, p.label AS pl, p.key AS pkey, e.pagerank FROM triples t "
        f"JOIN entities e ON e.id = t.s JOIN predicates p ON p.id = t.p WHERE t.id > ? AND t.holdout = 0 {NAMED} "
        "AND e.kind = 'item' "
        "AND t.status = 'sourced' ORDER BY t.id LIMIT ?",
        (cursor, EXAMINE),
    )


def classes(db: Database, s: int) -> list[int]:
    return [
        int(r["o"])
        for r in db.query(
            "SELECT t.o FROM triples t JOIN predicates p ON p.id = t.p WHERE t.s = ? AND p.key = 'P31' LIMIT 3", (s,)
        )
    ]


Cache = dict[tuple[int, int, str | None], list[float]]


def peers_of(db: Database, cls: int, p: int, unit: str | None, cache: Cache | None = None) -> list[float]:
    """Positive numbers of relation ``p`` in ``unit`` among subjects of class ``cls`` (cached for one run)."""
    if cache is not None and (cls, p, unit) in cache:
        return cache[(cls, p, unit)]
    out = [
        a
        for r in db.query(
            "SELECT x.value FROM triples x JOIN triples c ON c.s = x.s AND c.o = ? JOIN predicates cp "
            "ON cp.id = c.p AND cp.key = 'P31' WHERE x.p = ? AND x.o = 0 AND x.holdout = 0 LIMIT 5000",
            (cls, p),
        )
        if (a := _amount(str(r["value"]), unit)) is not None and a > 0
    ]
    if cache is not None:
        cache[(cls, p, unit)] = out
    return out


def extreme(db: Database, t: Any, cache: Cache | None = None) -> dict[str, Any] | None:
    """The fact's number against the same relation, in the same unit, among subjects of the same kind."""
    value = _amount(str(t["value"]))
    if value is None or value <= 0:
        return None
    if int(db.scalar("SELECT COUNT(*) FROM triples WHERE s = ? AND p = ?", (int(t["s"]), int(t["p"])), default=0)) > 1:
        return None  # several values (a series over the years): no single number to rank
    unit = _unit(str(t["value"]))
    for cls in classes(db, int(t["s"])):
        peers = peers_of(db, cls, int(t["p"]), unit, cache)
        if len(peers) < MIN_PEERS:
            continue
        below = sum(1 for a in peers if a < value) / len(peers)
        above = sum(1 for a in peers if a > value) / len(peers)
        most = above == 0 or below == 0
        if not most and (len(peers) < 100 or min(above, below) > EXTREME):
            return None  # "top 2%" means something only among many; otherwise only the very largest/smallest
        kind = plural(_label(db, cls)) if _named(db, cls) else "similar things"
        side = "largest" if below > above else "smallest"
        return {
            "kind": "extreme",
            "rank": side,
            "most": most,
            "peers": len(peers),
            "class": kind,
            "score": 1.0 - min(above, below) + (0.5 if most else 0.0) + 0.1 * math.log1p(len(peers)),
        }
    return None


def unexpected(db: Database, t: Any, predictor: Any, single: set[int] | None = None) -> dict[str, Any] | None:
    """Hide the fact; did the predictor confidently expect another answer? Only for single-valued relations:
    where a subject has many true answers (borders, diplomatic relations), "another answer" is no surprise."""
    from polymath.reasoning.predictions import options_for

    if not int(t["o"]) or not _named(db, int(t["o"])) or (single is not None and int(t["p"]) not in single):
        return None
    true = {int(r["o"]) for r in db.query("SELECT o FROM triples WHERE s = ? AND p = ?", (int(t["s"]), int(t["p"])))}
    options = [o for o in options_for(db, int(t["s"]), int(t["p"])) if o not in true][: OPTIONS - 1]
    if len(options) < 2:
        return None
    options.append(int(t["o"]))
    pred = predictor.predict(int(t["s"]), int(t["p"]), options, exclude=int(t["id"]))
    truth = pred.probs.get(int(t["o"]), 0.0)
    if pred.method == "none" or pred.best == int(t["o"]) or pred.confidence < EXPECT or truth > TRUE_AT_MOST:
        return None
    return {"kind": "unexpected", "expected": pred.best, "confidence": pred.confidence, "truth_p": truth,
            "score": pred.confidence - truth + 0.5}  # fmt: skip


def sentence(db: Database, t: Any, why: dict[str, Any]) -> str:
    s, p = _label(db, int(t["s"])), str(t["pl"])
    value = render_value(db, int(t["o"]), str(t["value"]))
    if why["kind"] == "extreme":
        if why["most"]:
            return f"{s} has the {why['rank']} {p} of the {why['peers']:,} {why['class']} it knows: {value}"
        share = "more" if why["rank"] == "largest" else "less"
        return f"{s}'s {p} is {value}, {share} than 98% of the {why['peers']:,} {why['class']} it knows"
    expected = _label(db, int(why["expected"]))
    return f"{s}'s {p} is {value}, although everything else it knew pointed to {expected}"


def surprise_job(ctx: JobContext, predictor: Any = None) -> JobOutcome:
    """Job ``eval.surprise``: find facts that surprised its model since the last run and share the best."""
    db = ctx.db
    cursor = int(db.kv_get("surprise_cursor") or 0)
    rows = candidates(db, cursor)
    seen = set(db.kv_get("surprise_seen") or [])
    found: list[tuple[float, Any, dict[str, Any]]] = []
    cache: Cache = {}
    asked = 0
    from polymath.reasoning.predictions import relations

    single = {r["p"] for r in relations(db, time.time())}
    # the most important subjects first: when time or the prediction budget runs out, the rest are skipped
    for t in sorted(rows, key=lambda r: -float(r["pagerank"] or 0.0)):
        if int(t["id"]) in seen or DULL.search(str(t["pl"])):
            continue
        why = extreme(db, t, cache)
        if why is None and int(t["o"]) and asked < MAX_PREDICT:
            if predictor is None:
                from polymath.evaluation.jobs import predictor_for

                predictor = predictor_for(ctx)
            asked += 1
            why = unexpected(db, t, predictor, single)
        if why is not None:
            found.append((why["score"] + 2.0 * float(t["pagerank"] or 0.0), t, why))
        ctx.tick()
        if hasattr(ctx, "should_stop") and ctx.should_stop():
            break
    found.sort(key=lambda x: -x[0])
    shared: list[int] = []
    topics: set[tuple[int, int]] = set()
    for _score, t, why in found:
        if len(shared) >= PER_RUN or (int(t["s"]), int(t["p"])) in topics:
            continue  # one fact per subject and relation
        topics.add((int(t["s"]), int(t["p"])))
        text = sentence(db, t, why)
        event(db, "didyouknow", f"Did you know? {text}.", triple=int(t["id"]), why=why["kind"])
        shared.append(int(t["id"]))
    if rows:
        db.kv_set("surprise_cursor", int(rows[-1]["id"]))
    db.kv_set("surprise_seen", (list(seen) + shared)[-SEEN_KEEP:])
    return JobOutcome(done=True, value=0.05 * len(shared), result={"examined": len(rows), "shared": len(shared)})


def recent(db: Database, since: float, limit: int = 3) -> list[str]:
    """The latest "did you know" lines since ``since`` (digest, recap)."""
    return [
        str(r["text"])
        for r in db.query(
            "SELECT text FROM events WHERE kind = 'didyouknow' AND at >= ? ORDER BY id DESC LIMIT ?", (since, limit)
        )
    ]


def planner(agent: Any) -> None:
    if agent.db.scalar("SELECT 1 FROM triples LIMIT 1"):
        agent.scheduler.ensure_recurring("eval.surprise", 3 * 3600, priority=0.9)
