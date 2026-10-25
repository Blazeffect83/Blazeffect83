"""What each drive holds, and relearning it when a drive is lost (v0.11.0).

**The inventory** (``storage.inventory``, hourly): for every drive Polymath knows, what is on it: the documents whose
bodies were spilled there (how many, how much text, from which sources, about which topics) and how many facts rest
on them; the downloads, backups and old brain copies on it; and for the brain's own drive, that it holds everything.
It goes into the database (``kv inventory``: the dashboard, ``polymath storage list``) and onto each drive itself
(``polymath-brain/inventory.json``), so the drive says what it holds even on another computer.

**When a drive goes away** (unplugged, or the connection dropped by accident), its bodies read as unavailable and the
facts stay. If it is still gone after ``storage.relearn_after_h`` hours (2), ``storage.recover`` (every 10 minutes)
counts it as lost:

1. its documents are marked lost (``meta.lost``; the pointer to the drive is kept, in case it comes back);
2. each gets a place in the relearning queue (table ``relearn``) with a priority: ten points per fact that rests on
   it, fifty when it is about a topic you asked for, and a little for its size. So what the most knowledge depends on
   comes back first;
3. the queue is worked through, the most important first, a few hundred an hour: Wikipedia articles are read again
   from Wikipedia's dump (``wikipedia.titles``, by byte range: no download), web pages and feed articles are fetched
   again (the crawler, vetted and polite as always). Documents from other dumps come back when their dump is read
   again (``later``);
4. a relearned document is stored on the brain's disk first, and spilled to the drives that are there when it
   fills (``body.spill``): the other drives make up for the lost one;
5. if the drive held backups, a backup is taken now; downloads it held are planned again (``sources.plan``).

If the drive comes back after all, its documents are readable again at once and their places in the queue are
dropped (``back``). The feed, doctor and the caretaker's inbox say what was lost and how much is back.

Not in SD-card mode or on the standby brain: there the drives are left alone on purpose.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path
from typing import Any

from polymath.core import issues
from polymath.core.jobs import JobContext, JobOutcome
from polymath.core.logging import get_logger
from polymath.memory import pool as storage_pool

log = get_logger("inventory")

INVENTORY_FILE = "inventory.json"  # in each drive's polymath-brain folder
PER_RUN = 300  # documents sent to be relearned per run
PER_DAY = 3000  # and at most this many a day (it keeps learning new things too)
WIKI_BATCH = 100  # titles per wikipedia.titles job (it reads at most 200)
QUEUED_STALE_S = 2 * 86400.0  # queued and still lost after this: it comes back later (with its source)
WEB_SOURCES = ("web", "feed")


def _spilled_on(vol: str) -> tuple[str, tuple[Any, ...]]:
    return "codec = 'spilled' AND json_extract(meta, '$.spill.vol') = ?", (vol,)


def volume_inventory(db: Any, vol: str) -> dict[str, Any]:
    """What the database knows is on drive ``vol``: documents, text, sources, topics, facts resting on them, lost."""
    where, args = _spilled_on(vol)
    row = db.one(
        f"SELECT COUNT(*) AS n, COALESCE(SUM(nchars), 0) AS chars, "
        f"SUM(json_extract(meta, '$.lost') IS NOT NULL) AS lost FROM documents WHERE {where}",
        args,
    )
    out: dict[str, Any] = {
        "documents": int(row["n"] or 0),
        "chars": int(row["chars"] or 0),
        "lost": int(row["lost"] or 0),
    }
    if not out["documents"]:
        return out | {"sources": {}, "topics": [], "facts": 0}
    out["sources"] = {
        str(r["source"]): int(r["n"])
        for r in db.query(
            f"SELECT source, COUNT(*) AS n FROM documents WHERE {where} GROUP BY source ORDER BY n DESC", args
        )
    }
    out["facts"] = int(
        db.scalar(
            f"SELECT COUNT(DISTINCT p.triple_id) FROM provenance p WHERE p.doc_id IN (SELECT id FROM documents WHERE "
            f"{where})",
            args,
            0,
        )
        or 0
    )
    out["topics"] = [
        str(r["name"])
        for r in db.query(
            f"SELECT t.name, COUNT(*) AS n FROM doc_topics dt JOIN topics t ON t.id = dt.topic_id WHERE dt.doc_id IN "
            f"(SELECT id FROM documents WHERE {where}) GROUP BY t.id ORDER BY n DESC LIMIT 5",
            args,
        )
    ]
    return out


def files_on(brain: Path) -> dict[str, Any]:
    """What lies in a drive's polymath-brain folder: bodies, downloads, backups, old brain copies."""

    def tree(p: Path) -> tuple[int, int]:
        n = b = 0
        for base, _dirs, names in os.walk(p):
            for name in names:
                with contextlib.suppress(OSError):
                    b += os.lstat(os.path.join(base, name)).st_size
                    n += 1
        return n, b

    out: dict[str, Any] = {}
    for key, sub in (("bodies", "bodies"), ("downloads", "raw"), ("backups", "backups")):
        n, b = tree(brain / sub)
        out[key] = {"files": n, "bytes": b}
    with contextlib.suppress(OSError):
        out["newest_backup"] = max((p.name for p in (brain / "backups").glob("polymath-*.sqlite3.xz")), default="")
        out["previous_brain"] = sorted(p.name for p in brain.iterdir() if p.name.startswith("home-previous-"))
    return out


