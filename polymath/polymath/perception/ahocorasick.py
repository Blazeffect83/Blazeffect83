"""Token-level Aho–Corasick automaton for dictionary (alias) matching.

Patterns are sequences of normalised tokens; each token is represented by a
stable 32-bit CRC so the automaton needs no vocabulary in memory. After
construction the trie is compacted into flat ``array`` buffers (CSR layout:
per-state sorted child tokens, found with ``bisect``), about 25 bytes per state,
and can be saved to / loaded from an ``.npz`` file.

Matching reports every pattern occurrence (overlaps included) in one left-to-
right pass in O(tokens + matches).
"""

from __future__ import annotations

import bisect
import zlib
from array import array
from collections import deque
from collections.abc import Iterable, Iterator
from pathlib import Path

import numpy as np


def tok_hash(token: str) -> int:
    return zlib.crc32(token.encode("utf-8"))


class TokenAutomaton:
    def __init__(self) -> None:
        self._children: list[dict[int, int]] = [{}]
        self._depth_out: list[int] = [0]  # pattern length (tokens) ending at this state, 0 if none
        self.compact = False
        self.n_patterns = 0

    # ---------------------------------------------------------------- build
    def add(self, tokens: Iterable[str]) -> None:
        if self.compact:
            raise RuntimeError("automaton is compacted; build a new one to add patterns")
        state = 0
        length = 0
        for tok in tokens:
            h = tok_hash(tok)
            nxt = self._children[state].get(h)
            if nxt is None:
                nxt = len(self._children)
                self._children[state][h] = nxt
                self._children.append({})
                self._depth_out.append(0)
            state = nxt
            length += 1
        if length and not self._depth_out[state]:
            self._depth_out[state] = length
            self.n_patterns += 1

    def build(self) -> TokenAutomaton:
        n = len(self._children)
        fail = array("i", [0]) * n
        out_link = array("i", [-1]) * n  # nearest proper suffix state that ends a pattern
        order: list[int] = []
        queue: deque[int] = deque()
        for child in self._children[0].values():
            queue.append(child)
        while queue:
            s = queue.popleft()
            order.append(s)
            for h, t in self._children[s].items():
                f = fail[s]
                while f and h not in self._children[f]:
                    f = fail[f]
                cand = self._children[f].get(h, 0)
                fail[t] = cand if cand != t else 0
                fo = fail[t]
                out_link[t] = fo if self._depth_out[fo] else out_link[fo]
                queue.append(t)
        starts = array("i", [0]) * (n + 1)
        toks = array("q")
        nxts = array("i")
        for s in range(n):
            starts[s] = len(toks)
            for h in sorted(self._children[s]):
                toks.append(h)
                nxts.append(self._children[s][h])
        starts[n] = len(toks)
        self._starts, self._toks, self._nxts = starts, toks, nxts
        self._fail, self._out_link = fail, out_link
        self._depth = array("i", self._depth_out)
        self._children = []
        self._depth_out = []
        self.compact = True
        return self

    # ---------------------------------------------------------------- match
    def _goto(self, state: int, h: int) -> int:
        lo, hi = self._starts[state], self._starts[state + 1]
        if lo == hi:
            return -1
        i = bisect.bisect_left(self._toks, h, lo, hi)
        if i < hi and self._toks[i] == h:
            return self._nxts[i]
        return -1

    def finditer(self, tokens: list[str]) -> Iterator[tuple[int, int]]:
        """Yield (start, end) token index spans (end exclusive) of every pattern occurrence."""
        if not self.compact:
            raise RuntimeError("call build() first")
        state = 0
        for i, tok in enumerate(tokens):
            h = tok_hash(tok)
            while True:
                nxt = self._goto(state, h)
                if nxt >= 0:
                    state = nxt
                    break
                if state == 0:
                    break
                state = self._fail[state]
            s = state if self._depth[state] else self._out_link[state]
            while s > 0:
                length = self._depth[s]
                yield i + 1 - length, i + 1
                s = self._out_link[s]

    @property
    def n_states(self) -> int:
        return len(self._fail) if self.compact else len(self._children)

    # -------------------------------------------------------------- persist
    def save(self, path: Path) -> None:
        if not self.compact:
            raise RuntimeError("build() before save()")
        tmp = path.with_suffix(".tmp.npz")
        np.savez(
            tmp,
            starts=np.frombuffer(self._starts, dtype=np.int32),
            toks=np.frombuffer(self._toks, dtype=np.int64),
            nxts=np.frombuffer(self._nxts, dtype=np.int32),
            fail=np.frombuffer(self._fail, dtype=np.int32),
            out_link=np.frombuffer(self._out_link, dtype=np.int32),
            depth=np.frombuffer(self._depth, dtype=np.int32),
            n_patterns=np.array([self.n_patterns]),
        )
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> TokenAutomaton:
        data = np.load(path)
        a = cls()
        a._children, a._depth_out = [], []

        def arr(code: str, name: str) -> array:  # type: ignore[type-arg]
            out = array(code)
            out.frombytes(data[name].tobytes())
            return out

        a._starts, a._toks, a._nxts = arr("i", "starts"), arr("q", "toks"), arr("i", "nxts")
        a._fail, a._out_link, a._depth = arr("i", "fail"), arr("i", "out_link"), arr("i", "depth")
        a.n_patterns = int(data["n_patterns"][0])
        a.compact = True
        return a


def longest_non_overlapping(spans: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    """Greedy selection preferring longer spans, then earlier ones."""
    chosen: list[tuple[int, int]] = []
    taken: set[int] = set()
    for a, b in sorted(set(spans), key=lambda s: (-(s[1] - s[0]), s[0])):
        if not any(i in taken for i in range(a, b)):
            chosen.append((a, b))
            taken.update(range(a, b))
    return sorted(chosen)
