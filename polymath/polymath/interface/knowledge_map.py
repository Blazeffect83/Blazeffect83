"""The knowledge map: topics as a star map that grows as it learns, with a time-lapse.

* **Nodes** are the ``NODES`` topics with the most documents, sized by documents read and lit up by how much
  was read in the last day and week.
* **Edges** join topics that share documents (co-occurrence, normalised by size, so a big topic does not link to
  everything). Connected groups are found by label propagation and coloured as clusters.
* **Layout** is a force-directed layout (Fruchterman–Reingold) computed here with numpy. It is seeded with the
  previous positions (stored in ``kv``), so the map stays recognisable from day to day and new topics appear next
  to their closest neighbours.
* **Time-lapse**: how many documents each topic had at the end of each day. It is rebuilt from when every document
  was read, so there is a history on the first day. ``memory.snapshot`` also records each day's sizes
  (``topic_snapshots``), so the history survives documents being evicted to free disk space.
"""

from __future__ import annotations

import math
import time
from collections import defaultdict
from typing import Any

import numpy as np

from polymath.core.db import Database
from polymath.core.jobs import JobContext, JobOutcome

NODES = 120
EDGES_PER_NODE = 4
ITERATIONS = 250
GRAVITY = 0.6
SNAPSHOT_TOPICS = 300
FRAMES = 96  # time-lapse steps at most
STEPS = (3600.0, 3 * 3600.0, 6 * 3600.0, 86400.0, 2 * 86400.0, 7 * 86400.0)
DAY = 86400.0


