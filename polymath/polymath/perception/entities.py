"""Entity recognition (Aho–Corasick over learned aliases) and disambiguation.

Everything is learned from the agent's own data:

* **Aliases** — Wikidata labels/aliases, article titles, redirects and the anchor
  texts of Wikipedia links (with counts → the prior P(entity | surface)).
* **Keyphraseness** — how often a surface is a link where it occurs; suppresses
  linking of ordinary words ("may", "set").
* **Entity profiles** — tf–idf term vectors of each entity's article/description,
  compared with the mention's document context.
* **Coherence** — Milne–Witten relatedness (shared linking documents) and graph
  adjacency with the other confidently linked entities of the document.
* **Scorer** — a logistic regression over those features, trained by gradient
  descent in numpy on Wikipedia anchors (held-out documents choose the threshold).
"""

from __future__ import annotations

import json
import math
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

from polymath.core.db import Database
from polymath.memory.graph import KnowledgeGraph, norm_alias
from polymath.perception.ahocorasick import TokenAutomaton, longest_non_overlapping
from polymath.perception.stem import stem
from polymath.perception.tokenize import tokenize

FEATURES = [
    "bias",
    "log_prior",
    "top_prior",
    "context",
    "coherence",
    "log_keyphrase",
    "capitalised",
    "exact_label",
    "log_candidates",
    "log_popularity",
    "has_article",
    "length",
]
DEFAULT_WEIGHTS = [-2.0, 1.0, 0.8, 3.0, 4.0, 0.6, 0.8, 0.5, -0.3, 0.1, 0.4, 0.2]  # until trained
MAX_ALIAS_TOKENS = 8
MAX_CANDIDATES = 8
_ANCHOR_WEIGHT = {"anchor": 1.0, "title": 2.0, "redirect": 1.0, "label": 2.0, "alias": 0.5}


@dataclass
class MatchToken:
    norm: str
    start: int
    end: int
    capital: bool


def match_tokens(text: str) -> list[MatchToken]:
    """Normalised tokens (alias form) with character offsets; punctuation is skipped."""
    out: list[MatchToken] = []
    for t in tokenize(text):
        if t.kind not in {"word", "num"}:
            continue
        cap = t.text[:1].isupper()
        for piece in norm_alias(t.text).split():
            out.append(MatchToken(piece, t.start, t.end, cap))
    return out


@dataclass
class Mention:
    start: int
    end: int
    surface: str  # normalised alias form
    capital: bool


@dataclass
class Candidate:
    entity_id: int
    label: str
    features: npt.NDArray[np.float64]
    score: float = 0.0


@dataclass
class Link:
    start: int
    end: int
    entity_id: int
    label: str
    score: float
    candidates: int


@dataclass
class LinkerModel:
    weights: list[float] = field(default_factory=lambda: list(DEFAULT_WEIGHTS))
    threshold: float = 0.5
    min_keyphrase: float = 0.02
    trained_on: int = 0
    validation: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "weights": self.weights,
            "threshold": self.threshold,
            "min_keyphrase": self.min_keyphrase,
            "trained_on": self.trained_on,
            "validation": self.validation,
            "features": FEATURES,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> LinkerModel:
        if not d or len(d.get("weights", [])) != len(FEATURES):
            return cls()
        return cls(
            list(d["weights"]),
            float(d["threshold"]),
            float(d.get("min_keyphrase", 0.02)),
            int(d.get("trained_on", 0)),
            dict(d.get("validation", {})),
        )


def sigmoid(x: npt.NDArray[np.float64] | float) -> Any:
    return 1.0 / (1.0 + np.exp(-np.clip(x, -30, 30)))


# --------------------------------------------------------------- alias automaton


