"""Append-only, hash-chained audit log.

The table rejects UPDATE/DELETE via triggers and every row stores the SHA-256
of the previous row, so silent edits made with external tools are detectable
with :func:`verify_chain`. No agent tool exposes write access to this table.
"""
from __future__ import annotations

import hashlib
import threading
from typing import Any

from ..db import Database, dumps, now_iso
from ..security.redact import get_redactor

GENESIS = "0" * 64
_FIELDS = (
    "created_at", "actor", "event", "objective_id", "task_id", "tool", "args", "decision",
    "approval_id", "result", "exit_code", "duration_ms", "error", "resources",
)
_lock = threading.Lock()


def _digest(prev_hash: str, row: dict) -> str:
    payload = prev_hash + "|" + dumps({k: row.get(k) for k in _FIELDS})
    return hashlib.sha256(payload.encode()).hexdigest()


class AuditLog:
    def __init__(self, db: Database):
        self.db = db

    def record(
        self,
        event: str,
        *,
        actor: str = "agent",
        objective_id: str | None = None,
        task_id: str | None = None,
        tool: str | None = None,
        args: Any = None,
        decision: str | None = None,
        approval_id: str | None = None,
        result: str | None = None,
        exit_code: int | None = None,
        duration_ms: int | None = None,
        error: str | None = None,
        resources: Any = None,
    ) -> int:
        r = get_redactor()
        row = {
            "created_at": now_iso(),
            "actor": actor,
            "event": event,
            "objective_id": objective_id,
            "task_id": task_id,
            "tool": tool,
            "args": dumps(r.obj(args)) if args is not None else None,
            "decision": decision,
            "approval_id": approval_id,
            "result": r.text(result[:4000]) if result else result,
            "exit_code": exit_code,
            "duration_ms": duration_ms,
            "error": r.text(error[:4000]) if error else error,
            "resources": dumps(resources) if resources is not None else None,
        }
        with _lock, self.db.transaction() as c:
            prev = c.execute("SELECT hash FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()
            prev_hash = prev[0] if prev else GENESIS
            row["prev_hash"] = prev_hash
            row["hash"] = _digest(prev_hash, row)
            cols = ", ".join(row)
            cur = c.execute(
                f"INSERT INTO audit_events ({cols}) VALUES ({', '.join('?' for _ in row)})",
                tuple(row.values()),
            )
            return cur.lastrowid

    def verify_chain(self) -> tuple[bool, int | None]:
        """Return (ok, first_bad_id)."""
        prev = GENESIS
        for row in self.db.query("SELECT * FROM audit_events ORDER BY id"):
            if row["prev_hash"] != prev or _digest(prev, row) != row["hash"]:
                return False, row["id"]
            prev = row["hash"]
        return True, None

    def recent(self, limit: int = 100, objective_id: str | None = None) -> list[dict]:
        if objective_id:
            return self.db.query(
                "SELECT * FROM audit_events WHERE objective_id = ? ORDER BY id DESC LIMIT ?",
                (objective_id, limit),
            )
        return self.db.query("SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,))
