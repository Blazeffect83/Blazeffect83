"""The agent's own live snapshot for the feed: ``/run/polymath/feed.json``.

The feed normally reads the dashboard (``/api/feed``). If the dashboard stops answering, the feed must not go
dark, so the agent itself writes what the feed shows (its status and the newest events, each with a sequence
number) to a small file every few seconds. ``/run`` is memory, so this costs the SSD and the SD card nothing,
and the file is readable by everyone, so the desktop's feed can use it without access to the database.

The directory is made by systemd (``RuntimeDirectory=polymath``); without it (development, tests) nothing is
written.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from polymath.core.logging import get_logger

log = get_logger(__name__)

SNAPSHOT_EVERY = 5.0  # seconds between writes (the feed polls every 1.5 s; events arrive in batches anyway)
KEEP = 200  # newest events kept in the file
STALE_S = 120.0  # an older snapshot means the agent is not writing it (stopped, hung or restarting)
FILE = "feed.json"


@dataclass
class _Writer:
    feed: Any = None
    cursor: str | None = None
    seq: int = 0
    events: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=KEEP))
    failed: bool = False


def path_for(config: Any) -> Path:
    return Path(config.paths.run_dir) / FILE


def planner(agent: Any) -> None:
    """Refresh the snapshot (cheap: id-range reads, no full-table counts)."""
    target = path_for(agent.config)
    if not target.parent.is_dir():
        return  # no runtime directory: not under systemd
    w: _Writer = agent.services.setdefault("snapshot", _Writer())
    try:
        if w.feed is None:
            from polymath.core.loop import heartbeat_path
            from polymath.interface.feed import Feed

            w.feed = Feed(agent.db, pulse=heartbeat_path(agent.config), stale=agent.config.loop.heartbeat_stale,
                          heavy_counts=False)  # fmt: skip
        data = w.feed.poll(w.cursor)
        approximate_counts(agent.db, data.get("status") or {})
        from polymath.core import issues

        # the caretaker's inbox, for `polymath doctor` (which cannot read the database)
        data.setdefault("status", {})["issues"] = issues.recent(agent.db, time.time() - 86400, 5)
        from polymath.core import standby

        on_standby = standby.status(agent.config, agent.db)
        if on_standby:  # learning on the standby brain on the SD card: the feed and doctor say so
            data["status"]["standby"] = on_standby
        w.cursor = str(data.get("cursor") or "") or None
        for ev in data.get("events") or []:
            w.seq += 1
            w.events.append({**ev, "seq": w.seq})
        write(target, {"version": 1, "written": time.time(), "seq": w.seq, "status": data.get("status") or {},
                       "events": list(w.events)})  # fmt: skip
        w.failed = False
    except Exception as exc:  # the snapshot is a convenience: it never disturbs the agent
        if not w.failed:
            log.warning("feed snapshot not written", extra={"error": f"{type(exc).__name__}: {exc}"})
            with contextlib.suppress(Exception):
                from polymath.core import issues

                issues.report(agent.db, "feed", "snapshot", f"the feed's fallback snapshot cannot be written: "
                              f"{type(exc).__name__}: {exc}")  # fmt: skip
        w.failed = True


def approximate_counts(db: Any, status: dict[str, Any]) -> None:
    """Exact counts take a full scan (the dashboard does them in the background); the snapshot gets the
    highest row numbers instead: instant, and close (deleted rows and duplicates are not subtracted)."""
    counts = status.setdefault("counts", {})
    if "facts" in counts:
        return
    for key, table in (("documents", "documents"), ("entities", "entities"), ("facts", "triples")):
        counts[key] = int(db.scalar(f"SELECT MAX(id) FROM {table}", default=0) or 0)
    counts["approx"] = True


def write(target: Path, payload: dict[str, Any]) -> None:
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, default=str, separators=(",", ":")), encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, target)


def write_waiting(config: Any, reason: str, *, now: float | None = None, since: float | None = None) -> bool:
    """While the agent waits for its storage (``cli.wait_for_storage``) the snapshot says why, so the feed and the
    dashboard show it instead of going quiet. Returns whether it was written (no runtime directory: not)."""
    target = path_for(config)
    if not target.parent.is_dir():
        return False
    now = time.time() if now is None else now
    status = {
        "online": True,
        "state": "paused",
        "mode": "pause",
        "paused_reason": reason,
        "activity": "waiting for the brain's drive",
        "net": {"online": True},
        "counts": {},
    }
    event = {"kind": "note", "what": "home", "status": "missing", "text": reason, "at": since or now, "seq": 1}
    try:
        write(target, {"version": 1, "written": now, "seq": 1, "status": status, "events": [event]})
    except OSError:
        return False
    return True


def parse_cursor(cursor: str | None) -> int | None:
    if cursor and cursor.startswith("s") and cursor[1:].isdigit():
        return int(cursor[1:])
    return None


def source(target: Path, *, clock: Callable[[], float] = time.time) -> Callable[[str | None], dict[str, Any]]:
    """A feed source over the snapshot file (same shape as ``/api/feed``; cursors look like ``s123``)."""
    from polymath.interface.feed import FeedUnavailable

    def fetch(cursor: str | None) -> dict[str, Any]:
        try:
            snap = json.loads(target.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise FeedUnavailable(f"no agent snapshot at {target}") from exc
        except (OSError, ValueError) as exc:
            raise FeedUnavailable(f"cannot read the agent snapshot: {type(exc).__name__}") from exc
        status = dict(snap.get("status") or {})
        age = clock() - float(snap.get("written") or 0)
        if age > STALE_S:
            status["online"] = False  # nobody is refreshing it: the agent is not running
            status.setdefault("state", "not running")
        after = parse_cursor(cursor)
        events = [e for e in snap.get("events") or [] if after is None or int(e.get("seq", 0)) > after]
        if after is None:
            events = events[-40:]  # a fresh start shows the recent past, like /api/feed does
        for e in events:
            e.pop("seq", None)
        return {"status": status, "events": events, "cursor": f"s{int(snap.get('seq') or 0)}", "snapshot_age_s": age}

    return fetch


def read_status(target: Path) -> tuple[dict[str, Any] | None, float | None]:
    """The snapshot's status and its age in seconds, for ``polymath doctor`` (None when there is none)."""
    with contextlib.suppress(OSError, ValueError):
        snap = json.loads(target.read_text(encoding="utf-8"))
        return dict(snap.get("status") or {}), time.time() - float(snap.get("written") or 0)
    return None, None