def day_of(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def top_topics(db: Database, limit: int = NODES) -> list[dict[str, Any]]:
    now = time.time()
    return [
        {
            "id": int(r["id"]),
            "name": str(r["name"]),
            "kind": str(r["kind"]),
            "docs": int(r["docs"]),
            "day": int(r["day"] or 0),
            "week": int(r["week"] or 0),
        }
        for r in db.query(
            "SELECT t.id, t.name, t.kind, COUNT(*) AS docs, SUM(d.fetched >= ?) AS day, SUM(d.fetched >= ?) AS week "
            "FROM doc_topics dt JOIN topics t ON t.id = dt.topic_id JOIN documents d ON d.id = dt.doc_id "
            "WHERE d.state != 'duplicate' GROUP BY t.id ORDER BY docs DESC LIMIT ?",
            (now - DAY, now - 7 * DAY, limit),
        )
    ]


def cooccurrence(db: Database, ids: list[int]) -> dict[tuple[int, int], int]:
    if len(ids) < 2:
        return {}
    marks = ",".join("?" * len(ids))
    out: dict[tuple[int, int], int] = {}
    for r in db.query(
        f"SELECT a.topic_id AS a, b.topic_id AS b, COUNT(*) AS n FROM doc_topics a JOIN doc_topics b "
        f"ON b.doc_id = a.doc_id AND b.topic_id > a.topic_id WHERE a.topic_id IN ({marks}) "
        f"AND b.topic_id IN ({marks}) GROUP BY a.topic_id, b.topic_id",
        [*ids, *ids],
    ):
        out[(int(r["a"]), int(r["b"]))] = int(r["n"])
    return out


def strongest_edges(nodes: list[dict[str, Any]], co: dict[tuple[int, int], int]) -> list[tuple[int, int, float]]:
    """Each topic's ``EDGES_PER_NODE`` strongest links by cosine-normalised co-occurrence (union over both ends)."""
    size = {n["id"]: max(1, n["docs"]) for n in nodes}
    by: dict[int, list[tuple[float, int, int]]] = defaultdict(list)
    for (a, b), n in co.items():
        w = n / math.sqrt(size[a] * size[b])
        by[a].append((w, a, b))
        by[b].append((w, a, b))
    keep: dict[tuple[int, int], float] = {}
    for lst in by.values():
        for w, a, b in sorted(lst, reverse=True)[:EDGES_PER_NODE]:
            keep[(a, b)] = round(w, 4)
    return [(a, b, w) for (a, b), w in keep.items()]


def clusters(ids: list[int], edges: list[tuple[int, int, float]], rounds: int = 20) -> dict[int, int]:
    """Label propagation (weighted, deterministic order): densely linked topics end up with the same label."""
    label = {i: i for i in ids}
    nbrs: dict[int, list[tuple[int, float]]] = defaultdict(list)
    for a, b, w in edges:
        nbrs[a].append((b, w))
        nbrs[b].append((a, w))
    for _ in range(rounds):
        changed = False
        for i in ids:
            if not nbrs[i]:
                continue
            votes: dict[int, float] = defaultdict(float)
            for j, w in nbrs[i]:
                votes[label[j]] += w
            best = max(votes, key=lambda k: (votes[k], -k))
            if best != label[i]:
                label[i], changed = best, True
        if not changed:
            break
    sizes: dict[int, int] = defaultdict(int)
    for lab in label.values():
        sizes[lab] += 1
    order = {lab: k for k, lab in enumerate(sorted(sizes, key=lambda x: (-sizes[x], x)))}  # biggest cluster first
    return {i: order[label[i]] for i in ids}


def layout(
    ids: list[int],
    edges: list[tuple[int, int, float]],
    previous: dict[int, list[float]] | None = None,
    *,
    seed: int = 7,
) -> dict[int, tuple[float, float]]:
    """Fruchterman–Reingold in the unit square, warm-started from ``previous`` positions."""
    n = len(ids)
    if n == 0:
        return {}
    rng = np.random.default_rng(seed)
    index = {t: k for k, t in enumerate(ids)}
    pos = rng.random((n, 2))
    previous = previous or {}
    nbr: dict[int, list[int]] = defaultdict(list)
    for a, b, _w in edges:
        nbr[a].append(b)
        nbr[b].append(a)
    for t in ids:
        if t in previous:
            pos[index[t]] = previous[t]
    for t in ids:
        placed = [index[m] for m in nbr[t] if m in previous]
        if t not in previous and placed:  # a new topic starts next to a neighbour already on the map
            pos[index[t]] = pos[placed[0]] + rng.normal(0, 0.02, 2)
    k = math.sqrt(1.0 / n)
    w = np.zeros((n, n))
    for a, b, wt in edges:
        w[index[a], index[b]] = w[index[b], index[a]] = 1.0 + 4.0 * wt
    temp = 0.05 if previous else 0.15
    for _ in range(ITERATIONS):
        delta = pos[:, None, :] - pos[None, :, :]
        dist = np.sqrt((delta**2).sum(-1)) + 1e-9
        rep = (k * k / dist**2)[..., None] * delta  # every pair repels
        att = (w * dist / k)[..., None] * delta  # linked topics attract
        disp = (rep - att).sum(1) + (0.5 - pos) * GRAVITY  # gravity keeps separate clusters close
        length = np.sqrt((disp**2).sum(-1))[:, None] + 1e-9
        pos += disp / length * np.minimum(length, temp)
        temp *= 0.985
    lo, hi = pos.min(0), pos.max(0)
    pos = 0.04 + 0.92 * (pos - lo) / np.maximum(hi - lo, 1e-9)  # fill the square (no topic stuck on a wall)
    return {t: (round(float(pos[index[t], 0]), 4), round(float(pos[index[t], 1]), 4)) for t in ids}


def history(db: Database, ids: list[int], frames: int = FRAMES) -> tuple[list[float], dict[int, list[int]]]:
    """Documents per topic at the end of each step (rebuilt from read times, raised to recorded snapshots).

    The step adapts to how long it has been learning: hours in its first days, days later, at most ``frames``.
    Returns the step end times and, per topic, its size at each of them."""
    if not ids:
        return [], {}
    marks = ",".join("?" * len(ids))
    first = db.scalar(
        f"SELECT MIN(d.fetched) FROM doc_topics dt JOIN documents d ON d.id = dt.doc_id WHERE dt.topic_id IN ({marks})",
        ids,
    )
    snap_first = db.scalar(f"SELECT MIN(day) FROM topic_snapshots WHERE topic_id IN ({marks})", ids)
    if snap_first is not None:
        t0 = time.mktime(time.strptime(str(snap_first), "%Y-%m-%d"))
        first = t0 if first is None else min(float(first), t0)
    if first is None:
        return [], {}
    now = time.time()
    span = max(now - float(first), 3600.0)
    step = next((s for s in STEPS if span / s <= frames), STEPS[-1])
    start = max(float(first), now - frames * step)
    n = math.ceil((now - start) / step) or 1
    ends = [start + step * (k + 1) for k in range(n)]
    before: dict[int, int] = defaultdict(int)
    grid: dict[int, list[int]] = {t: [0] * n for t in ids}
    for r in db.query(
        f"SELECT dt.topic_id AS t, CAST((d.fetched - ?) / ? AS INTEGER) AS k, COUNT(*) AS c FROM doc_topics dt "
        f"JOIN documents d ON d.id = dt.doc_id WHERE dt.topic_id IN ({marks}) GROUP BY t, k",
        [start, step, *ids],
    ):
        k = int(r["k"] or 0)
        if k < 0:
            before[int(r["t"])] += int(r["c"])
        else:
            grid[int(r["t"])][min(k, n - 1)] += int(r["c"])
    snaps: dict[int, dict[int, int]] = defaultdict(dict)
    for r in db.query(f"SELECT day, topic_id, docs FROM topic_snapshots WHERE topic_id IN ({marks})", ids):
        end_of_day = time.mktime(time.strptime(str(r["day"]), "%Y-%m-%d")) + DAY
        k = min(n - 1, max(0, int((end_of_day - start) // step)))
        snaps[int(r["topic_id"])][k] = max(snaps[int(r["topic_id"])].get(k, 0), int(r["docs"]))
    out: dict[int, list[int]] = {}
    for tid in ids:
        total, series = before[tid], []
        for k in range(n):
            total += grid[tid][k]
            series.append(max(total, snaps[tid].get(k, 0)))
        out[tid] = series
    return [round(e, 1) for e in ends], out


def build_map(db: Database, *, store: bool = True) -> dict[str, Any]:
    nodes = top_topics(db)
    ids = [n["id"] for n in nodes]
    edges = strongest_edges(nodes, cooccurrence(db, ids))
    previous = (db.kv_get("map_layout") or {}) if store else {}
    pos = layout(ids, edges, {int(k): v for k, v in previous.items()})
    groups = clusters(ids, edges)
    if store and not db.readonly:
        db.kv_set("map_layout", {str(t): list(xy) for t, xy in pos.items()})
    days, hist = history(db, ids)
    return {
        "nodes": [n | {"x": pos[n["id"]][0], "y": pos[n["id"]][1], "group": groups[n["id"]]} for n in nodes],
        "edges": [{"a": a, "b": b, "w": w} for a, b, w in edges],
        "times": days,
        "history": {str(t): s for t, s in hist.items()},
    }


def snapshot_job(ctx: JobContext) -> JobOutcome:
    """Job ``memory.snapshot``: record today's size of the biggest topics (the map's time-lapse) and its layout."""
    db = ctx.db
    today = day_of(time.time())
    rows = top_topics(db, SNAPSHOT_TOPICS)
    for r in rows:
        db.execute(
            "INSERT OR REPLACE INTO topic_snapshots(day, topic_id, docs, facts) VALUES(?,?,?,0)",
            (today, r["id"], r["docs"]),
        )
    build_map(db)  # keeps the stored layout warm, so the dashboard's map changes smoothly
    return JobOutcome(done=True, value=0.02, result={"day": today, "topics": len(rows)})


def planner(agent: Any) -> None:
    from polymath.core.scheduler import local_phase

    if agent.db.scalar("SELECT 1 FROM doc_topics LIMIT 1"):
        agent.scheduler.ensure_recurring("memory.snapshot", DAY, priority=1.0, phase=local_phase(23))
