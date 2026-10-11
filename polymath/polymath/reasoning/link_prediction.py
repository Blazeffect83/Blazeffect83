"""Link prediction: which of several candidate objects completes ``(subject, predicate, ?)``.

Used by the self-evaluation quiz (and available to answering). Five independent
evidence sources each turn the candidates into a probability distribution — or
abstain (uniform) when they have nothing to say:

* **graph** — the fact itself is known (sourced or inferred, never held out).
* **related** — other facts connect subject and candidate (inverse relations,
  part-of chains, …): the number of distinct predicates linking them.
* **association** — naive Bayes over the subject's other facts: subjects that
  share facts with this one tend to share this fact's object too
  (``P(o | p) · Π P(f | p, o)`` with Laplace smoothing, estimated from up to
  ``SUBJECTS`` other subjects per candidate).
* **text** — sentences the agent has read that mention subject and candidate
  together, divided by how common the candidate is (lift), with a bonus when
  the relation's name appears too.
* **embedding** — word-vector translation: the average offset
  ``v(object) − v(subject)`` over known facts of the predicate, applied to the
  subject (from the agent's own skip-gram vectors; labels are averaged words).

They are combined as a product of experts, ``score(o) = Σ_m w_m · log p_m(o)``.
The weights are *learned* (:func:`calibrate`): the agent quizzes itself on facts
it is allowed to see, hiding each fact from every method while it is asked
(leave-one-out), and fits the weights by maximum likelihood. Evidence that
predicts well gains weight; evidence that misleads loses it.
"""

from __future__ import annotations

import math
import random
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from polymath.core.db import Database
from polymath.memory.graph import norm_alias
from polymath.memory.text_index import TextIndex
from polymath.perception.stem import stem

METHODS = ("graph", "related", "association", "text", "embedding")
DEFAULT_WEIGHTS = {"graph": 3.0, "related": 1.0, "association": 1.0, "text": 1.0, "embedding": 0.5}
SUBJECTS = 400
FEATURES = 40
EPS = 1e-3
_WORD = re.compile(r"[^\W_]+", re.U)
_BARE_ID = re.compile(r"[QP]\d+")


def named(label: str) -> bool:
    """A real name, not a bare Wikidata id waiting for its record to be read."""
    return bool(label) and _BARE_ID.fullmatch(label) is None


@dataclass
class Prediction:
    best: int
    confidence: float
    method: str  # the evidence that contributed most to the choice ("none" when every method abstained)
    probs: dict[int, float] = field(default_factory=dict)
    by_method: dict[str, dict[int, float]] = field(default_factory=dict)


class WordVectors:
    """Unit word vectors from the agent's skip-gram model, averaged over a label's words."""

    def __init__(self, words: dict[str, int], unit: Any) -> None:
        self.words = words
        self.unit = unit

    @classmethod
    def from_model(cls, model: Any) -> WordVectors:
        return cls(dict(model.vocab.index), model.normalized())

    def label(self, text: str) -> Any:
        ids = [self.words[w] for w in (t.lower() for t in _WORD.findall(text)) if w in self.words]
        if not ids:
            return None
        v = self.unit[ids].mean(axis=0)
        n = float(np.linalg.norm(v))
        return v / n if n > 0 else None


