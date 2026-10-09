"""Research topic schedules."""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from ..db import Database, dumps, loads, now_iso


def create_topic(db: Database, *, name: str, description: str = "", query: str = "", feeds=None, seed_urls=None,
                 allowed_domains=None, interval_minutes: int = 720, max_docs_per_run: int = 10,
                 freshness_days: int = 365, priority: int = 5, enabled: bool = True) -> int:
    if interval_minutes < 15:
        raise ValueError("interval_minutes must be at least 15")
    return db.insert("research_topics", {
        "name": name.strip()[:100], "description": description[:1000], "query": query[:300],
        "feeds": dumps(list(feeds or [])), "seed_urls": dumps(list(seed_urls or [])),
        "allowed_domains": dumps(list(allowed_domains or [])), "interval_minutes": interval_minutes,
        "max_docs_per_run": max(1, min(100, max_docs_per_run)), "freshness_days": freshness_days,
        "priority": priority, "enabled": int(enabled), "next_run_at": now_iso(), "created_at": now_iso()})


def update_topic(db: Database, topic_id: int, **fields) -> None:
    for k in ("feeds", "seed_urls", "allowed_domains"):
        if k in fields and not isinstance(fields[k], str):
            fields[k] = dumps(list(fields[k]))
    if "enabled" in fields:
        fields["enabled"] = int(bool(fields["enabled"]))
    if "interval_minutes" in fields and int(fields["interval_minutes"]) < 15:
        raise ValueError("interval_minutes must be at least 15")
    db.update("research_topics", "id", topic_id, fields)


def due_topics(db: Database) -> list[dict]:
    return db.query("SELECT * FROM research_topics WHERE enabled = 1 AND (next_run_at IS NULL OR next_run_at <= ?) "
                    "ORDER BY priority, next_run_at", (now_iso(),))


def mark_run(db: Database, topic: dict, ok: bool, status: str) -> None:
    failures = 0 if ok else topic["consecutive_failures"] + 1
    # Exponential back-off on repeated failures (capped at 8× the interval).
    factor = 1 if ok else min(8, 2 ** failures)
    nxt = now_iso(timedelta(minutes=topic["interval_minutes"] * factor))
    db.update("research_topics", "id", topic["id"], {
        "last_run_at": now_iso(), "next_run_at": nxt, "last_status": status[:1000],
        "consecutive_failures": failures})


def load_topics_file(db: Database, path: Path) -> list[str]:
    data = json.loads(Path(path).read_text())
    created = []
    for t in data.get("topics", []):
        if db.one("SELECT id FROM research_topics WHERE name = ?", (t["name"],)):
            continue
        create_topic(db, **t)
        created.append(t["name"])
    return created


def topic_view(t: dict) -> dict:
    t = dict(t)
    for k in ("feeds", "seed_urls", "allowed_domains"):
        t[k] = loads(t[k], [])
    return t
