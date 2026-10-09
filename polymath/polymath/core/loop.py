"""The agent loop: OBSERVE → DECIDE → ACT → INTEGRATE → EVALUATE → LEARN.

One cycle runs at most one bounded job slice. Everything a cycle changes in
the database — the handler's writes, the job's new checkpoint/state, the cycle
record, the heartbeat and the policy update — commits in ONE transaction, so a
crash or power cut at any instant leaves the database as it was before the
cycle (WAL guarantees no corruption) and the job resumes from its last
checkpoint.
"""

from __future__ import annotations

import json
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from types import FrameType
from typing import Any, Protocol

from polymath.body.sensors import Vitals, read_vitals
from polymath.body.systemd_notify import Notifier
from polymath.core.config import Config
from polymath.core.db import Database
from polymath.core.jobs import Interrupted, JobContext, JobOutcome, JobRegistry, PermanentError
from polymath.core.logging import get_logger
from polymath.core.scheduler import Job, QueueStats, Scheduler

log = get_logger("loop")


@dataclass
class BodyState:
    mode: str = "normal"  # normal | throttle | yield | pause
    intensity: float = 1.0
    reasons: list[str] = field(default_factory=list)
    vitals: Vitals | None = None
    players_online: int | None = None


class Body(Protocol):
    def observe(self) -> BodyState: ...