class LinkPredictor:
    def __init__(
        self,
        db: Database,
        *,
        text: TextIndex | None = None,
        vectors: WordVectors | None = None,
        weights: dict[str, float] | None = None,
        subjects: int | None = None,
        features: int | None = None,
    ) -> None:
        self.db = db
        from polymath.drive.selftune import tuned

        self.subjects = int(subjects if subjects is not None else tuned(db, "link.subjects", SUBJECTS))
        self.features = int(features if features is not None else tuned(db, "link.features", FEATURES))
        self.text = text or TextIndex(db)
        self.vectors = vectors
        stored = db.kv_get("link_weights") or {}
        self.weights = dict(weights or stored.get("weights") or DEFAULT_WEIGHTS)
        self._offsets: dict[int, Any] = {}
        self._labels: dict[int, str] = {}

    # ------------------------------------------------------------- helpers
    def label(self, entity: int) -> str:
        if entity not in self._labels:
            self._labels[entity] = str(self.db.scalar("SELECT label FROM entities WHERE id=?", (entity,), default=""))
        return self._labels[entity]

    @staticmethod
    def _normalize(raw: dict[int, float]) -> dict[int, float] | None:
        total = sum(raw.values())
        if total <= 0:
            return None
        return {o: v / total for o, v in raw.items()}

    # ------------------------------------------------------------- methods
    def graph(self, s: int, p: int, options: list[int], exclude: int | None) -> dict[int, float] | None:
        """The fact is known. A hidden fact (held out, or the one being asked) counts only through evidence
        independent of its hidden source: re-derived by a rule, or read in text (pattern / infobox)."""
        raw = {}
        for o in options:
            t = self.db.one(
                "SELECT id, confidence, holdout FROM triples WHERE s=? AND p=? AND o=? AND status!='disputed'",
                (s, p, o),
            )
            if t is None:
                raw[o] = 0.0
            elif int(t["holdout"]) == 0 and int(t["id"]) != exclude:
                raw[o] = float(t["confidence"])
            else:
                independent = self.db.scalar(
                    "SELECT MAX(weight) FROM provenance WHERE triple_id=? AND kind != 'wikidata'", (int(t["id"]),)
                )
                raw[o] = 0.8 * min(1.0, float(independent)) if independent else 0.0
        return self._normalize(raw)

    def related(self, s: int, p: int, options: list[int], exclude: int | None) -> dict[int, float] | None:
        raw = {}
        for o in options:
            n = self.db.scalar(
                "SELECT COUNT(DISTINCT p) FROM triples WHERE ((s=? AND o=?) OR (s=? AND o=?)) AND holdout=0 "
                "AND status!='disputed' AND id != ? AND NOT (s=? AND p=? AND o=?)",
                (s, o, o, s, exclude or -1, s, p, o),
                default=0,
            )
            raw[o] = float(n)
        return self._normalize(raw)

    def association(self, s: int, p: int, options: list[int], exclude: int | None) -> dict[int, float] | None:
        feats = [
            (int(r["p"]), int(r["o"]))
            for r in self.db.query(
                "SELECT p, o FROM triples WHERE s=? AND p != ? AND o != 0 AND holdout=0 AND status!='disputed' "
                "AND id != ? LIMIT ?",
                (s, p, exclude or -1, self.features),
            )
        ]
        counts = {
            o: int(
                self.db.scalar(
                    "SELECT COUNT(*) FROM (SELECT 1 FROM triples WHERE o=? AND p=? AND s != ? AND holdout=0 LIMIT ?)",
                    (o, p, s, self.subjects * 10),
                    default=0,
                )
            )
            for o in options
        }
        if not any(counts.values()):
            return None
        total = sum(counts.values())
        logs: dict[int, float] = {}
        for o in options:
            score = math.log((counts[o] + 1) / (total + len(options)))  # popularity prior P(o | p)
            if feats and counts[o]:
                cond = " OR ".join(["(t.p=? AND t.o=?)"] * len(feats))
                rows = self.db.query(
                    f"SELECT t.p, t.o, COUNT(DISTINCT t.s) AS c FROM (SELECT s FROM triples WHERE o=? AND p=? AND "
                    f"s != ? AND holdout=0 LIMIT ?) x JOIN triples t ON t.s = x.s WHERE t.holdout=0 AND ({cond}) "
                    "GROUP BY t.p, t.o",
                    (o, p, s, self.subjects, *[v for f in feats for v in f]),
                )
                n = min(counts[o], self.subjects)
                seen = {(int(r["p"]), int(r["o"])): int(r["c"]) for r in rows}
                for f in feats:
                    score += math.log((seen.get(f, 0) + 0.1) / (n + 0.2))
            elif feats:
                score += len(feats) * math.log(0.1 / 0.2)  # never seen with this object: uninformed
            logs[o] = score
        top = max(logs.values())
        return self._normalize({o: math.exp(v - top) for o, v in logs.items()})

    def text_evidence(self, s: int, p: int, options: list[int]) -> dict[int, float] | None:
        s_label = self.label(s)
        if not named(s_label):
            return None
        s_norm = norm_alias(s_label)
        p_label = str(self.db.scalar("SELECT label FROM predicates WHERE id=?", (p,), default=""))
        rel = {stem(w) for w in norm_alias(p_label).split() if len(w) > 2} if named(p_label) else set()
        raw: dict[int, float] = {}
        for o in options:
            o_label = self.label(o)
            if not named(o_label):
                raw[o] = 0.0
                continue
            o_norm = norm_alias(o_label)
            together = 0.0
            for h in self.text.search(f"{s_label} {o_label}", limit=20, mode="and"):
                for sent in re.split(r"(?<=[.!?])\s+", h.text):
                    body = f" {norm_alias(sent)} "
                    if f" {s_norm} " in body and f" {o_norm} " in body:
                        words = {stem(w) for w in body.split()}
                        together += 1.0 + (1.0 if rel & words else 0.0)
            if together:
                freq = len(self.text.search(o_label, limit=50, mode="and"))
                raw[o] = together / math.sqrt(1.0 + freq)
            else:
                raw[o] = 0.0
        return self._normalize(raw)

    def _offset(self, p: int, exclude: int | None) -> Any:
        if p not in self._offsets:
            assert self.vectors is not None
            diffs = []
            for r in self.db.query(
                "SELECT id, s, o FROM triples WHERE p=? AND o != 0 AND holdout=0 AND status!='disputed' LIMIT 300",
                (p,),
            ):
                if int(r["id"]) == exclude:
                    continue
                vs, vo = self.vectors.label(self.label(int(r["s"]))), self.vectors.label(self.label(int(r["o"])))
                if vs is not None and vo is not None:
                    diffs.append(vo - vs)
            self._offsets[p] = np.mean(diffs, axis=0) if len(diffs) >= 5 else None
        return self._offsets[p]

    def embedding(self, s: int, p: int, options: list[int], exclude: int | None) -> dict[int, float] | None:
        if self.vectors is None:
            return None
        vs = self.vectors.label(self.label(s))
        r = self._offset(p, exclude)
        if vs is None or r is None:
            return None
        target = vs + r
        target = target / (np.linalg.norm(target) or 1.0)
        sims = {}
        for o in options:
            vo = self.vectors.label(self.label(o))
            sims[o] = float(vo @ target) if vo is not None else None
        known = [v for v in sims.values() if v is not None]
        if len(known) < 2:
            return None
        floor = min(known)
        return self._normalize({o: math.exp(8.0 * ((v if v is not None else floor) - 1.0)) for o, v in sims.items()})

    # -------------------------------------------------------------- combine
    def evidence(self, s: int, p: int, options: list[int], exclude: int | None = None) -> dict[str, dict[int, float]]:
        out: dict[str, dict[int, float]] = {}
        for name, dist in (
            ("graph", self.graph(s, p, options, exclude)),
            ("related", self.related(s, p, options, exclude)),
            ("association", self.association(s, p, options, exclude)),
            ("text", self.text_evidence(s, p, options)),
            ("embedding", self.embedding(s, p, options, exclude)),
        ):
            if dist is not None:
                out[name] = dist
        return out

    def combine(self, options: list[int], ev: dict[str, dict[int, float]]) -> Prediction:
        logit = {o: 0.0 for o in options}
        contrib: dict[str, dict[int, float]] = {}
        for m, dist in ev.items():
            w = self.weights.get(m, 0.0)
            contrib[m] = {o: w * math.log(dist.get(o, 0.0) + EPS) for o in options}
            for o in options:
                logit[o] += contrib[m][o]
        top = max(logit.values())
        expd = {o: math.exp(v - top) for o, v in logit.items()}
        z = sum(expd.values())
        probs = {o: v / z for o, v in expd.items()}
        best = max(options, key=lambda o: (probs[o], -options.index(o)))
        method = "none"
        if contrib:
            margins = {m: c[best] - sum(c.values()) / len(c) for m, c in contrib.items()}
            m_best = max(margins, key=lambda m: margins[m])
            method = m_best if margins[m_best] > 1e-9 else "none"
        return Prediction(best, probs[best], method, probs, ev)

    def predict(self, s: int, p: int, options: list[int], exclude: int | None = None) -> Prediction:
        return self.combine(options, self.evidence(s, p, options, exclude))


