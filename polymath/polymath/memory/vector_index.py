"""Approximate nearest-neighbour search: IVF (k-means coarse quantiser) + int8 vectors in a memmap.

Layout of one index generation (``<dir>/gen-<n>/``):

* ``centroids.npy`` (nlist × d, float32, unit length) — trained by spherical
  k-means (Lloyd iterations on a sample, numpy).
* ``codes.i8`` (N × d int8 memmap) and ``scales.f32`` (N float32): each unit
  vector stored as round(x / max|x| · 127) with its scale — 4× smaller than
  float32 and accurate to ~0.4% in dot products.
* ``ids.i64`` (N) and ``offsets.npy`` (nlist + 1): vectors are grouped by
  list, so a probe reads contiguous memory.

New vectors go to an append-only *tail* (searched exhaustively) until a rebuild
folds them in. Rebuilds write a new generation and atomically switch
``CURRENT``, so readers never see a half-written index. Queries are cosine
similarity (vectors are unit length).
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

F32 = npt.NDArray[np.float32]
I64 = npt.NDArray[np.int64]


def quantize(x: F32) -> tuple[npt.NDArray[np.int8], F32]:
    m = np.abs(x).max(axis=1)
    m = np.where(m == 0, 1.0, m).astype(np.float32)
    codes = np.rint(x / m[:, None] * 127.0).astype(np.int8)
    scales: F32 = (m / 127.0).astype(np.float32)
    return codes, scales


def normalize(x: F32) -> F32:
    n = np.linalg.norm(x, axis=1, keepdims=True)
    out: F32 = (x / np.maximum(n, 1e-12)).astype(np.float32)
    return out


def kmeans(x: F32, k: int, *, iters: int = 20, seed: int = 0, batch: int = 65536) -> F32:
    """Spherical k-means (cosine). ``x`` must be unit length. k-means++-lite init by random distinct rows."""
    rng = np.random.default_rng(seed)
    k = min(k, len(x))
    cent = x[rng.choice(len(x), size=k, replace=False)].copy()
    for _ in range(iters):
        assign = assign_lists(x, cent, batch=batch)
        sums = np.zeros_like(cent)
        np.add.at(sums, assign, x)
        counts = np.bincount(assign, minlength=k)
        empty = counts == 0
        if empty.any():  # re-seed empty clusters with random points
            sums[empty] = x[rng.choice(len(x), size=int(empty.sum()))]
        cent = normalize(sums)
    return cent


def assign_lists(x: F32, centroids: F32, batch: int = 65536) -> npt.NDArray[np.int64]:
    out = np.empty(len(x), dtype=np.int64)
    for i in range(0, len(x), batch):
        out[i : i + batch] = np.argmax(x[i : i + batch] @ centroids.T, axis=1)
    return out


class IVFIndex:
    def __init__(self, directory: Path, dim: int) -> None:
        self.dir = directory
        self.dim = dim
        self.dir.mkdir(parents=True, exist_ok=True)
        self._gen: dict[str, Any] | None = None
        self._loaded_gen = -1

    # ------------------------------------------------------------ generations
    def _current(self) -> int:
        try:
            return int((self.dir / "CURRENT").read_text().strip())
        except (OSError, ValueError):
            return -1

    def _load(self) -> dict[str, Any] | None:
        gen = self._current()
        if gen < 0:
            return None
        if self._gen is not None and self._loaded_gen == gen:
            return self._gen
        g = self.dir / f"gen-{gen}"
        meta = json.loads((g / "meta.json").read_text())
        n = int(meta["n"])
        data = {
            "meta": meta,
            "centroids": np.load(g / "centroids.npy"),
            "offsets": np.load(g / "offsets.npy"),
            "codes": np.memmap(g / "codes.i8", dtype=np.int8, mode="r", shape=(n, self.dim))
            if n
            else np.zeros((0, self.dim), np.int8),
            "scales": np.memmap(g / "scales.f32", dtype=np.float32, mode="r", shape=(n,))
            if n
            else np.zeros(0, np.float32),
            "ids": np.memmap(g / "ids.i64", dtype=np.int64, mode="r", shape=(n,)) if n else np.zeros(0, np.int64),
        }
        self._gen, self._loaded_gen = data, gen
        return data

    def _tail_paths(self) -> tuple[Path, Path, Path]:
        return self.dir / "tail.i8", self.dir / "tail_scales.f32", self.dir / "tail_ids.i64"

    def _tail(self) -> tuple[npt.NDArray[np.int8], F32, I64]:
        codes_p, scales_p, ids_p = self._tail_paths()

        def read(path: Path, dtype: Any) -> Any:
            return np.fromfile(path, dtype=dtype) if path.exists() else np.zeros(0, dtype=dtype)

        ids = read(ids_p, np.int64)
        scales = read(scales_p, np.float32)
        codes = read(codes_p, np.int8)
        n = min(len(ids), len(scales), codes.size // self.dim)  # tolerate a torn last append
        return codes[: n * self.dim].reshape(n, self.dim), scales[:n], ids[:n]

    # ---------------------------------------------------------------- writes
    def add(self, ids: I64, vectors: F32) -> None:
        """Append unit vectors to the tail (durable once this returns)."""
        if len(ids) == 0:
            return
        codes, scales = quantize(normalize(vectors.astype(np.float32)))
        codes_p, scales_p, ids_p = self._tail_paths()
        for path, arr in ((codes_p, codes), (scales_p, scales), (ids_p, ids.astype(np.int64))):
            with open(path, "ab") as fh:
                fh.write(arr.tobytes())
                fh.flush()
                os.fsync(fh.fileno())

    def rebuild(self, *, nlist: int = 0, sample: int = 100_000, iters: int = 15, seed: int = 0) -> dict[str, Any]:
        """Fold the tail into a new generation (re-training centroids). Superseded ids keep the newest vector."""
        t0 = time.monotonic()
        old = self._load()
        parts_codes, parts_scales, parts_ids = [], [], []
        if old is not None and old["meta"]["n"]:
            parts_codes.append(np.asarray(old["codes"]))
            parts_scales.append(np.asarray(old["scales"]))
            parts_ids.append(np.asarray(old["ids"]))
        tc, ts, ti = self._tail()
        parts_codes.append(tc)
        parts_scales.append(ts)
        parts_ids.append(ti)
        codes = np.concatenate(parts_codes) if parts_codes else np.zeros((0, self.dim), np.int8)
        scales = np.concatenate(parts_scales)
        ids = np.concatenate(parts_ids)
        # keep the last occurrence of each id (tail entries override older ones)
        _, last = np.unique(ids[::-1], return_index=True)
        keep = np.sort(len(ids) - 1 - last)
        codes, scales, ids = codes[keep], scales[keep], ids[keep]
        n = len(ids)
        gen = self._current() + 1
        g = self.dir / f"gen-{gen}"
        if g.exists():
            shutil.rmtree(g)
        g.mkdir(parents=True)
        nlist = nlist or max(1, min(4096, int(np.sqrt(max(n, 1)))))
        if n:
            rng = np.random.default_rng(seed)
            pick = rng.choice(n, size=min(sample, n), replace=False)
            train = normalize(codes[pick].astype(np.float32) * scales[pick, None])
            centroids = kmeans(train, nlist, iters=iters, seed=seed)
            assign = np.empty(n, dtype=np.int64)
            for i in range(0, n, 65536):
                block = codes[i : i + 65536].astype(np.float32) * scales[i : i + 65536, None]
                assign[i : i + 65536] = np.argmax(block @ centroids.T, axis=1)
            order = np.argsort(assign, kind="stable")
            codes, scales, ids, assign = codes[order], scales[order], ids[order], assign[order]
            offsets = np.searchsorted(assign, np.arange(len(centroids) + 1))
        else:
            centroids = np.zeros((1, self.dim), np.float32)
            offsets = np.zeros(2, np.int64)
        np.save(g / "centroids.npy", centroids.astype(np.float32))
        np.save(g / "offsets.npy", offsets.astype(np.int64))
        for name, arr in (("codes.i8", codes), ("scales.f32", scales), ("ids.i64", ids)):
            with open(g / name, "wb") as fh:
                fh.write(np.ascontiguousarray(arr).tobytes())
                fh.flush()
                os.fsync(fh.fileno())
        meta = {"n": int(n), "nlist": len(centroids), "dim": self.dim, "built": time.time()}
        (g / "meta.json").write_text(json.dumps(meta))
        tmp = self.dir / "CURRENT.tmp"
        tmp.write_text(str(gen))
        os.replace(tmp, self.dir / "CURRENT")
        for p in self._tail_paths():
            p.unlink(missing_ok=True)
        for old_gen in self.dir.glob("gen-*"):
            if old_gen != g:
                shutil.rmtree(old_gen, ignore_errors=True)
        self._gen = None
        return {**meta, "seconds": round(time.monotonic() - t0, 2)}

    # ---------------------------------------------------------------- search
    def search(
        self, query: F32, k: int = 10, *, nprobe: int = 16, exclude: set[int] | None = None
    ) -> list[tuple[int, float]]:
        q = query.astype(np.float32)
        q = q / max(float(np.linalg.norm(q)), 1e-12)
        cand_ids: list[I64] = []
        cand_scores: list[F32] = []
        g = self._load()
        if g is not None and g["meta"]["n"]:
            cs = g["centroids"] @ q
            probe = np.argsort(-cs)[: max(1, nprobe)]
            offs = g["offsets"]
            for lst in probe:
                a, b = int(offs[lst]), int(offs[lst + 1])
                if a == b:
                    continue
                block = np.asarray(g["codes"][a:b], dtype=np.float32)
                cand_scores.append((block @ q) * np.asarray(g["scales"][a:b]))
                cand_ids.append(np.asarray(g["ids"][a:b]))
        tc, ts, ti = self._tail()
        if len(ti):
            cand_scores.append((tc.astype(np.float32) @ q) * ts)
            cand_ids.append(ti)
        if not cand_ids:
            return []
        ids = np.concatenate(cand_ids)
        scores = np.concatenate(cand_scores)
        if exclude:
            mask = ~np.isin(ids, np.fromiter(exclude, dtype=np.int64))
            ids, scores = ids[mask], scores[mask]
        if len(ids) == 0:
            return []
        top = np.argpartition(-scores, min(k, len(scores) - 1))[: k * 3]
        top = top[np.argsort(-scores[top])]
        out: list[tuple[int, float]] = []
        seen: set[int] = set()
        for i in top:  # an id can appear twice (indexed + newer tail copy): first (best) wins
            doc = int(ids[i])
            if doc not in seen:
                seen.add(doc)
                out.append((doc, float(scores[i])))
            if len(out) >= k:
                break
        return out

    def stats(self) -> dict[str, Any]:
        g = self._load()
        _tc, _ts, ti = self._tail()
        return {
            "indexed": int(g["meta"]["n"]) if g else 0,
            "nlist": int(g["meta"]["nlist"]) if g else 0,
            "tail": len(ti),
            "generation": self._current(),
        }

    def needs_rebuild(self, tail_share: float = 0.05, min_tail: int = 1000) -> bool:
        st = self.stats()
        big_tail = st["tail"] >= min_tail and st["tail"] > tail_share * max(st["indexed"], 1)
        return bool(big_tail or (st["indexed"] == 0 and st["tail"] > 0))
