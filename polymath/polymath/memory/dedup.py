"""Near-duplicate detection: 64-bit SimHash and 128-permutation MinHash with LSH banding.

* SimHash (Charikar) over weighted word 3-shingles: documents within a small
  Hamming distance are near-identical (mirrors, reformatted copies).
* MinHash estimates Jaccard similarity of shingle sets; LSH with ``BANDS`` bands
  of ``ROWS`` rows makes candidate lookup sub-linear. The S-curve threshold is
  (1/BANDS)^(1/ROWS) ≈ 0.71 for 16×8, and candidates are confirmed with the full
  signature (estimated Jaccard ≥ ``NEAR_DUP``).
"""

from __future__ import annotations

import hashlib
import re

import numpy as np
import numpy.typing as npt

from polymath.core.db import Database

PERMS = 128
BANDS = 16
ROWS = PERMS // BANDS
NEAR_DUP = 0.85
_MERSENNE = np.uint64((1 << 61) - 1)
_rng = np.random.default_rng(20261009)  # fixed seed: signatures must be stable across restarts
_A = _rng.integers(1, (1 << 32) - 1, size=PERMS, dtype=np.uint64)
_B = _rng.integers(0, (1 << 32) - 1, size=PERMS, dtype=np.uint64)
_WORD = re.compile(r"\w+", re.U)
_MASK64 = (1 << 64) - 1


def _h64(s: str) -> int:
    return int.from_bytes(hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest(), "little")


def shingles(text: str, k: int = 3) -> list[str]:
    words = _WORD.findall(text.lower())
    if len(words) < k:
        return [" ".join(words)] if words else []
    return [" ".join(words[i : i + k]) for i in range(len(words) - k + 1)]


def simhash(text: str) -> int:
    """64-bit SimHash as a *signed* int (SQLite INTEGER range)."""
    counts: dict[str, int] = {}
    for sh in shingles(text):
        counts[sh] = counts.get(sh, 0) + 1
    if not counts:
        return 0
    hashes = np.fromiter((_h64(s) for s in counts), dtype=np.uint64, count=len(counts))
    weights = np.fromiter(counts.values(), dtype=np.float64, count=len(counts))
    bits = ((hashes[:, None] >> np.arange(64, dtype=np.uint64)[None, :]) & np.uint64(1)).astype(np.float64)
    vec = (weights[:, None] * (2 * bits - 1)).sum(axis=0)
    value = 0
    for i in np.nonzero(vec > 0)[0]:
        value |= 1 << int(i)
    return value - (1 << 64) if value >= 1 << 63 else value


def hamming(a: int, b: int) -> int:
    return bin((a ^ b) & _MASK64).count("1")


def minhash(text: str) -> npt.NDArray[np.uint64]:
    sh = set(shingles(text))
    if not sh:
        return np.full(PERMS, np.iinfo(np.uint64).max, dtype=np.uint64)
    x = np.fromiter((_h64(s) & 0xFFFFFFFF for s in sh), dtype=np.uint64, count=len(sh))
    # (a*x + b) mod (2^61 - 1); a, x < 2^32 so the product fits in uint64 exactly.
    sig: npt.NDArray[np.uint64] = ((_A[:, None] * x[None, :] + _B[:, None]) % _MERSENNE).min(axis=1)
    return sig.astype(np.uint64)


def band_hashes(sig: npt.NDArray[np.uint64]) -> list[int]:
    out = []
    for b in range(BANDS):
        chunk = sig[b * ROWS : (b + 1) * ROWS].tobytes()
        out.append(int.from_bytes(hashlib.blake2b(chunk, digest_size=8).digest(), "little", signed=True))
    return out


def jaccard_estimate(a: npt.NDArray[np.uint64], b: npt.NDArray[np.uint64]) -> float:
    return float(np.mean(a == b))


class NearDupIndex:
    def __init__(self, db: Database) -> None:
        self.db = db

    def find(self, sig: npt.NDArray[np.uint64], *, exclude: int | None = None) -> tuple[int, float] | None:
        """Best earlier document with estimated Jaccard ≥ NEAR_DUP, if any."""
        bands = band_hashes(sig)
        cands: set[int] = set()
        for b, h in enumerate(bands):
            for row in self.db.query("SELECT doc_id FROM minhash_bands WHERE band=? AND h=?", (b, h)):
                if row["doc_id"] != exclude:
                    cands.add(int(row["doc_id"]))
        best: tuple[int, float] | None = None
        for doc_id in cands:
            blob = self.db.scalar("SELECT sig FROM minhash_sigs WHERE doc_id=?", (doc_id,))
            if blob is None:
                continue
            j = jaccard_estimate(sig, np.frombuffer(blob, dtype=np.uint64))
            if j >= NEAR_DUP and (best is None or j > best[1]):
                best = (doc_id, j)
        return best

    def add(self, doc_id: int, sig: npt.NDArray[np.uint64]) -> None:
        self.db.executemany(
            "INSERT OR IGNORE INTO minhash_bands(band, h, doc_id) VALUES(?,?,?)",
            [(b, h, doc_id) for b, h in enumerate(band_hashes(sig))],
        )
        self.db.execute("INSERT OR REPLACE INTO minhash_sigs(doc_id, sig) VALUES(?,?)", (doc_id, sig.tobytes()))

    def remove(self, doc_id: int) -> None:
        blob = self.db.scalar("SELECT sig FROM minhash_sigs WHERE doc_id=?", (doc_id,))
        if blob is None:
            return
        bands = band_hashes(np.frombuffer(blob, dtype=np.uint64))
        self.db.executemany(
            "DELETE FROM minhash_bands WHERE band=? AND h=? AND doc_id=?", [(b, h, doc_id) for b, h in enumerate(bands)]
        )
        self.db.execute("DELETE FROM minhash_sigs WHERE doc_id=?", (doc_id,))
