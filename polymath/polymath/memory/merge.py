"""Duplicate entries folded into one (``memory.merge``).

An article it read before Wikidata named it becomes a placeholder (``wiki:Title``). Usually the Wikidata item takes the
placeholder over when it arrives, but not when the two disagree on the title: the article was read under a redirect
("NYC"), or Wikidata links a different spelling. Facts then split between two entries for one thing. This job finds
such pairs and merges them, with evidence:

* **redirect**: the placeholder's title redirects to the item's article, or the item's article redirects to it;
* **unique name**: exactly one Wikidata item has the placeholder's title as its name or article title (an alias is
  not enough: Neptune's alias "Poseidon" is a different god), and none of the dates both know (birth, death,
  founding) disagree.

Two Wikidata items are never merged (Wikidata says they are different things). What points at the dropped entry
(facts both ways, aliases, mentions in documents, what kinds can do) moves to the kept one, and every merge is logged
in ``entity_merges`` and the changelog.
"""

from __future__ import annotations

import time
from typing import Any

from polymath.core.db import Database
from polymath.core.jobs import JobContext, JobOutcome
from polymath.drive import changelog
from polymath.memory.graph import KnowledgeGraph, norm_alias

BATCH = 2000
DATE_KEYS = ("P569", "P570", "P571")


def _item_by_title(db: Database, title: str) -> int | None:
    r = db.one(
        "SELECT id FROM entities WHERE wiki_title = ? AND key GLOB 'Q[0-9]*' AND kind = 'item' LIMIT 1", (title,)
    )
    return int(r["id"]) if r else None


def candidate(db: Database, eid: int, title: str) -> tuple[int, str] | None:
    """The Wikidata item a placeholder duplicates, and why; None when the evidence is not clear."""
    target = db.scalar("SELECT target FROM wiki_redirects WHERE lang = 'en' AND title = ?", (title,))
    if target and (keep := _item_by_title(db, str(target))) is not None:
        return keep, f"“{title}” redirects to “{target}”"
    for r in db.query("SELECT title FROM wiki_redirects WHERE lang = 'en' AND target = ? LIMIT 20", (title,)):
        if (keep := _item_by_title(db, str(r["title"]))) is not None:
            return keep, f"“{r['title']}” redirects to “{title}”"
    items = {
        int(r["entity_id"])
        for r in db.query(
            "SELECT a.entity_id FROM aliases a JOIN entities e ON e.id = a.entity_id WHERE a.alias = ? "
            "AND a.source IN ('label', 'title') AND e.key GLOB 'Q[0-9]*' AND e.kind = 'item' "
            "LIMIT 3",
            (norm_alias(title),),
        )
    }
    if len(items) == 1:
        keep = items.pop()
        if not conflicting(db, keep, eid):
            return keep, f"the only Wikidata item named “{title}”"
    return None


def conflicting(db: Database, a: int, b: int) -> bool:
    """The two disagree on a date both know."""
    for key in DATE_KEYS:
        va = {
            str(r["value"])[:15]
            for r in db.query(
                "SELECT t.value FROM triples t JOIN predicates p ON p.id = t.p WHERE t.s = ? AND p.key = ?", (a, key)
            )
        }
        vb = {
            str(r["value"])[:15]
            for r in db.query(
                "SELECT t.value FROM triples t JOIN predicates p ON p.id = t.p WHERE t.s = ? AND p.key = ?", (b, key)
            )
        }
        if va and vb and not va & vb:
            return True
    return False


def merge(db: Database, graph: KnowledgeGraph, keep: int, drop: int, reason: str) -> None:
    key = str(db.scalar("SELECT key FROM entities WHERE id = ?", (drop,), default=""))
    for table in ("doc_entities", "category_props"):  # what would otherwise be deleted with the dropped entry
        db.execute(f"UPDATE OR IGNORE {table} SET entity_id = ? WHERE entity_id = ?", (keep, drop))
    graph.merge(keep, drop)
    db.execute("DELETE FROM triples WHERE s = ? AND o = ?", (keep, keep))  # a fact linking the two is now a loop
    db.execute(
        "INSERT OR REPLACE INTO entity_merges(dropped_key, kept_id, reason, at) VALUES(?,?,?,?)",
        (key, keep, reason, time.time()),
    )


def run(db: Database, *, limit: int = BATCH) -> dict[str, Any]:
    graph = KnowledgeGraph(db)
    cursor = int(db.kv_get("merge_cursor", 0) or 0)
    rows = db.query(
        "SELECT id, label, wiki_title FROM entities WHERE key GLOB 'wiki:*' AND id > ? ORDER BY id LIMIT ?",
        (cursor, limit),
    )
    examples: list[str] = []
    merged = 0
    for r in rows:
        title = str(r["wiki_title"] or r["label"])
        got = candidate(db, int(r["id"]), title)
        if got is None:
            continue
        keep, reason = got
        kept_label = str(db.scalar("SELECT label FROM entities WHERE id = ?", (keep,), default="?"))
        merge(db, graph, keep, int(r["id"]), reason)
        merged += 1
        if len(examples) < 3:
            examples.append(f"“{title}” into {kept_label} ({reason})")
    db.kv_set("merge_cursor", int(rows[-1]["id"]) if len(rows) == limit else 0)
    if merged:
        changelog.record(
            db, "memory", "duplicates", "merged",
            f"Merged {merged} duplicate entr{'ies' if merged != 1 else 'y'} into one, e.g. {examples[0]}",
            detail={"examples": examples, "merged": merged},
        )  # fmt: skip
    return {"checked": len(rows), "merged": merged, "examples": examples, "remaining": int(len(rows) == limit)}


def merge_job(ctx: JobContext) -> JobOutcome:
    res = run(ctx.db)
    ctx.tick()
    return JobOutcome(done=not res["remaining"], value=0.02 + 0.05 * res["merged"] ** 0.5, result=res)


def planner(agent: Any) -> None:
    if agent.db.scalar("SELECT 1 FROM entities WHERE key GLOB 'wiki:*' LIMIT 1"):
        agent.scheduler.ensure_recurring("memory.merge", 12 * 3600, priority=0.7)
