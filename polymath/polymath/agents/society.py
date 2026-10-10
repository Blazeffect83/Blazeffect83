"""The agent society: who works next, what it does, how it is paid, and how the society evolves.

* **Allocation.** Each ``agents.step`` slice gives turns to agents by Thompson
  sampling on their verified reward per step (new agents start optimistic). Agents
  with your questions waiting go first. Higher levels earn a small bonus, so
  proven agents get more of the CPU.
* **Choice.** The agent picks one of its actions with its *own* Thompson-sampling
  arms, which are updated with every verified reward, including rewards for tasks
  settled days later. Over time each agent learns what works for its directive.
* **Verification.** ``agents.verify`` settles open tasks: open predictions against
  facts that have since arrived, disputes against hidden copies or settled
  evidence, reading requests against what was stored.
* **Evolution.** Once an agent has enough verified outcomes and positive reward,
  it may fork a child with mutated heritable parameters (confidence threshold,
  batch size, breadth, exploration, evidence weights). The child competes on the
  same directive. A child that does worse is retired. One that does better passes
  its parameters on: to its user-made parent, which keeps its name and history,
  or it replaces an evolved parent. User-made agents are never retired
  automatically, only flagged.
"""

from __future__ import annotations

import json
import math
import random
import time
from typing import Any

from polymath.agents import directives, skills, store
from polymath.agents.store import REWARDS, Agent
from polymath.core.jobs import JobContext, JobOutcome
from polymath.core.logging import get_logger

log = get_logger("agents")
SCOPE_REFRESH = 6 * 3600.0
LEVEL_BONUS = 0.05
TASK_EXPIRY = {"predict": skills.PREDICT_HORIZON, "dispute": 90 * 86400.0, "read": 6 * 3600.0}


# ------------------------------------------------------------------ spawning and commands
def spawn(db: Any, text: str, *, name: str | None = None) -> Agent:
    d = directives.parse(text)
    scope = directives.resolve_scope(db, d)
    if d.qualifier:
        scope["qualifier"] = d.qualifier
    params: dict[str, Any] = {}
    if d.kind == "watch":  # a watch reports what is new from now on, not everything ever read
        params["cursor"] = int(db.scalar("SELECT MAX(id) FROM documents", default=0) or 0)
    aid = store.create(db, kind=d.kind, directive=d.text, subject=d.subject, scope=scope, name=name, params=params)
    agent = store.get(db, aid)
    assert agent is not None
    store.refresh_scope(db, agent)
    log.info("agent spawned", extra={"agent": agent.name, "kind": d.kind, "subject": d.subject})
    return agent


def give_task(db: Any, ident: str, text: str) -> int:
    agent = store.get(db, ident)
    if agent is None:
        raise KeyError(f"no agent called {ident!r}")
    cur = db.execute(
        "INSERT INTO agent_tasks(agent_id, kind, action, payload, state, created) VALUES(?, 'user', 'answer', ?, "
        "'pending', ?)",
        (agent.id, json.dumps({"text": text}), time.time()),
    )
    return int(cur.lastrowid or 0)


def feedback(db: Any, task_id: int, verdict: str, note: str = "") -> dict[str, Any]:
    """Your verdict on a task: the strongest reward signal there is."""
    t = db.one("SELECT id, agent_id, kind, state FROM agent_tasks WHERE id=?", (task_id,))
    if t is None:
        raise KeyError(f"no task {task_id}")
    good = verdict.lower() in {"correct", "right", "good", "yes", "up", "+", "1", "true"}
    if t["state"] in {"correct", "wrong"}:
        return {"task": task_id, "unchanged": True, "state": t["state"]}
    amount = REWARDS["user_correct"] if good else REWARDS["user_wrong"]
    store.reward(db, int(t["id"]), amount, ("you: correct" if good else "you: wrong") + (f" — {note}" if note else ""),
                 correct=good)  # fmt: skip
    return {"task": task_id, "reward": amount, "state": "correct" if good else "wrong"}


