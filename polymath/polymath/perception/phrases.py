"""Collocation learning with normalised pointwise mutual information (NPMI).

Counts of unigrams, bigrams and trigrams are accumulated per batch in memory and
merged into SQLite. To bound growth, a new n-gram (n ≥ 2) is stored only once it
occurs at least twice in a batch; after that every occurrence is counted.

NPMI(x, y) = ln(p(xy) / (p(x) p(y))) / −ln p(xy)  ∈ [−1, 1]. Bigrams with
NPMI ≥ ``threshold`` and count ≥ ``min_count`` whose words are not stop-words
become phrases. Stop-words are *learned*: the most frequent unigrams. Trigrams
may have a stop-word in the middle ("bank of america"); their score is the
weaker of the two binary splits.
"""

from __future__ import annotations

import itertools
import math
from collections import Counter

from polymath.core.db import Database

STOPWORD_RANK = 150


class PhraseLearner:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.uni: Counter[str] = Counter()
        self.bi: Counter[str] = Counter()
        self.tri: Counter[str] = Counter()
        self.tokens = 0

    def add_sentence(self, words: list[str]) -> None:
        w = [x for x in words if x]
        self.tokens += len(w)
        self.uni.update(w)
        self.bi.update(f"{a} {b}" for a, b in itertools.pairwise(w))
        self.tri.update(f"{a} {b} {c}" for a, b, c in zip(w, w[1:], w[2:]))

    def flush(self) -> int:
        if not self.tokens:
            return 0
        rows: list[tuple[int, str, int]] = [(1, g, c) for g, c in self.uni.items()]
        for n, counter in ((2, self.bi), (3, self.tri)):
            frequent = [(n, g, c) for g, c in counter.items() if c >= 2]
            rows.extend(frequent)
            rare = [g for g, c in counter.items() if c == 1]
            # singletons only bump n-grams the store already knows
            for i in range(0, len(rare), 500):
                part = rare[i : i + 500]
                known = self.db.query(
                    f"SELECT gram FROM ngram_counts WHERE n=? AND gram IN ({','.join('?' * len(part))})", [n, *part]
                )
                rows.extend((n, r["gram"], 1) for r in known)
        self.db.executemany(
            "INSERT INTO ngram_counts(n, gram, count) VALUES(?,?,?) "
            "ON CONFLICT(n, gram) DO UPDATE SET count=count+excluded.count",
            rows,
        )
        total = int(self.db.kv_get("ngram_tokens", 0)) + self.tokens
        self.db.kv_set("ngram_tokens", total)
        n = len(rows)
        self.uni.clear()
        self.bi.clear()
        self.tri.clear()
        self.tokens = 0
        return n


def stopwords(db: Database, rank: int = STOPWORD_RANK) -> set[str]:
    """The most frequent words (at most 2% of the vocabulary, so tiny corpora are not all 'stop-words')."""
    vocab = int(db.scalar("SELECT COUNT(*) FROM ngram_counts WHERE n=1", default=0))
    k = min(rank, vocab // 50)
    if k <= 0:
        return set()
    return {r["gram"] for r in db.query("SELECT gram FROM ngram_counts WHERE n=1 ORDER BY count DESC LIMIT ?", (k,))}


def npmi(c_xy: int, c_x: int, c_y: int, total: int) -> float:
    if min(c_xy, c_x, c_y) <= 0 or total <= 0:
        return -1.0
    p_xy, p_x, p_y = c_xy / total, c_x / total, c_y / total
    if p_xy >= 1.0:
        return 1.0
    return math.log(p_xy / (p_x * p_y)) / -math.log(p_xy)


def recompute_phrases(db: Database, *, min_count: int = 5, threshold: float = 0.5, limit: int = 500_000) -> int:
    total = int(db.kv_get("ngram_tokens", 0))
    if total == 0:
        return 0
    stops = stopwords(db)

    def uni(word: str) -> int:
        return int(db.scalar("SELECT count FROM ngram_counts WHERE n=1 AND gram=?", (word,), 0))

    found: list[tuple[str, int, int, float]] = []
    for r in db.query(
        "SELECT gram, count FROM ngram_counts WHERE n=2 AND count>=? ORDER BY count DESC LIMIT ?", (min_count, limit)
    ):
        a, b = r["gram"].split(" ")
        if a in stops or b in stops or (not a.isalpha() and not b.isalpha()):
            continue
        score = npmi(int(r["count"]), uni(a), uni(b), total)
        if score >= threshold:
            found.append((r["gram"], 2, int(r["count"]), score))
    for r in db.query(
        "SELECT gram, count FROM ngram_counts WHERE n=3 AND count>=? ORDER BY count DESC LIMIT ?", (min_count, limit)
    ):
        a, b, c = r["gram"].split(" ")
        if a in stops or c in stops:
            continue
        ab = int(db.scalar("SELECT count FROM ngram_counts WHERE n=2 AND gram=?", (f"{a} {b}",), 0))
        bc = int(db.scalar("SELECT count FROM ngram_counts WHERE n=2 AND gram=?", (f"{b} {c}",), 0))
        cnt = int(r["count"])
        score = min(npmi(cnt, ab, uni(c), total), npmi(cnt, uni(a), bc, total))
        if score >= threshold:
            found.append((r["gram"], 3, cnt, score))
    db.execute("DELETE FROM phrases")
    db.executemany("INSERT INTO phrases(phrase, n, count, npmi) VALUES(?,?,?,?)", found)
    return len(found)


def load_phrases(db: Database) -> set[str]:
    return {r["phrase"] for r in db.query("SELECT phrase FROM phrases")}


def merge_phrases(words: list[str], phrases: set[str]) -> list[str]:
    """Greedy left-to-right merge of known trigram/bigram phrases into single tokens (joined by '_')."""
    out: list[str] = []
    i = 0
    while i < len(words):
        if i + 2 < len(words) and f"{words[i]} {words[i + 1]} {words[i + 2]}" in phrases:
            out.append("_".join(words[i : i + 3]))
            i += 3
        elif i + 1 < len(words) and f"{words[i]} {words[i + 1]}" in phrases:
            out.append(f"{words[i]}_{words[i + 1]}")
            i += 2
        else:
            out.append(words[i])
            i += 1
    return out
