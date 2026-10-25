"""Where the storage goes, and when it would run out: ``polymath storage usage``, the dashboard, doctor and Orin.

Cheap by default: file sizes and free space, plus a forecast from the agent's own vitals (free space every few
minutes, kept 30 days). ``detail=True`` also splits the database by what it holds (SQLite's ``dbstat``). That
reads every page of the database, so it is only done when asked (``polymath storage usage --detail``).

The forecast fits a straight line to the last three days of free space (hourly averages). When free space falls,
``days_left`` is how long until it reaches the reserve the agent keeps free. Eviction starts before that, so this
says when Polymath would start dropping old document texts (never facts), not when it breaks.
"""

from __future__ import annotations

import contextlib
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

from polymath.core.config import Config
from polymath.core.db import Database

FORECAST_HOURS = 72.0
MIN_SPAN_HOURS = 6.0  # less history than this: no forecast yet
MIN_DECLINE_GB_DAY = 0.02  # slower than this counts as steady

# what each table holds, for --detail (prefix → group); anything else is "other"
GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("search index", ("chunk_fts", "chunk_vocab", "chunks", "sqlite_autoindex_chunks")),
    ("document texts", ("documents", "sqlite_autoindex_documents", "minhash")),
    ("knowledge graph", ("entities", "sqlite_autoindex_entities", "aliases", "triples", "sqlite_autoindex_triples",
                         "provenance", "sqlite_autoindex_provenance", "predicates", "wd_", "fact_time", "geo",
                         "category_props", "entity_", "topic", "doc_topics", "wiki_")),
    ("language statistics", ("ngram_counts", "phrases", "surface_stats", "pair_contexts", "patterns",
                             "doc_entities", "infobox_alignment", "phrasings")),
    ("reasoning and tests", ("rules", "reasoning_runs", "quiz", "remediation", "predictions", "sanity",
                             "value_ranges", "rule_trust", "withdrawn")),
)  # fmt: skip


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _tree(path: Path) -> int:
    total = 0
    with contextlib.suppress(OSError):
        for p in path.rglob("*"):
            with contextlib.suppress(OSError):
                if p.is_file() and not p.is_symlink():
                    total += p.stat().st_size
    return total


def files(config: Config) -> dict[str, Any]:
    """Sizes in bytes: the database (with its write-ahead log), backups, the vector indexes, raw downloads,
    and the disk's free space and the agent's budget."""
    p = config.paths
    db = p.db_path
    backups = 0
    count = 0
    from polymath.body.maintenance import backup_dirs
    from polymath.body.maintenance import backups as backup_files

    for b in backup_files(backup_dirs(config)):
        backups += _size(b)
        count += 1
    probe = Path(p.data_dir)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        disk = shutil.disk_usage(probe)
        free, total = int(disk.free), int(disk.total)
    except OSError:
        free = total = 0
    return {
        "database": _size(db) + _size(Path(str(db) + "-wal")) + _size(Path(str(db) + "-shm")),
        "backups": backups,
        "backup_count": count,
        "indexes": _tree(p.index_dir),
        "downloads": _tree(p.raw_dir),
        "free": free,
        "disk": total,
        "budget": int(config.body.disk_budget_gb * 1e9),
        "keep_free": int(config.body.disk_min_free_gb * 1e9),
        "sd_card": bool(config.storage.sd_only),
    }


def forecast(db: Database, *, keep_free_gb: float, now: float | None = None) -> dict[str, Any] | None:
    """How fast free space falls (GB/day) and how many days are left until the reserve, from the vitals."""
    now = time.time() if now is None else now
    rows = db.query(
        "SELECT CAST(at / 3600 AS INTEGER) AS h, AVG(disk_free_gb) AS free FROM vitals WHERE at >= ? AND "
        "disk_free_gb IS NOT NULL GROUP BY h ORDER BY h",
        (now - FORECAST_HOURS * 3600,),
    )
    pts = [(float(r["h"]), float(r["free"])) for r in rows]
    if len(pts) < 3 or pts[-1][0] - pts[0][0] < MIN_SPAN_HOURS:
        return None
    n = len(pts)
    mx = sum(x for x, _ in pts) / n
    my = sum(y for _, y in pts) / n
    var = sum((x - mx) ** 2 for x, _ in pts)
    slope_h = sum((x - mx) * (y - my) for x, y in pts) / var if var else 0.0
    decline = -slope_h * 24  # GB per day, positive when free space falls
    free_now = pts[-1][1]
    out: dict[str, Any] = {"free_gb": round(free_now, 2), "decline_gb_per_day": round(decline, 3),
                           "hours": round(pts[-1][0] - pts[0][0], 1)}  # fmt: skip
    if decline < MIN_DECLINE_GB_DAY:
        out["days_left"] = None  # steady (or growing free space)
    else:
        out["days_left"] = round(max(0.0, (free_now - keep_free_gb) / decline), 1)
    return out


def breakdown(db_path: Path) -> list[tuple[str, int]]:
    """Bytes per kind of content inside the database (reads the whole file: on request only)."""
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:  # no database (yet)
        return []
    try:
        rows = con.execute("SELECT name, SUM(pgsize) FROM dbstat GROUP BY name").fetchall()
    except sqlite3.Error:  # not a database, or SQLite built without dbstat
        return []
    finally:
        con.close()
    sums: dict[str, int] = {}
    for name, size in rows:
        group = next((g for g, prefixes in GROUPS if str(name).startswith(prefixes)), "other")
        sums[group] = sums.get(group, 0) + int(size or 0)
    return sorted(sums.items(), key=lambda kv: -kv[1])


def gb(n: float) -> str:
    return f"{n / 1e9:.2f} GB" if n >= 1e8 else f"{n / 1e6:.0f} MB"


def lines(config: Config, db: Database | None, *, detail: bool = False) -> list[str]:
    """The report, in words."""
    f = files(config)
    where = "the SD card" if f["sd_card"] else "the brain's disk"
    out = [
        f"{where}: {gb(f['free'])} free of {gb(f['disk'])} (Polymath keeps {gb(f['keep_free'])} free)",
        f"  database        {gb(f['database'])}",
        f"  backups         {gb(f['backups'])} ({f['backup_count']} kept)",
        f"  vector indexes  {gb(f['indexes'])}",
        f"  downloads       {gb(f['downloads'])} (deleted once read)",
    ]
    if detail:
        for group, size in breakdown(config.paths.db_path):
            if size >= 1e6:
                out.append(f"    {group:<20}{gb(size)}")
    fc = forecast(db, keep_free_gb=config.body.disk_min_free_gb) if db is not None else None
    if fc is None:
        out.append("forecast: not enough history yet (6 hours of the agent running)")
    elif fc["days_left"] is None:
        out.append(
            f"forecast: steady (free space changed {-fc['decline_gb_per_day']:+.2f} GB/day over {fc['hours']:.0f} h)"
        )
    else:
        out.append(
            f"forecast: free space falls {fc['decline_gb_per_day']:.2f} GB/day: about {fc['days_left']:.0f} days "
            "until Polymath starts dropping old document texts to keep its reserve (facts are kept)"
        )
    return out
