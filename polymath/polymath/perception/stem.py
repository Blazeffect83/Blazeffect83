"""Porter (1980) stemmer — the same algorithm SQLite FTS5's ``porter`` tokenizer applies.

Used to map query words to index terms (for document-frequency lookups) and as
a light normaliser for perception statistics.
"""

from __future__ import annotations

from functools import lru_cache

_VOWELS = frozenset("aeiou")


def _cons(w: str, i: int) -> bool:
    ch = w[i]
    if ch in _VOWELS:
        return False
    if ch == "y":
        return i == 0 or not _cons(w, i - 1)
    return True


def _m(stem: str) -> int:
    """Number of VC sequences in ``stem``."""
    n = 0
    i = 0
    L = len(stem)
    while i < L and _cons(stem, i):
        i += 1
    while i < L:
        while i < L and not _cons(stem, i):
            i += 1
        if i >= L:
            break
        n += 1
        while i < L and _cons(stem, i):
            i += 1
    return n


def _has_vowel(stem: str) -> bool:
    return any(not _cons(stem, i) for i in range(len(stem)))


def _double_cons(w: str) -> bool:
    return len(w) >= 2 and w[-1] == w[-2] and _cons(w, len(w) - 1)


def _cvc(w: str) -> bool:
    if len(w) < 3:
        return False
    return _cons(w, len(w) - 3) and not _cons(w, len(w) - 2) and _cons(w, len(w) - 1) and w[-1] not in "wxy"


def _replace(w: str, suffix: str, repl: str, min_m: int) -> str | None:
    if w.endswith(suffix):
        stem = w[: len(w) - len(suffix)]
        return stem + repl if _m(stem) > min_m else w
    return None


_STEP2 = [
    ("ational", "ate"),
    ("tional", "tion"),
    ("enci", "ence"),
    ("anci", "ance"),
    ("izer", "ize"),
    ("bli", "ble"),
    ("alli", "al"),
    ("entli", "ent"),
    ("eli", "e"),
    ("ousli", "ous"),
    ("ization", "ize"),
    ("ation", "ate"),
    ("ator", "ate"),
    ("alism", "al"),
    ("iveness", "ive"),
    ("fulness", "ful"),
    ("ousness", "ous"),
    ("aliti", "al"),
    ("iviti", "ive"),
    ("biliti", "ble"),
    ("logi", "log"),
]
_STEP3 = [("icate", "ic"), ("ative", ""), ("alize", "al"), ("iciti", "ic"), ("ical", "ic"), ("ful", ""), ("ness", "")]
_STEP4 = [
    "al",
    "ance",
    "ence",
    "er",
    "ic",
    "able",
    "ible",
    "ant",
    "ement",
    "ment",
    "ent",
    "ion",
    "ou",
    "ism",
    "ate",
    "iti",
    "ous",
    "ive",
    "ize",
]


@lru_cache(maxsize=200_000)
def stem(word: str) -> str:
    w = word.lower()
    if len(w) <= 2:
        return w
    # Step 1a
    if w.endswith("sses") or w.endswith("ies"):
        w = w[:-2]
    elif w.endswith("ss"):
        pass
    elif w.endswith("s"):
        w = w[:-1]
    # Step 1b
    flag = False
    if w.endswith("eed"):
        if _m(w[:-3]) > 0:
            w = w[:-1]
    elif w.endswith("ed") and _has_vowel(w[:-2]):
        w, flag = w[:-2], True
    elif w.endswith("ing") and _has_vowel(w[:-3]):
        w, flag = w[:-3], True
    if flag:
        if w.endswith(("at", "bl", "iz")):
            w += "e"
        elif _double_cons(w) and w[-1] not in "lsz":
            w = w[:-1]
        elif _m(w) == 1 and _cvc(w):
            w += "e"
    # Step 1c
    if w.endswith("y") and _has_vowel(w[:-1]):
        w = w[:-1] + "i"
    # Step 2
    for suf, rep in _STEP2:
        if w.endswith(suf):
            r = _replace(w, suf, rep, 0)
            w = r if r is not None else w
            break
    # Step 3
    for suf, rep in _STEP3:
        if w.endswith(suf):
            r = _replace(w, suf, rep, 0)
            w = r if r is not None else w
            break
    # Step 4
    for suf in _STEP4:
        if w.endswith(suf):
            stem_ = w[: len(w) - len(suf)]
            if suf == "ion":
                if _m(stem_) > 1 and stem_[-1:] in ("s", "t"):
                    w = stem_
            elif _m(stem_) > 1:
                w = stem_
            break
    # Step 5a
    if w.endswith("e"):
        s = w[:-1]
        if _m(s) > 1 or (_m(s) == 1 and not _cvc(s)):
            w = s
    # Step 5b
    if _m(w) > 1 and _double_cons(w) and w.endswith("l"):
        w = w[:-1]
    return w