def command_job(ctx: JobContext) -> JobOutcome:
    """Job ``agents.command``: a CLI request applied by the running agent (spawn, task, feedback, control)."""
    p = ctx.job.payload
    op = str(p.get("op"))
    if op == "spawn":
        a = spawn(ctx.db, str(p["directive"]), name=p.get("name"))
        return JobOutcome(done=True, result={"agent": a.name, "kind": a.kind})
    if op == "task":
        return JobOutcome(done=True, result={"task": give_task(ctx.db, str(p["agent"]), str(p["text"]))})
    if op == "feedback":
        verdict = feedback(ctx.db, int(p["task"]), str(p["verdict"]), str(p.get("note", "")))
        return JobOutcome(done=True, result=verdict)
    if op in {"pause", "resume", "retire"}:
        target = store.get(ctx.db, str(p["agent"]))
        if target is None:
            return JobOutcome(done=True, result={"error": f"no agent {p['agent']!r}"})
        store.set_status(ctx.db, target.id, {"pause": "paused", "resume": "active", "retire": "retired"}[op], "by you")
        return JobOutcome(done=True, result={"agent": target.name, "status": op})
    return JobOutcome(done=True, result={"error": f"unknown op {op!r}"})


# ------------------------------------------------------------------ allocation and choice
def reward_rate(db: Any, agent: Agent) -> tuple[float, float, int]:
    """Posterior (mean, sd, n) of an agent's verified reward per step; optimistic for newcomers."""
    steps = int(db.scalar("SELECT steps FROM agents WHERE id=?", (agent.id,), 0))
    mean = (agent.reward_total + 1.0) / (steps + 1)  # one imaginary +1 step: try new agents
    sd = 1.0 / math.sqrt(steps + 1)
    return mean, sd, steps


def choose_agent(db: Any, agents: list[Agent], rng: random.Random) -> Agent | None:
    if not agents:
        return None
    waiting = [
        a for a in agents
        if db.scalar("SELECT 1 FROM agent_tasks WHERE agent_id=? AND kind='user' AND state='pending' LIMIT 1", (a.id,))
    ]  # fmt: skip
    if waiting:
        return waiting[0]

    def sample(a: Agent) -> float:
        mean, sd, _n = reward_rate(db, a)
        return rng.gauss(mean, sd) + LEVEL_BONUS * a.level

    return max(agents, key=sample)


def choose_action(db: Any, agent: Agent, rng: random.Random) -> str:
    explore = float(agent.params.get("explore", 1.0))
    scores = {}
    for action in skills.REPERTOIRE[agent.kind]:
        arm = store.arm(db, agent.id, "", action)
        if arm.n == 0:
            scores[action] = rng.gauss(1.0, 1.0) + 1.0  # untried: optimistic
        else:
            scores[action] = rng.gauss(arm.mean, explore * math.sqrt(arm.var / (arm.n + 1)))
    return max(scores, key=lambda k: scores[k])


def run_agent(ctx: JobContext, agent: Agent, rng: random.Random) -> dict[str, Any]:
    t0 = time.process_time()
    if time.time() - agent.scope_at > SCOPE_REFRESH:
        store.refresh_scope(ctx.db, agent)
    rt = skills.Runtime(ctx.db, ctx.scheduler, ctx.config, ctx.services, agent, rng, tick=ctx.tick)
    answered = skills.answer_pending(rt)
    action = choose_action(ctx.db, agent, rng)
    res = skills.ACTIONS[action](rt)
    if res.tasks == 0:  # nothing to do with this action right now: a (small) negative experience for it
        store.learn(ctx.db, agent.id, "", action, -0.05)
    cpu = time.process_time() - t0
    ctx.db.execute(
        "UPDATE agents SET steps=steps+1, cpu_total=cpu_total+?, updated=? WHERE id=?", (cpu, time.time(), agent.id)
    )
    return {"agent": agent.name, "action": action, "tasks": res.tasks, "reward": round(res.reward, 3),
            "answered": answered, "notes": res.notes}  # fmt: skip


def step_job(ctx: JobContext) -> JobOutcome:
    """Job ``agents.step``: give agents turns until the slice budget is used (at least one turn)."""
    rng = random.Random()
    agents = store.active(ctx.db)
    turns: list[dict[str, Any]] = []
    total = 0.0
    first = True
    while agents and (first or not ctx.should_stop()):
        first = False
        agent = choose_agent(ctx.db, agents, rng)
        if agent is None:
            break
        out = run_agent(ctx, agent, rng)
        turns.append(out)
        total += max(0.0, float(out["reward"]))
        agents = [a for a in agents if a.id != agent.id]  # each agent at most once per slice
        ctx.tick()
    return JobOutcome(done=True, value=total, result={"turns": turns})


