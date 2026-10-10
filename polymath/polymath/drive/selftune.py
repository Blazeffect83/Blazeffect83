"""Self-tuning: it changes its own settings when a test shows the change is better, and undoes it if it is not.

Many of its settings are numbers someone picked. ``self.tune`` (every 8 hours) takes one of them (``KNOBS``) and
runs a **paired** test: the current value and the candidates answer the *same* questions, so the comparison is
fair. A candidate is adopted only when its average gain exceeds twice its standard error (and a minimum effect).
Otherwise nothing changes, and the trial is still recorded. A trial runs in short slices like every other job: its
questions and the answers so far are the job's checkpoint, so it pauses for anything more urgent and survives a
restart.

* ``link.subjects``: similar subjects compared when predicting a fact. Tested by the log-likelihood the
  *association* clue (the one this setting controls) gives the right answer on hidden-fact questions. It is judged on
  its own, because the combined answer can ignore a weak clue entirely. A better clue then earns its weight back
  when the evidence weights are re-learned.
* ``link.features``: a subject's own facts used as clues. Tested the same way.

  Both are tested against half and double their current value, so over several trials they climb to the best value.
* ``predict.min_confidence``: how sure it must be before guessing a missing fact. Tested by the reward of guessing on
  hidden-fact questions (+2 right, −0.8 wrong, 0 abstain).
* ``infer.min_confidence``: how sure a reasoning step must be to keep its conclusion. Set from the measured
  precision of its conclusions (``reason.audit``).

**Safety net.** An adopted change stays *watching* until two self-tests (``eval.quiz``) have run after it. If the
accuracy then falls below the accuracy before the change (by 5 points, or two standard errors), the old value comes
back, and the changelog and feed say so. Values never leave each knob's bounds, and the code never changes: only
these numbers do. You can overrule it: ``polymath changes --reset NAME`` (or ``all``) puts a setting back to its
default and pins it, and ``--allow NAME`` lets it tune that setting again.
"""

from __future__ import annotations

import json
import math
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from polymath.core.db import Database
from polymath.core.jobs import JobContext, JobOutcome
from polymath.drive import changelog

KV = "tuned"
GUARD_QUIZZES = 2
GUARD_DROP = 0.05
SAMPLE = 60
_cache: dict[int, tuple[float, dict[str, Any]]] = {}
CACHE_S = 60.0


def tuned(db: Database, name: str, default: Any) -> Any:
    """The tuned value of a knob (its default until tuning changes it). Cached for a minute per database."""
    now = time.monotonic()
    hit = _cache.get(id(db))
    if hit is None or now - hit[0] > CACHE_S:
        try:
            values = db.kv_get(KV) or {}
        except Exception:  # an old database without kv access: defaults
            values = {}
        hit = (now, values if isinstance(values, dict) else {})
        _cache[id(db)] = hit
    return hit[1].get(name, default)


def set_value(db: Database, name: str, value: Any) -> None:
    values = dict(db.kv_get(KV) or {})
    values[name] = value
    db.kv_set(KV, values)
    _cache.pop(id(db), None)


# ------------------------------------------------------------------ paired tests
@dataclass
class Trial:
    knob: str
    current: Any
    best: Any
    before: float  # score of the current value
    after: float  # score of the best candidate
    n: int
    se: float  # standard error of the paired difference
    unit: str

    @property
    def gain(self) -> float:
        return self.after - self.before

    def significant(self, min_effect: float) -> bool:
        return self.best != self.current and self.gain > max(min_effect, 2 * self.se)


def paired(a: list[float], b: list[float]) -> tuple[float, float]:
    """Mean of b − a and its standard error."""
    d = [y - x for x, y in zip(a, b, strict=True)]
    if len(d) < 2:
        return 0.0, float("inf")
    mean = sum(d) / len(d)
    var = sum((x - mean) ** 2 for x in d) / (len(d) - 1)
    return mean, math.sqrt(var / len(d))