def fit_weights(
    samples: list[tuple[list[int], int, dict[str, dict[int, float]]]], *, iters: int = 300
) -> dict[str, Any]:
    """Maximum-likelihood weights for the product of experts (projected gradient ascent, w ≥ 0)."""
    w = np.array([DEFAULT_WEIGHTS[m] for m in METHODS], dtype=np.float64)
    if not samples:
        return {"weights": dict(zip(METHODS, w.tolist(), strict=True)), "n": 0}
    feats = []  # per question: matrix options × methods of log-probabilities, and the answer row
    for options, answer, ev in samples:
        mat = np.zeros((len(options), len(METHODS)))
        for j, m in enumerate(METHODS):
            if m in ev:
                mat[:, j] = [math.log(ev[m].get(o, 0.0) + EPS) for o in options]
        feats.append((mat, options.index(answer)))
    lr = 0.05
    for _ in range(iters):
        grad = np.zeros_like(w)
        for mat, a in feats:
            z = mat @ w
            pz = np.exp(z - z.max())
            pz /= pz.sum()
            grad += mat[a] - pz @ mat
        w = np.clip(w + lr * grad / len(feats), 0.0, 10.0)

    def loglik(weights: Any) -> float:
        total = 0.0
        for mat, a in feats:
            z = mat @ weights
            total += float(z[a] - z.max() - math.log(np.exp(z - z.max()).sum()))
        return total / len(feats)

    acc = float(np.mean([int(np.argmax(mat @ w) == a) for mat, a in feats]))
    return {
        "weights": {m: round(float(v), 4) for m, v in zip(METHODS, w, strict=True)},
        "n": len(feats),
        "loglik": round(loglik(w), 4),
        "chance_loglik": round(float(np.mean([-math.log(len(mat)) for mat, _a in feats])), 4),
        "train_accuracy": round(acc, 4),
    }