def inventory_job(ctx: JobContext) -> JobOutcome:
    """Job ``storage.inventory``: what each drive holds, into the database and onto each drive."""
    cfg, db = ctx.config, ctx.db
    pool = storage_pool.for_config(cfg) or storage_pool.active()
    online = {v.id: v for v in (pool.volumes(fresh=True) if pool is not None else [])}
    from polymath.body.roles import read_public

    roles = {str(d.get("id")): str(d.get("role")) for d in (read_public(cfg) or {}).get("drives") or []}
    drives: dict[str, Any] = {}
    for r in db.query("SELECT id, label, model, online, last_seen FROM volumes WHERE retired = 0 ORDER BY id"):
        vid = str(r["id"])
        entry = volume_inventory(db, vid) | {
            "name": str(r["label"] or r["model"] or vid[:8]),
            "online": vid in online,
            "last_seen": r["last_seen"],
            "role": roles.get(vid, ""),
        }
        v = online.get(vid)
        if v is not None:
            entry |= files_on(v.path)
            if v.home:
                entry["role"] = "brain"
                entry["holds"] = "the whole brain: the database, the indexes and everything not spilled elsewhere"
            with contextlib.suppress(OSError):
                tmp = v.path / f".{INVENTORY_FILE}.tmp"
                tmp.write_text(json.dumps({"at": time.time(), "drive": vid, **entry}, indent=1), encoding="utf-8")
                tmp.replace(v.path / INVENTORY_FILE)
        drives[vid] = entry
        ctx.tick()
    db.kv_set("inventory", {"at": time.time(), "drives": drives})
    return JobOutcome(
        done=True,
        value=0.01,
        result={"drives": len(drives), "documents_on_drives": sum(d["documents"] for d in drives.values())},
    )


def _interest_topics(db: Any) -> list[int]:
    from polymath.drive.learn import interests

    return sorted(interests(db))


def mark_lost(db: Any, vol: str, now: float) -> int:
    """Mark the documents on a lost drive and queue them for relearning, the most important first."""
    where, args = _spilled_on(vol)
    cur = db.execute(
        f"UPDATE documents SET meta = json_set(meta, '$.lost', json(?)) WHERE {where} AND "
        "json_extract(meta, '$.lost') IS NULL",
        (json.dumps({"vol": vol, "at": now}), *args),
    )
    n = int(cur.rowcount or 0)
    if not n:
        return 0
    topics = _interest_topics(db)
    interest = (
        f"50.0 * EXISTS(SELECT 1 FROM doc_topics dt WHERE dt.doc_id = d.id AND dt.topic_id IN "
        f"({','.join('?' * len(topics))}))"
        if topics
        else "0.0"
    )
    web = ",".join(f"'{s}'" for s in WEB_SOURCES)
    db.execute(
        "INSERT OR IGNORE INTO relearn(doc_id, volume_id, priority, how, state, added) "
        f"SELECT d.id, ?, 10.0 * (SELECT COUNT(*) FROM provenance p WHERE p.doc_id = d.id) + {interest} + "
        "MIN(d.nchars, 200000) / 100000.0, CASE WHEN d.source = 'wikipedia' THEN 'wikipedia' "
        f"WHEN d.source IN ({web}) AND d.url LIKE 'http%' THEN 'web' ELSE 'later' END, 'waiting', ? "
        f"FROM documents d WHERE d.codec = 'spilled' AND json_extract(d.meta, '$.spill.vol') = ? "
        "AND json_extract(d.meta, '$.lost') IS NOT NULL",
        (vol, *topics, now, vol),
    )
    return n


