"""Every problem Polymath notices about itself, in one place: ``events`` rows of kind ``issue``.

This is the caretaker's inbox (agent society addendum C): anything that goes wrong is reported here with its
area, a stable code and a severity, so the caretaker can read one list instead of hunting through tables and
logs. Until the caretaker exists, the same rows show in the live feed as "problem" lines. The internet going
away or coming back (kind ``net``) and a drive arriving or leaving (``volume_events``) keep their own rows;
the caretaker reads those too.

Reports are rate-limited per code (``EVERY``), so a problem that repeats is one line an hour, not a flood.
"""

from __future__ import annotations

import json
import time
from typing import Any

from polymath.core.db import Database

EVERY = 3600.0  # at most one report per code per hour
KEEP_CODES = 200  # remembered codes for the rate limit (oldest forgotten first)
SEVERITIES = ("info", "warn", "bad")


def report(
    db: Database,
    area: str,
    code: str,
    text: str,
    *,
    severity: str = "warn",
    detail: dict[str, Any] | None = None,
    now: float | None = None,
    every: float = EVERY,
) -> bool:
    """Record one problem. Returns False when the same code was reported within ``every`` seconds."""
    now = time.time() if now is None else now
    severity = severity if severity in SEVERITIES else "warn"
    last: dict[str, float] = dict(db.kv_get("issue_last") or {})
    if code in last and now - float(last[code]) < every:
        return False
    db.execute(
        "INSERT INTO events(at, kind, text, detail) VALUES(?, 'issue', ?, ?)",
        (now, text[:240], json.dumps({"status": severity, "area": area, "code": code, **(detail or {})})),
    )
    last[code] = now
    if len(last) > KEEP_CODES:
        last = dict(sorted(last.items(), key=lambda kv: kv[1])[-KEEP_CODES:])
    db.kv_set("issue_last", last)
    return True


def recent(db: Database, since: float, limit: int = 50) -> list[dict[str, Any]]:
    """Issues reported since ``since``, newest first (for ``polymath doctor`` and, later, the caretaker)."""
    out = []
    for r in db.query(
        "SELECT at, text, detail FROM events WHERE kind = 'issue' AND at >= ? ORDER BY id DESC LIMIT ?",
        (since, limit),
    ):
        try:
            detail = json.loads(r["detail"] or "{}")
        except ValueError:
            detail = {}
        out.append({"at": float(r["at"]), "text": str(r["text"]), **(detail if isinstance(detail, dict) else {})})
    return out