def build_alias_automaton(db: Database, *, min_count: int = 1) -> tuple[TokenAutomaton, int]:
    auto = TokenAutomaton()
    n = 0
    cursor = ""
    while True:
        rows = db.query(
            "SELECT DISTINCT a.alias FROM aliases a JOIN entities e ON e.id=a.entity_id WHERE a.alias > ? "
            "AND e.kind != 'stub' ORDER BY a.alias LIMIT 50000",
            (cursor,),
        )
        if not rows:
            break
        for r in rows:
            toks = r["alias"].split()
            if 1 <= len(toks) <= MAX_ALIAS_TOKENS and len(r["alias"]) >= 2 and not r["alias"].isdigit():
                auto.add(toks)
                n += 1
        cursor = rows[-1]["alias"]
    return auto.build(), n


class AliasIndex:
    """The compiled alias automaton, persisted under ``index_dir`` and rebuilt when stale."""

    def __init__(self, db: Database, index_dir: Path) -> None:
        self.db = db
        self.path = index_dir / "aliases.npz"
        self.automaton: TokenAutomaton | None = None

    def load(self) -> TokenAutomaton | None:
        if self.automaton is None and self.path.exists():
            self.automaton = TokenAutomaton.load(self.path)
        return self.automaton

    def rebuild(self) -> dict[str, Any]:
        t0 = time.monotonic()
        auto, n = build_alias_automaton(self.db)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        auto.save(self.path)
        self.automaton = auto
        info = {
            "patterns": n,
            "states": auto.n_states,
            "seconds": round(time.monotonic() - t0, 2),
            "aliases_total": int(self.db.scalar("SELECT COUNT(*) FROM aliases", default=0)),
            "built": time.time(),
        }
        self.db.kv_set("alias_automaton", info)
        return info

    def stale(self, growth: float = 0.1, max_age: float = 86400.0) -> bool:
        info = self.db.kv_get("alias_automaton")
        if not info or not self.path.exists():
            return int(self.db.scalar("SELECT COUNT(*) FROM aliases", default=0)) > 0
        total = int(self.db.scalar("SELECT COUNT(*) FROM aliases", default=0))
        return bool(total > info["aliases_total"] * (1 + growth) or time.time() - info["built"] > max_age)


# ---------------------------------------------------------------------- linker