def came_back(db: Any, vol: str, now: float) -> int:
    """A lost drive is back: its documents read again, and their places in the relearning queue go."""
    where, args = _spilled_on(vol)
    cur = db.execute(
        f"UPDATE documents SET meta = json_remove(meta, '$.lost') WHERE {where} AND "
        "json_extract(meta, '$.lost') IS NOT NULL",
        args,
    )
    db.execute(
        "UPDATE relearn SET state = 'back', done = ? WHERE volume_id = ? AND state IN ('waiting', 'queued')", (now, vol)
    )
    return int(cur.rowcount or 0)


def _queue(ctx: JobContext, now: float) -> dict[str, int]:
    """Send the most important lost documents to be read again."""
    db = ctx.db
    sent_today = int(db.scalar("SELECT COUNT(*) FROM relearn WHERE queued > ?", (now - 86400,), 0) or 0)
    room = min(PER_RUN, PER_DAY - sent_today)
    if room <= 0:
        return {"wikipedia": 0, "web": 0}
    rows = db.query(
        "SELECT r.doc_id, r.how, r.volume_id, d.title, d.url, COALESCE(d.lang, 'en') AS lang FROM relearn r "
        "JOIN documents d ON d.id = r.doc_id WHERE r.state = 'waiting' AND r.how IN ('wikipedia', 'web') "
        "ORDER BY r.priority DESC, r.doc_id LIMIT ?",
        (room,),
    )
    names = {
        str(v["id"]): str(v["label"] or v["model"] or str(v["id"])[:8])
        for v in db.query("SELECT id, label, model FROM volumes")
    }
    by_lang: dict[tuple[str, str], list[tuple[int, str]]] = {}
    web: list[tuple[int, str]] = []
    for r in rows:
        if r["how"] == "wikipedia" and r["title"]:
            by_lang.setdefault((str(r["lang"]), str(r["volume_id"])), []).append((int(r["doc_id"]), str(r["title"])))
        elif r["how"] == "web" and r["url"]:
            web.append((int(r["doc_id"]), str(r["url"])))
    sent: list[int] = []
    for (lang, vol), items in by_lang.items():
        for i in range(0, len(items), WIKI_BATCH):
            batch = items[i : i + WIKI_BATCH]
            ctx.scheduler.enqueue(
                "wikipedia.titles",
                {
                    "titles": [t for _id, t in batch],
                    "lang": lang,
                    "about": f"relearning what was on {names.get(vol, vol[:8])}",
                },
                key=f"relearn:wiki:{batch[0][0]}:{len(batch)}:{int(now)}",
                priority=2.6,
            )
            sent += [d for d, _t in batch]
    crawler = ctx.services.get("crawler")
    if web and crawler is not None:
        for d, url in web:
            crawler.frontier.add(url, priority=3.0, depth=0)
            sent.append(d)
        ctx.scheduler.enqueue("crawl.step", {}, key=f"relearn:crawl:{int(now // 600)}", priority=2.5)
    if sent:
        q = ",".join("?" * len(sent))
        db.execute(f"UPDATE relearn SET state = 'queued', queued = ? WHERE doc_id IN ({q})", (now, *sent))
    return {"wikipedia": sum(len(v) for v in by_lang.values()), "web": len(web) if crawler is not None else 0}


