"""Proof of life while the agent works on one long step: the vigil.

The agent proves it is alive in three ways: the database heartbeat (written when a slice commits), the pulse file
(touched whenever a job calls ``ctx.tick()``) and the systemd watchdog ping (``WatchdogSec=120``). All three come
from the main thread, between the steps of a job. One long step without a tick, such as a big SQL query on a brain
of several GB, a ``PRAGMA quick_check`` or training on a slow SD card, used to leave all three silent. After 60 s
the feed said "agent offline". After 120 s systemd killed the agent and restarted it, with growing pauses
(``RestartSteps``). The job then started over and could be killed again. On the Pi that looked like going offline
for a few minutes after learning something.

The vigil is a small daemon thread that vouches for the main thread every ``EVERY`` seconds:
- it pings the systemd watchdog and touches the pulse file;
- it writes ``<run_dir>/pulse.json`` (memory, not the SD card): what the agent is busy with and since when, so the
  feed can say "quizzing itself (one long step: 4 min)" instead of "offline".

It vouches only while the main thread keeps moving. Every section change (a slice, a planner, the WAL checkpoint)
and every ``ctx.tick()`` counts as progress (``progress``). After ``HARD_S`` seconds without progress the main
thread counts as hung: the vigil goes quiet, systemd's watchdog restarts the agent as before, and the feed shows
it. The vigil never touches the database: the connection belongs to the main thread.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from polymath.core.logging import get_logger

log = get_logger(__name__)

EVERY = 10.0  # seconds between vouches (the watchdog allows 120 s, the feed 60 s)
HARD_S = 15 * 60.0  # this long without progress means hung: stop vouching, so systemd restarts the agent
FILE = "pulse.json"


class Vigil:
    """Vouches for the main thread while it makes progress (see the module docstring)."""

    def __init__(
        self,
        *,
        ping: Callable[[], Any],
        pulse: Callable[[], Any],
        run_dir: Path | None,
        every: float = EVERY,
        hard_s: float = HARD_S,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.ping = ping
        self.pulse = pulse
        self.path = run_dir / FILE if run_dir is not None else None
        self.every = every
        self.hard_s = hard_s
        self.clock = clock
        self.wall = wall
        self._lock = threading.Lock()
        self._what = ""  # the section: a job kind, "planner …", "checkpoint", or "" between them
        self._job: int | None = None
        self._since = wall()  # when the section began (wall clock, for the feed)
        self._moved = clock()  # the main thread's last sign of progress
        self._began = clock()
        self._worst = 0.0  # the longest stretch without progress in this section
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.hung = False

    # ------------------------------------------------------------------ main thread
    def enter(self, what: str, job: int | None = None) -> None:
        """The main thread starts a section (``""`` when it is between sections)."""
        with self._lock:
            now = self.clock()
            self._what, self._job, self._since, self._moved, self._began, self._worst = (
                what, job, self.wall(), now, now, 0.0)  # fmt: skip

    def progress(self) -> None:
        """The main thread made progress inside its section (a job's ``ctx.tick()``)."""
        with self._lock:
            now = self.clock()
            self._worst = max(self._worst, now - self._moved)
            self._moved = now

    def section(self) -> dict[str, float]:
        """How the current section went so far: ``took`` seconds, ``silent`` = its longest stretch without
        progress (the end counts too). The loop reports slow steps from this (``core.loop``)."""
        with self._lock:
            now = self.clock()
            return {"took": now - self._began, "silent": max(self._worst, now - self._moved)}

    def state(self) -> dict[str, Any]:
        with self._lock:
            return {"what": self._what, "job": self._job, "since": self._since, "stalled_s": self.clock() - self._moved}

    # ------------------------------------------------------------------ the vigil
    def beat(self) -> bool:
        """One vouch. Returns False (and vouches for nothing) once the main thread counts as hung."""
        st = self.state()
        if st["stalled_s"] >= self.hard_s:
            if not self.hung:
                log.error("the main thread made no progress: no longer vouching, so systemd restarts the agent",
                          extra={"what": st["what"], "stalled_s": round(st["stalled_s"])})  # fmt: skip
            self.hung = True
            return False
        self.hung = False
        with contextlib.suppress(Exception):
            self.ping()
        with contextlib.suppress(Exception):
            self.pulse()
        self.write(st)
        return True

    def write(self, st: dict[str, Any]) -> None:
        """``pulse.json`` in the runtime directory (memory): the feed reads what the agent is busy with."""
        if self.path is None or not self.path.parent.is_dir():
            return
        data = {"pid": os.getpid(), "at": self.wall(), "what": st["what"], "job": st["job"], "since": st["since"]}
        tmp = self.path.with_name(f".{FILE}.{os.getpid()}")
        try:
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.chmod(tmp, 0o644)
            tmp.replace(self.path)
        except OSError:
            with contextlib.suppress(OSError):
                tmp.unlink()

    def _run(self) -> None:
        while not self._stop.wait(self.every):
            self.beat()

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="vigil", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2 * self.every)
            self._thread = None
        if self.path is not None:
            with contextlib.suppress(OSError):
                self.path.unlink()


def read(run_dir: Path, *, now: float | None = None, stale: float = 60.0) -> dict[str, Any] | None:
    """The agent's pulse (``pulse.json``) if it is fresh, else ``None``. For the feed and the dashboard."""
    now = time.time() if now is None else now
    try:
        data = json.loads((run_dir / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        if now - float(data.get("at") or 0) > stale:
            return None
        data["since"] = float(data.get("since") or now)
    except (TypeError, ValueError):
        return None
    return data
