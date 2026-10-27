"""Own tokenizer: Unicode words, numbers, URLs/e-mails and punctuation, with character offsets."""

from __future__ import annotations

import re
from dataclasses import dataclass

_TOKEN = re.compile(
    r"""
    (?P<url>https?://[^\s<>"'\]\)]+|www\.[^\s<>"'\]\)]+)
  | (?P<email>[\w.+-]+@[\w-]+(?:\.[\w-]+)+)
  | (?P<num>[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][+-]?\d+)?%?)
  | (?P<abbr>(?:[^\W\d_]{1,3}\.){2,})
  | (?P<word>[^\W\d_](?:[\w’'·]*[\w])?(?:[-‐][^\W_][\w’']*)*(?:\.(?!\.))?)
  | (?P<punct>\.\.\.|…|--|[^\w\s])
    """,
    re.X | re.U,
)
_WORDISH = re.compile(r"[^\W_]", re.U)


@dataclass(frozen=True, slots=True)
class Token:
    text: str
    start: int
    end: int
    kind: str  # word | num | url | email | punct

    @property
    def lower(self) -> str:
        return self.text.lower()

    @property
    def is_word(self) -> bool:
        return self.kind == "word"


def tokenize(text: str) -> list[Token]:
    """Tokens with offsets. A word may keep a trailing period (``Dr.``); see :func:`split_period`."""
    out = []
    for m in _TOKEN.finditer(text):
        kind = m.lastgroup or "punct"
        if kind == "abbr":
            kind = "word"
        out.append(Token(m.group(0), m.start(), m.end(), kind))
    return out


def split_period(tokens: list[Token]) -> list[Token]:
    """Detach trailing periods from words (``U.S.`` keeps internal periods)."""
    out = []
    for t in tokens:
        if t.kind == "word" and t.text.endswith(".") and len(t.text) > 1:
            out.append(Token(t.text[:-1], t.start, t.end - 1, "word"))
            out.append(Token(".", t.end - 1, t.end, "punct"))
        else:
            out.append(t)
    return out


def words(text: str) -> list[str]:
    """Lower-cased word and number tokens (no punctuation)."""
    res = []
    for t in tokenize(text):
        if t.kind == "word":
            res.append(t.text.rstrip(".").lower())
        elif t.kind == "num":
            res.append(t.text)
    return res


def normalize_token(tok: str) -> str:
    """Matching form: lower-case, curly apostrophes folded, trailing period removed."""
    return tok.lower().replace("’", "'").rstrip(".")


def is_wordish(s: str) -> bool:
    return bool(_WORDISH.search(s))
