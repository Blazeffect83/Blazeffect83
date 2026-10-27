"""Unsupervised sentence boundary detection with learned abbreviations (Punkt-style).

Training (Kiss & Strunk, 2006) needs only raw text the agent collected:

* **Abbreviations** — a word type that is almost always followed by a period
  is an abbreviation. Score = Dunning log-likelihood of the (type, period)
  collocation × exp(−length) × (internal periods + 1) × ratio⁴, where ratio is
  the share of occurrences carrying a period (must be ≥ 0.6). Punkt's original
  penalty, length^(−times seen without a period), wipes out "Mr" in a corpus
  that mixes British and American spelling, so the ratio form is used instead.
  Types scoring ≥ 0.3 are abbreviations.
* **Orthographic context** — for every type, how often it appears lower-case or
  capitalised in the middle of sentences. A capitalised word after an
  abbreviation starts a new sentence only if that word is normally lower-case
  mid-sentence (it is capitalised *because* it starts a sentence).
* **Sentence starters** — types that unusually often follow a sentence break.

Splitting runs over the agent's own tokenizer with character offsets.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from polymath.perception.tokenize import Token, tokenize

ABBREV_THRESHOLD = 0.3
MIN_PERIOD_RATIO = 0.6
ORTHO_BEG_UC, ORTHO_MID_UC, ORTHO_MID_LC, ORTHO_BEG_LC = 1, 2, 4, 8
_CLOSERS = set("\"'”’)]}»")
_OPENERS = set("\"'“‘([{«")
_TERMINAL = {".", "?", "!", "…", "..."}


def _type(tok: str) -> str:
    return tok.lower().rstrip(".")


def dunning_ll(count_a: int, count_b: int, count_ab: int, n: int) -> float:
    """Punkt's log-likelihood that type a (count_a) is followed by a period (count_b total) count_ab times."""
    p1 = count_b / n
    p2 = 0.99
    p1 = min(max(p1, 1e-12), 1 - 1e-12)
    null = count_ab * math.log(p1) + (count_a - count_ab) * math.log(1 - p1)
    alt = count_ab * math.log(p2) + (count_a - count_ab) * math.log(1 - p2)
    return -2.0 * (null - alt)


@dataclass
class PunktModel:
    abbreviations: set[str] = field(default_factory=set)
    ortho: dict[str, int] = field(default_factory=dict)
    starters: set[str] = field(default_factory=set)
    trained_tokens: int = 0

    def to_json(self) -> str:
        return json.dumps(
            {
                "abbreviations": sorted(self.abbreviations),
                "ortho": self.ortho,
                "starters": sorted(self.starters),
                "trained_tokens": self.trained_tokens,
            }
        )

    @classmethod
    def from_json(cls, raw: str | dict[str, Any] | None) -> PunktModel:
        if not raw:
            return cls()
        d = json.loads(raw) if isinstance(raw, str) else raw
        return cls(
            set(d.get("abbreviations", [])),
            dict(d.get("ortho", {})),
            set(d.get("starters", [])),
            int(d.get("trained_tokens", 0)),
        )


class PunktTrainer:
    def __init__(self) -> None:
        self.with_period: Counter[str] = Counter()
        self.without_period: Counter[str] = Counter()
        self.ortho: Counter[tuple[str, int]] = Counter()
        self.after_break: Counter[str] = Counter()
        self.type_count: Counter[str] = Counter()
        self.n_tokens = 0
        self.n_periods = 0
        self.n_breaks = 0

    def add_text(self, text: str) -> None:
        for para in text.split("\n\n"):
            toks = [t for t in tokenize(para) if t.kind in {"word", "num", "punct"}]
            self._add_tokens(toks)

    def _add_tokens(self, toks: list[Token]) -> None:
        prev_final = True  # paragraph start behaves like a sentence start
        for i, t in enumerate(toks):
            self.n_tokens += 1
            if t.kind == "word":
                ty = _type(t.text)
                self.type_count[ty] += 1
                if t.text.endswith("."):
                    self.with_period[ty] += 1
                    self.n_periods += 1
                else:
                    self.without_period[ty] += 1
                first = t.text[:1]
                if first.isalpha():
                    flag = (
                        (ORTHO_BEG_UC if first.isupper() else ORTHO_BEG_LC)
                        if prev_final
                        else (ORTHO_MID_UC if first.isupper() else ORTHO_MID_LC)
                    )
                    self.ortho[(ty, flag)] += 1
                if prev_final:
                    self.after_break[ty] += 1
                    self.n_breaks += 1
                # A plain (non-abbreviation-looking) sentence end: long word + period + capital next.
                nxt = toks[i + 1] if i + 1 < len(toks) else None
                prev_final = t.text.endswith(".") and len(ty) > 3 and (nxt is None or nxt.text[:1].isupper())
            elif t.kind == "punct":
                if t.text in {"?", "!"}:
                    prev_final = True
                elif t.text == ".":
                    self.n_periods += 1
                    prev_final = True
                elif t.text not in _CLOSERS:
                    prev_final = False
            else:
                prev_final = False

    def train(self, min_count: int = 2) -> PunktModel:
        model = PunktModel(trained_tokens=self.n_tokens)
        n = max(1, self.n_tokens)
        for ty, c_with in self.with_period.items():
            if c_with < min_count or not ty or not ty.replace(".", "").isalpha():
                continue
            c_without = self.without_period.get(ty, 0)
            ll = dunning_ll(c_with + c_without, max(1, self.n_periods), c_with, n)
            ratio = c_with / (c_with + c_without)
            if ratio < MIN_PERIOD_RATIO:
                continue
            nonperiod = len(ty.replace(".", ""))
            f_length = math.exp(-nonperiod)
            f_periods = ty.count(".") + 1
            if ll * f_length * f_periods * ratio**4 >= ABBREV_THRESHOLD:
                model.abbreviations.add(ty)
        ortho: dict[str, int] = {}
        for (ty, flag), c in self.ortho.items():
            if c >= 1:
                ortho[ty] = ortho.get(ty, 0) | flag
        model.ortho = {ty: v for ty, v in ortho.items() if self.type_count[ty] >= 2}
        if self.n_breaks:
            for ty, c in self.after_break.items():
                total = self.type_count[ty]
                capitalised = model.ortho.get(ty, 0) & ORTHO_BEG_UC
                if c >= 10 and total and c / total > 0.5 and capitalised and ty not in model.abbreviations:
                    model.starters.add(ty)
        return model