# ------------------------------------------------------------------ verification of delayed tasks
def _verify_predict(db: Any, t: Any, p: dict[str, Any]) -> bool | None:
    objs = {
        int(r["o"])
        for r in db.query(
            "SELECT o FROM triples WHERE s=? AND p=? AND o != 0 AND status != 'disputed' AND created > ?",
            (p["s"], p["p"], float(t["created"])),
        )
    }
    if not objs:
        return None
    return int(p["choice"]) in objs


def _verify_dispute(db: Any, t: Any, p: dict[str, Any]) -> bool | None:
    """A hidden copy decides only what it can: matching *any* hidden value is right, and missing all of them is
    wrong only for a single-valued (functional) relation. Otherwise wait for the evidence to settle."""
    from polymath.reasoning.contradictions import functional_predicates_all

    chosen = db.one("SELECT o, value FROM triples WHERE id=?", (p["choice"],))
    if chosen is None:
        return False  # the chosen claim was retracted
    hidden = {(int(h["o"]), str(h["value"])) for h in db.query(
        "SELECT o, value FROM triples WHERE s=? AND p=? AND holdout=1", (p["s"], p["p"]))}  # fmt: skip
    if (int(chosen["o"]), str(chosen["value"])) in hidden:
        return True
    functional = int(p["p"]) in functional_predicates_all(db)
    if hidden and functional:
        return False
    if not functional:  # several values can be true: right if the chosen claim survives as accepted
        status = db.scalar("SELECT status FROM triples WHERE id=?", (p["choice"],))
        return None if status == "disputed" else status in {"sourced", "inferred"}
    group = db.query(
        "SELECT id, status, confidence FROM triples WHERE s=? AND p=? AND holdout=0 AND "
        "status IN ('sourced','disputed')",
        (p["s"], p["p"]),
    )
    if any(g["status"] == "disputed" for g in group) or not group:
        return None  # still open
    winner = max(group, key=lambda g: float(g["confidence"]))
    return int(winner["id"]) == int(p["choice"])


def verify_job(ctx: JobContext) -> JobOutcome:
    """Job ``agents.verify``: settle open tasks whose truth has become known."""
    now = time.time()
    settled = {"correct": 0, "wrong": 0, "expired": 0, "read_rewards": 0}
    rows = ctx.db.query(
        "SELECT * FROM agent_tasks WHERE state='done' AND verify_after IS NOT NULL AND verify_after <= ? "
        "ORDER BY verify_after LIMIT 2000",
        (now,),
    )
    for t in rows:
        p = json.loads(t["payload"])
        kind = str(t["kind"])
        verdict: bool | None = None
        if kind == "predict":
            verdict = _verify_predict(ctx.db, t, p)
            amount = REWARDS["predict_correct"] if verdict else REWARDS["predict_wrong"]
            why = "the dumps confirmed it" if verdict else "the dumps say otherwise"
        elif kind == "dispute":
            verdict = _verify_dispute(ctx.db, t, p)
            amount = REWARDS["dispute_correct"] if verdict else REWARDS["dispute_wrong"]
            why = "judged right" if verdict else "judged wrong"
        elif kind == "read":
            titles = p.get("titles", [])
            q = ",".join("?" * len(titles))
            got = int(ctx.db.scalar(
                f"SELECT COUNT(*) FROM documents WHERE source='wikipedia' AND title IN ({q})", titles, 0
            )) if titles else 0  # fmt: skip
            if got:
                store.reward(ctx.db, int(t["id"]), REWARDS["read_doc"] * got, f"{got}/{len(titles)} articles arrived",
                             correct=True)  # fmt: skip
                settled["read_rewards"] += 1
                continue
            amount, why = 0.0, ""
        else:
            continue
        if verdict is None:
            if now - float(t["created"]) > TASK_EXPIRY.get(kind, 30 * 86400.0):
                store.expire(ctx.db, int(t["id"]), "never settled")
                settled["expired"] += 1
            else:
                ctx.db.execute("UPDATE agent_tasks SET verify_after=? WHERE id=?", (now + 3600, t["id"]))
            continue
        store.reward(ctx.db, int(t["id"]), amount, why, correct=verdict)
        settled["correct" if verdict else "wrong"] += 1
        ctx.tick()
    value = 0.1 * settled["correct"]
    return JobOutcome(done=True, value=value, result=settled)