class BasicBody:
    """Reads vitals only; the full guard (thermal, disk, Minecraft) lives in ``body.guard``."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def observe(self) -> BodyState:
        return BodyState(vitals=read_vitals(self.config.body.thermal_zone, self.config.paths.data_dir))


@dataclass
class Observation:
    now: float
    body: BodyState
    queue: QueueStats
    cycle: int


@dataclass
class Decision:
    kinds: list[str] | None  # None → any ready job; [] → deliberately idle
    action: str = "any"
    reason: str = ""
    context: str = ""
    scores: dict[str, float] = field(default_factory=dict)


class Policy(Protocol):
    def decide(self, obs: Observation, registry: JobRegistry) -> Decision: ...

    def learn(self, decision: Decision, job: Job, reward: float, cpu: float, value: float) -> None: ...


class PriorityPolicy:
    """Default policy: run the highest-priority ready job; heavy jobs wait while yielding."""

    def decide(self, obs: Observation, registry: JobRegistry) -> Decision:
        if obs.body.mode in {"yield", "throttle"}:
            light = [s.kind for s in registry.specs() if not s.heavy]
            return Decision(kinds=light, action="light-only", reason=f"body mode {obs.body.mode}")
        return Decision(kinds=None, action="any", reason="highest priority ready job")

    def learn(self, decision: Decision, job: Job, reward: float, cpu: float, value: float) -> None:
        return None


@dataclass
class CycleRecord:
    cycle: int
    status: str
    action: str | None = None
    job_id: int | None = None
    kind: str | None = None
    cpu: float = 0.0
    wall: float = 0.0
    value: float = 0.0
    reward: float = 0.0
    error: str | None = None


Planner = Callable[["Agent"], None]


class Agent:
    def __init__(
        self,
        config: Config,
        db: Database,
        registry: JobRegistry,
        *,
        policy: Policy | None = None,
        body: Body | None = None,
        notifier: Notifier | None = None,
        services: dict[str, Any] | None = None,
        planners: list[tuple[float, Planner]] | None = None,
    ) -> None:
        self.config = config
        self.db = db
        self.registry = registry
        self.scheduler = Scheduler(
            db,
            retry_base=config.loop.retry_base,
            retry_cap=config.loop.retry_cap,
            max_crashes=config.loop.max_crashes,
        )
        self.policy: Policy = policy or PriorityPolicy()
        self.body: Body = body or BasicBody(config)
        self.notifier = notifier or Notifier()
        self.services: dict[str, Any] = services if services is not None else {}
        self.planners: list[tuple[float, Planner]] = planners or []
        self._planner_due: list[float] = [0.0] * len(self.planners)
        self.stop_event = threading.Event()
        self.cycle_no = int(db.scalar("SELECT MAX(id) FROM cycles", default=0))
        self.started = False
        self.last: CycleRecord | None = None

    # --------------------------------------------------------------- lifecycle
    def start(self) -> dict[str, int]:
        recovered = self.scheduler.recover()
        if recovered["requeued"] or recovered["dead"]:
            log.warning("recovered jobs after unclean shutdown", extra={"recovered": recovered})
        with self.db.transaction():
            self.db.kv_set("agent_started", time.time())
            self._heartbeat("starting")
        self.started = True
        self.notifier.ready(f"started at cycle {self.cycle_no}")
        log.info("agent started", extra={"cycle": self.cycle_no, "recovered": recovered})
        return recovered

    def stop(self, *_: Any) -> None:
        self.stop_event.set()

    def install_signal_handlers(self) -> None:
        def handler(signum: int, frame: FrameType | None) -> None:
            log.info("signal received; finishing current slice", extra={"signal": signum})
            self.stop()

        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)

    def run(self, max_cycles: int | None = None, *, until_idle: bool = False) -> int:
        """Run cycles until stopped (or ``max_cycles`` job cycles / the queue drains)."""
        if not self.started:
            self.start()
        ran = 0
        try:
            while not self.stop_event.is_set():
                record = self.cycle()
                if record.status not in {"idle", "paused"}:
                    ran += 1
                if max_cycles is not None and ran >= max_cycles:
                    break
                if until_idle and record.status == "idle":
                    break
                if record.status in {"idle", "paused"}:
                    self._sleep_idle(record.status)
        finally:
            self.shutdown()
        return ran

    def shutdown(self) -> None:
        self.notifier.stopping()
        if self.db.is_open:
            try:
                with self.db.transaction():
                    self._heartbeat("stopped")
            except Exception:  # pragma: no cover - best effort on the way out
                log.exception("could not write final heartbeat")
        log.info("agent stopped", extra={"cycle": self.cycle_no})

    def _sleep_idle(self, status: str) -> None:
        wait = self.config.loop.idle_sleep
        if status == "idle":
            nxt = self.scheduler.next_wakeup()
            if nxt is not None:
                wait = min(wait, max(0.05, nxt - time.time()))
        self.stop_event.wait(wait)

    # ------------------------------------------------------------------- cycle
    def _tick(self) -> None:
        self.notifier.watchdog()

    def _heartbeat(self, state: str) -> None:
        self.db.kv_set("heartbeat", {"ts": time.time(), "cycle": self.cycle_no, "state": state})

    def observe(self) -> Observation:
        now = time.time()
        for i, (interval, planner) in enumerate(self.planners):
            if now >= self._planner_due[i]:
                self._planner_due[i] = now + interval
                try:
                    planner(self)
                except Exception:
                    log.exception("planner failed", extra={"planner": getattr(planner, "__name__", "?")})
        return Observation(now=now, body=self.body.observe(), queue=self.scheduler.stats(), cycle=self.cycle_no)

    def cycle(self) -> CycleRecord:
        started = time.time()
        t0, c0 = time.monotonic(), time.process_time()
        obs = self.observe()  # OBSERVE
        self.services["observation"] = obs
        if obs.body.mode == "pause":
            return self._quiet_cycle("paused", obs, ", ".join(obs.body.reasons))
        decision = self.policy.decide(obs, self.registry)  # DECIDE
        job = self.scheduler.claim(decision.kinds)
        if job is None:
            return self._quiet_cycle("idle", obs, decision.reason)
        spec = self.registry.get(job.kind)
        self.cycle_no += 1
        self.services["cycle"] = self.cycle_no
        rec = CycleRecord(cycle=self.cycle_no, status="failed", action=decision.action, job_id=job.id, kind=job.kind)
        if spec is None:
            with self.db.transaction():
                self.scheduler.fail(job, f"no handler registered for kind {job.kind!r}", retryable=False)
            rec.error = "unknown kind"
            return self._finish(rec, started, t0, c0)
        budget = self.config.loop.job_time_budget * max(0.2, obs.body.intensity)
        ctx = JobContext(
            config=self.config,
            db=self.db,
            scheduler=self.scheduler,
            job=job,
            deadline=time.monotonic() + budget,
            stop_event=self.stop_event,
            services=self.services,
            on_tick=self._tick,
            intensity=obs.body.intensity,
        )
        try:
            with self.db.transaction():
                outcome = spec.handler(ctx)  # ACT
                if not isinstance(outcome, JobOutcome):
                    raise TypeError(f"handler for {job.kind} returned {type(outcome).__name__}")
                cpu = time.process_time() - c0
                self._integrate(job, outcome, cpu)  # INTEGRATE
                rec.status = "done" if outcome.done else "continue"
                rec.value = outcome.value
                rec.reward = self.evaluate(outcome, cpu)  # EVALUATE
                rec.cpu = cpu
                self.policy.learn(decision, job, rec.reward, cpu, outcome.value)  # LEARN
                self._record(rec, started, t0, json.dumps(outcome.result, default=str)[:4000])
                self._heartbeat("running")
        except Interrupted:
            with self.db.transaction():
                self.scheduler.release(job)
            rec.status = "interrupted"
        except PermanentError as exc:
            rec.error = str(exc)
            with self.db.transaction():
                self.scheduler.fail(job, f"permanent: {exc}", cpu=time.process_time() - c0, retryable=False)
                self._record(rec, started, t0, json.dumps({"error": str(exc)}))
            log.warning("job failed permanently", extra={"job": job.id, "kind": job.kind, "error": str(exc)})
        except Exception as exc:
            rec.error = f"{type(exc).__name__}: {exc}"
            with self.db.transaction():
                state = self.scheduler.fail(job, rec.error, cpu=time.process_time() - c0)
                self._record(rec, started, t0, json.dumps({"error": rec.error, "state": state}))
            log.exception("job slice failed", extra={"job": job.id, "kind": job.kind, "state": state})
        return self._finish(rec, started, t0, c0)

    def _integrate(self, job: Job, outcome: JobOutcome, cpu: float) -> None:
        if outcome.done:
            self.scheduler.complete(job, result=outcome.result or None, cpu=cpu, value=outcome.value)
        else:
            self.scheduler.checkpoint(
                job, outcome.checkpoint or job.checkpoint or {}, delay=outcome.delay, cpu=cpu, value=outcome.value
            )

    @staticmethod
    def evaluate(outcome: JobOutcome, cpu: float) -> float:
        """Reward = value produced per CPU-second (floored to avoid division blow-ups)."""
        return outcome.value / max(cpu, 0.05)

    def _record(self, rec: CycleRecord, started: float, t0: float, detail: str) -> None:
        rec.wall = time.monotonic() - t0
        self.db.execute(
            "INSERT INTO cycles(id, started, ended, action, job_id, status, cpu_seconds, wall_seconds, value, "
            "reward, detail) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                rec.cycle,
                started,
                time.time(),
                f"{rec.kind}" if rec.kind else rec.action,
                rec.job_id,
                rec.status,
                rec.cpu,
                rec.wall,
                rec.value,
                rec.reward,
                detail,
            ),
        )

    def _quiet_cycle(self, status: str, obs: Observation, reason: str) -> CycleRecord:
        with self.db.transaction():
            self._heartbeat(status)
            if status == "paused":
                self.db.kv_set("paused_reason", reason)
        self.notifier.watchdog()
        rec = CycleRecord(cycle=self.cycle_no, status=status, action=reason)
        self.last = rec
        return rec

    def _finish(self, rec: CycleRecord, started: float, t0: float, c0: float) -> CycleRecord:
        rec.wall = time.monotonic() - t0
        rec.cpu = rec.cpu or (time.process_time() - c0)
        self.last = rec
        self.notifier.watchdog()
        if self.notifier.active:
            self.notifier.status(f"cycle {rec.cycle}: {rec.kind} {rec.status}")
        log.debug(
            "cycle",
            extra={
                "cycle": rec.cycle,
                "kind": rec.kind,
                "status": rec.status,
                "cpu": round(rec.cpu, 4),
                "reward": round(rec.reward, 3),
            },
        )
        return rec