def questions(db: Database, size: int, seed: int) -> list[dict[str, Any]]:
    """Hidden-fact questions from visible facts (each fact is hidden from every method while it is asked)."""
    from polymath.evaluation.quiz import make_question, questionable_sql

    rng = random.Random(seed)
    p31 = db.scalar("SELECT id FROM predicates WHERE key='P31'")
    out = []
    for t in db.query(f"{questionable_sql(holdout=False)} ORDER BY RANDOM() LIMIT ?", (size * 4,)):
        q = make_question(db, t, rng, int(p31) if p31 is not None else None)
        if q is not None:
            out.append(q)
        if len(out) >= size:
            break
    return out


LINK_CHOICES = {"link.subjects": (100, 200, 400, 800), "link.features": (5, 10, 20, 40, 80)}


def neighbours(knob: str, current: int) -> list[int]:
    """Half and double the current value (when allowed): each trial is one step of a hill climb."""
    ladder = LINK_CHOICES[knob]
    if current not in ladder:
        return [v for v in ladder if v != current][:2]
    i = ladder.index(current)
    return [ladder[j] for j in (i - 1, i + 1) if 0 <= j < len(ladder)]


QUESTION_KEYS = ("triple_id", "subject", "predicate", "answer", "options")


def start_trial(db: Database, knob: str, seed: int) -> dict[str, Any] | None:
    """The plan of a trial: its questions and the values to test (None when there is not enough data yet).

    The plan is plain JSON, so a trial survives being paused between job slices and restarts.
    """
    if knob == "infer.min_confidence":
        return {"knob": knob}  # read off the rule audit: nothing to work through
    qs = questions(db, SAMPLE, seed)
    if len(qs) < 20:
        return None
    if knob in LINK_CHOICES:
        current = int(tuned(db, knob, KNOBS[knob].default))
        values: list[Any] = [current, *neighbours(knob, current)]
    else:
        values = ["answers"]  # one pass; every threshold is scored from the same answers
    return {"knob": knob, "qs": [{k: q[k] for k in QUESTION_KEYS} for q in qs], "values": values, "scores": {}}


def advance(db: Database, state: dict[str, Any], should_stop: Callable[[], bool], tick: Callable[[], None]) -> bool:
    """Answer the trial's questions, one at a time; False when it has to pause (``state`` keeps the progress).

    Each call makes at least one step, so a trial always finishes however short the slices are.
    """
    from polymath.memory.text_index import TextIndex
    from polymath.reasoning.link_prediction import LinkPredictor

    knob, qs = state["knob"], state.get("qs")
    if not qs:
        return True
    text = TextIndex(db)
    stepped = False
    for v in state["values"]:
        done = state["scores"].setdefault(str(v), [])
        if len(done) >= len(qs):
            continue
        if knob == "link.subjects":
            lp = LinkPredictor(db, text=text, subjects=int(v))
        elif knob == "link.features":
            lp = LinkPredictor(db, text=text, features=int(v))
        else:
            lp = LinkPredictor(db, text=text)
        for q in qs[len(done) :]:
            if stepped and should_stop():
                return False
            stepped = True
            if knob in LINK_CHOICES:  # log-probability the association clue gives the right answer
                probs = lp.association(q["subject"], q["predicate"], q["options"], q["triple_id"])
                p_right = probs.get(q["answer"], 0.0) if probs else 1 / len(q["options"])
                done.append(round(math.log(max(p_right, 1e-6)), 6))
            else:  # (confidence, right, had a view)
                pred = lp.predict(q["subject"], q["predicate"], q["options"], exclude=q["triple_id"])
                done.append([round(pred.confidence, 6), pred.best == q["answer"], pred.method != "none"])
            tick()
    return True


