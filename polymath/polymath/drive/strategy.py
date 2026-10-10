"""Reading strategy: read more of what teaches it the most, and the basics first.

``self.strategy`` (daily) measures, for the last ``WINDOW_DAYS`` days:

* **per source** (Wikipedia, Wikidata, OpenAlex, PubMed, Gutenberg, Stack Exchange, feeds, the web): what it learned
  from that source per CPU-minute spent on it. What it learned is the *good facts* (facts with that source's evidence
  that are not disputed) plus half a point per document it could link to things it knows (``LINKED_DOC``): papers,
  books and answers mostly teach through text and links rather than new facts, and must not be starved for it.
  The CPU is the source's own ingest jobs plus its share of the reading (``perception.read``) by documents. Each
  source's weight is its yield against the median of the sources that taught anything, square-rooted and kept
  between 0.5 and 2. The weights scale the
  priority of that source's jobs (``sources.plan``, feeds, the crawler), so the agent's own scheduler gives more time
  to what pays.
* **per topic**: the facts learned from a topic's documents per CPU-second of targeted reading on it, as a factor
  between 0.5 and 1.5 in curiosity (:mod:`polymath.drive.priority`). Topics that teach a lot per minute get read
  sooner.

"Basics first" lives in :func:`polymath.drive.priority.unread_entities`. Among the unread articles of a topic, the
ones that the documents it already read refer to most often come first, weighted by importance. Those are the
concepts everything else builds on.

A change of a source's weight by ``CHANGE`` or more is written to the changelog with the numbers.
"""

from __future__ import annotations

import math
import time
from typing import Any

from polymath.core.db import Database
from polymath.core.jobs import JobContext, JobOutcome
from polymath.drive import changelog

WINDOW_DAYS = 7
MIN_DOCS = 20
MIN_CPU = 30.0
CHANGE = 0.25
LINKED_DOC = 0.5  # a document linked to known things is worth half a fact
DAY = 86400.0
# job kinds whose CPU belongs to a source
INGEST = {
    "wikipedia": ("wikipedia.part", "wikipedia.titles"),
    "wikidata": ("wikidata.dump", "wikidata.properties", "wikidata.propindex", "memory.graph"),
    "openalex": ("openalex.ingest",),
    "pubmed": ("pubmed.ingest",),
    "gutenberg": ("gutenberg.books",),
    "stackexchange": ("stackexchange.ingest",),
    "feed": ("feeds.poll",),
    "web": ("crawl.step",),
}


def weight(db: Database, source: str) -> float:
    """The reading weight of a source (1 until measured)."""
    return float((db.kv_get("reading_weights") or {}).get(source, 1.0))


def topic_factor(db: Database) -> dict[int, float]:
    return {int(k): float(v) for k, v in (db.kv_get("topic_yield") or {}).items()}


def source_yield(db: Database, now: float) -> dict[str, dict[str, float]]:
    since = now - WINDOW_DAYS * DAY
    cpu = {
        str(r["action"]): float(r["c"] or 0.0)
        for r in db.query("SELECT action, SUM(cpu_seconds) AS c FROM cycles WHERE ended >= ? GROUP BY action", (since,))
    }
    docs = {
        str(r["source"]): int(r["n"])
        for r in db.query(
            "SELECT source, COUNT(*) AS n FROM documents WHERE fetched >= ? AND state != 'duplicate' GROUP BY source",
            (since,),
        )
    }
    total_docs = sum(docs.values()) or 1
    linked = {
        str(r["source"]): int(r["n"])
        for r in db.query(
            "SELECT d.source, COUNT(*) AS n FROM documents d WHERE d.fetched >= ? AND d.state != 'duplicate' "
            "AND EXISTS (SELECT 1 FROM doc_entities de WHERE de.doc_id = d.id) GROUP BY d.source",
            (since,),
        )
    }
    good: dict[str, int] = {}
    for r in db.query(
        "SELECT COALESCE(d.source, v.source) AS src, COUNT(DISTINCT v.triple_id) AS n FROM provenance v "
        "JOIN triples t ON t.id = v.triple_id LEFT JOIN documents d ON d.id = v.doc_id AND v.doc_id != 0 "
        "WHERE v.created >= ? AND v.kind != 'rule' AND t.status != 'disputed' GROUP BY src",
        (since,),
    ):
        good[str(r["src"])] = int(r["n"])
    out: dict[str, dict[str, float]] = {}
    reading = cpu.get("perception.read", 0.0) + cpu.get("perception.anchors", 0.0)
    for source, kinds in INGEST.items():
        spent = sum(cpu.get(k, 0.0) for k in kinds) + reading * docs.get(source, 0) / total_docs
        n_docs = docs.get(source, 0)
        facts = good.get(source, 0)
        if spent < MIN_CPU or (source != "wikidata" and n_docs < MIN_DOCS):
            continue
        value = facts + LINKED_DOC * linked.get(source, 0)
        out[source] = {"facts": facts, "linked": linked.get(source, 0), "cpu_min": round(spent / 60, 2),
                       "docs": n_docs, "per_cpu_min": round(value / (spent / 60), 3)}  # fmt: skip
    return out


