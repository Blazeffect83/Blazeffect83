"""Exact and near-duplicate detection (SHA-256 + 64-bit SimHash + Jaccard)."""
from __future__ import annotations

import hashlib
import re

_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset("""a an the and or but of to in on for with by at from as is are was were be been being it its
this that these those which who whom what when where why how not no can could should would may might will shall
do does did has have had than then there their they them we you your our i he she his her""".split())


def normalize(text: str) -> str:
    return " ".join(_WORD.findall(text.lower()))


def content_hash(text: str) -> str:
    return hashlib.sha256(normalize(text).encode()).hexdigest()


def tokens(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in _STOP]


def shingles(text: str, k: int = 3) -> set[str]:
    t = tokens(text)
    if len(t) < k:
        return {" ".join(t)} if t else set()
    return {" ".join(t[i:i + k]) for i in range(len(t) - k + 1)}


def simhash(text: str) -> int:
    v = [0] * 64
    for sh in shingles(text):
        h = int.from_bytes(hashlib.blake2b(sh.encode(), digest_size=8).digest(), "big")
        for i in range(64):
            v[i] += 1 if (h >> i) & 1 else -1
    out = 0
    for i in range(64):
        if v[i] > 0:
            out |= 1 << i
    return out


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def is_near_duplicate(a_hash: int, b_hash: int, threshold: int = 6) -> bool:
    return hamming(a_hash, b_hash) <= threshold


def jaccard(a: str, b: str) -> float:
    sa, sb = set(tokens(a)), set(tokens(b))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def claim_key(text: str) -> str:
    return hashlib.sha256(" ".join(tokens(text)).encode()).hexdigest()
