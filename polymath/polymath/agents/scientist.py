"""The scientist (v0.13.0): it forms hypotheses about its own reading, tests them on evidence it set aside, and
keeps only what holds up.

**The evidence.** A fact it read in text (an extraction pattern or an infobox) about a subject and relation that
Wikidata also covers can be checked: Wikidata agrees when the same fact carries Wikidata's provenance too. Every
such check falls in one of two halves by a fixed hash of (subject, relation): **train** and **test**. The test half
is never looked at while a hypothesis is formed.

**Hypotheses** (``hypotheses``), two families:

* ``source_reliable``: "facts from source S agree with Wikidata more (or less) often than reading in general";
* ``relation_reliable``: "what it reads in text about relation R is more (or less) reliable than reading in
  general".

One is proposed when the train half differs from the overall rate by at least ``EFFECT`` with at least ``MIN_N``
checks. It is then tested on the test half: a one-sided binomial test against the overall rate. ``supported`` when
p < ``ALPHA`` in the claimed direction; ``refuted`` when the test half points the other way; ``inconclusive``
otherwise. Decided hypotheses are tested afresh after ``RETEST_DAYS``.

**Acting on it.** Only a supported hypothesis changes anything: the facts its subject produced that nothing else
backs (a single source, not Wikidata) get surer (+``NUDGE``, at most 0.95) or less sure (× (1 − ``NUDGE``)), so a
weak source loses disputes and a strong one wins them. What changed is written in ``applied``. Job
``science.step`` (daily); ``polymath science`` lists the hypotheses.
"""

from __future__ import annotations

import math
import time
import zlib
from typing import Any

from polymath.core.jobs import JobContext, JobOutcome

MIN_N = 30
EFFECT = 0.15
ALPHA = 0.05
NUDGE = 0.1
RETEST_DAYS = 30
SAMPLE = 50_000


def half(s: int, p: int) -> str:
    """The fixed half a check belongs to (never changes between runs)."""
    return "test" if zlib.crc32(f"{s}:{p}".encode()) % 2 else "train"


def checks(db: Any) -> list[tuple[str, str, str, bool]]:
    """(source, relation key, half, agrees) for text-read facts Wikidata can judge."""
    rows = db.query(
        "SELECT t.s, t.p, pr.key AS rel, p.source, EXISTS (SELECT 1 FROM provenance w WHERE w.triple_id = t.id "
        "AND w.kind = 'wikidata') AS agrees FROM triples t JOIN provenance p ON p.triple_id = t.id "
        "JOIN predicates pr ON pr.id = t.p WHERE p.kind IN ('pattern', 'infobox') AND t.o != 0 AND t.holdout = 0 "
        "AND EXISTS (SELECT 1 FROM triples w JOIN provenance pw ON pw.triple_id = w.id WHERE w.s = t.s AND "
        "w.p = t.p AND pw.kind = 'wikidata') LIMIT ?",
        (SAMPLE,),
    )
    return [(str(r["source"]), str(r["rel"]), half(int(r["s"]), int(r["p"])), bool(r["agrees"])) for r in rows]


def binomial_p(k: int, n: int, p0: float, *, greater: bool) -> float:
    """One-sided P(X ≥ k) (or ≤ k) for X ~ Binomial(n, p0): exact up to 1000 trials, normal beyond."""
    if n == 0:
        return 1.0
    p0 = min(max(p0, 1e-9), 1 - 1e-9)
    if n <= 1000:
        rng = range(k, n + 1) if greater else range(0, k + 1)
        return min(1.0, sum(math.comb(n, i) * p0**i * (1 - p0) ** (n - i) for i in rng))
    mu, sd = n * p0, math.sqrt(n * p0 * (1 - p0))
    z = ((k - 0.5) - mu) / sd if greater else ((k + 0.5) - mu) / sd
    tail = 0.5 * math.erfc(z / math.sqrt(2))
    return tail if greater else 1 - tail


def _tally(rows: list[tuple[str, str, str, bool]], which: int) -> dict[str, dict[str, list[int]]]:
    out: dict[str, dict[str, list[int]]] = {}
    for row in rows:
        key = str(row[which])
        cell = out.setdefault(key, {"train": [0, 0], "test": [0, 0]})[row[2]]
        cell[0] += 1
        cell[1] += int(row[3])
    return out


