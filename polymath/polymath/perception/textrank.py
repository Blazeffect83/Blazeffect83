"""TextRank (Mihalcea & Tarau, 2004): keywords and extractive summaries via PageRank in numpy."""

from __future__ import annotations

import math
from collections import Counter, defaultdict

import numpy as np
import numpy.typing as npt

from polymath.perception.stem import stem
from polymath.perception.tokenize import tokenize


def pagerank(
    adj: npt.NDArray[np.float64], damping: float = 0.85, iters: int = 60, tol: float = 1e-8
) -> npt.NDArray[np.float64]:
    """PageRank on a weighted adjacency matrix (rows = sources). Dangling rows spread uniformly."""
    n = adj.shape[0]
    if n == 0:
        return np.zeros(0)
    out = adj.sum(axis=1)
    trans = np.where(out[:, None] > 0, adj / np.where(out[:, None] > 0, out[:, None], 1), 1.0 / n)
    r = np.full(n, 1.0 / n)
    for _ in range(iters):
        nxt: npt.NDArray[np.float64] = (1 - damping) / n + damping * (r @ trans)
        if np.abs(nxt - r).sum() < tol:
            return nxt
        r = nxt
    return r


def _candidates(text: str, stops: set[str]) -> list[tuple[str, str] | None]:
    """Per token: (stem, surface) for candidate words, None for breaks (stop-words, punctuation)."""
    seq: list[tuple[str, str] | None] = []
    for t in tokenize(text):
        if t.kind == "word":
            w = t.text.lower().rstrip(".")
            if len(w) > 2 and w not in stops and w.isalpha():
                seq.append((stem(w), w))
                continue
        seq.append(None)
    return seq


def keywords(text: str, stops: set[str], *, window: int = 4, top: int = 10) -> list[str]:
    seq = _candidates(text, stops)
    nodes: dict[str, int] = {}
    surfaces: dict[str, Counter[str]] = defaultdict(Counter)
    for item in seq:
        if item:
            nodes.setdefault(item[0], len(nodes))
            surfaces[item[0]][item[1]] += 1
    if not nodes:
        return []
    adj = np.zeros((len(nodes), len(nodes)))
    words = [nodes[i[0]] if i else -1 for i in seq]
    for i, a in enumerate(words):
        if a < 0:
            continue
        for b in words[i + 1 : i + window]:
            if b >= 0 and b != a:
                adj[a, b] += 1
                adj[b, a] += 1
    scores = pagerank(adj)
    best = {s for s, _ in sorted(nodes.items(), key=lambda kv: -scores[kv[1]])[: max(top * 2, 10)]}
    # merge adjacent top words into key phrases
    phrases: Counter[str] = Counter()
    run: list[tuple[str, str]] = []
    for item in [*seq, None]:
        if item and item[0] in best:
            run.append(item)
            continue
        if run:
            key = " ".join(surfaces[s].most_common(1)[0][0] for s, _w in run)
            phrases[key] += sum(scores[nodes[s]] for s, _w in run) / len(run) * math.sqrt(len(run))
            run = []
    return [p for p, _ in phrases.most_common(top)]


def summarize(sentences: list[str], stops: set[str], k: int = 3) -> list[int]:
    """Indices (in original order) of the ``k`` most central sentences."""
    bags = [{c[0] for c in _candidates(s, stops) if c} for s in sentences]
    n = len(bags)
    if n <= k:
        return list(range(n))
    adj = np.zeros((n, n))
    for i in range(n):
        for j in range(i + 1, n):
            inter = len(bags[i] & bags[j])
            if inter and len(bags[i]) > 1 and len(bags[j]) > 1:
                w = inter / (math.log(len(bags[i])) + math.log(len(bags[j])))
                adj[i, j] = adj[j, i] = w
    scores = pagerank(adj)
    return sorted(int(i) for i in np.argsort(-scores)[:k])