def train_model(texts: Iterable[str]) -> PunktModel:
    tr = PunktTrainer()
    for t in texts:
        tr.add_text(t)
    return tr.train()


class SentenceSplitter:
    def __init__(self, model: PunktModel | None = None) -> None:
        self.model = model or PunktModel()

    def _starts_sentence(self, tok: Token) -> bool:
        """Orthographic evidence that a capitalised token begins a sentence."""
        ty = _type(tok.text)
        if ty in self.model.starters:
            return True
        flags = self.model.ortho.get(ty, 0)
        first = tok.text[:1]
        if first.isupper():
            # seen lower-case mid-sentence, never capitalised mid-sentence → capital here signals a start
            return bool(flags & ORTHO_MID_LC) and not flags & ORTHO_MID_UC
        return False

    def _is_boundary(self, toks: list[Token], i: int, text: str) -> bool:
        t = toks[i]
        nxt_i = i + 1
        while nxt_i < len(toks) and toks[nxt_i].text in _CLOSERS:
            nxt_i += 1
        if nxt_i >= len(toks):
            return True
        nxt = toks[nxt_i]
        if text[toks[nxt_i - 1].end : nxt.start].strip(" \t") == "" and nxt.start == toks[nxt_i - 1].end:
            return False  # no whitespace: "3.5", "e.g.x"
        first = nxt.text[:1]
        if t.text in {"?", "!"}:
            return not first.islower()
        if first.islower():
            return False
        if t.kind == "word" and t.text.endswith("."):
            ty = _type(t.text)
            is_abbrev = ty in self.model.abbreviations or (len(ty) == 1 and ty.isalpha()) or "." in ty
            if is_abbrev:
                return self._starts_sentence(nxt)
            if nxt.kind == "num" and t.text[:1].isupper() and len(ty) <= 4:
                return False  # "Jan. 5", "Vol. 3", "Fig. 2": a short capitalised word qualifying a number
            return first.isupper() or first.isdigit() or first in _OPENERS or nxt.kind == "punct"
        if t.text in {".", "…", "..."}:
            if t.text != "." and not (first.isupper() and self._starts_sentence(nxt)):
                return first.isupper() and _type(nxt.text) in self.model.starters
            return first.isupper() or first.isdigit() or first in _OPENERS
        return False

    def spans(self, text: str) -> list[tuple[int, int]]:
        """Character spans of sentences; paragraph breaks always end a sentence."""
        out: list[tuple[int, int]] = []
        offset = 0
        for para in text.split("\n"):
            toks = tokenize(para)
            start = None
            skip_to = 0
            for i, t in enumerate(toks):
                if i < skip_to:
                    continue
                if start is None:
                    start = t.start
                terminal = t.text in _TERMINAL or (t.kind == "word" and t.text.endswith("."))
                if terminal and self._is_boundary(toks, i, para):
                    end = t.end
                    j = i + 1
                    while j < len(toks) and toks[j].text in _CLOSERS and toks[j].start == end:
                        end = toks[j].end
                        j += 1
                    out.append((offset + start, offset + end))
                    start = None
                    skip_to = j
            if start is not None and toks:
                out.append((offset + start, offset + toks[-1].end))
            offset += len(para) + 1
        merged: list[tuple[int, int]] = []
        for a, b in out:  # closers consumed above can leave an empty tail sentence
            if merged and a < merged[-1][1]:
                continue
            merged.append((a, b))
        return merged

    def split(self, text: str) -> list[str]:
        return [text[a:b] for a, b in self.spans(text)]
