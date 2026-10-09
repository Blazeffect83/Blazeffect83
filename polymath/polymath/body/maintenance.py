"""Housekeeping jobs: nightly verified backups with rotation, and eviction under disk pressure.

**Backup** (``body.backup``, nightly at ``body.backup_hour``): SQLite's online
backup API copies the live database page by page (feeding the watchdog between
steps), ``PRAGMA quick_check`` verifies the copy, it is compressed with lzma
(``.sqlite3.xz``), fsynced, and only the newest ``body.backup_keep`` (7) are kept.
A backup is skipped — loudly — when the disk could not hold it.

**Eviction** (``body.evict``, requested by the guard): frees space in order of
least regret until the data directory is back under 90 % of its budget and the
disk has room again:

1. backups beyond the newest two (only when the disk itself is short of space);
2. raw download leftovers no job refers to any more (older than a week);
3. bodies of exact duplicates (their text lives on in the original);
4. bodies of the least valuable documents — oldest first, feeds and web pages
   before books, papers and encyclopedia articles, never one that is the
   evidence for a fact. Metadata, license and the facts learned stay; the
   passages leave the full-text index.

Freed SQLite pages are reused by later writes, so the database stops growing
(the guard's size accounting excludes free pages).
"""

from __future__ import annotations

import lzma
import os
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

from polymath.core.jobs import JobContext, JobOutcome
from polymath.core.logging import get_logger
from polymath.core.scheduler import local_phase
from polymath.memory.documents import DocumentStore
from polymath.memory.text_index import TextIndex

log = get_logger("maintenance")
BACKUP_GLOB = "polymath-*.sqlite3.xz"
EVICTION_ORDER = ("feed", "web", "stackexchange", "gutenberg", "pubmed", "openalex", "wikipedia")
EVICT_BATCH = 200
RAW_GRACE = 7 * 86400.0


# ---------------------------------------------------------------- backups
def backups(directory: Path) -> list[Path]:
    return sorted(directory.glob(BACKUP_GLOB), key=lambda p: p.name, reverse=True)


def rotate(directory: Path, keep: int) -> list[str]:
    removed = []
    for old in backups(directory)[keep:]:
        old.unlink(missing_ok=True)
        removed.append(old.name)
    return removed


def _fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def backup_job(ctx: JobContext) -> JobOutcome:
    cfg = ctx.config
    out_dir = cfg.paths.backup_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    db_size = cfg.paths.db_path.stat().st_size
    free = shutil.disk_usage(out_dir).free
    if free < 1.5 * db_size + cfg.body.disk_min_free_gb * 1e9 / 2:
        detail = f"skipped: {free / 1e9:.1f} GB free, database is {db_size / 1e9:.1f} GB"
        ctx.db.execute(
            "INSERT INTO backups(created, path, bytes, ok, detail) VALUES(?,?,?,?,?)", (time.time(), "", 0, 0, detail)
        )
        log.warning("backup skipped for lack of space", extra={"free_gb": free / 1e9, "db_gb": db_size / 1e9})
        return JobOutcome(done=True, result={"ok": False, "detail": detail})
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    tmp = out_dir / f".polymath-{stamp}.sqlite3"
    final = out_dir / f"polymath-{stamp}.sqlite3.xz"
    t0 = time.monotonic()
    try:
        # A separate read-only connection: the job itself runs inside the agent's write transaction, which
        # would make the backup API wait on itself. WAL gives the reader a consistent committed snapshot.
        reader = sqlite3.connect(f"file:{cfg.paths.db_path}?mode=ro", uri=True)
        dst = sqlite3.connect(str(tmp))
        try:
            reader.backup(dst, pages=4096, progress=lambda *_a: ctx.tick())
            check = dst.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            dst.close()
            reader.close()
        if check != "ok":
            raise RuntimeError(f"backup failed verification: {check}")
        with tmp.open("rb") as src, lzma.open(final.with_suffix(".xz.part"), "wb", preset=3) as xz:
            while chunk := src.read(8 << 20):
                xz.write(chunk)
                ctx.tick()
        final.with_suffix(".xz.part").replace(final)
        _fsync(final)
    finally:
        tmp.unlink(missing_ok=True)
        final.with_suffix(".xz.part").unlink(missing_ok=True)
    size = final.stat().st_size
    removed = rotate(out_dir, cfg.body.backup_keep)
    detail = f"quick_check ok; {db_size / 1e6:.1f} MB → {size / 1e6:.1f} MB in {time.monotonic() - t0:.1f} s"
    ctx.db.execute(
        "INSERT INTO backups(created, path, bytes, ok, detail) VALUES(?,?,?,?,?)",
        (time.time(), str(final), size, 1, detail),
    )
    ctx.db.execute("DELETE FROM backups WHERE created < ?", (time.time() - 90 * 86400,))
    return JobOutcome(done=True, value=0.1, result={"ok": True, "path": str(final), "bytes": size, "removed": removed})