def sample_questions(
    db: Database,
    predictor: LinkPredictor,
    *,
    size: int = 120,
    seed: int | None = None,
    where: str = "",
    params: tuple[Any, ...] = (),
    tick: Callable[[], None] | None = None,
) -> list[tuple[list[int], int, dict[str, dict[int, float]]]]:
    """Leave-one-out training questions from *visible* facts (optionally restricted by ``where`` on ``t``)."""
    from polymath.evaluation.quiz import make_question, questionable_sql

    rng = random.Random(seed)
    p31 = db.scalar("SELECT id FROM predicates WHERE key='P31'")
    sql = questionable_sql(holdout=False) + (f" AND ({where})" if where else "")
    rows = db.query(f"{sql} ORDER BY RANDOM() LIMIT ?", (*params, size * 4))
    samples = []
    for t in rows:
        q = make_question(db, t, rng, int(p31) if p31 is not None else None)
        if q is None:
            continue
        ev = predictor.evidence(q["subject"], q["predicate"], q["options"], exclude=q["triple_id"])
        samples.append((q["options"], q["answer"], ev))
        if tick is not None:
            tick()  # long-running: keep the systemd watchdog fed
        if len(samples) >= size:
            break
    return samples


def mean_loglik(samples: list[tuple[list[int], int, dict[str, dict[int, float]]]], weights: dict[str, float]) -> float:
    """Average log-likelihood of the right answers under ``weights`` (higher is better; chance is −log(options))."""
    if not samples:
        return 0.0
    w = np.array([float(weights.get(m, 0.0)) for m in METHODS])
    total = 0.0
    for options, answer, ev in samples:
        mat = np.zeros((len(options), len(METHODS)))
        for j, m in enumerate(METHODS):
            if m in ev:
                mat[:, j] = [math.log(ev[m].get(o, 0.0) + EPS) for o in options]
        z = mat @ w
        a = options.index(answer)
        total += float(z[a] - z.max() - math.log(np.exp(z - z.max()).sum()))
    return total / len(samples)


def calibrate(
    db: Database,
    predictor: LinkPredictor,
    *,
    size: int = 120,
    seed: int | None = None,
    tick: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Learn method weights from visible facts, each hidden from every method while it is asked."""
    samples = sample_questions(db, predictor, size=size, seed=seed, tick=tick)
    fit = fit_weights(samples)
    db.kv_set("link_weights", fit)
    predictor.weights = dict(fit["weights"])
    return fit