class EntityLinker:
    def __init__(self, db: Database, automaton: TokenAutomaton | None, model: LinkerModel | None = None) -> None:
        self.db = db
        self.graph = KnowledgeGraph(db)
        self.automaton = automaton
        self.model = model or LinkerModel.from_dict(db.kv_get("linker_model"))
        self._w = np.array(self.model.weights, dtype=np.float64)
        self._profile_cache: dict[int, dict[str, float]] = {}
        self._inlinks_cache: dict[int, int] = {}
        self._wiki_docs = max(
            1, int(db.scalar("SELECT COUNT(*) FROM documents WHERE stage>=1 AND source='wikipedia'", default=1))
        )

    # --------------------------------------------------------- statistics
    def keyphraseness(self, surface: str) -> float:
        r = self.db.one("SELECT linked, seen FROM surface_stats WHERE alias=?", (surface,))
        if r is None:
            return 0.0
        return (int(r["linked"]) + 0.1) / (max(int(r["seen"]), int(r["linked"])) + 1.0)

    def candidates(
        self, surface: str, *, exclude_anchor: tuple[int, int] | None = None
    ) -> list[tuple[int, str, float]]:
        """(entity, label, weighted alias count); ``exclude_anchor`` = (entity, n) for leave-one-out training."""
        rows = self.db.query(
            "SELECT a.entity_id, e.label, a.source, a.count FROM aliases a JOIN entities e ON e.id=a.entity_id "
            "WHERE a.alias=? AND e.kind='item'",
            (surface,),
        )
        agg: dict[int, list[Any]] = {}
        for r in rows:
            c = float(r["count"])
            if exclude_anchor and r["entity_id"] == exclude_anchor[0] and r["source"] == "anchor":
                c = max(0.0, c - exclude_anchor[1])
            entry = agg.setdefault(int(r["entity_id"]), [r["label"], 0.0])
            entry[1] += c * _ANCHOR_WEIGHT.get(r["source"], 0.5)
        ranked = sorted(((e, v[0], v[1]) for e, v in agg.items() if v[1] > 0), key=lambda x: -x[2])
        return ranked[:MAX_CANDIDATES]

    def inlinks(self, entity_id: int) -> int:
        if entity_id not in self._inlinks_cache:
            if len(self._inlinks_cache) > 100_000:
                self._inlinks_cache.clear()
            self._inlinks_cache[entity_id] = int(
                self.db.scalar("SELECT COUNT(*) FROM doc_entities WHERE entity_id=?", (entity_id,), 0)
            )
        return self._inlinks_cache[entity_id]

    def relatedness(self, a: int, b: int) -> float:
        """Milne–Witten relatedness from co-linking documents, plus a bonus for a direct graph edge."""
        if a == b:
            return 1.0
        na, nb = self.inlinks(a), self.inlinks(b)
        mw = 0.0
        if na and nb:
            both = int(
                self.db.scalar(
                    "SELECT COUNT(*) FROM doc_entities x JOIN doc_entities y ON y.doc_id=x.doc_id AND y.entity_id=? "
                    "WHERE x.entity_id=?",
                    (b, a),
                    0,
                )
            )
            if both:
                w = max(self._wiki_docs, na, nb) + 1
                mw = 1 - (math.log(max(na, nb)) - math.log(both)) / (math.log(w) - math.log(min(na, nb)))
                mw = max(0.0, min(1.0, mw))
        edge = self.db.scalar("SELECT 1 FROM triples WHERE (s=? AND o=?) OR (s=? AND o=?) LIMIT 1", (a, b, b, a))
        return min(1.0, mw + (0.5 if edge else 0.0))

    def profile(self, entity_id: int) -> dict[str, float]:
        if entity_id in self._profile_cache:
            return self._profile_cache[entity_id]
        raw = self.db.scalar("SELECT terms FROM entity_profiles WHERE entity_id=?", (entity_id,))
        prof: dict[str, float] = json.loads(raw) if raw else {}
        if not prof:
            e = self.graph.entity(entity_id)
            if e and e.description:
                prof = context_vector(e.description)
        if len(self._profile_cache) > 50_000:
            self._profile_cache.clear()
        self._profile_cache[entity_id] = prof
        return prof

    # ------------------------------------------------------------ mentions
    def mentions(self, text: str, spans: list[tuple[int, int]] | None = None) -> list[Mention]:
        if self.automaton is None:
            return []
        out: list[Mention] = []
        for a, b in spans or [(0, len(text))]:
            toks = match_tokens(text[a:b])
            if not toks:
                continue
            found = longest_non_overlapping(self.automaton.finditer([t.norm for t in toks]))
            for i, j in found:
                surface = " ".join(t.norm for t in toks[i:j])
                out.append(Mention(a + toks[i].start, a + toks[j - 1].end, surface, any(t.capital for t in toks[i:j])))
        return out

    def features(
        self,
        mention: Mention,
        cands: list[tuple[int, str, float]],
        ctx: dict[str, float],
        anchors: list[int],
        kp: float,
    ) -> list[Candidate]:
        total = sum(c[2] for c in cands) or 1.0
        out = []
        for rank, (eid, label, cnt) in enumerate(cands):
            prof = self.profile(eid)
            sim = sum(w * prof.get(t, 0.0) for t, w in ctx.items()) if prof else 0.0
            coh = (
                (sum(self.relatedness(eid, o) for o in anchors if o != eid) / max(1, len(anchors))) if anchors else 0.0
            )
            has_doc = 1.0 if self.db.scalar("SELECT doc_id FROM entities WHERE id=?", (eid,)) else 0.0
            f = np.array(
                [
                    1.0,
                    math.log((cnt + 0.01) / (total + 0.01)),
                    1.0 if rank == 0 else 0.0,
                    sim,
                    coh,
                    math.log(kp + 1e-3),
                    1.0 if mention.capital else 0.0,
                    1.0 if norm_alias(label) == mention.surface else 0.0,
                    math.log(len(cands)),
                    math.log(self.inlinks(eid) + 1),
                    has_doc,
                    min(4, len(mention.surface.split())) / 4,
                ]
            )
            out.append(Candidate(eid, label, f))
        return out

    def link(
        self, text: str, spans: list[tuple[int, int]] | None = None, *, ctx: dict[str, float] | None = None
    ) -> list[Link]:
        """Detect and disambiguate entity mentions in ``text``."""
        ctx = ctx if ctx is not None else context_vector(text)
        scored: list[tuple[Mention, list[Candidate]]] = []
        for m in self.mentions(text, spans):
            kp = self.keyphraseness(m.surface)
            if kp < self.model.min_keyphrase and not m.capital:
                continue
            cands = self.candidates(m.surface)
            if not cands:
                continue
            scored.append((m, self.features(m, cands, ctx, [], kp)))
        # coherence anchors: confidently unambiguous mentions (pass 1), then re-score (pass 2)
        anchors = [
            cs[0].entity_id
            for _m, cs in scored
            if len(cs) == 1 or (cs[0].features[1] > math.log(0.9) and cs[0].features[5] > math.log(0.2))
        ][:12]
        links: list[Link] = []
        for m, cs in scored:
            # recompute coherence for this mention with the document's anchors
            for c in cs:
                others = [a for a in anchors if a != c.entity_id]
                c.features[4] = sum(self.relatedness(c.entity_id, o) for o in others) / len(others) if others else 0.0
                c.score = float(sigmoid(float(self._w @ c.features)))
            best = max(cs, key=lambda c: c.score)
            if best.score >= self.model.threshold:
                links.append(Link(m.start, m.end, best.entity_id, best.label, best.score, len(cs)))
        return links


