"""What it changed about itself: one record per self-improvement, with the evidence.

Every self-improving part writes here: self-tuning (:mod:`polymath.drive.selftune`), rule trust
(:mod:`polymath.reasoning.rule_audit`), specialists (:mod:`polymath.drive.specialists`), the reading strategy
(:mod:`polymath.drive.strategy`) and learned phrasings (:mod:`polymath.perception.phrasing`). Each record is also a
feed line (``improved``), and the digest, weekly recap, dashboard and ``polymath changes`` read them.
"""

from __future__ import annotations

import json
import time
from typing import Any

from polymath.core.db import Database
from polymath.senses.openweb import event

AREAS = ("tuning", "rules", "specialists", "reading", "writing")


def record(
    db: Database,
    area: str,
    subject: str,
    action: str,
    summary: str,
    *,
    old: Any = None,
    new: Any = None,
    before: float | None = None,
    after: float | None = None,
    state: str = "final",
    detail: dict[str, Any] | None = None,
    feed: bool = True,
) -> int:
    """Write one change (and its feed line). Returns its id."""
    now = time.time()
    cur = db.execute(
        "INSERT INTO self_changes(at, area, subject, action, summary, old, new, before, after, state, detail) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (now, area, subject, action, summary, None if old is None else json.dumps(old),
         None if new is None else json.dumps(new), before, after, state, json.dumps(detail or {})),
    )  # fmt: skip
    if feed:
        event(db, "selfchange", summary, status=action, area=area)
    return int(cur.lastrowid or 0)


def recent(db: Database, since: float = 0.0, limit: int = 20, *, include_rejected: bool = True) -> list[dict[str, Any]]:
    skip = "" if include_rejected else "AND action != 'rejected' "
    return [
        dict(r)
        for r in db.query(
            "SELECT id, at, area, subject, action, summary, before, after, state FROM self_changes "
            f"WHERE at >= ? {skip}ORDER BY id DESC LIMIT ?",
            (since, limit),
        )
    ]


def counts(db: Database, since: float) -> dict[str, int]:
    return {
        str(r["action"]): int(r["n"])
        for r in db.query("SELECT action, COUNT(*) AS n FROM self_changes WHERE at >= ? GROUP BY action", (since,))
    }


def describe_counts(c: dict[str, int]) -> str:
    """'Improved itself 3 times (2 settings tuned, 1 rule dropped), 1 change rolled back.'"""
    good = {k: v for k, v in c.items() if k not in {"rejected", "rolled_back", "reset"}}
    if not good and not c.get("rolled_back"):
        return ""
    parts = ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in sorted(good.items()))
    text = f"Improved itself {sum(good.values())} time{'s' if sum(good.values()) != 1 else ''}"
    text += f" ({parts})" if parts else ""
    if c.get("rolled_back"):
        text += f"; undid {c['rolled_back']} change{'s' if c['rolled_back'] != 1 else ''} that made it worse"
    return text + "."
