"""Durable priority job queue on SQLite.

Jobs are identified by an idempotency ``key``: enqueueing the same key twice is
a no-op, so generators can re-run after a crash without duplicating work.
A job runs in *slices*: each slice either finishes the job or stores a
``checkpoint`` and requeues it. Failures back off exponentially; a job that
keeps killing the process (OOM, segfault) is dead-lettered after
``max_crashes`` so it cannot crash-loop the agent.
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from polymath.core.db import Database


@dataclass
class Job:
    id: int
    kind: str
    key: str
    payload: dict[str, Any]
    checkpoint: dict[str, Any] | None
    priority: float
    attempts: int
    crashes: int
    max_attempts: int
    slices: int

    @classmethod
    def from_row(cls, row: Any) -> Job:
        return cls(
            id=int(row["id"]),
            kind=str(row["kind"]),
            key=str(row["key"]),
            payload=json.loads(row["payload"]),
            checkpoint=json.loads(row["checkpoint"]) if row["checkpoint"] else None,
            priority=float(row["priority"]),
            attempts=int(row["attempts"]),
            crashes=int(row["crashes"]),
            max_attempts=int(row["max_attempts"]),
            slices=int(row["slices"]),
        )


@dataclass
class QueueStats:
    queued: int = 0
    ready: int = 0
    running: int = 0
    done: int = 0
    dead: int = 0
    ready_by_kind: dict[str, int] = field(default_factory=dict)


class Scheduler:
    def __init__(
        self,
        db: Database,
        *,
        clock: Callable[[], float] = time.time,
        retry_base: float = 30.0,
        retry_cap: float = 6 * 3600.0,
        max_crashes: int = 3,
        lease_seconds: float = 300.0,
        rng: random.Random | None = None,
    ) -> None:
        self.db = db
        self.clock = clock
        self.retry_base = retry_base
        self.retry_cap = retry_cap
        self.max_crashes = max_crashes
        self.lease_seconds = lease_seconds
        self.rng = rng or random.Random()

    # ----------------------------------------------------------------- enqueue
    def enqueue(
        self,
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        key: str | None = None,
        priority: float = 0.0,
        not_before: float = 0.0,
        max_attempts: int = 5,
    ) -> tuple[int, bool]:
        """Insert a job; returns ``(job_id, created)``. Existing keys are left untouched."""
        payload = payload or {}
        key = key or f"{kind}:{json.dumps(payload, sort_keys=True)}"
        now = self.clock()
        cur = self.db.execute(
            "INSERT INTO jobs(kind, key, payload, state, priority, max_attempts, not_before, created, updated) "
            "VALUES(?,?,?,'queued',?,?,?,?,?) ON CONFLICT(key) DO NOTHING",
            (kind, key, json.dumps(payload, sort_keys=True), priority, max_attempts, not_before, now, now),
        )
        if cur.rowcount:
            return int(cur.lastrowid or 0), True
        return int(self.db.scalar("SELECT id FROM jobs WHERE key=?", (key,))), False

    def ensure_recurring(
        self,
        kind: str,
        interval: float,
        *,
        payload: dict[str, Any] | None = None,
        priority: float = 0.0,
        phase: float = 0.0,
    ) -> tuple[int, bool]:
        """Enqueue one job per ``interval`` window (idempotent within the window)."""
        now = self.clock()
        slot = int((now - phase) // interval)
        start = slot * interval + phase
        return self.enqueue(kind, payload, key=f"{kind}@{slot}", priority=priority, not_before=start)

    # ------------------------------------------------------------------ claims
    def claim(self, kinds: Iterable[str] | None = None) -> Job | None:
        """Atomically lease the best ready job (optionally restricted to ``kinds``)."""
        now = self.clock()
        kinds = list(kinds) if kinds is not None else None
        sql = "SELECT * FROM jobs WHERE state='queued' AND not_before<=?"
        params: list[Any] = [now]
        if kinds is not None:
            if not kinds:
                return None
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            params.extend(kinds)
        sql += " ORDER BY priority DESC, id ASC LIMIT 1"
        with self.db.transaction():
            row = self.db.one(sql, params)
            if row is None:
                return None
            self.db.execute(
                "UPDATE jobs SET state='running', lease_until=?, updated=? WHERE id=?",
                (now + self.lease_seconds, now, row["id"]),
            )
        return Job.from_row(row)

    def complete(self, job: Job, *, result: Any = None, cpu: float = 0.0, value: float = 0.0) -> None:
        now = self.clock()
        self.db.execute(
            "UPDATE jobs SET state='done', lease_until=NULL, slices=slices+1, cpu_seconds=cpu_seconds+?, "
            "value=value+?, result=?, updated=?, last_error=NULL WHERE id=?",
            (cpu, value, json.dumps(result, default=str) if result is not None else None, now, job.id),
        )

    def checkpoint(
        self, job: Job, checkpoint: dict[str, Any], *, delay: float = 0.0, cpu: float = 0.0, value: float = 0.0
    ) -> None:
        """Store progress and requeue (a continuation is not a failure)."""
        now = self.clock()
        self.db.execute(
            "UPDATE jobs SET state='queued', checkpoint=?, not_before=?, lease_until=NULL, slices=slices+1, "
            "cpu_seconds=cpu_seconds+?, value=value+?, attempts=0, updated=? WHERE id=?",
            (json.dumps(checkpoint, sort_keys=True), now + delay, cpu, value, now, job.id),
        )

    def release(self, job: Job, *, delay: float = 0.0) -> None:
        """Put a leased job back untouched (e.g. the agent decided to pause)."""
        now = self.clock()
        self.db.execute(
            "UPDATE jobs SET state='queued', lease_until=NULL, not_before=?, updated=? WHERE id=? AND state='running'",
            (now + delay, now, job.id),
        )

    def backoff(self, attempts: int) -> float:
        base = min(self.retry_cap, self.retry_base * (2 ** max(0, attempts - 1)))
        return float(base * (0.75 + 0.5 * self.rng.random()))

    def fail(self, job: Job, error: str, *, cpu: float = 0.0, retryable: bool = True) -> str:
        """Record a failed slice. Returns the new state (``queued`` or ``dead``)."""
        now = self.clock()
        attempts = job.attempts + 1
        dead = not retryable or attempts >= job.max_attempts
        state = "dead" if dead else "queued"
        self.db.execute(
            "UPDATE jobs SET state=?, attempts=?, last_error=?, not_before=?, lease_until=NULL, "
            "cpu_seconds=cpu_seconds+?, updated=? WHERE id=?",
            (state, attempts, error[:2000], now + (0 if dead else self.backoff(attempts)), cpu, now, job.id),
        )
        return state

    def recover(self) -> dict[str, int]:
        """Startup recovery: jobs left 'running' by a dead process go back to the queue.

        Each such job gets ``crashes+1``; at ``max_crashes`` it is dead-lettered.
        """
        now = self.clock()
        with self.db.transaction():
            rows = self.db.query("SELECT id, crashes FROM jobs WHERE state='running'")
            requeued = dead = 0
            for row in rows:
                crashes = int(row["crashes"]) + 1
                if crashes >= self.max_crashes:
                    dead += 1
                    self.db.execute(
                        "UPDATE jobs SET state='dead', crashes=?, lease_until=NULL, updated=?, "
                        "last_error='process died while running this job too many times' WHERE id=?",
                        (crashes, now, row["id"]),
                    )
                else:
                    requeued += 1
                    self.db.execute(
                        "UPDATE jobs SET state='queued', crashes=?, lease_until=NULL, updated=? WHERE id=?",
                        (crashes, now, row["id"]),
                    )
        return {"requeued": requeued, "dead": dead}

    # ------------------------------------------------------------------- views
    def stats(self) -> QueueStats:
        now = self.clock()
        st = QueueStats()
        for row in self.db.query("SELECT state, COUNT(*) AS n FROM jobs GROUP BY state"):
            setattr(st, str(row["state"]), int(row["n"]))
        for row in self.db.query(
            "SELECT kind, COUNT(*) AS n FROM jobs WHERE state='queued' AND not_before<=? GROUP BY kind", (now,)
        ):
            st.ready_by_kind[str(row["kind"])] = int(row["n"])
        st.ready = sum(st.ready_by_kind.values())
        return st

    def next_wakeup(self) -> float | None:
        value = self.db.scalar("SELECT MIN(not_before) FROM jobs WHERE state='queued'")
        return None if value is None else float(value)

    def get(self, job_id: int) -> dict[str, Any] | None:
        row = self.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        return dict(row) if row else None

    def purge_done(self, older_than: float) -> int:
        """Delete finished jobs older than ``older_than`` seconds, keeping recurring idempotency fresh."""
        cutoff = self.clock() - older_than
        cur = self.db.execute("DELETE FROM jobs WHERE state='done' AND updated<?", (cutoff,))
        return int(cur.rowcount)