# ------------------------------------------------------------------- contexts

_STOP_CACHE: dict[str, set[str]] = {}


def context_vector(text: str, stops: set[str] | None = None, max_terms: int = 200) -> dict[str, float]:
    """L2-normalised term-frequency vector of stems (no stop-words, short tokens dropped)."""
    stops = stops if stops is not None else _STOP_CACHE.get("default", set())
    counts: Counter[str] = Counter()
    for t in tokenize(text):
        if t.kind == "word":
            w = t.text.lower().rstrip(".")
            if len(w) > 2 and w not in stops:
                counts[stem(w)] += 1
    top = counts.most_common(max_terms)
    norm = math.sqrt(sum(c * c for _t, c in top)) or 1.0
    return {t: c / norm for t, c in top}


def set_stopwords(stops: set[str]) -> None:
    _STOP_CACHE["default"] = set(stops)


def tfidf_profile(db: Database, text: str, *, max_terms: int = 60) -> dict[str, float]:
    """Entity profile: tf–idf weighted stems of its article (idf from the passage index)."""
    counts: Counter[str] = Counter()
    for t in tokenize(text[:20000]):
        if t.kind == "word" and len(t.text) > 2:
            counts[stem(t.text.lower().rstrip("."))] += 1
    if not counts:
        return {}
    total = max(1, int(db.scalar("SELECT MAX(id) FROM chunks", default=1)))
    terms = list(counts)
    df: dict[str, int] = {}
    for i in range(0, len(terms), 500):
        part = terms[i : i + 500]
        for r in db.query(f"SELECT term, doc FROM chunk_vocab WHERE term IN ({','.join('?' * len(part))})", part):
            df[r["term"]] = int(r["doc"])
    weights = {t: (1 + math.log(c)) * math.log((total + 1) / (df.get(t, 0) + 1)) for t, c in counts.items()}
    top = sorted(weights.items(), key=lambda x: -x[1])[:max_terms]
    norm = math.sqrt(sum(w * w for _t, w in top)) or 1.0
    return {t: round(w / norm, 4) for t, w in top if w > 0}


