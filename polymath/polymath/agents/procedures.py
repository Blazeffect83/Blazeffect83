"""Agents that invent their own tools (v0.13.0): procedures are data, run by a fixed interpreter.

**Finding one.** Each agent's history is a sequence of actions (``agent_tasks`` in the order they were made). Once
a day (with ``agents.evolve``) the society looks for short runs of actions (two or three in a row) that worked: an
agent's run that came up at least ``MIN_SUPPORT`` times and earned, on average, clearly more than that agent's
other turns (by ``MARGIN`` and two standard errors). Such a run is saved as a named procedure
(``procedures``: e.g. ``research:read+quiz``) for every agent of that kind to use.

**Using one.** A procedure is one more arm an agent can choose (``proc:<name>``, Thompson sampling like any
action); its outcome teaches that arm, so agents keep using what pays. Running it is the interpreter's job
(``run``): every step must be one of the society's fixed actions (``skills.ACTIONS``), at most ``MAX_STEPS`` steps,
and a step may only change the whitelisted, bounded settings in ``ARGS`` for its own duration. A procedure is
data, never code: agents get more capable without rewriting a line.

**Losing one.** After ``RETIRE_AFTER`` uses, a procedure whose mean reward per use is below zero is retired.
"""

from __future__ import annotations

import json
import math
import time
from collections import defaultdict
from typing import Any

from polymath.agents import skills

MAX_STEPS = 4
MIN_SUPPORT = 5
MARGIN = 0.2
RETIRE_AFTER = 20
ARGS = {"batch": (1, 20), "min_conf": (0.1, 0.95), "breadth": (0.25, 2.0)}  # what a step may set, and its bounds
PREFIX = "proc:"


def valid(steps: Any) -> bool:
    """A procedure the interpreter will run: a short list of known actions with whitelisted, bounded settings."""
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        return False
    for st in steps:
        if not isinstance(st, dict) or st.get("op") not in skills.ACTIONS:
            return False
        args = st.get("args", {})
        if not isinstance(args, dict) or any(k not in ARGS for k in args):
            return False
        if any(not isinstance(v, (int, float)) or isinstance(v, bool) for v in args.values()):
            return False
    return True


def available(db: Any, kind: str) -> list[dict[str, Any]]:
    rows = db.query("SELECT id, name, steps FROM procedures WHERE kind = ? AND state = 'active' ORDER BY id", (kind,))
    out = []
    for r in rows:
        steps = json.loads(r["steps"])
        if valid(steps):
            out.append({"id": int(r["id"]), "name": str(r["name"]), "steps": steps})
    return out


def run(rt: skills.Runtime, proc: dict[str, Any]) -> skills.StepResult:
    """The interpreter: run each step's action with its (bounded) settings, then restore the agent's own."""
    total = skills.StepResult(action=PREFIX + str(proc["name"]))
    if not valid(proc.get("steps")):
        total.notes.append("refused: not a valid procedure")
        return total
    own = dict(rt.agent.params)
    try:
        for st in proc["steps"]:
            for key, value in (st.get("args") or {}).items():
                lo, hi = ARGS[key]
                v = min(hi, max(lo, float(value)))
                rt.agent.params[key] = round(v) if key == "batch" else v
            res = skills.ACTIONS[str(st["op"])](rt)
            total.tasks += res.tasks
            total.reward += res.reward
            total.notes += res.notes[:2]
            rt.agent.params = dict(own)
            rt.tick()
    finally:
        rt.agent.params = own
    rt.db.execute("UPDATE procedures SET uses = uses + 1, wins = wins + ? WHERE id = ?", (total.reward, proc["id"]))
    return total


def discover(db: Any, *, now: float | None = None, window_s: float = 30 * 86400.0) -> dict[str, Any]:
    """Save the runs of actions that keep working as procedures; retire the ones that stopped paying."""
    now = time.time() if now is None else now
    found: list[str] = []
    for agent in db.query("SELECT id, kind FROM agents WHERE status = 'active'"):
        tasks = db.query(
            "SELECT action, reward FROM agent_tasks WHERE agent_id = ? AND created > ? AND action NOT LIKE 'proc:%' "
            "ORDER BY created, id",
            (agent["id"], now - window_s),
        )
        if len(tasks) < MIN_SUPPORT * 2:
            continue
        rewards = [float(t["reward"]) for t in tasks]
        base = sum(rewards) / len(rewards)
        runs: dict[tuple[str, ...], list[float]] = defaultdict(list)
        for n in (2, 3):
            for i in range(len(tasks) - n + 1):
                seq = tuple(str(t["action"]) for t in tasks[i : i + n])
                if len(set(seq)) == 1:
                    continue  # the same action again is not a new tool
                runs[seq].append(sum(rewards[i : i + n]) / n)
        for seq, got in runs.items():
            if len(got) < MIN_SUPPORT:
                continue
            mean = sum(got) / len(got)
            sd = math.sqrt(sum((x - mean) ** 2 for x in got) / (len(got) - 1)) if len(got) > 1 else 0.0
            if mean - base < MARGIN or mean - 2 * sd / math.sqrt(len(got)) <= base:
                continue
            name = f"{agent['kind']}:{'+'.join(seq)}"
            steps = [{"op": op, "args": {}} for op in seq]
            cur = db.execute(
                "INSERT OR IGNORE INTO procedures(name, kind, steps, found_by, support, mean, created) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (name, str(agent["kind"]), json.dumps(steps), int(agent["id"]), len(got), round(mean, 4), now),
            )
            if cur.rowcount:
                found.append(name)
    stale = db.query("SELECT name FROM procedures WHERE state = 'active' AND uses >= ? AND wins / uses < 0",
                     (RETIRE_AFTER,))  # fmt: skip
    retired = [str(r["name"]) for r in stale]
    if retired:
        db.execute(f"UPDATE procedures SET state = 'retired' WHERE name IN ({','.join('?' * len(retired))})", retired)
    return {"found": found, "retired": retired}