def best_of(base: list[float], candidates: dict[Any, list[float]], current: Any) -> tuple[Any, list[float], float]:
    """The candidate with the best lower bound (gain − 2·SE) over the current value's scores."""
    best, best_scores, best_bound, best_se = current, base, -math.inf, 0.0
    for v, scores in candidates.items():
        gain, se = paired(base, scores)
        if gain - 2 * se > best_bound:
            best, best_scores, best_bound, best_se = v, scores, gain - 2 * se, se
    return best, best_scores, best_se


def finish(db: Database, state: dict[str, Any]) -> Trial | None:
    """Score a worked-through trial."""
    knob = state["knob"]
    if knob == "infer.min_confidence":
        return infer_trial(db)
    qs, scores = state["qs"], state["scores"]
    if knob in LINK_CHOICES:
        current = state["values"][0]
        base = scores[str(current)]
        others = {v: scores[str(v)] for v in state["values"][1:]}
        unit = "log-likelihood of the association clue"
    else:
        current = float(tuned(db, knob, KNOBS[knob].default))
        answers = scores["answers"]

        def rewards(t: float) -> list[float]:
            return [(2.0 if right else -0.8) if spoke and conf >= t else 0.0 for conf, right, spoke in answers]

        base = rewards(current)
        others = {t: rewards(t) for t in KNOBS[knob].grid() if abs(t - current) > 1e-9}
        unit = "reward per question"
    best, best_scores, se = best_of(base, others, current)
    return Trial(knob, current, best, sum(base) / len(base), sum(best_scores) / len(best_scores), len(qs), se, unit)


