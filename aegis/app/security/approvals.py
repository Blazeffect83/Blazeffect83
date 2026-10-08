"""Single-use, expiring approvals bound to an exact action hash."""
from __future__ import annotations

import hashlib
from datetime import timedelta

from ..db import Database, dumps, loads, new_id, now_iso, parse_iso, utcnow


def action_hash(tool: str, args: dict) -> str:
    return hashlib.sha256(f"{tool}\n{dumps(args)}".encode()).hexdigest()


class ApprovalError(Exception):
    pass


class Approvals:
    def __init__(self, db: Database, ttl_minutes: int = 60):
        self.db = db
        self.ttl = ttl_minutes

    def request(self, *, objective_id: str | None, task_id: str | None, tool: str, args: dict,
                reason: str, affected: str, consequences: str, reversible: bool, operation: str,
                scope: str) -> str:
        h = action_hash(tool, args)
        existing = self.db.one(
            "SELECT id FROM approvals WHERE action_hash = ? AND task_id IS ? AND status = 'pending'",
            (h, task_id))
        if existing:
            return existing["id"]
        aid = new_id("apr")
        self.db.insert("approvals", {
            "id": aid, "objective_id": objective_id, "task_id": task_id, "tool": tool,
            "args": dumps(args), "action_hash": h, "reason": reason, "affected": affected,
            "consequences": consequences, "reversible": int(reversible), "operation": operation,
            "scope": scope, "status": "pending", "expires_at": now_iso(timedelta(minutes=self.ttl)),
            "created_at": now_iso(),
        })
        return aid

    def expire_stale(self) -> int:
        cur = self.db.execute(
            "UPDATE approvals SET status = 'expired' WHERE status IN ('pending','approved') AND expires_at < ?",
            (now_iso(),))
        return cur.rowcount

    def decide(self, approval_id: str, approve: bool, user: str) -> dict:
        self.expire_stale()
        row = self.get(approval_id)
        if not row:
            raise ApprovalError("approval not found")
        if row["status"] != "pending":
            raise ApprovalError(f"approval is {row['status']}, not pending")
        status = "approved" if approve else "rejected"
        # An approval is valid for a fixed window after the decision too.
        expires = now_iso(timedelta(minutes=self.ttl))
        self.db.execute(
            "UPDATE approvals SET status = ?, decided_by = ?, decided_at = ?, expires_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (status, user, now_iso(), expires, approval_id))
        return self.get(approval_id)

    def consume(self, approval_id: str, tool: str, args: dict) -> None:
        """Validate and mark used. Raises ApprovalError if anything mismatches."""
        row = self.get(approval_id)
        if not row:
            raise ApprovalError("approval not found")
        if row["status"] != "approved":
            raise ApprovalError(f"approval is {row['status']}")
        if parse_iso(row["expires_at"]) < utcnow():
            self.db.execute("UPDATE approvals SET status = 'expired' WHERE id = ?", (approval_id,))
            raise ApprovalError("approval expired")
        if row["action_hash"] != action_hash(tool, args):
            raise ApprovalError("approval does not match this exact action")
        cur = self.db.execute(
            "UPDATE approvals SET status = 'consumed', consumed_at = ? WHERE id = ? AND status = 'approved'",
            (now_iso(), approval_id))
        if cur.rowcount != 1:
            raise ApprovalError("approval already used")

    def get(self, approval_id: str) -> dict | None:
        row = self.db.one("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        if row:
            row["args"] = loads(row["args"], {})
        return row

    def list(self, status: str | None = None, limit: int = 100) -> list[dict]:
        self.expire_stale()
        if status:
            rows = self.db.query("SELECT * FROM approvals WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                                 (status, limit))
        else:
            rows = self.db.query("SELECT * FROM approvals ORDER BY created_at DESC LIMIT ?", (limit,))
        for r in rows:
            r["args"] = loads(r["args"], {})
        return rows
