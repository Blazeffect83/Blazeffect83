"""What agents can do. Every action produces *tasks* whose correctness can be checked without trusting the agent:

=============  =============================================  ==========================================
action         the task                                       how it is judged (and when)
=============  =============================================  ==========================================
quiz           answer a 4-option question about a hidden      immediately, against the hidden fact
               (held-out) fact in scope
predict        guess a missing fact in scope (or abstain      later: when the dumps deliver that fact,
               when unsure)                                   right or wrong
dispute        judge a disputed fact in scope                 later: against a hidden copy, or when the
                                                              evidence settles the dispute
read           fetch the most important unread articles       later: did the articles arrive?
               about the scope from the dump
scan           find newly read documents about the subject    immediately: the entity linker must find
               (the watch digest)                             an in-scope entity in the document
calibrate      re-learn its own evidence weights on its       immediately: improvement in held-out-style
               scope                                          log-likelihood over its current weights
answer         answer a question you gave it                  your 👍 / 👎 (`polymath agents feedback`)
=============  =============================================  ==========================================

Rewards are deliberately asymmetric so that guessing does not pay (a random 4-way
guess has negative expected reward) and abstaining is a real choice.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from polymath.agents import store
from polymath.agents.store import REWARDS, Agent
from polymath.core.config import Config
from polymath.core.db import Database
from polymath.core.scheduler import Scheduler

REPERTOIRE: dict[str, list[str]] = {
    "research": ["quiz", "predict", "read", "calibrate"],
    "predict": ["predict", "quiz", "calibrate"],
    "verify": ["dispute", "quiz", "calibrate"],
    "watch": ["scan", "read"],
    "answer": ["read", "quiz"],
}
PREDICT_HORIZON = 60 * 86400.0  # open predictions expire (unrewarded) if the dumps never settle them
READ_CHECK = 1800.0


@dataclass
class Runtime:
    db: Database
    scheduler: Scheduler
    config: Config
    services: dict[str, Any]
    agent: Agent
    rng: random.Random
    tick: Callable[[], None] = lambda: None
    _predictor: Any = None

    def predictor(self) -> Any:
        if self._predictor is None:
            from polymath.reasoning.link_prediction import LinkPredictor, WordVectors

            model = self.services.get("sgns")
            vectors = WordVectors.from_model(model[0]) if model else None
            self._predictor = LinkPredictor(self.db, vectors=vectors, weights=self.agent.params.get("weights") or None)
        return self._predictor


@dataclass
class StepResult:
    action: str
    tasks: int = 0
    reward: float = 0.0  # immediate, verified reward
    notes: list[str] = field(default_factory=list)


def _scope_filter(agent: Agent, col: str = "t.s") -> tuple[str, tuple[Any, ...]]:
    preds = [int(p) for p in agent.scope.get("predicates", [])]
    cond = f"{col} IN (SELECT entity_id FROM agent_scope WHERE agent_id=?)"
    params: tuple[Any, ...] = (agent.id,)
    if preds:
        cond = f"({cond} OR t.p IN ({','.join('?' * len(preds))}))"
        params += tuple(preds)
    return cond, params


# ------------------------------------------------------------------ quiz (immediate verification)
def quiz(rt: Runtime) -> StepResult:
    from polymath.evaluation.quiz import make_question, questionable_sql

    res = StepResult("quiz")
    a = rt.agent
    cond, params = _scope_filter(a)
    rows = rt.db.query(
        f"{questionable_sql(holdout=True)} AND {cond} AND NOT EXISTS (SELECT 1 FROM agent_tasks k WHERE "
        "k.agent_id=? AND k.kind='quiz' AND k.target='quiz:' || t.id) ORDER BY RANDOM() LIMIT 500",
        (*params, a.id),
    )
    p31 = rt.db.scalar("SELECT id FROM predicates WHERE key='P31'")
    for t in rows:
        q = make_question(rt.db, t, rt.rng, int(p31) if p31 is not None else None)
        if q is None:
            continue
        pred = rt.predictor().predict(q["subject"], q["predicate"], q["options"])
        ok = pred.best == q["answer"]
        tid = store.open_task(
            rt.db, a, kind="quiz", action="quiz", target=f"quiz:{q['triple_id']}",
            payload={"question": q["question"], "options": q["labels"], "chosen": pred.best,
                     "confidence": round(pred.confidence, 3), "evidence": pred.method},
        )  # fmt: skip
        if tid is None:
            continue
        amount = REWARDS["quiz_correct"] if ok else REWARDS["quiz_wrong"]
        store.reward(rt.db, tid, amount, "hidden fact matched" if ok else "hidden fact differs", correct=ok)
        res.tasks += 1
        res.reward += amount
        rt.tick()
        if res.tasks >= int(a.params["batch"]):
            break
    if not rows:
        res.notes.append("no unasked hidden facts in scope")
    elif not res.tasks:
        res.notes.append("hidden facts in scope lack enough sibling answers to ask about")
    return res


# ------------------------------------------------------------------ open predictions (delayed verification)
def _relations(rt: Runtime) -> list[int]:
    preds = [int(p) for p in rt.agent.scope.get("predicates", [])]
    if preds:
        return preds
    rows = rt.db.query(
        "SELECT t.p, COUNT(*) AS n FROM triples t JOIN agent_scope s ON s.entity_id=t.s AND s.agent_id=? "
        "JOIN predicates p ON p.id=t.p WHERE t.o != 0 AND t.holdout=0 AND p.key NOT IN ('P31','P279','P910','P1343') "
        "GROUP BY t.p ORDER BY n DESC LIMIT 4",
        (rt.agent.id,),
    )
    return [int(r["p"]) for r in rows if int(r["n"]) >= 5]


def predict(rt: Runtime) -> StepResult:
    res = StepResult("predict")
    a = rt.agent
    min_conf = float(a.params["min_conf"])
    abstained = 0
    for p in _relations(rt):
        options = [
            int(r["o"])
            for r in rt.db.query(
                "SELECT t.o, COUNT(*) AS n FROM triples t JOIN agent_scope s ON s.entity_id=t.s AND s.agent_id=? "
                "JOIN entities e ON e.id=t.o WHERE t.p=? AND t.o != 0 AND t.holdout=0 AND e.label NOT GLOB 'Q[0-9]*' "
                "GROUP BY t.o ORDER BY n DESC LIMIT 4",
                (a.id, p),
            )
        ]
        if len(options) < 2:
            continue
        subjects = rt.db.query(
            "SELECT s.entity_id AS e FROM agent_scope s JOIN entities x ON x.id=s.entity_id WHERE s.agent_id=? AND "
            "x.kind='item' AND x.label NOT GLOB 'Q[0-9]*' AND NOT EXISTS (SELECT 1 FROM triples t "
            "WHERE t.s=s.entity_id "
            "AND t.p=?) AND NOT EXISTS (SELECT 1 FROM agent_tasks k WHERE k.agent_id=? AND k.kind='predict' AND "
            "k.target='predict:' || s.entity_id || ':' || ?) ORDER BY s.weight DESC LIMIT ?",
            (a.id, p, a.id, p, int(a.params["batch"]) * 3),
        )
        for r in subjects:
            s = int(r["e"])
            pred = rt.predictor().predict(s, p, options)
            if pred.confidence < min_conf or pred.method == "none":
                abstained += 1
                continue
            tid = store.open_task(
                rt.db, a, kind="predict", action="predict", target=f"predict:{s}:{p}",
                payload={"s": s, "p": p, "choice": pred.best, "options": options,
                         "confidence": round(pred.confidence, 3), "evidence": pred.method},
                verify_after=time.time(),
            )  # fmt: skip
            if tid is not None:
                res.tasks += 1
            rt.tick()
            if res.tasks >= int(a.params["batch"]):
                break
        if res.tasks >= int(a.params["batch"]):
            break
    res.notes.append(f"{res.tasks} predictions made, {abstained} abstained (min_conf {min_conf:.2f})")
    return res


# ------------------------------------------------------------------ disputes (delayed verification)
def dispute(rt: Runtime) -> StepResult:
    res = StepResult("dispute")
    a = rt.agent
    cond, params = _scope_filter(a)
    groups = rt.db.query(
        f"SELECT DISTINCT t.s, t.p FROM triples t WHERE t.status='disputed' AND {cond} AND NOT EXISTS "
        "(SELECT 1 FROM agent_tasks k WHERE k.agent_id=? AND k.kind='dispute' AND k.target='dispute:' || t.s || ':' "
        "|| t.p) LIMIT ?",
        (*params, a.id, int(a.params["batch"])),
    )
    for g in groups:
        s, p = int(g["s"]), int(g["p"])
        cands = rt.db.query(
            "SELECT id, o, value, confidence, n_sources FROM triples WHERE s=? AND p=? AND holdout=0 AND "
            "status IN ('disputed','sourced')",
            (s, p),
        )
        if len(cands) < 2:
            continue
        objects = [int(c["o"]) for c in cands if int(c["o"])]
        if len(objects) == len(cands):
            pred = rt.predictor().predict(s, p, objects)
            choice = next(int(c["id"]) for c in cands if int(c["o"]) == pred.best)
            conf, how = pred.confidence, pred.method
        else:  # literal values: weigh independent evidence (sources × confidence)
            best = max(cands, key=lambda c: (float(c["confidence"]) * (1 + int(c["n_sources"])), -int(c["id"])))
            choice, conf, how = int(best["id"]), float(best["confidence"]), "evidence"
        tid = store.open_task(
            rt.db, a, kind="dispute", action="dispute", target=f"dispute:{s}:{p}",
            payload={"s": s, "p": p, "choice": choice, "candidates": [int(c["id"]) for c in cands],
                     "confidence": round(conf, 3), "evidence": how},
            verify_after=time.time(),
        )  # fmt: skip
        if tid is not None:
            res.tasks += 1
        rt.tick()
    if not res.tasks:
        res.notes.append("no open disputes in scope")
    return res


# ------------------------------------------------------------------ reading (delayed verification)
def read(rt: Runtime) -> StepResult:
    res = StepResult("read")
    a = rt.agent
    titles = [
        str(r["wiki_title"])
        for r in rt.db.query(
            "SELECT e.wiki_title FROM agent_scope s JOIN entities e ON e.id=s.entity_id JOIN wiki_index w ON "
            "w.lang='en' AND w.title=e.wiki_title WHERE s.agent_id=? AND e.doc_id IS NULL ORDER BY s.weight DESC, "
            "e.pagerank DESC LIMIT ?",
            (a.id, int(a.params["batch"]) * 4),
        )
    ]
    if not titles:
        res.notes.append("nothing unread in the dump index for this scope")
        return res
    hour = int(time.time() // 3600)
    _jid, created = rt.scheduler.enqueue(
        "wikipedia.titles", {"titles": titles, "lang": "en"}, key=f"agent:{a.id}:read:{hour}", priority=2.0
    )
    if created:
        tid = store.open_task(
            rt.db, a, kind="read", action="read", target=f"read:{hour}", payload={"titles": titles},
            verify_after=time.time() + READ_CHECK,
        )  # fmt: skip
        res.tasks += int(tid is not None)
        res.notes.append(f"queued {len(titles)} articles")
    return res


# ------------------------------------------------------------------ watch digest (immediate verification)
def scan(rt: Runtime) -> StepResult:
    from polymath.memory.text_index import TextIndex

    res = StepResult("scan")
    a = rt.agent
    cursor = int(a.params.get("cursor", 0))
    newest = int(rt.db.scalar("SELECT MAX(id) FROM documents WHERE state='perceived'", default=0) or 0)
    if newest <= cursor:
        res.notes.append("nothing new has been read yet")
        return res
    linked = rt.db.query(
        "SELECT de.doc_id, COUNT(*) AS n, MAX(de.score) AS sc FROM doc_entities de JOIN agent_scope s ON "
        "s.entity_id=de.entity_id AND s.agent_id=? WHERE de.doc_id > ? AND de.doc_id <= ? GROUP BY de.doc_id "
        "ORDER BY n DESC LIMIT 200",
        (a.id, cursor, newest),
    )
    seen = set()
    for r in linked:
        d = int(r["doc_id"])
        seen.add(d)
        doc = rt.db.one("SELECT title, url, source FROM documents WHERE id=?", (d,))
        tid = store.open_task(
            rt.db, a, kind="digest", action="scan", target=f"digest:{d}",
            payload={"doc_id": d, "title": doc["title"] if doc else "", "url": doc["url"] if doc else None,
                     "source": doc["source"] if doc else "", "entities": int(r["n"])},
        )  # fmt: skip
        if tid is not None:
            store.reward(rt.db, tid, REWARDS["digest_doc"], f"{int(r['n'])} in-scope entities found", correct=True)
            res.tasks += 1
            res.reward += REWARDS["digest_doc"]
    # keyword matches without an in-scope entity: kept for you to judge, unrewarded until you do
    for d, _score in TextIndex(rt.db).search_docs(a.subject, limit=50):
        if cursor < d <= newest and d not in seen:
            doc = rt.db.one("SELECT title, url, source FROM documents WHERE id=?", (d,))
            tid = store.open_task(
                rt.db, a, kind="digest", action="scan", target=f"digest:{d}",
                payload={"doc_id": d, "title": doc["title"] if doc else "", "url": doc["url"] if doc else None,
                         "source": doc["source"] if doc else "", "entities": 0},
            )  # fmt: skip
            res.tasks += int(tid is not None)
    a.params["cursor"] = newest
    store.save_params(rt.db, a.id, a.params)
    return res


# ------------------------------------------------------------------ self-improvement (immediate verification)
def calibrate(rt: Runtime) -> StepResult:
    from polymath.reasoning.link_prediction import DEFAULT_WEIGHTS, fit_weights, mean_loglik, sample_questions

    res = StepResult("calibrate")
    a = rt.agent
    cond, params = _scope_filter(a)
    pred = rt.predictor()
    samples = sample_questions(rt.db, pred, size=40, where=cond, params=params, tick=rt.tick)
    if len(samples) < 10:
        res.notes.append(f"only {len(samples)} visible facts in scope to learn from")
        return res
    current = dict(a.params.get("weights") or pred.weights or DEFAULT_WEIGHTS)
    fit = fit_weights(samples)
    before, after = mean_loglik(samples, current), mean_loglik(samples, fit["weights"])
    gain = after - before
    tid = store.open_task(
        rt.db, a, kind="calibrate", action="calibrate", target=f"calibrate:{int(time.time() // 3600)}",
        payload={"samples": len(samples), "before": round(before, 4), "after": round(after, 4)},
    )  # fmt: skip
    if tid is None:
        return res
    if gain > 0.005:
        a.params["weights"] = fit["weights"]
        store.save_params(rt.db, a.id, a.params)
        pred.weights = dict(fit["weights"])
    amount = round(min(1.0, max(0.0, gain) * 2), 4)
    store.reward(rt.db, tid, amount, f"log-likelihood {before:.3f} → {after:.3f}", correct=None)
    res.tasks, res.reward = 1, amount
    return res


ACTIONS: dict[str, Callable[[Runtime], StepResult]] = {
    "quiz": quiz,
    "predict": predict,
    "dispute": dispute,
    "read": read,
    "scan": scan,
    "calibrate": calibrate,
}


# ------------------------------------------------------------------ user tasks
def answer_pending(rt: Runtime) -> int:
    """Answer the questions you gave this agent (state pending → done, awaiting your verdict)."""
    from polymath.interface.answer import Answerer

    linker = rt.services.get("linker")
    n = 0
    for t in rt.db.query(
        "SELECT id, payload FROM agent_tasks WHERE agent_id=? AND kind='user' AND state='pending' ORDER BY id LIMIT 5",
        (rt.agent.id,),
    ):
        import json

        q = str(json.loads(t["payload"]).get("text", ""))
        ans = Answerer(rt.db, linker).ask(q)
        rt.db.execute(
            "UPDATE agent_tasks SET state='done', done_at=?, result=? WHERE id=?",
            (time.time(), json.dumps({**ans.to_dict(), "rendered": ans.render()}), t["id"]),
        )
        rt.db.execute("UPDATE agents SET tasks_done=tasks_done+1 WHERE id=?", (rt.agent.id,))
        n += 1
    return n