# ------------------------------------------------------------------- training


def train_logistic(
    x: npt.NDArray[np.float64],
    y: npt.NDArray[np.float64],
    *,
    l2: float = 1e-3,
    epochs: int = 400,
    lr: float = 0.5,
    init: npt.NDArray[np.float64] | None = None,
) -> npt.NDArray[np.float64]:
    """Full-batch gradient descent with L2 (no regularisation on the bias), class-balanced."""
    w = init.copy() if init is not None else np.zeros(x.shape[1])
    pos = max(1.0, float(y.sum()))
    neg = max(1.0, float(len(y) - y.sum()))
    sw = np.where(y > 0, len(y) / (2 * pos), len(y) / (2 * neg))
    for _ in range(epochs):
        p = sigmoid(x @ w)
        grad = x.T @ ((p - y) * sw) / len(y)
        grad[1:] += l2 * w[1:]
        w -= lr * grad
    return w


def train_from_anchors(linker: EntityLinker, docs: list[tuple[int, str, list[tuple[int, int, int]]]]) -> LinkerModel:
    """Train on (doc_id, text, [(start, end, true_entity)]) Wikipedia anchors; every 10th doc validates."""
    rows_x: list[npt.NDArray[np.float64]] = []
    rows_y: list[float] = []
    groups: list[tuple[int, int, int, bool]] = []  # (start row, end row, gold index or -1, is_validation)
    for doc_id, text, anchors in docs:
        ctx = context_vector(text)
        gold_entities = [e for _a, _b, e in anchors][:12]
        for a, b, true_e in anchors:
            surface = norm_alias(text[a:b])
            cands = linker.candidates(surface, exclude_anchor=(true_e, 1))
            if not cands:
                continue
            m = Mention(a, b, surface, text[a : a + 1].isupper())
            kp = linker.keyphraseness(surface)
            others = [e for e in gold_entities if e != true_e]
            cs = linker.features(m, cands, ctx, others, kp)
            start = len(rows_x)
            gold = -1
            for k, c in enumerate(cs):
                rows_x.append(c.features)
                rows_y.append(1.0 if c.entity_id == true_e else 0.0)
                if c.entity_id == true_e:
                    gold = k
            groups.append((start, len(rows_x), gold, doc_id % 10 == 0))
    if not rows_x:
        return linker.model
    x = np.vstack(rows_x)
    y = np.array(rows_y)
    train_mask = np.zeros(len(y), dtype=bool)
    for s, e, _g, val in groups:
        if not val:
            train_mask[s:e] = True
    w = train_logistic(x[train_mask], y[train_mask], init=np.array(DEFAULT_WEIGHTS))
    probs = sigmoid(x @ w)
    # choose the threshold on validation documents: highest recall with precision >= 0.9
    val_groups = [(s, e, g) for s, e, g, val in groups if val] or [(s, e, g) for s, e, g, _ in groups]
    best = (0.5, 0.0, 0.0)
    for th in np.linspace(0.05, 0.95, 37):
        tp = fp = 0
        for s, e, g in val_groups:
            k = int(np.argmax(probs[s:e]))
            if probs[s + k] >= th:
                tp += k == g
                fp += k != g
        total = len(val_groups)
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / total if total else 0.0
        if prec >= 0.9 and rec > best[2]:
            best = (float(th), prec, rec)
    if best[2] == 0.0:
        best = (0.5, 0.0, 0.0)
    acc_top1 = float(np.mean([int(np.argmax(probs[s:e])) == g for s, e, g in val_groups])) if val_groups else 0.0
    model = LinkerModel(
        [float(v) for v in w],
        best[0],
        linker.model.min_keyphrase,
        len(groups),
        {
            "precision": round(best[1], 4),
            "recall": round(best[2], 4),
            "top1_accuracy": round(acc_top1, 4),
            "validation_mentions": len(val_groups),
        },
    )
    return model
