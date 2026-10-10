"""Persistence for the agent society: agents, scopes, tasks, the reward ledger and per-agent preferences.

Rewards are the only currency. Every reward is a ledger row tied to the task that
earned it and the action that produced the task, so an agent's preferences (its
Thompson-sampling arms) learn from *verified* outcomes, even when the verdict
arrives days later.
"""

from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any

from polymath.core.db import Database
from polymath.drive.bandit import ArmStats

DEFAULT_PARAMS: dict[str, Any] = {
    "batch": 6,  # questions / predictions per step
    "min_conf": 0.4,  # abstain from open predictions below this confidence (heritable, mutated by evolution)
    "breadth": 1.0,  # scope size multiplier
    "explore": 1.0,  # Thompson sampling width
    "weights": None,  # the agent's own link-prediction weights (None → the society's)
}
SCOPE_CAP = 20_000
REWARDS = {  # what a verified outcome is worth
    "quiz_correct": 1.0,
    "quiz_wrong": -0.4,  # random guessing among 4 has negative expected value: 0.25 − 0.75·0.4 < 0
    "predict_correct": 2.0,  # an open prediction later confirmed by the dumps: the hardest, most valuable
    "predict_wrong": -0.8,
    "dispute_correct": 1.5,
    "dispute_wrong": -0.6,
    "user_correct": 2.0,  # you said it was right
    "user_wrong": -1.0,
    "read_doc": 0.1,  # per requested in-scope article that actually arrived
    "digest_doc": 0.15,  # per new document verified (by the entity linker) to be about the subject
}


@dataclass
class Agent:
    id: int
    name: str
    kind: str
    directive: str
    subject: str
    scope: dict[str, Any]
    params: dict[str, Any]
    parent: int | None
    generation: int
    origin: str
    status: str
    xp: float
    level: int
    reward_total: float
    cpu_total: float
    tasks_correct: int
    tasks_wrong: int
    scope_at: float
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def accuracy(self) -> float | None:
        n = self.tasks_correct + self.tasks_wrong
        return self.tasks_correct / n if n else None


def level_for(xp: float) -> int:
    """Levels double in cost: 1 at 0 XP, 2 at 5, 3 at 15, 4 at 35, 5 at 75 …"""
    return 1 + int(math.log2(1 + max(0.0, xp) / 5))


def _row(r: Any) -> Agent:
    params = {**DEFAULT_PARAMS, **json.loads(r["params"])}
    return Agent(
        int(r["id"]), str(r["name"]), str(r["kind"]), str(r["directive"]), str(r["subject"]),
        json.loads(r["scope"]), params, r["parent"], int(r["generation"]), str(r["origin"]), str(r["status"]),
        float(r["xp"]), int(r["level"]), float(r["reward_total"]), float(r["cpu_total"]),
        int(r["tasks_correct"]), int(r["tasks_wrong"]), float(r["scope_at"]),
    )  # fmt: skip


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:32] or "agent"


def unique_name(db: Database, base: str) -> str:
    name, i = base, 2
    while db.scalar("SELECT 1 FROM agents WHERE name=?", (name,)):
        name, i = f"{base}-{i}", i + 1
    return name


def create(
    db: Database,
    *,
    kind: str,
    directive: str,
    subject: str,
    scope: dict[str, Any],
    name: str | None = None,
    params: dict[str, Any] | None = None,
    parent: int | None = None,
    generation: int = 0,
    origin: str = "user",
) -> int:
    now = time.time()
    nm = unique_name(db, slug(name) if name else f"{kind}-{slug(subject)}")
    cur = db.execute(
        "INSERT INTO agents(name, kind, directive, subject, scope, params, parent, generation, origin, created, "
        "updated) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            nm,
            kind,
            directive,
            subject,
            json.dumps(scope),
            json.dumps(params or {}),
            parent,
            generation,
            origin,
            now,
            now,
        ),
    )
    return int(cur.lastrowid or 0)


def get(db: Database, ident: int | str) -> Agent | None:
    if isinstance(ident, int) or str(ident).isdigit():
        r = db.one("SELECT * FROM agents WHERE id=?", (int(ident),))
    else:
        r = db.one("SELECT * FROM agents WHERE name=?", (str(ident),))
    return _row(r) if r else None


