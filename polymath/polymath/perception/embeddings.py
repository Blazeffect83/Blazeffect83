"""Word embeddings trained from scratch: skip-gram with negative sampling (word2vec SGNS) in numpy.

* Vocabulary from the agent's own unigram counts (``ngram_counts``), capped and
  thresholded; known phrases are merged into single tokens first.
* Frequent-word subsampling (keep ∝ √(t/f) + t/f), dynamic windows, negatives
  drawn from the unigram distribution raised to ¾.
* Mini-batch SGD: each batch of (centre, context) pairs updates the input and
  output matrices with scatter-adds; learning rate decays linearly per epoch.
* Vectors persist as ``.npy`` under ``index_dir/embeddings``; growing the
  vocabulary keeps already-learned vectors.

Document vectors use SIF (Arora et al., 2017): a/(a + p(w)) weighted averages
with the first principal component removed.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

from polymath.core.db import Database
from polymath.perception.phrases import merge_phrases
from polymath.perception.tokenize import words

F32 = npt.NDArray[np.float32]


@dataclass
class Vocab:
    words: list[str]
    counts: npt.NDArray[np.int64]

    def __post_init__(self) -> None:
        self.index = {w: i for i, w in enumerate(self.words)}

    def __len__(self) -> int:
        return len(self.words)

    @classmethod
    def from_db(cls, db: Database, *, min_count: int, max_size: int) -> Vocab:
        rows = db.query(
            "SELECT gram, count FROM ngram_counts WHERE n=1 AND count>=? ORDER BY count DESC LIMIT ?",
            (min_count, max_size),
        )
        phrase_rows = db.query("SELECT phrase, count FROM phrases ORDER BY count DESC LIMIT ?", (max_size // 10,))
        ws = [str(r["gram"]) for r in rows] + [str(r["phrase"]).replace(" ", "_") for r in phrase_rows]
        cs = [int(r["count"]) for r in rows] + [int(r["count"]) for r in phrase_rows]
        order = np.argsort(-np.array(cs, dtype=np.int64), kind="stable")
        return cls([ws[i] for i in order], np.array(cs, dtype=np.int64)[order])

    def to_json(self) -> str:
        return json.dumps({"words": self.words, "counts": self.counts.tolist()})

    @classmethod
    def from_json(cls, raw: str) -> Vocab:
        d = json.loads(raw)
        return cls(list(d["words"]), np.array(d["counts"], dtype=np.int64))


class SGNS:
    def __init__(
        self, vocab: Vocab, dim: int = 128, *, seed: int = 1, w_in: F32 | None = None, w_out: F32 | None = None
    ) -> None:
        self.vocab = vocab
        self.dim = dim
        rng = np.random.default_rng(seed)
        n = len(vocab)
        self.w_in: F32 = (
            w_in if w_in is not None else ((rng.random((n, dim), dtype=np.float32) - 0.5) / dim).astype(np.float32)
        )
        self.w_out: F32 = w_out if w_out is not None else np.zeros((n, dim), dtype=np.float32)
        self.rng = rng
        freq = vocab.counts.astype(np.float64)
        p = freq**0.75
        self.neg_cdf = np.cumsum(p / p.sum())
        total = freq.sum()
        f = freq / max(total, 1.0)
        t = 1e-4
        self.keep = np.minimum(1.0, (np.sqrt(f / t) + 1) * t / np.maximum(f, 1e-12))
        self.pairs_seen = 0

    # ------------------------------------------------------------- data
    def encode(self, tokens: list[str]) -> npt.NDArray[np.int64]:
        ids = np.fromiter((self.vocab.index.get(t, -1) for t in tokens), dtype=np.int64, count=len(tokens))
        ids = ids[ids >= 0]
        if ids.size == 0:
            return ids
        kept: npt.NDArray[np.int64] = ids[self.rng.random(ids.size) < self.keep[ids]]
        return kept

    def pairs(
        self, sentences: Iterable[npt.NDArray[np.int64]], window: int
    ) -> tuple[npt.NDArray[np.int64], npt.NDArray[np.int64]]:
        cs: list[npt.NDArray[np.int64]] = []
        os_: list[npt.NDArray[np.int64]] = []
        for ids in sentences:
            n = ids.size
            if n < 2:
                continue
            spans = self.rng.integers(1, window + 1, size=n)  # dynamic window per centre word
            for off in range(1, min(window, n - 1) + 1):
                mask = spans[: n - off] >= off
                if not mask.any():
                    continue
                a, b = ids[: n - off][mask], ids[off:][mask]
                cs.extend((a, b))
                os_.extend((b, a))
        if not cs:
            empty = np.zeros(0, dtype=np.int64)
            return empty, empty
        return np.concatenate(cs), np.concatenate(os_)

    # ------------------------------------------------------------ train
    def train_pairs(
        self,
        centres: npt.NDArray[np.int64],
        contexts: npt.NDArray[np.int64],
        *,
        negatives: int = 5,
        lr: float = 0.025,
        batch: int = 4096,
    ) -> float:
        """One pass of SGD over the pairs; returns mean loss."""
        if centres.size == 0:
            return 0.0
        # Each batch sums gradients computed from the same (stale) vectors, so a row hit by many pairs takes
        # one big step; capping the batch at ~4 pairs per vocabulary word keeps that bounded (tiny vocabularies
        # train like plain SGD, large ones keep large, fast batches).
        batch = max(32, min(batch, 4 * len(self.vocab)))
        perm = self.rng.permutation(centres.size)
        centres, contexts = centres[perm], contexts[perm]
        total_loss = 0.0
        for i in range(0, centres.size, batch):
            c = centres[i : i + batch]
            o = contexts[i : i + batch]
            # Negatives are shared by the whole batch: K = 4·negatives samples, each weighted negatives/K so
            # the expected gradient matches per-pair sampling, but updates become dense matmuls.
            k = 4 * negatives
            neg = np.minimum(np.searchsorted(self.neg_cdf, self.rng.random(k)), len(self.vocab) - 1)
            wk = negatives / k
            v = self.w_in[c]  # (b, d)
            u = self.w_out[o]  # (b, d)
            un = self.w_out[neg]  # (k, d)
            s_pos = 1.0 / (1.0 + np.exp(-np.clip(np.einsum("bd,bd->b", v, u), -10, 10)))
            s_neg = 1.0 / (1.0 + np.exp(-np.clip(v @ un.T, -10, 10)))  # (b, k)
            g_pos = (s_pos - 1.0).astype(np.float32)
            g_neg = (wk * s_neg).astype(np.float32)
            grad_v = g_pos[:, None] * u + g_neg @ un
            grad_u = g_pos[:, None] * v
            grad_un = g_neg.T @ v  # (k, d)
            np.add.at(self.w_in, c, -lr * grad_v)
            np.add.at(self.w_out, o, -lr * grad_u)
            np.add.at(self.w_out, neg, -lr * grad_un)
            total_loss += float(-np.log(s_pos + 1e-7).sum() - wk * np.log(1 - s_neg + 1e-7).sum())
        if not (np.isfinite(self.w_in).all() and np.isfinite(self.w_out).all()):  # pragma: no cover - safety net
            bad_in = ~np.isfinite(self.w_in).all(axis=1)
            bad_out = ~np.isfinite(self.w_out).all(axis=1)
            self.w_in[bad_in] = (self.rng.random((int(bad_in.sum()), self.dim), dtype=np.float32) - 0.5) / self.dim
            self.w_out[bad_out] = 0.0
        self.pairs_seen += int(centres.size)
        return total_loss / centres.size

    # ------------------------------------------------------------ query
    def normalized(self) -> F32:
        n = np.linalg.norm(self.w_in, axis=1, keepdims=True)
        out: F32 = (self.w_in / np.maximum(n, 1e-8)).astype(np.float32)
        return out

    def vector(self, word: str) -> F32 | None:
        i = self.vocab.index.get(word)
        return None if i is None else self.w_in[i]

    def most_similar(self, word: str, k: int = 10, *, unit: F32 | None = None) -> list[tuple[str, float]]:
        unit = unit if unit is not None else self.normalized()
        i = self.vocab.index.get(word)
        if i is None:
            return []
        sims = unit @ unit[i]
        sims[i] = -1
        top = np.argpartition(-sims, min(k, len(sims) - 1))[:k]
        return [(self.vocab.words[j], float(sims[j])) for j in top[np.argsort(-sims[top])]]

    # ---------------------------------------------------------- persist
    def save(self, directory: Path, state: dict[str, Any]) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        tmp = directory / f".tmp-{os.getpid()}"
        tmp.mkdir(exist_ok=True)
        np.save(tmp / "w_in.npy", self.w_in)
        np.save(tmp / "w_out.npy", self.w_out)
        (tmp / "vocab.json").write_text(self.vocab.to_json())
        (tmp / "state.json").write_text(
            json.dumps({**state, "dim": self.dim, "pairs_seen": self.pairs_seen, "saved": time.time()})
        )
        for name in ("w_in.npy", "w_out.npy", "vocab.json", "state.json"):
            os.replace(tmp / name, directory / name)  # state.json last: it marks a complete save
        tmp.rmdir()

    @classmethod
    def load(cls, directory: Path) -> tuple[SGNS, dict[str, Any]] | None:
        try:
            state = json.loads((directory / "state.json").read_text())
            vocab = Vocab.from_json((directory / "vocab.json").read_text())
            w_in = np.load(directory / "w_in.npy")
            w_out = np.load(directory / "w_out.npy")
        except (OSError, ValueError, KeyError):
            return None
        if w_in.shape != (len(vocab), state["dim"]):
            return None
        model = cls(vocab, int(state["dim"]), w_in=w_in.astype(np.float32), w_out=w_out.astype(np.float32))
        model.pairs_seen = int(state.get("pairs_seen", 0))
        return model, state

    def regrow(self, vocab: Vocab) -> SGNS:
        """A model over a new vocabulary that keeps every vector already learned."""
        new = SGNS(vocab, self.dim, seed=int(self.rng.integers(1 << 30)))
        for w, i in vocab.index.items():
            j = self.vocab.index.get(w)
            if j is not None:
                new.w_in[i] = self.w_in[j]
                new.w_out[i] = self.w_out[j]
        new.pairs_seen = self.pairs_seen
        return new


def sentence_tokens(text: str, phrases: set[str]) -> Iterator[list[str]]:
    """Training sentences: lines/paragraph sentences → lower-case words with phrases merged."""
    for para in text.split("\n"):
        for sent in para.replace("? ", "?\n").replace("! ", "!\n").replace(". ", ".\n").split("\n"):
            toks = words(sent)
            if len(toks) >= 3:
                yield merge_phrases(toks, phrases) if phrases else toks


# ------------------------------------------------------------------- SIF


class SIF:
    """Smooth-inverse-frequency sentence/document embeddings over a trained SGNS model."""

    def __init__(self, model: SGNS, a: float = 1e-3, pc: F32 | None = None) -> None:
        self.model = model
        total = float(model.vocab.counts.sum()) or 1.0
        p = model.vocab.counts.astype(np.float64) / total
        self.weights = (a / (a + p)).astype(np.float32)
        self.unit = model.normalized()
        self.pc = pc

    def raw(self, tokens: list[str]) -> F32 | None:
        ids = [self.model.vocab.index[t] for t in tokens if t in self.model.vocab.index]
        if not ids:
            return None
        idx = np.array(ids)
        v: F32 = (self.weights[idx, None] * self.unit[idx]).mean(axis=0).astype(np.float32)
        return v

    def fit_pc(self, samples: list[F32]) -> F32 | None:
        """First principal component of a sample of raw vectors (power iteration on the Gram matrix)."""
        if len(samples) < 2:
            return None
        x = np.vstack(samples).astype(np.float64)
        vec = np.asarray(np.random.default_rng(0).normal(size=(x.shape[1],)), dtype=np.float64)
        for _ in range(50):
            vec = np.asarray(x.T @ (x @ vec), dtype=np.float64)
            vec = np.divide(vec, float(np.linalg.norm(vec)) or 1.0)
        self.pc = vec.astype(np.float32)
        return self.pc

    def embed(self, tokens: list[str]) -> F32 | None:
        v = self.raw(tokens)
        if v is None:
            return None
        if self.pc is not None:
            v = v - float(v @ self.pc) * self.pc
        n = float(np.linalg.norm(v))
        if n == 0:
            return None
        out: F32 = (v / n).astype(np.float32)
        return out
