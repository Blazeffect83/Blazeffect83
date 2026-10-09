"""Contextual Thompson-sampling bandit that decides which kind of work to do next.

* **Arms** are action groups (``read``, ``perceive``, ``learn``, ``reason``,
  ``crawl``, ``memorize``, ``evaluate``, ``download``, ``plan``, …) that have a
  job ready right now.
* **Context** is a coarse, explainable bucket: body mode (normal / throttle /
  yield) × time of day (night / day) × backlog (is unread material piling up).
* **Reward** is value produced per CPU-second (log-compressed), observed after
  the job slice runs. Each (context, arm) keeps a running mean/variance
  (Welford); Thompson sampling draws from a Normal posterior
  N(mean, σ² / (n + 1)) with an optimistic prior for unseen arms, so every arm
  keeps being tried occasionally and the best one is used most.
* **Overrides**: jobs with priority ≥ ``URGENT`` (planner work, backups, quiz
  reports) run first regardless of the bandit, so the agent's housekeeping can
  never starve.

Every decision — context, sampled scores, choice, later its reward — is written
to ``decisions`` (pruned after 30 days) so ``polymath why`` can explain it.
"""

from __future__ import annotations

import json
import math
import random
import time
from dataclasses import dataclass
from typing import Any

from polymath.core.db import Database
from polymath.core.jobs import JobRegistry
from polymath.core.loop import Decision, Observation
from polymath.core.scheduler import Job

URGENT = 2.9
PRIOR_MEAN = 1.0
PRIOR_SD = 1.0


@dataclass
class ArmStats:
    n: int = 0
    mean: float = 0.0
    m2: float = 0.0

    def update(self, x: float) -> None:
        self.n += 1
        d = x - self.mean
        self.mean += d / self.n
        self.m2 += d * (x - self.mean)

    @property
    def var(self) -> float:
        return self.m2 / (self.n - 1) if self.n > 1 else PRIOR_SD**2


class BanditPolicy:
    def __init__(self, db: Database, *, rng: random.Random | None = None) -> None:
        self.db = db
        self.rng = rng or random.Random()
        self._last: dict[str, Any] = {}

    # ---------------------------------------------------------------- state
    def _arm(self, context: str, action: str) -> ArmStats:
        r = self.db.one("SELECT n, mean, m2 FROM bandit_arms WHERE context=? AND action=?", (context, action))
        return ArmStats(int(r["n"]), float(r["mean"]), float(r["m2"])) if r else ArmStats()

    def _save(self, context: str, action: str, a: ArmStats) -> None:
        self.db.execute(
            "INSERT INTO bandit_arms(context, action, n, mean, m2, updated) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(context, action) DO UPDATE SET n=excluded.n, mean=excluded.mean, m2=excluded.m2, "
            "updated=excluded.updated",
            (context, action, a.n, a.mean, a.m2, time.time()),
        )

    @staticmethod
    def context_of(obs: Observation, backlog: bool) -> str:
        hour = time.localtime(obs.now).tm_hour
        daypart = "night" if hour < 7 or hour >= 23 else "day"
        return f"{obs.body.mode}|{daypart}|{'backlog' if backlog else 'clear'}"

    # --------------------------------------------------------------- decide
    def decide(self, obs: Observation, registry: JobRegistry) -> Decision:
        ready = obs.queue.ready_by_kind
        if not ready:
            return Decision(kinds=None, action="any", reason="nothing ready")
        urgent = [
            k
            for k in ready
            if self.db.scalar(
                "SELECT 1 FROM jobs WHERE kind=? AND state='queued' AND not_before<=? AND priority>=? LIMIT 1",
                (k, obs.now, URGENT),
            )
        ]
        if urgent:
            return Decision(kinds=urgent, action="urgent", reason=f"urgent work first: {', '.join(sorted(urgent))}")
        groups: dict[str, list[str]] = {}
        for kind in ready:
            spec = registry.get(kind)
            if spec is None:
                continue
            if obs.body.mode in {"yield", "throttle"} and spec.heavy:
                continue
            groups.setdefault(spec.action, []).append(kind)
        if not groups:
            return Decision(kinds=[], action="idle", reason=f"only heavy work ready while body is {obs.body.mode}")
        backlog = bool(self.db.scalar("SELECT 1 FROM documents WHERE stage < 2 AND state != 'duplicate' LIMIT 1"))
        context = self.context_of(obs, backlog)
        samples: dict[str, float] = {}
        for action in groups:
            a = self._arm(context, action)
            if a.n == 0:
                samples[action] = self.rng.gauss(PRIOR_MEAN, PRIOR_SD) + 1.0  # optimism: try unseen arms soon
            else:
                samples[action] = self.rng.gauss(a.mean, math.sqrt(a.var / (a.n + 1)))
        chosen = max(samples, key=lambda k: samples[k])
        reason = f"Thompson sample {samples[chosen]:.3f} highest of {len(samples)} in context {context}"
        cur = self.db.execute(
            "INSERT INTO decisions(at, cycle, context, options, chosen, reason) VALUES(?,?,?,?,?,?)",
            (obs.now, obs.cycle, context, json.dumps({k: round(v, 4) for k, v in samples.items()}), chosen, reason),
        )
        self._last = {"decision_id": int(cur.lastrowid or 0), "context": context, "action": chosen}
        return Decision(kinds=sorted(groups[chosen]), action=chosen, reason=reason, context=context, scores=samples)

    # ---------------------------------------------------------------- learn
    def learn(self, decision: Decision, job: Job, reward: float, cpu: float, value: float) -> None:
        if decision.action in {"urgent", "any", "idle"} or not decision.context:
            return
        x = math.log1p(max(0.0, reward))
        arm = self._arm(decision.context, decision.action)
        arm.update(x)
        self._save(decision.context, decision.action, arm)
        if self._last.get("action") == decision.action:
            self.db.execute(
                "UPDATE decisions SET job_id=?, reward=?, cpu=? WHERE id=?",
                (job.id, round(reward, 4), round(cpu, 4), self._last["decision_id"]),
            )
        topic = job.payload.get("topic_id")
        if topic is not None:
            from polymath.drive.priority import record_effort

            record_effort(self.db, int(topic), cpu)
        self.db.execute("DELETE FROM decisions WHERE at < ?", (time.time() - 30 * 86400,))

    def arms(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.query("SELECT * FROM bandit_arms ORDER BY context, mean DESC")]