def recover_job(ctx: JobContext) -> JobOutcome:
    """Job ``storage.recover``: notice lost drives, queue what they held for relearning, notice them coming back."""
    from polymath.core import standby

    cfg, db = ctx.config, ctx.db
    if cfg.storage.sd_only or standby.is_standby(cfg):
        return JobOutcome(done=True, result={"skipped": "the drives are left alone in SD-card mode / on standby"})
    now = time.time()
    after = cfg.storage.relearn_after_h * 3600
    result: dict[str, Any] = {"lost": {}, "back": {}}
    from polymath.body.roles import read_public

    roles = read_public(cfg) or {}
    backup_drive = str(roles.get("backup") or "")
    for r in db.query("SELECT id, label, model, online, retired, last_seen FROM volumes"):
        vid, name = str(r["id"]), str(r["label"] or r["model"] or str(r["id"])[:8])
        if r["online"]:
            n = came_back(db, vid, now)
            if n:
                result["back"][vid] = n
                db.execute(
                    "INSERT INTO volume_events(at, volume_id, event, detail) VALUES(?,?,?,?)",
                    (now, vid, "back", json.dumps({"documents": n})),
                )
                issues.report(
                    db,
                    "storage",
                    f"relearn:back:{vid}",
                    f"drive {name} is back: {n:,} documents are readable again; relearning them stopped",
                    severity="info",
                    now=now,
                )
            continue
        if r["retired"] or now - float(r["last_seen"] or now) < after:
            continue
        inv = volume_inventory(db, vid)
        n = mark_lost(db, vid, now)
        if not n:
            continue
        result["lost"][vid] = n
        queued = {
            str(x["how"]): int(x["c"])
            for x in db.query(
                "SELECT how, COUNT(*) AS c FROM relearn WHERE volume_id = ? AND state = 'waiting' GROUP BY how", (vid,)
            )
        }
        detail = {
            "documents": n,
            "facts": inv.get("facts", 0),
            "topics": inv.get("topics", [])[:3],
            "relearn": queued.get("wikipedia", 0) + queued.get("web", 0),
            "later": queued.get("later", 0),
        }
        db.execute(
            "INSERT INTO volume_events(at, volume_id, event, detail) VALUES(?,?,?,?)",
            (now, vid, "lost", json.dumps(detail)),
        )
        hours = (now - float(r["last_seen"] or now)) / 3600
        issues.report(
            db,
            "storage",
            f"relearn:lost:{vid}",
            f"drive {name} has been gone {hours:.0f} h: it held "
            f"{n:,} documents ({inv.get('facts', 0):,} facts rest on them); relearning "
            f"{detail['relearn']:,} of them onto the other drives, the most important first",
            severity="warn",
            now=now,
        )
        if vid == backup_drive or db.scalar(
            "SELECT 1 FROM backups WHERE ok = 1 AND path LIKE ? LIMIT 1", (f"%{vid}%",)
        ):
            ctx.scheduler.enqueue("body.backup", {}, key=f"relearn:backup:{vid}:{int(now)}", priority=3.0)
        ctx.scheduler.enqueue("sources.plan", {}, key=f"relearn:plan:{vid}:{int(now)}", priority=2.0)
    # what has been read again (a stored body replaced the lost one), and what waits too long
    db.execute(
        "UPDATE relearn SET state = 'done', done = ? WHERE state IN ('waiting', 'queued') AND doc_id IN "
        "(SELECT id FROM documents WHERE codec != 'spilled' OR json_extract(meta, '$.lost') IS NULL)",
        (now,),
    )
    db.execute(
        "UPDATE relearn SET how = 'later', state = 'waiting' WHERE state = 'queued' AND queued < ?",
        (now - QUEUED_STALE_S,),
    )
    result["sent"] = _queue(ctx, now)
    return JobOutcome(done=True, value=0.05 if result["lost"] or any(result["sent"].values()) else 0.0, result=result)


def relearn_status(db: Any) -> dict[str, Any]:
    """For the snapshot, doctor and the dashboard: how much of what lost drives held is back."""
    rows = {str(r["state"]): int(r["c"]) for r in db.query("SELECT state, COUNT(*) AS c FROM relearn GROUP BY state")}
    later = int(db.scalar("SELECT COUNT(*) FROM relearn WHERE state = 'waiting' AND how = 'later'", default=0) or 0)
    return {
        "waiting": rows.get("waiting", 0) - later,
        "queued": rows.get("queued", 0),
        "done": rows.get("done", 0),
        "back": rows.get("back", 0),
        "later": later,
    }


def planner(agent: Any) -> None:
    from polymath.core import standby

    if agent.config.storage.sd_only or standby.is_standby(agent.config):
        return
    agent.scheduler.ensure_recurring("storage.inventory", 3600, priority=1.5)
    agent.scheduler.ensure_recurring("storage.recover", 600, priority=2.5)