def active(db: Database) -> list[Agent]:
    return [_row(r) for r in db.query("SELECT * FROM agents WHERE status='active' ORDER BY id")]


def all_agents(db: Database) -> list[Agent]:
    return [_row(r) for r in db.query("SELECT * FROM agents ORDER BY status='retired', id")]


def set_status(db: Database, agent_id: int, status: str, reason: str = "") -> None:
    db.execute(
        "UPDATE agents SET status=?, status_reason=?, updated=? WHERE id=?", (status, reason, time.time(), agent_id)
    )


def save_params(db: Database, agent_id: int, params: dict[str, Any]) -> None:
    db.execute("UPDATE agents SET params=?, updated=? WHERE id=?", (json.dumps(params), time.time(), agent_id))


# ------------------------------------------------------------------ scope
def scope_entities(db: Database, agent_id: int, limit: int = SCOPE_CAP) -> list[int]:
    return [
        int(r["entity_id"])
        for r in db.query(
            "SELECT entity_id FROM agent_scope WHERE agent_id=? ORDER BY weight DESC LIMIT ?", (agent_id, limit)
        )
    ]


def refresh_scope(db: Database, agent: Agent) -> int:
    """Materialise the entities an agent is responsible for (seed entity and neighbours, entities of documents
    about its subject or in its topics, subjects of its relations). Re-run as knowledge grows."""
    from polymath.memory.text_index import TextIndex

    cap = int(SCOPE_CAP * min(2.0, max(0.25, float(agent.params.get("breadth", 1.0)))))
    weights: dict[int, float] = {}

    def add(eid: int, w: float) -> None:
        if len(weights) < cap or eid in weights:
            weights[eid] = max(weights.get(eid, 0.0), w)

    sc = agent.scope
    for e in sc.get("entities", []):
        add(int(e), 3.0)
        for r in db.query(
            "SELECT o AS x FROM triples WHERE s=? AND o != 0 AND holdout=0 UNION SELECT s FROM triples WHERE o=? "
            "AND holdout=0 LIMIT 800",
            (e, e),
        ):
            add(int(r["x"]), 2.0)
    docs: list[int] = [d for d, _score in TextIndex(db).search_docs(agent.subject, limit=150)] if agent.subject else []
    for t in sc.get("topics", []):
        docs += [int(r["doc_id"]) for r in db.query("SELECT doc_id FROM doc_topics WHERE topic_id=? LIMIT 400", (t,))]
    for d in dict.fromkeys(docs):
        for r in db.query(
            "SELECT entity_id, score FROM doc_entities WHERE doc_id=? ORDER BY score DESC LIMIT 40", (d,)
        ):
            add(int(r["entity_id"]), 1.0 + float(r["score"] or 0) / 10)
        e = db.scalar("SELECT id FROM entities WHERE doc_id=?", (d,))
        if e is not None:
            add(int(e), 2.5)
    p31 = db.scalar("SELECT id FROM predicates WHERE key='P31'")
    for p in sc.get("predicates", []):
        cls = sc.get("class")
        if cls and p31:
            rows = db.query(
                "SELECT t.s FROM triples t JOIN triples c ON c.s=t.s AND c.p=? AND c.o=? WHERE t.p=? LIMIT ?",
                (p31, cls, p, cap),
            )
        else:
            rows = db.query("SELECT DISTINCT s FROM triples WHERE p=? LIMIT ?", (p, cap))
        for r in rows:
            add(int(r["s"]), 1.5)
        if not cls and p31:  # no class given: the relation's dominant subject type ("continents" → countries)
            top = db.one(
                "SELECT c.o, COUNT(*) AS n FROM triples t JOIN triples c ON c.s=t.s AND c.p=? WHERE t.p=? "
                "GROUP BY c.o ORDER BY n DESC LIMIT 1",
                (p31, p),
            )
            cls = int(top["o"]) if top is not None else None
        if cls and p31:  # subjects of that type which still lack the relation: what there is to predict
            for r in db.query("SELECT s FROM triples WHERE p=? AND o=? LIMIT ?", (p31, cls, cap)):
                add(int(r["s"]), 1.2)
    db.execute("DELETE FROM agent_scope WHERE agent_id=?", (agent.id,))
    db.executemany(
        "INSERT INTO agent_scope(agent_id, entity_id, weight) VALUES(?,?,?)",
        [(agent.id, e, w) for e, w in weights.items()],
    )
    db.execute("UPDATE agents SET scope_at=? WHERE id=?", (time.time(), agent.id))
    return len(weights)