def topic_yield(db: Database, now: float) -> dict[int, float]:
    since = now - 2 * WINDOW_DAYS * DAY
    effort = {
        int(r["topic_id"]): float(r["c"])
        for r in db.query(
            "SELECT topic_id, SUM(cpu_seconds) AS c FROM topic_effort WHERE at >= ? GROUP BY topic_id", (since,)
        )
        if float(r["c"] or 0) > 1.0
    }
    if not effort:
        return {}
    marks = ",".join("?" * len(effort))
    facts = {
        int(r["topic_id"]): int(r["n"])
        for r in db.query(
            f"SELECT dt.topic_id, COUNT(DISTINCT v.triple_id) AS n FROM doc_topics dt JOIN provenance v "
            f"ON v.doc_id = dt.doc_id WHERE dt.topic_id IN ({marks}) AND v.created >= ? GROUP BY dt.topic_id",
            [*effort, since],
        )
    }
    rates = {t: facts.get(t, 0) / c for t, c in effort.items()}
    med = sorted(rates.values())[len(rates) // 2] or max(rates.values()) or 1.0
    return {t: round(min(1.5, max(0.5, math.sqrt((r + 1e-9) / med))), 3) for t, r in rates.items()}


def step(db: Database, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    ys = source_yield(db, now)
    old = dict(db.kv_get("reading_weights") or {})
    weights: dict[str, float] = {}
    if len(ys) >= 2:
        rates = sorted(v["per_cpu_min"] for v in ys.values() if v["per_cpu_min"] > 0) or [1.0]
        med = rates[len(rates) // 2]
        weights = {s: round(min(2.0, max(0.5, math.sqrt((v["per_cpu_min"] + 1e-9) / med))), 3) for s, v in ys.items()}
    db.kv_set("reading_weights", weights)
    db.kv_set("reading_yield", ys)
    db.kv_set("topic_yield", {str(k): v for k, v in topic_yield(db, now).items()})
    moved = {s: w for s, w in weights.items() if abs(w - float(old.get(s, 1.0))) >= CHANGE}
    if moved:
        up = sorted((s for s in moved if moved[s] > float(old.get(s, 1.0))), key=lambda s: -weights[s])
        down = sorted((s for s in moved if moved[s] < float(old.get(s, 1.0))), key=lambda s: weights[s])

        def say(names: list[str]) -> str:
            shown = ", ".join(f"{s} ({rate(ys[s]['per_cpu_min'])} per CPU-minute)" for s in names[:3])
            return shown + (f" and {len(names) - 3} more" if len(names) > 3 else "")

        parts = []
        if up:
            parts.append("more " + say(up))
        if down:
            parts.append("less " + say(down))
        changelog.record(db, "reading", ", ".join(sorted(moved)), "adjusted", "Reading " + " and ".join(parts)
                         + " (what it learned: good facts, plus half a point per document linked to things it knows)",
                         old={s: old.get(s, 1.0) for s in moved}, new=moved, detail={"yield": ys})  # fmt: skip
    return {"sources": ys, "weights": weights, "changed": sorted(moved)}


def rate(v: float) -> str:
    """1,494 / 12.5 / 0.23: readable at any size."""
    return f"{v:,.0f}" if v >= 100 else f"{v:.3g}"


def strategy_job(ctx: JobContext) -> JobOutcome:
    res = step(ctx.db)
    return JobOutcome(done=True, value=0.05 + 0.05 * len(res["changed"]), result=res)


def planner(agent: Any) -> None:
    if agent.db.scalar("SELECT 1 FROM documents LIMIT 1"):
        agent.scheduler.ensure_recurring("self.strategy", DAY, priority=1.0)
