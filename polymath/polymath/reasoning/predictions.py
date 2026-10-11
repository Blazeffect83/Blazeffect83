"""Predictions: facts it guesses before reading them, checked later, so its reasoning is measured on the future.

The self-quiz hides facts it already has. This is a harder test: real gaps. Every few hours ``reason.predict``
does two things.

**Predict.** It looks for gaps: a subject that lacks a relation which almost every subject of its kind has, and
which has a single value (a country without a recorded capital, a person without a country of citizenship). The
candidate answers are the relation's most common objects plus things already linked to the subject. The link
predictor (:mod:`polymath.reasoning.link_prediction`) picks one, using everything except the missing fact
itself: related facts, similar subjects, sentences it read, and word vectors. A guess is recorded only when some
evidence spoke and the predictor is at least ``MIN_CONFIDENCE`` sure. Each gap is guessed once.

**Check.** When a fact for that subject and relation later arrives *from a source* (Wikidata, an infobox, a
sentence, never its own inference rules), the prediction is marked *confirmed* or *refuted*, with the truth it
read. After ``EXPIRE_DAYS`` an unchecked guess expires.

The scoreboard compares the confirmed share with chance (1 in the number of candidates), so "70 % right" means
something. Confirmations appear in the feed as they happen.
"""

from __future__ import annotations

import re
import time
from typing import Any

from polymath.core.db import Database
from polymath.core.jobs import JobContext, JobOutcome
from polymath.senses.openweb import event

OPTIONS = 8
PER_RUN = 40
MIN_OPTIONS = 3
SCAN = 400  # class members examined per relation per run
MIN_CONFIDENCE = 0.4
EXPIRE_DAYS = 90
PREDS_EVERY = 86400.0
MIN_SUBJECTS = 30
FUNCTIONAL = 0.85
NOT_WORTH = re.compile(r"categor|flag|coat of arms|image|logo|seal|signature|icon|locator|map|gallery|commons|"
                       r"template|portal|list of|audio|video|pronunciation|banner|outline|wikimedia|different from|"
                       r"same as|described by|follows|followed by|topic.s main", re.I)  # fmt: skip


def _label(db: Database, e: int) -> str:
    return str(db.scalar("SELECT label FROM entities WHERE id=?", (e,), default="?"))


def relations(db: Database, now: float) -> list[dict[str, int]]:
    """Single-valued, entity-valued relations with enough examples, each with its subjects' usual class (cached)."""
    cached = db.kv_get("predict_relations") or {}
    if now - float(cached.get("at", 0)) < PREDS_EVERY and "rels" in cached:
        return list(cached["rels"])
    p31 = db.scalar("SELECT id FROM predicates WHERE key='P31'")
    rels: list[dict[str, int]] = []
    if p31 is not None:
        for r in db.query(
            "SELECT p, COUNT(*) AS subjects, SUM(n = 1) AS single FROM (SELECT p, s, COUNT(*) AS n FROM triples "
            "WHERE o != 0 AND status != 'disputed' AND p != ? GROUP BY p, s) GROUP BY p HAVING subjects >= ?",
            (p31, MIN_SUBJECTS),
        ):
            if int(r["single"]) / int(r["subjects"]) < FUNCTIONAL:
                continue
            label = str(db.scalar("SELECT label FROM predicates WHERE id = ?", (int(r["p"]),), default=""))
            if NOT_WORTH.search(label):
                continue  # housekeeping relations (categories, flags, images): nothing learned by guessing them
            cls = db.one(
                "SELECT c.o AS cls, COUNT(*) AS n FROM triples t JOIN triples c ON c.s = t.s AND c.p = ? "
                "WHERE t.p = ? GROUP BY c.o ORDER BY n DESC LIMIT 1",
                (p31, int(r["p"])),
            )
            if cls is not None and int(cls["n"]) >= MIN_SUBJECTS // 2:
                rels.append({"p": int(r["p"]), "cls": int(cls["cls"]), "p31": int(p31)})
    db.kv_set("predict_relations", {"at": now, "rels": rels})
    return rels