# ------------------------------------------------------------------ tasks and rewards
def open_task(
    db: Database,
    agent: Agent,
    *,
    kind: str,
    action: str,
    context: str = "",
    target: str = "",
    payload: dict[str, Any] | None = None,
    state: str = "done",
    verify_after: float | None = None,
    result: dict[str, Any] | None = None,
) -> int | None:
    """Record a task. Returns None if the agent already holds a task for this target (rewarded once only)."""
    now = time.time()
    cur = db.execute(
        "INSERT OR IGNORE INTO agent_tasks(agent_id, kind, action, context, target, payload, result, state, created, "
        "done_at, verify_after) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (agent.id, kind, action, context, target, json.dumps(payload or {}), json.dumps(result or {}), state, now,
         now if state != "pending" else None, verify_after),
    )  # fmt: skip
    if not cur.rowcount:
        return None
    if state != "pending":
        db.execute("UPDATE agents SET tasks_done=tasks_done+1 WHERE id=?", (agent.id,))
    return int(cur.lastrowid or 0)


def reward(db: Database, task_id: int, amount: float, reason: str, *, correct: bool | None) -> None:
    """Settle a task: ledger row, agent totals, XP / level, and the producing action's arm."""
    t = db.one("SELECT * FROM agent_tasks WHERE id=?", (task_id,))
    if t is None or (t["state"] in {"correct", "wrong", "expired"} and correct is not None):
        return
    now = time.time()
    state = "correct" if correct else "wrong" if correct is not None else str(t["state"])
    db.execute(
        "UPDATE agent_tasks SET state=?, reward=reward+?, verified_at=?, reason=? WHERE id=?",
        (state, amount, now, reason[:300], task_id),
    )
    agent_id = int(t["agent_id"])
    db.execute(
        "INSERT INTO agent_rewards(agent_id, task_id, amount, reason, at) VALUES(?,?,?,?,?)",
        (agent_id, task_id, amount, reason[:300], now),
    )
    db.execute(
        "UPDATE agents SET reward_total=reward_total+?, xp=xp+?, tasks_correct=tasks_correct+?, "
        "tasks_wrong=tasks_wrong+?, updated=? WHERE id=?",
        (amount, max(0.0, amount), int(correct is True), int(correct is False), now, agent_id),
    )
    xp = float(db.scalar("SELECT xp FROM agents WHERE id=?", (agent_id,), 0.0))
    db.execute("UPDATE agents SET level=? WHERE id=?", (level_for(xp), agent_id))
    learn(db, agent_id, str(t["context"]), str(t["action"]), amount)


def expire(db: Database, task_id: int, reason: str) -> None:
    db.execute(
        "UPDATE agent_tasks SET state='expired', verified_at=?, reason=? WHERE id=?", (time.time(), reason, task_id)
    )


# ------------------------------------------------------------------ preferences (per-agent Thompson arms)
def arm(db: Database, agent_id: int, context: str, action: str) -> ArmStats:
    r = db.one(
        "SELECT n, mean, m2 FROM agent_arms WHERE agent_id=? AND context=? AND action=?", (agent_id, context, action)
    )
    return ArmStats(int(r["n"]), float(r["mean"]), float(r["m2"])) if r else ArmStats()


def learn(db: Database, agent_id: int, context: str, action: str, x: float) -> None:
    a = arm(db, agent_id, context, action)
    a.update(x)
    db.execute(
        "INSERT INTO agent_arms(agent_id, context, action, n, mean, m2, updated) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(agent_id, context, action) DO UPDATE SET n=excluded.n, mean=excluded.mean, m2=excluded.m2, "
        "updated=excluded.updated",
        (agent_id, context, action, a.n, a.mean, a.m2, time.time()),
    )