# ------------------------------------------------------------------ evolution
MUTATIONS = {
    "min_conf": (0.1, 0.9, 0.1, "add"),
    "batch": (2, 20, 2, "add"),
    "breadth": (0.25, 2.0, 0.3, "mul"),
    "explore": (0.3, 3.0, 0.3, "mul"),
}


def mutate(params: dict[str, Any], rng: random.Random) -> dict[str, Any]:
    child = dict(params)
    for key, (lo, hi, step, how) in MUTATIONS.items():
        v = float(child.get(key, store.DEFAULT_PARAMS[key]))
        v = v + rng.uniform(-step, step) if how == "add" else v * math.exp(rng.uniform(-step, step))
        v = min(hi, max(lo, v))
        child[key] = round(v) if key == "batch" else round(v, 3)
    if child.get("weights"):
        child["weights"] = {m: round(max(0.0, w * math.exp(rng.gauss(0, 0.2))), 4) for m, w in child["weights"].items()}
    child.pop("cursor", None)
    return child


def _judged(a: Agent) -> int:
    return a.tasks_correct + a.tasks_wrong


def evolve_job(ctx: JobContext) -> JobOutcome:
    """Job ``agents.evolve`` (daily): fork strong agents, settle parent/child contests."""
    cfg = ctx.config.agents
    rng = random.Random()
    out: dict[str, list[str]] = {"forked": [], "retired": [], "adopted": [], "struggling": []}
    agents = store.active(ctx.db)
    by_id = {a.id: a for a in agents}
    need = cfg.min_tasks_to_judge
    for child in [a for a in agents if a.origin == "evolved" and a.parent in by_id]:
        parent = by_id[child.parent]  # type: ignore[index]
        if _judged(child) < need:
            continue
        c_rate, c_sd, _ = reward_rate(ctx.db, child)
        p_rate, p_sd, _ = reward_rate(ctx.db, parent)
        margin = 2 * math.hypot(c_sd, p_sd)
        if c_rate + margin < p_rate:
            store.set_status(ctx.db, child.id, "retired", f"out-performed by {parent.name}")
            out["retired"].append(child.name)
        elif c_rate - margin > p_rate:
            if parent.origin == "user":  # the user's agent keeps its identity and adopts the better genes
                genes = {k: v for k, v in child.params.items() if k != "cursor"}
                store.save_params(ctx.db, parent.id, {**parent.params, **genes})
                store.set_status(ctx.db, child.id, "retired", f"genes adopted by {parent.name}")
                out["adopted"].append(f"{parent.name}←{child.name}")
            else:
                store.set_status(ctx.db, parent.id, "retired", f"out-performed by {child.name}")
                out["retired"].append(parent.name)
    if cfg.evolve:
        agents = store.active(ctx.db)
        living_children = {a.parent for a in agents if a.origin == "evolved"}
        for a in sorted(agents, key=lambda x: -x.reward_total):
            if len(store.active(ctx.db)) >= cfg.max_agents:
                break
            rate, _sd, _n = reward_rate(ctx.db, a)
            if a.id in living_children or _judged(a) < need or rate <= 0:
                continue
            kid = store.create(
                ctx.db, kind=a.kind, directive=a.directive, subject=a.subject, scope=a.scope,
                name=f"{a.name}-g{a.generation + 1}", params=mutate(a.params, rng), parent=a.id,
                generation=a.generation + 1, origin="evolved",
            )  # fmt: skip
            forked = store.get(ctx.db, kid)
            if forked is not None:
                store.refresh_scope(ctx.db, forked)
                out["forked"].append(forked.name)
    for a in store.active(ctx.db):
        if a.origin == "user" and _judged(a) >= need and reward_rate(ctx.db, a)[0] < 0:
            out["struggling"].append(a.name)
    return JobOutcome(done=True, value=0.05 * len(out["forked"]), result=out)


def planner(agent: Any) -> None:
    s = agent.scheduler
    if not agent.db.scalar("SELECT 1 FROM agents WHERE status='active' LIMIT 1"):
        return
    s.ensure_recurring("agents.step", agent.config.agents.step_interval, priority=1.4)
    s.ensure_recurring("agents.verify", 900, priority=1.5)
    s.ensure_recurring("agents.evolve", 86400, priority=1.2)