def gaps(db: Database, rel: dict[str, int], limit: int) -> list[int]:
    """Members of the relation's class that lack it entirely (not even a hidden quiz fact) and were not guessed."""
    key = f"predict_cursor:{rel['p']}"
    cursor = int(db.kv_get(key) or 0)
    rows = db.query(
        "SELECT t.id, t.s FROM triples t JOIN entities e ON e.id = t.s WHERE t.p = ? AND t.o = ? AND t.id > ? "
        "AND e.label NOT GLOB 'Q[0-9]*' ORDER BY t.id LIMIT ?",
        (rel["p31"], rel["cls"], cursor, SCAN),
    )
    out = []
    for r in rows:
        s = int(r["s"])
        if db.scalar("SELECT 1 FROM triples WHERE s = ? AND p = ? LIMIT 1", (s, rel["p"])):
            continue
        if db.scalar("SELECT 1 FROM predictions WHERE s = ? AND p = ?", (s, rel["p"])):
            continue
        out.append(s)
        if len(out) >= limit:
            break
    db.kv_set(key, int(rows[-1]["id"]) if len(rows) == SCAN else 0)  # wrap around at the end of the class
    return out


def options_for(db: Database, s: int, p: int) -> list[int]:
    """The relation's most common objects, plus things linked to the subject that are objects of it elsewhere."""
    common = [
        int(r["o"])
        for r in db.query(
            "SELECT t.o, COUNT(*) AS n FROM triples t JOIN entities e ON e.id = t.o WHERE t.p = ? AND t.o != 0 "
            "AND e.label NOT GLOB 'Q[0-9]*' GROUP BY t.o ORDER BY n DESC LIMIT ?",
            (p, OPTIONS),
        )
    ]
    linked = []
    for r in db.query(
        "SELECT o AS x FROM triples WHERE s = ? AND o != 0 UNION SELECT s AS x FROM triples WHERE o = ? LIMIT 60",
        (s, s),
    ):
        x = int(r["x"])
        if x in common or x == s or not db.scalar("SELECT 1 FROM triples WHERE p = ? AND o = ? LIMIT 1", (p, x)):
            continue
        if re.fullmatch(r"[QP]\d+", str(db.scalar("SELECT label FROM entities WHERE id = ?", (x,), default="Q0"))):
            continue  # an unnamed entity: a guess nobody could read
        linked.append(x)
    linked = linked[: OPTIONS // 2]
    return (linked + [c for c in common if c not in linked])[:OPTIONS]


def check(db: Database, now: float) -> dict[str, int]:
    """Settle open predictions against facts read since; expire old ones."""
    out = {"confirmed": 0, "refuted": 0, "expired": 0}
    for pr in db.query("SELECT * FROM predictions WHERE state = 'open' ORDER BY made_at LIMIT 2000"):
        rows = db.query(
            "SELECT id, o FROM triples WHERE s = ? AND p = ? AND o != 0 AND status != 'inferred' AND created > ? "
            "ORDER BY confidence DESC",
            (int(pr["s"]), int(pr["p"]), float(pr["made_at"])),
        )
        if rows:
            hit = next((r for r in rows if int(r["o"]) == int(pr["o"])), None)
            state = "confirmed" if hit is not None else "refuted"
            got = hit if hit is not None else rows[0]
            db.execute(
                "UPDATE predictions SET state = ?, checked_at = ?, truth = ?, triple_id = ? WHERE id = ?",
                (state, now, int(got["o"]), int(got["id"]), int(pr["id"])),
            )
            out[state] += 1
            plabel = str(db.scalar("SELECT label FROM predicates WHERE id = ?", (int(pr["p"]),), default="?"))
            subj, guess = _label(db, int(pr["s"])), _label(db, int(pr["o"]))
            if state == "confirmed":
                text = (f"guessed {subj} → {plabel} → {guess} ({float(pr['score']):.0%} sure) before reading it; "
                        "now confirmed by what it read")  # fmt: skip
            else:
                text = f"guessed {subj} → {plabel} → {guess}, but it is {_label(db, int(got['o']))}"
            event(db, "prediction", text, status=state)
        elif float(pr["made_at"]) < now - EXPIRE_DAYS * 86400:
            db.execute("UPDATE predictions SET state = 'expired', checked_at = ? WHERE id = ?", (now, int(pr["id"])))
            out["expired"] += 1
    return out


def predict_job(ctx: JobContext, predictor: Any = None) -> JobOutcome:
    """Job ``reason.predict``: settle earlier guesses, then guess some new gaps."""
    db, now = ctx.db, time.time()
    settled = check(db, now)
    made = 0
    rels = relations(db, now)
    if rels:
        if predictor is None:
            from polymath.evaluation.jobs import predictor_for

            predictor = predictor_for(ctx)
        per_rel = max(2, PER_RUN // len(rels))
        from polymath.drive.selftune import tuned

        threshold = float(tuned(db, "predict.min_confidence", MIN_CONFIDENCE))
        for rel in rels:
            for s in gaps(db, rel, per_rel):
                options = options_for(db, s, rel["p"])
                if len(options) < MIN_OPTIONS:
                    continue
                pred = predictor.predict(s, rel["p"], options)
                if pred.method == "none" or pred.confidence < threshold:
                    continue
                ranked = sorted(pred.probs, key=lambda o: -pred.probs[o])
                db.execute(
                    "INSERT OR IGNORE INTO predictions(s, p, o, score, runner_up, n_options, method, made_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (s, rel["p"], pred.best, pred.confidence, ranked[1] if len(ranked) > 1 else None, len(options),
                     pred.method, now),
                )  # fmt: skip
                made += 1
                ctx.tick()
                if made >= PER_RUN:
                    break
            if made >= PER_RUN:
                break
    value = 0.02 * made + 0.2 * settled["confirmed"]
    return JobOutcome(done=True, value=value, result={"made": made, **settled, "relations": len(rels)})


def scoreboard(db: Database, since: float = 0.0) -> dict[str, Any]:
    """Predictions made, confirmed and refuted (since ``since``), accuracy against chance, recent hits."""
    by = {
        str(r["state"]): int(r["n"])
        for r in db.query("SELECT state, COUNT(*) AS n FROM predictions WHERE made_at >= ? GROUP BY state", (since,))
    }
    settled = by.get("confirmed", 0) + by.get("refuted", 0)
    chance = db.scalar(
        "SELECT AVG(1.0 / n_options) FROM predictions WHERE state IN ('confirmed', 'refuted') AND n_options > 0 "
        "AND made_at >= ?",
        (since,),
    )
    hits = [
        {
            "subject": _label(db, int(r["s"])),
            "relation": str(r["pl"]),
            "guess": _label(db, int(r["o"])),
            "confidence": round(float(r["score"]), 2),
        }
        for r in db.query(
            "SELECT pr.s, pr.o, pr.score, p.label AS pl FROM predictions pr JOIN predicates p ON p.id = pr.p "
            "WHERE pr.state = 'confirmed' AND pr.made_at >= ? ORDER BY pr.checked_at DESC LIMIT 5",
            (since,),
        )
    ]
    return {
        "made": sum(by.values()),
        "open": by.get("open", 0),
        "confirmed": by.get("confirmed", 0),
        "refuted": by.get("refuted", 0),
        "expired": by.get("expired", 0),
        "accuracy": None if settled == 0 else round(by.get("confirmed", 0) / settled, 3),
        "chance": None if chance is None else round(float(chance), 3),
        "recent_hits": hits,
    }


def describe(sb: dict[str, Any]) -> str:
    """One plain line for the feed, digest, recap and CLI."""
    text = f"Predicted {sb['made']:,} facts before reading them: {sb['confirmed']:,} confirmed, {sb['refuted']:,} wrong"
    if sb.get("accuracy") is not None:
        text += f" ({sb['accuracy']:.0%} right"
        text += f"; guessing would be {sb['chance']:.0%})" if sb.get("chance") is not None else ")"
    return text + f", {sb['open']:,} still open."


def planner(agent: Any) -> None:
    if agent.db.scalar("SELECT 1 FROM triples LIMIT 1"):
        agent.scheduler.ensure_recurring("reason.predict", 2 * 3600, priority=1.0)
