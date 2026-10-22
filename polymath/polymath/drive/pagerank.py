"""PageRank over the entity graph (sparse power iteration in numpy, no external graph library)."""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

from polymath.core.db import Database


def sparse_pagerank(
    src: npt.NDArray[np.int64],
    dst: npt.NDArray[np.int64],
    n: int,
    *,
    damping: float = 0.85,
    iters: int = 50,
    tol: float = 1e-9,
) -> npt.NDArray[np.float64]:
    """PageRank for edges src→dst over nodes 0..n-1. Dangling mass is spread uniformly."""
    if n == 0:
        return np.zeros(0)
    out_deg = np.bincount(src, minlength=n).astype(np.float64)
    r = np.full(n, 1.0 / n)
    w = 1.0 / np.where(out_deg[src] > 0, out_deg[src], 1.0)
    for _ in range(iters):
        contrib = np.bincount(dst, weights=r[src] * w, minlength=n)
        dangling = r[out_deg == 0].sum()
        nxt: npt.NDArray[np.float64] = (1 - damping) / n + damping * (contrib + dangling / n)
        if np.abs(nxt - r).sum() < tol:
            return nxt
        r = nxt
    return r


def entity_pagerank(db: Database, *, batch: int = 500_000) -> dict[str, float]:
    """Compute PageRank over entity–entity triples (both directions count as links) and store it."""
    ids = np.array([int(r["id"]) for r in db.query("SELECT id FROM entities ORDER BY id")], dtype=np.int64)
    if ids.size == 0:
        return {"entities": 0}
    pos = {int(e): i for i, e in enumerate(ids)}
    srcs, dsts = [], []
    cursor = 0
    while True:
        rows = db.query(
            "SELECT id, s, o FROM triples WHERE id > ? AND o != 0 AND holdout=0 AND status != 'disputed' "
            "ORDER BY id LIMIT ?",
            (cursor, batch),
        )
        if not rows:
            break
        for r in rows:
            a, b = pos.get(int(r["s"])), pos.get(int(r["o"]))
            if a is not None and b is not None and a != b:
                srcs.append(a)
                dsts.append(b)
        cursor = int(rows[-1]["id"])
    # Co-mentions in documents are links too (entity A's article links to B).
    for r in db.query(
        "SELECT d.doc_id, d.entity_id, e.id AS src FROM doc_entities d JOIN entities e "
        "ON e.doc_id = d.doc_id WHERE e.id != d.entity_id"
    ):
        a, b = pos.get(int(r["src"])), pos.get(int(r["entity_id"]))
        if a is not None and b is not None:
            srcs.append(a)
            dsts.append(b)
    src = np.array(srcs, dtype=np.int64)
    dst = np.array(dsts, dtype=np.int64)
    pr = sparse_pagerank(np.concatenate([src, dst]), np.concatenate([dst, src]), len(ids))
    scaled = pr * len(ids)  # 1.0 = average importance
    db.executemany(
        "UPDATE entities SET pagerank=?, degree=? WHERE id=?",
        [
            (float(scaled[i]), int(d), int(ids[i]))
            for i, d in enumerate(np.bincount(np.concatenate([src, dst]), minlength=len(ids)))
        ],
    )
    return {"entities": len(ids), "edges": len(src), "max": float(scaled.max())}
