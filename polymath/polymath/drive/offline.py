"""What to do without the internet: study what is already on disk.

Every one of these jobs needs only the database and the files already downloaded: reasoning over the graph,
checking its own facts, re-training what it learned from text, quizzing itself, tuning itself. Online they run
on their usual schedules; offline the planner runs them more often, so the time without new reading turns into
better use of what was read. Nothing here fetches anything. When the internet is back the normal schedules
take over again by themselves.
"""

from __future__ import annotations

import time
from typing import Any

from polymath.core import netstate

HOUR = 3600.0
# job kind → how often it runs while offline (online schedules are in each module's planner)
STUDY: dict[str, float] = {
    "reason.infer": 600.0,  # online every 30 min
    "reason.contradictions": 900.0,  # 1 h
    "reason.sanity": 1800.0,  # 3 h
    "reason.predict": 1800.0,  # 2 h
    "reason.audit": HOUR,  # 6 h
    "reason.reliability": HOUR,  # 6 h
    "reason.rules": 4 * HOUR,  # 24 h
    "eval.quiz": 1800.0,  # 6 h: more self-tests, so its own scores keep moving
    "eval.surprise": 1800.0,  # 3 h
    "self.tune": 2 * HOUR,  # 8 h
    "drive.pagerank": 2 * HOUR,  # 6 h
    "memory.merge": 2 * HOUR,  # 12 h
    "memory.geo": 1200.0,  # 1 h
    "memory.topics": 1200.0,  # 1 h
    "perception.semantic": 900.0,  # 1 h
    "perception.relations": 1200.0,  # 1 h
    "perception.howto": HOUR,  # 6 h
    "perception.phrasing": 4 * HOUR,  # 24 h
    "perception.train_phrases": 6 * HOUR,  # 24 h
    "perception.train_linker": 6 * HOUR,  # 24 h
    "embed.quality": HOUR,  # 6 h
    "agents.step": 60.0,  # 2 min: the agents quiz, predict and calibrate from what is known
}
PRIORITY = 0.9  # below the work the normal schedules queue (perception of stored documents comes first)


def planner(agent: Any) -> None:
    """While offline: keep a study job of each kind queued at its offline pace (never two of one kind)."""
    db = agent.db
    if netstate.get(db).online:
        return
    now = time.time()
    for kind, interval in STUDY.items():
        if agent.registry.get(kind) is None:
            continue
        if db.scalar("SELECT 1 FROM jobs WHERE kind = ? AND state IN ('queued', 'running') LIMIT 1", (kind,)):
            continue  # one is waiting already (its own schedule or an earlier offline round)
        slot = int(now // interval)
        last = db.scalar("SELECT MAX(updated) FROM jobs WHERE kind = ? AND state = 'done'", (kind,))
        if last is not None and now - float(last) < interval:
            continue  # it ran recently enough
        agent.scheduler.enqueue(kind, {}, key=f"offline:{kind}@{slot}", priority=PRIORITY)