def _apply(db: Any, family: str, subject: str, better: bool) -> str:
    cond = "p.source = ?" if family == "source_reliable" else "pr.key = ?"
    ids = [int(r["id"]) for r in db.query(
        "SELECT t.id FROM triples t JOIN provenance p ON p.triple_id = t.id JOIN predicates pr ON pr.id = t.p WHERE "
        f"{cond} AND p.kind IN ('pattern', 'infobox') AND t.status = 'sourced' AND t.n_sources = 1 AND NOT EXISTS "
        "(SELECT 1 FROM provenance w WHERE w.triple_id = t.id AND w.kind = 'wikidata')", (subject,))]  # fmt: skip
    if not ids:
        return "nothing to change yet"
    q = ",".join("?" * len(ids))
    if better:
        db.execute(f"UPDATE triples SET confidence = MIN(0.95, confidence + ?) WHERE id IN ({q})", (NUDGE, *ids))
    else:
        db.execute(f"UPDATE triples SET confidence = confidence * ? WHERE id IN ({q})", (1 - NUDGE, *ids))
    return f"{len(ids)} facts it alone backs became {'surer' if better else 'less sure'}"


def step(db: Any, *, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    rows = checks(db)
    out: dict[str, Any] = {"checks": len(rows), "proposed": 0, "supported": [], "refuted": [], "inconclusive": 0}
    train = [r for r in rows if r[2] == "train"]
    if len(train) < MIN_N:
        return {**out, "note": "too little evidence that Wikidata can judge yet"}
    base = sum(r[3] for r in train) / len(train)
    for family, which, words in (("source_reliable", 0, "facts from {} agree with Wikidata"),
                                 ("relation_reliable", 1, "what it reads about {} agrees with Wikidata")):  # fmt: skip
        for subject, cells in _tally(rows, which).items():
            (tn, tk), (sn, sk) = cells["train"], cells["test"]
            if tn < MIN_N or sn < MIN_N:
                continue
            rate = tk / tn
            if abs(rate - base) < EFFECT:
                continue
            old = db.one("SELECT state, decided FROM hypotheses WHERE family = ? AND subject = ?", (family, subject))
            if old is not None and old["decided"] and now - float(old["decided"]) < RETEST_DAYS * 86400:
                continue
            greater = rate > base
            claim = words.format(subject) + (" more" if greater else " less") + f" often than reading in general " \
                f"({rate:.0%} against {base:.0%})"  # fmt: skip
            p = binomial_p(sk, sn, base, greater=greater)
            test_rate = sk / sn
            state = "supported" if p < ALPHA else "refuted" if (test_rate > base) != greater else "inconclusive"
            applied = _apply(db, family, subject, greater) if state == "supported" else ""
            db.execute(
                "INSERT INTO hypotheses(family, subject, claim, state, train_n, train_k, test_n, test_k, p_value, "
                "applied, created, decided) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(family, subject) DO UPDATE "
                "SET claim=excluded.claim, state=excluded.state, train_n=excluded.train_n, train_k=excluded.train_k, "
                "test_n=excluded.test_n, test_k=excluded.test_k, p_value=excluded.p_value, applied=excluded.applied, "
                "decided=excluded.decided",
                (family, subject, claim, state, tn, tk, sn, sk, round(p, 6), applied, now, now),
            )
            out["proposed"] += 1
            if state == "inconclusive":
                out["inconclusive"] += 1
            else:
                out[state].append(f"{family}:{subject}")
    return out


def science_job(ctx: JobContext) -> JobOutcome:
    res = step(ctx.db)
    from polymath.senses.openweb import event

    for h in res["supported"][:3]:
        row = ctx.db.one("SELECT claim, applied FROM hypotheses WHERE family || ':' || subject = ?", (h,))
        if row is not None:
            event(ctx.db, "science", f"held up on evidence it had set aside: {row['claim']}; {row['applied']}")
    return JobOutcome(done=True, value=0.05 + 0.1 * len(res["supported"]), result=res)


def planner(agent: Any) -> None:
    agent.scheduler.ensure_recurring("science.step", 86400, priority=1.3)