def run_trial(db: Database, knob: str, tick: Callable[[], None]) -> Trial | None:
    """A whole trial in one go (tests and the command line)."""
    state = start_trial(db, knob, seed=int(time.time() // 86400))
    if state is None:
        return None
    advance(db, state, lambda: False, tick)
    return finish(db, state)


def infer_trial(db: Database) -> Trial | None:
    """The lowest threshold whose kept conclusions are at least ``TARGET`` precise, from the rule audit."""
    buckets = db.kv_get("rule_audit_buckets") or {}
    current = float(tuned(db, "infer.min_confidence", KNOBS["infer.min_confidence"].default))
    rows = sorted((float(k), int(v[0]), int(v[1])) for k, v in buckets.items())  # (lower edge, confirmed, refuted)
    if sum(c + r for _k, c, r in rows) < 30:
        return None

    def precision_from(t: float) -> tuple[float, int]:
        c = sum(cc for k, cc, _r in rows if k >= t - 1e-9)
        r = sum(rr for k, _c, rr in rows if k >= t - 1e-9)
        return ((c + 1) / (c + r + 2), c + r)

    best = current
    for t in KNOBS["infer.min_confidence"].grid():
        prec, n = precision_from(t)
        if n >= 20 and prec >= TARGET_PRECISION:
            best = t
            break
    before, n_before = precision_from(current)
    after, _n = precision_from(best)
    se = math.sqrt(max(before * (1 - before), 1e-6) / max(n_before, 1))
    return Trial("infer.min_confidence", current, best, before, after, n_before, se, "precision of conclusions")


TARGET_PRECISION = 0.8


@dataclass(frozen=True)
class Knob:
    default: Any
    lo: float
    hi: float
    step: float
    label: str
    min_effect: float

    def grid(self) -> list[float]:
        n = round((self.hi - self.lo) / self.step)
        return [round(self.lo + i * self.step, 3) for i in range(n + 1)]


KNOBS: dict[str, Knob] = {
    "link.subjects": Knob(400, 100, 800, 100, "how many similar subjects it compares when predicting", 0.02),
    "link.features": Knob(40, 5, 80, 5, "how many of a subject's facts it uses as clues", 0.02),
    "predict.min_confidence": Knob(0.4, 0.3, 0.8, 0.05, "how sure it must be before guessing a fact", 0.03),
    "infer.min_confidence": Knob(0.3, 0.3, 0.7, 0.05, "how sure a reasoning step must be to keep its conclusion", 0.03),
}


def fmt(v: Any) -> str:
    return f"{v:g}" if isinstance(v, float) else str(v)


# ------------------------------------------------------------------ the safety net
def quiz_accuracy(db: Database, after: float = 0.0, before: float | None = None, last: int = 3) -> tuple[float, int]:
    """Mean accuracy of the last ``last`` quizzes before ``before`` (or the first ones after ``after``)."""
    if before is not None:
        rows = db.query("SELECT accuracy FROM quizzes WHERE created < ? ORDER BY id DESC LIMIT ?", (before, last))
    else:
        rows = db.query("SELECT accuracy FROM quizzes WHERE created > ? ORDER BY id LIMIT ?", (after, last))
    acc = [float(r["accuracy"]) for r in rows]
    return (sum(acc) / len(acc) if acc else 0.0), len(acc)


def guard(db: Database) -> dict[str, int]:
    """Settle watching changes: keep them, or roll them back when self-test accuracy dropped after them."""
    out = {"kept": 0, "rolled_back": 0}
    for ch in db.query("SELECT * FROM self_changes WHERE state = 'watching' AND area = 'tuning' ORDER BY id"):
        detail = json.loads(ch["detail"] or "{}")
        base = detail.get("quiz_before")
        after, n = quiz_accuracy(db, after=float(ch["at"]), last=GUARD_QUIZZES)
        if n < GUARD_QUIZZES:
            continue
        if base is not None and after < float(base) - GUARD_DROP:
            old = json.loads(ch["old"])
            set_value(db, str(ch["subject"]), old)
            db.execute("UPDATE self_changes SET state = 'final', detail = ? WHERE id = ?",
                       (json.dumps(detail | {"quiz_after": after, "outcome": "rolled back"}), ch["id"]))  # fmt: skip
            changelog.record(
                db, "tuning", str(ch["subject"]), "rolled_back",
                f"Undid a change to {KNOBS[str(ch['subject'])].label} (back to {fmt(old)}): self-test accuracy fell "
                f"from {float(base):.0%} to {after:.0%} after it",
                old=json.loads(ch["new"]), new=old, before=float(base), after=after,
            )  # fmt: skip
            out["rolled_back"] += 1
        else:
            db.execute("UPDATE self_changes SET state = 'final', detail = ? WHERE id = ?",
                       (json.dumps(detail | {"quiz_after": after, "outcome": "kept"}), ch["id"]))  # fmt: skip
            out["kept"] += 1
    return out


# ------------------------------------------------------------------ the job
def pinned(db: Database) -> set[str]:
    """Settings you reset by hand: it leaves them alone until you allow tuning again."""
    return set(db.kv_get("tune_pinned") or [])


def reset(db: Database, names: list[str]) -> list[str]:
    """Put settings back to their defaults and stop tuning them (``polymath changes --reset``)."""
    values = dict(db.kv_get(KV) or {})
    names = list(KNOBS) if names == ["all"] else names
    unknown = [n for n in names if n not in KNOBS]
    if unknown:
        raise ValueError(f"unknown setting {', '.join(unknown)}; settings: {', '.join(KNOBS)}")
    done = []
    for name in names:
        old = values.pop(name, None)
        for ch in db.query("SELECT id, detail FROM self_changes WHERE state = 'watching' AND subject = ?", (name,)):
            detail = json.loads(ch["detail"] or "{}") | {"outcome": "reset by you"}
            db.execute(
                "UPDATE self_changes SET state = 'final', detail = ? WHERE id = ?", (json.dumps(detail), ch["id"])
            )
        if old is not None:
            changelog.record(db, "tuning", name, "reset",
                             f"You reset {KNOBS[name].label} to {fmt(KNOBS[name].default)} (it was {fmt(old)}); "
                             "it will not tune it again until you allow it",
                             old=old, new=KNOBS[name].default)  # fmt: skip
        done.append(name)
    db.kv_set(KV, values)
    db.kv_set("tune_pinned", sorted(pinned(db) | set(done)))
    _cache.pop(id(db), None)
    return done


def unpin(db: Database, names: list[str]) -> list[str]:
    """Allow tuning settings again."""
    keep = set() if names == ["all"] else pinned(db) - set(names)
    freed = sorted(pinned(db) - keep)
    db.kv_set("tune_pinned", sorted(keep))
    return freed


def pick_knob(db: Database) -> str | None:
    """The next knob in turn that is not under watch, and that you did not pin."""
    watching = {str(r["subject"]) for r in db.query("SELECT subject FROM self_changes WHERE state = 'watching'")}
    watching |= pinned(db)
    names = list(KNOBS)
    start = int(db.kv_get("tune_cursor") or 0)
    for k in range(len(names)):
        knob = names[(start + k) % len(names)]
        if knob not in watching:
            db.kv_set("tune_cursor", (start + k + 1) % len(names))
            return knob
    return None


def conclude(db: Database, trial: Trial) -> bool:
    """Adopt a clearly better value (watched by the guard), or record that the trial changed nothing."""
    k_def = KNOBS[trial.knob]
    if trial.significant(k_def.min_effect):
        quiz_before, _n = quiz_accuracy(db, before=time.time())
        set_value(db, trial.knob, trial.best)
        changelog.record(
            db, "tuning", trial.knob, "adopted",
            f"Changed {k_def.label} from {fmt(trial.current)} to {fmt(trial.best)}: on {trial.n} test questions "
            f"the {trial.unit} went from {trial.before:.3g} to {trial.after:.3g}",
            old=trial.current, new=trial.best, before=trial.before, after=trial.after, state="watching",
            detail={"quiz_before": quiz_before, "se": trial.se, "unit": trial.unit},
        )  # fmt: skip
        return True
    changelog.record(
        db, "tuning", trial.knob, "rejected",
        f"Tried {k_def.label} = {fmt(trial.best)} instead of {fmt(trial.current)}: no clear gain "
        f"({trial.before:.3g} → {trial.after:.3g} {trial.unit}, {trial.n} questions); kept {fmt(trial.current)}",
        old=trial.current, new=trial.best, before=trial.before, after=trial.after, feed=False,
    )  # fmt: skip
    return False


def tune_job(ctx: JobContext) -> JobOutcome:
    """Job ``self.tune``: settle earlier changes, then test one knob and adopt a clearly better value.

    The test runs across as many slices as it needs: the checkpoint holds its questions and the answers so far.
    """
    db = ctx.db
    cp = dict(ctx.job.checkpoint or {})
    state = cp.get("trial")
    result: dict[str, Any] = dict(cp.get("result") or {})
    if state is None:
        result = {**guard(db), "knob": None, "adopted": False}
        knob = pick_knob(db)
        if knob is None:
            result["skipped"] = "every setting is under watch or pinned"
            return JobOutcome(done=True, value=0.01, result=result)
        result["knob"] = knob
        state = start_trial(db, knob, seed=int(time.time() // 86400))
        if state is None:
            result["skipped"] = "not enough data yet"
            return JobOutcome(done=True, value=0.01, result=result)
    if not advance(db, state, ctx.should_stop, ctx.tick):
        answered = sum(len(v) for v in state["scores"].values())
        return JobOutcome(done=False, checkpoint={"trial": state, "result": result},
                          value=0.0, result=result | {"answered": answered})  # fmt: skip
    trial = finish(db, state)
    if trial is None:
        result["skipped"] = "not enough data yet"
        return JobOutcome(done=True, value=0.01, result=result)
    result |= {"current": trial.current, "best": trial.best, "gain": round(trial.gain, 4), "n": trial.n}
    result["adopted"] = conclude(db, trial)
    return JobOutcome(done=True, value=0.3 if result["adopted"] else 0.05, result=result)


def planner(agent: Any) -> None:
    if agent.db.scalar("SELECT 1 FROM quizzes LIMIT 1"):
        agent.scheduler.ensure_recurring("self.tune", 8 * 3600, priority=1.0)
