"""User-requested learning (``polymath learn <topic|url>``) turned into concrete agent work."""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlsplit

from polymath.core.jobs import JobContext, JobOutcome
from polymath.interface.answer import Answerer
from polymath.memory.graph import norm_alias
from polymath.senses.crawler import Crawler, host_of, normalize_url

INTEREST_DAYS = 14
INTEREST_BOOST = 3.0


def interests(db: Any) -> dict[int, float]:
    """topic_id → expiry timestamp for topics the user asked about (expired ones dropped)."""
    raw = db.kv_get("user_interests", {}) or {}
    now = time.time()
    return {int(k): float(v) for k, v in raw.items() if float(v) > now}


def learn_job(ctx: JobContext) -> JobOutcome:
    """Job ``drive.learn``: payload {query}. URLs are crawled; topics are read about and prioritised."""
    query = str(ctx.job.payload.get("query", "")).strip()
    actions: list[str] = []
    if not query:
        return JobOutcome(done=True, result={"actions": ["nothing to learn: empty query"]})
    if urlsplit(query).scheme in {"http", "https"}:
        crawler: Crawler = ctx.services["crawler"]
        url = normalize_url(query)
        if url is None:
            return JobOutcome(done=True, result={"actions": [f"not a crawlable URL: {query}"]})
        host = host_of(url).rsplit(":", 1)[0]
        if host not in crawler.allow:
            crawler.allow.append(host)
            allowed = sorted(set(ctx.db.kv_get("user_allow_domains", []) or []) | {host})
            ctx.db.kv_set("user_allow_domains", allowed)
            actions.append(f"allowed crawling of {host} (requested by you)")
        crawler.frontier.add(url, priority=3.0, depth=0)
        ctx.scheduler.enqueue("crawl.step", {}, key=f"learn-crawl:{url}", priority=2.5)
        actions.append(f"queued {url} for polite crawling")
        return JobOutcome(done=True, value=0.5, result={"actions": actions})
    answerer = Answerer(ctx.db, ctx.services.get("linker"))
    entity = answerer.find_entity(query)
    titles: list[str] = []
    if entity is not None:
        if entity.wiki_title and entity.doc_id is None:
            titles.append(entity.wiki_title)
        for r in ctx.db.query(
            "SELECT e.wiki_title FROM doc_entities de JOIN entities e ON e.id=de.entity_id WHERE de.doc_id=? "
            "AND e.doc_id IS NULL AND e.wiki_title IS NOT NULL ORDER BY e.pagerank DESC LIMIT 40",
            (entity.doc_id or -1,),
        ):
            titles.append(str(r["wiki_title"]))
        actions.append(f"recognised “{query}” as {entity.label}")
    for hit in answerer.text.search_docs(query, limit=10):  # what the agent has read that mentions it
        for r in ctx.db.query(
            "SELECT e.wiki_title FROM doc_entities de JOIN entities e ON e.id=de.entity_id WHERE de.doc_id=? "
            "AND e.doc_id IS NULL AND e.wiki_title IS NOT NULL ORDER BY e.pagerank DESC LIMIT 5",
            (hit[0],),
        ):
            titles.append(str(r["wiki_title"]))
    titles = list(dict.fromkeys(titles))[:120]
    if titles:
        q = ",".join("?" * len(titles))
        indexed = [str(r["title"]) for r in ctx.db.query(f"SELECT title FROM wiki_index WHERE title IN ({q})", titles)]
        if indexed:
            ctx.scheduler.enqueue(
                "wikipedia.titles",
                {"titles": indexed, "lang": "en"},
                key=f"learn:{norm_alias(query)}:{int(time.time() // 3600)}",
                priority=2.5,
            )
            actions.append(f"queued {len(indexed)} Wikipedia articles about it from the dump")
        if len(indexed) < len(titles):
            actions.append(f"{len(titles) - len(indexed)} related articles are not in the dump parts read so far")
    topics = ctx.db.query(
        "SELECT id, name FROM topics WHERE name LIKE ? ORDER BY n_total DESC LIMIT 5", (f"%{query}%",)
    )
    if topics:
        cur = interests(ctx.db)
        for t in topics:
            cur[int(t["id"])] = time.time() + INTEREST_DAYS * 86400
        ctx.db.kv_set("user_interests", {str(k): v for k, v in cur.items()})
        actions.append("prioritising topics: " + ", ".join(str(t["name"]) for t in topics))
    if not actions:
        actions.append(f"I know nothing about “{query}” yet; added it as a search interest")
        pending = list(ctx.db.kv_get("pending_interests", []) or [])
        ctx.db.kv_set("pending_interests", sorted(set(pending) | {query}))
    return JobOutcome(done=True, value=0.5, result={"actions": actions, "titles": len(titles)})