def restore(backup: Path, db_path: Path) -> Path:
    """Replace the database with a backup (agent must be stopped). Returns where the old database was moved."""
    tmp = db_path.with_name(db_path.name + ".restoring")
    with lzma.open(backup, "rb") as src, tmp.open("wb") as dst:
        shutil.copyfileobj(src, dst, 8 << 20)
    con = sqlite3.connect(str(tmp))
    try:
        check = con.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        con.close()
    if check != "ok":
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"backup {backup.name} is damaged: {check}")
    old = db_path.with_name(db_path.name + f".pre-restore-{int(time.time())}")
    if db_path.exists():
        db_path.replace(old)
    for suffix in ("-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    tmp.replace(db_path)
    return old


# --------------------------------------------------------------- eviction
def _need_bytes(ctx: JobContext, data_gb: float, free_gb: float | None) -> float:
    b = ctx.config.body
    over_budget = (data_gb - 0.9 * b.disk_budget_gb) * 1e9
    short_free = ((1.5 * b.disk_min_free_gb) - free_gb) * 1e9 if free_gb is not None else 0.0
    return max(0.0, over_budget, short_free)


def _referenced(ctx: JobContext, rel: str) -> bool:
    return bool(
        ctx.db.scalar(
            "SELECT 1 FROM jobs WHERE state IN ('queued','running') AND payload LIKE ? LIMIT 1", (f"%{rel}%",)
        )
    )


def evict_job(ctx: JobContext) -> JobOutcome:
    from polymath.body.guard import data_bytes

    cfg = ctx.config
    free_gb = shutil.disk_usage(cfg.paths.data_dir).free / 1e9
    data_gb = data_bytes(ctx.db, cfg.paths.data_dir) / 1e9
    need = _need_bytes(ctx, data_gb, free_gb)
    freed = 0.0
    steps: dict[str, Any] = {}
    if need <= 0:
        return JobOutcome(done=True, result={"need_mb": 0, "data_gb": round(data_gb, 2)})
    if free_gb < cfg.body.disk_min_free_gb:
        removed = 0
        for old in backups(cfg.paths.backup_dir)[2:]:
            freed += old.stat().st_size
            old.unlink(missing_ok=True)
            removed += 1
        steps["backups_removed"] = removed
    now = time.time()
    raw_removed = 0
    for path in sorted(cfg.paths.raw_dir.rglob("*")) if cfg.paths.raw_dir.exists() else []:
        if freed >= need:
            break
        if not path.is_file() or now - path.stat().st_mtime < RAW_GRACE:
            continue
        rel = str(path.relative_to(cfg.paths.raw_dir)).removesuffix(".part")
        if _referenced(ctx, rel):
            continue
        freed += path.stat().st_size
        path.unlink(missing_ok=True)
        raw_removed += 1
    steps["raw_files_removed"] = raw_removed
    if freed < need:
        row = ctx.db.one(
            "SELECT COUNT(*) AS n, COALESCE(SUM(LENGTH(body)), 0) AS b FROM documents "
            "WHERE state='duplicate' AND codec != 'evicted'"
        )
        ctx.db.execute("UPDATE documents SET body=NULL, codec='evicted' WHERE state='duplicate' AND codec != 'evicted'")
        freed += float(row["b"]) if row else 0.0
        steps["duplicate_bodies"] = int(row["n"]) if row else 0
    evicted = 0
    if freed < need:
        store = DocumentStore(ctx.db)
        text = TextIndex(ctx.db, store)
        order = " ".join(f"WHEN '{s}' THEN {i}" for i, s in enumerate(EVICTION_ORDER))
        first = True
        while freed < need and (first or not ctx.should_stop()):
            first = False
            rows = ctx.db.query(
                "SELECT d.id, d.title, LENGTH(d.body) AS b FROM documents d WHERE d.codec != 'evicted' "
                "AND d.state != 'duplicate' AND NOT EXISTS (SELECT 1 FROM provenance v WHERE v.doc_id = d.id) "
                "AND NOT EXISTS (SELECT 1 FROM entities e WHERE e.doc_id = d.id) "
                f"ORDER BY CASE d.source {order} ELSE {len(EVICTION_ORDER)} END, d.fetched LIMIT ?",
                (EVICT_BATCH,),
            )
            if not rows:
                break
            for r in rows:
                doc = store.get(int(r["id"]))
                if doc is not None and doc.text:
                    text.unindex(doc.id, doc.title, doc.text)
                ctx.db.execute(
                    "UPDATE documents SET body=NULL, codec='evicted', meta=json_set(meta, '$.evicted', ?) WHERE id=?",
                    (time.time(), r["id"]),
                )
                freed += float(r["b"] or 0)
                evicted += 1
                if freed >= need:
                    break
            ctx.tick()
    steps["documents_evicted"] = evicted
    result = {"need_mb": round(need / 1e6, 1), "freed_mb": round(freed / 1e6, 1), "freed_bytes": int(freed), **steps}
    if freed < need:
        log.warning("eviction could not free enough space", extra=result)
    return JobOutcome(done=True, value=0.05, result=result)


def planner(agent: Any) -> None:
    hour = agent.config.body.backup_hour
    agent.scheduler.ensure_recurring("body.backup", 86400, priority=3.0, phase=local_phase(hour))
