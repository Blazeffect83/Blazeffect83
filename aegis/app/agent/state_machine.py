"""Explicit, persisted state machine for objectives and tasks.

Transitions are compare-and-set: a transition only applies if the row is
still in the expected state, so a user's pause/cancel can never be silently
overwritten by the worker.
"""
from __future__ import annotations

from ..db import Database, now_iso

QUEUED = "QUEUED"
PLANNING = "PLANNING"
READY = "READY"
RUNNING = "RUNNING"
WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
RETRYING = "RETRYING"
VERIFYING = "VERIFYING"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"
PAUSED = "PAUSED"

ALL = (QUEUED, PLANNING, READY, RUNNING, WAITING_FOR_APPROVAL, RETRYING, VERIFYING, COMPLETED, FAILED,
       CANCELLED, PAUSED)
TERMINAL = {COMPLETED, FAILED, CANCELLED}
ACTIVE = {PLANNING, RUNNING, RETRYING, VERIFYING}

OBJECTIVE_TRANSITIONS: dict[str, set[str]] = {
    QUEUED: {PLANNING, PAUSED, CANCELLED, FAILED},
    PLANNING: {READY, PAUSED, FAILED, CANCELLED, QUEUED},
    READY: {RUNNING, PLANNING, PAUSED, CANCELLED, VERIFYING, FAILED},
    RUNNING: {READY, RETRYING, VERIFYING, WAITING_FOR_APPROVAL, PLANNING, PAUSED, FAILED, CANCELLED},
    RETRYING: {RUNNING, READY, PLANNING, PAUSED, FAILED, CANCELLED},
    WAITING_FOR_APPROVAL: {READY, PLANNING, PAUSED, FAILED, CANCELLED},
    VERIFYING: {COMPLETED, PLANNING, READY, PAUSED, FAILED, CANCELLED},
    PAUSED: {QUEUED, READY, PLANNING, CANCELLED, FAILED},
    COMPLETED: set(),
    FAILED: {QUEUED},  # explicit user retry
    CANCELLED: set(),
}

TASK_TRANSITIONS: dict[str, set[str]] = {
    READY: {RUNNING, WAITING_FOR_APPROVAL, CANCELLED, PAUSED, FAILED},
    RUNNING: {VERIFYING, RETRYING, FAILED, COMPLETED, READY, PAUSED, CANCELLED, WAITING_FOR_APPROVAL},
    RETRYING: {RUNNING, FAILED, CANCELLED, PAUSED},
    WAITING_FOR_APPROVAL: {READY, RUNNING, FAILED, CANCELLED, PAUSED},
    VERIFYING: {COMPLETED, FAILED, RETRYING, CANCELLED},
    PAUSED: {READY, CANCELLED, FAILED},
    COMPLETED: set(),
    FAILED: set(),
    CANCELLED: set(),
}


class InvalidTransition(Exception):
    pass


class StateMachine:
    def __init__(self, db: Database):
        self.db = db

    def objective_status(self, objective_id: str) -> str | None:
        return self.db.scalar("SELECT status FROM objectives WHERE id = ?", (objective_id,))

    def transition_objective(self, objective_id: str, to: str, reason: str = "", *,
                             expect: str | set[str] | None = None, extra: dict | None = None) -> bool:
        """Returns False (and changes nothing) if the current state is not ``expect``."""
        with self.db.transaction() as c:
            row = c.execute("SELECT status FROM objectives WHERE id = ?", (objective_id,)).fetchone()
            if row is None:
                raise InvalidTransition(f"objective {objective_id} not found")
            cur = row[0]
            if expect is not None and cur not in ({expect} if isinstance(expect, str) else expect):
                return False
            if to != cur and to not in OBJECTIVE_TRANSITIONS[cur]:
                raise InvalidTransition(f"objective {cur} → {to} not allowed")
            values = {"status": to, "status_reason": reason[:2000], "updated_at": now_iso()}
            if to in TERMINAL:
                values["finished_at"] = now_iso()
            values.update(extra or {})
            sets = ", ".join(f"{k} = ?" for k in values)
            c.execute(f"UPDATE objectives SET {sets} WHERE id = ?", (*values.values(), objective_id))
            if to != cur or reason:
                c.execute("INSERT INTO task_events(objective_id, task_id, from_status, to_status, reason, created_at) "
                          "VALUES (?, NULL, ?, ?, ?, ?)", (objective_id, cur, to, reason[:2000], now_iso()))
            return True

    def transition_task(self, task_id: str, to: str, reason: str = "", *, expect: str | set[str] | None = None,
                        extra: dict | None = None) -> bool:
        with self.db.transaction() as c:
            row = c.execute("SELECT status, objective_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise InvalidTransition(f"task {task_id} not found")
            cur, oid = row
            if expect is not None and cur not in ({expect} if isinstance(expect, str) else expect):
                return False
            if to != cur and to not in TASK_TRANSITIONS[cur]:
                raise InvalidTransition(f"task {cur} → {to} not allowed")
            values = {"status": to, "status_reason": reason[:2000], "updated_at": now_iso(), **(extra or {})}
            sets = ", ".join(f"{k} = ?" for k in values)
            c.execute(f"UPDATE tasks SET {sets} WHERE id = ?", (*values.values(), task_id))
            c.execute("INSERT INTO task_events(objective_id, task_id, from_status, to_status, reason, created_at) "
                      "VALUES (?, ?, ?, ?, ?, ?)", (oid, task_id, cur, to, reason[:2000], now_iso()))
            return True

    def events(self, objective_id: str, limit: int = 500) -> list[dict]:
        return self.db.query("SELECT * FROM task_events WHERE objective_id = ? ORDER BY id LIMIT ?",
                             (objective_id, limit))
