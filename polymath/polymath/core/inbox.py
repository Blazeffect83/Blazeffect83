"""Inbox spool: how other processes (CLI, dashboard) hand work to the running agent.

The agent holds SQLite's write lock for most of each job slice, so an outside
writer could wait a long time. Instead, requests are written as small JSON files
into ``<data_dir>/inbox`` (atomic tmp+rename); the agent ingests them during
OBSERVE inside its own transaction and deletes them after commit. Requests are
idempotent (they carry job keys), so a crash between commit and unlink is harmless.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from polymath.core.db import Database
from polymath.core.logging import get_logger
from polymath.core.scheduler import Scheduler

log = get_logger("inbox")
MAX_PER_CYCLE = 50
KINDS = {"enqueue", "learn"}


def inbox_dir(data_dir: Path) -> Path:
    return data_dir / "inbox"


def submit(data_dir: Path, request: dict[str, Any]) -> Path:
    """Write one request atomically. Returns the file path."""
    if request.get("type") not in KINDS:
        raise ValueError(f"unknown inbox request type {request.get('type')!r}")
    d = inbox_dir(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    name = f"{time.time():.6f}-{uuid.uuid4().hex[:8]}"
    tmp = d / f".{name}.tmp"
    tmp.write_text(json.dumps(request, sort_keys=True))
    final = d / f"{name}.json"
    os.replace(tmp, final)
    return final


def ingest(db: Database, scheduler: Scheduler, data_dir: Path) -> int:
    """Apply pending requests (oldest first). Returns how many were applied."""
    d = inbox_dir(data_dir)
    if not d.is_dir():
        return 0
    files = sorted(p for p in d.iterdir() if p.suffix == ".json")[:MAX_PER_CYCLE]
    applied: list[Path] = []
    with db.transaction():
        for path in files:
            try:
                req = json.loads(path.read_text())
            except (OSError, ValueError):
                log.warning("dropping unreadable inbox file", extra={"file": path.name})
                applied.append(path)
                continue
            if req.get("type") == "enqueue" and isinstance(req.get("kind"), str):
                scheduler.enqueue(
                    req["kind"], req.get("payload") or {}, key=req.get("key"), priority=float(req.get("priority", 0.0))
                )
            elif req.get("type") == "learn" and str(req.get("query", "")).strip():
                q = str(req["query"]).strip()
                scheduler.enqueue("drive.learn", {"query": q}, key=f"learn:{q}:{int(time.time())}", priority=3.5)
            else:
                log.warning("dropping invalid inbox request", extra={"file": path.name})
            applied.append(path)
    for path in applied:
        path.unlink(missing_ok=True)
    return len(applied)
