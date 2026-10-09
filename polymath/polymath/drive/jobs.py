"""Drive jobs: importance (PageRank), topic priorities, and pursuing the most valuable gaps."""

from __future__ import annotations

import time
from typing import Any

from polymath.core.jobs import JobContext, JobOutcome
from polymath.drive.pagerank import entity_pagerank
from polymath.drive.priority import compute_priorities, top_topics, unread_entities

PURSUE_TOPICS = 3
TITLES_PER_TOPIC = 25


def pagerank_job(ctx: JobContext) -> JobOutcome:
    res = entity_pagerank(ctx.db)
    return JobOutcome(done=True, value=0.3, result=res)


def priorities_job(ctx: JobContext) -> JobOutcome:
    """Job ``drive.priorities``: rank topics, then queue targeted reading for the best ones."""
    res = compute_priorities(ctx.db)
    hour = int(time.time() // 3600)
    pursued = []
    for t in top_topics(ctx.db, PURSUE_TOPICS):
        titles = [title for _e, title in unread_entities(ctx.db, int(t["topic_id"]), TITLES_PER_TOPIC)]
        if not titles:
            continue
        indexed = int(
            ctx.db.scalar(f"SELECT COUNT(*) FROM wiki_index WHERE title IN ({','.join('?' * len(titles))})", titles, 0)
        )
        if not indexed:
            continue
        _jid, created = ctx.scheduler.enqueue(
            "wikipedia.titles",
            {"titles": titles, "topic_id": int(t["topic_id"]), "lang": "en"},
            key=f"pursue:{t['topic_id']}:{hour}",
            priority=1.8,
        )
        if created:
            pursued.append({"topic": t["name"], "titles": len(titles), "indexed": indexed})
    res["pursued"] = pursued
    return JobOutcome(done=True, value=0.2 + 0.3 * len(pursued), result=res)


def planner(agent: Any) -> None:
    s = agent.scheduler
    if not agent.db.scalar("SELECT 1 FROM entities LIMIT 1"):
        return
    s.ensure_recurring("drive.pagerank", 6 * 3600, priority=1.0)
    if agent.db.scalar("SELECT 1 FROM topics WHERE n_total >= 3 LIMIT 1"):
        s.ensure_recurring("drive.priorities", 3600, priority=1.6)
