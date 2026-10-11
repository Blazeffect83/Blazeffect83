"""Job handler registry, the per-slice context and outcomes."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from polymath.core.config import Config
from polymath.core.db import Database
from polymath.core.scheduler import Job, Scheduler


class Interrupted(Exception):
    """Raised by :meth:`JobContext.check` when a slice must stop now."""


class PermanentError(Exception):
    """A failure that retrying cannot fix (bad payload, missing file, ...)."""


@dataclass
class JobOutcome:
    """What a slice achieved.

    ``done`` finishes the job; otherwise ``checkpoint`` is stored and the job is
    requeued after ``delay`` seconds. ``value`` is the slice's contribution in
    the agent's common currency (new facts/documents/quality gains) and drives
    reward-per-CPU-second learning.
    """

    done: bool = True
    checkpoint: dict[str, Any] | None = None
    value: float = 0.0
    delay: float = 0.0
    result: dict[str, Any] = field(default_factory=dict)


@dataclass
class JobContext:
    config: Config
    db: Database
    scheduler: Scheduler
    job: Job
    deadline: float
    stop_event: threading.Event
    services: dict[str, Any] = field(default_factory=dict)
    on_tick: Callable[[], None] | None = None
    intensity: float = 1.0  # 0..1; the body lowers it when hot or the disk is nearly full

    def time_left(self) -> float:
        return self.deadline - time.monotonic()

    def should_stop(self) -> bool:
        return self.stop_event.is_set() or time.monotonic() >= self.deadline

    def tick(self) -> None:
        """Called by long-running handlers: feeds the watchdog."""
        if self.on_tick is not None:
            self.on_tick()

    def check(self) -> None:
        self.tick()
        if self.should_stop():
            raise Interrupted


Handler = Callable[[JobContext], JobOutcome]


@dataclass
class JobSpec:
    kind: str
    handler: Handler
    description: str
    # Actions the drive may choose between are grouped by ``action`` (defaults to kind).
    action: str = ""
    heavy: bool = False  # deferred while the CPU is hot or the disk is nearly full


class JobRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, JobSpec] = {}

    def register(self, kind: str, handler: Handler, description: str, *, action: str = "", heavy: bool = False) -> None:
        if kind in self._specs:
            raise ValueError(f"job kind {kind!r} already registered")
        self._specs[kind] = JobSpec(kind, handler, description, action or kind, heavy)

    def get(self, kind: str) -> JobSpec | None:
        return self._specs.get(kind)

    def kinds(self) -> list[str]:
        return sorted(self._specs)

    def specs(self) -> list[JobSpec]:
        return [self._specs[k] for k in self.kinds()]


def noop_handler(ctx: JobContext) -> JobOutcome:
    """Built-in no-op job: records its execution once (exactly-once effect proof)."""
    sleep = float(ctx.job.payload.get("sleep", 0.0))
    if sleep:
        end = time.monotonic() + sleep
        while time.monotonic() < end:
            ctx.check()
            time.sleep(min(0.01, max(0.0, end - time.monotonic())))
    cycle = int(ctx.services.get("cycle", 0))
    ctx.db.execute("INSERT INTO noop_log(job_key, cycle, at) VALUES(?,?,?)", (ctx.job.key, cycle, time.time()))
    return JobOutcome(done=True, value=float(ctx.job.payload.get("value", 0.0)))
