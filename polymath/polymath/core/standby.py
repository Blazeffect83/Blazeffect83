"""The standby brain: keep learning on the SD card while the brain's drive is gone, then hand it all back.

The brain lives on its drive (the SSD). When that drive is missing, frozen or dead for longer than
``storage.standby_after_s``, the agent does not just wait: it starts a small, separate **standby brain** on the
SD card (``storage.standby_dir``, ``/var/lib/polymath/standby``), and learns there. The feed, the face and
``doctor`` say so.

What it does in standby:

* **light reading only**: feeds, the polite crawler, single Wikipedia articles, vetted open-web sites. No dumps:
  the kinds in ``DROPPED_KINDS`` are not registered, so multi-gigabyte downloads never land on the SD card;
* everything else as usual (perception, memory, reasoning, self-tests) on what it reads;
* inside ``storage.standby_budget_gb`` (3 GB), with the SD card wear guard watching its writes, and never onto
  plugged-in drives (the pool is switched off).

When the drive is back (the storage helper restarts the agent, or the agent sees it within
``STANDBY_CHECK_S``), the agent restarts on its real brain, and **folds** the standby brain into it
(``fold_job``): the documents it read, through the ordinary document store (so duplicates are recognised and
the real brain reads them again with its whole knowledge graph), the Wikidata records, the crawler's progress
and the problems it noted. Then the standby brain is removed. Folding is resumable (a job checkpoint) and
idempotent (a document already there is "unchanged"; a standby brain once folded is recorded by id).

Nothing in here rewrites the real brain's existing knowledge: folding only adds.
"""

from __future__ import annotations

import contextlib
import copy
import json
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from polymath.core.config import Config
from polymath.core.logging import get_logger

log = get_logger(__name__)

MARKER = "standby.json"  # in the standby data directory: what this brain is and when it started
FOLDED_KEY = "standby_folded"  # in the real brain's kv: ids of standby brains already folded in
FOLD_KIND = "storage.fold_standby"
STANDBY_CHECK_S = 30.0  # while in standby, look for the real brain this often
FOLD_BATCH = 50  # documents per transaction while folding
STANDBY_MIN_FREE_GB = 2.0  # the SD card keeps at least this much free for the system

# Not run in standby: whole-dump work (gigabytes on the SD card) and the drive pool.
DROPPED_KINDS = frozenset({
    "sources.plan",
    "dump.download",
    "wikipedia.part",
    "wikidata.dump",
    "wikidata.propindex",
    "wikidata.properties",
    "openalex.ingest",
    "pubmed.ingest",
    "stackexchange.ingest",
    "gutenberg.books",
    "body.backup",  # a temporary brain needs no nightly backup (and the SD card no extra writes)
    "body.spill",
    "body.recall",
})  # fmt: skip


def is_standby(config: Config) -> bool:
    return bool(getattr(config, "standby", False))


def standby_config(config: Config) -> Config:
    """The configuration of the standby brain: same settings, its own small data directory on the SD card."""
    s = copy.deepcopy(config)
    s.standby = True
    s.paths.data_dir = Path(config.storage.standby_dir)
    s.paths.require_separate_mount = False  # it lives on the SD card on purpose
    s.body.disk_budget_gb = config.storage.standby_budget_gb
    # an SD card often has less free room than a drive: keep 2 GB for the system, not the drive's 10 GB
    s.body.disk_min_free_gb = min(config.body.disk_min_free_gb, STANDBY_MIN_FREE_GB)
    s.storage.move_home = False
    s.storage.mount_root = Path(config.storage.standby_dir) / "no-drives"  # the pool sees no drives
    return s


def marker_path(config: Config) -> Path:
    return Path(config.storage.standby_dir) / MARKER


def read_marker(config: Config) -> dict[str, Any] | None:
    try:
        raw = json.loads(marker_path(config).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) and raw.get("id") else None


def begin(config: Config, *, reason: str = "", now: float | None = None) -> dict[str, Any]:
    """Start (or resume) the standby brain; returns its marker. Resuming keeps its id, so it folds once."""
    found = read_marker(config)
    if found is not None:
        return found
    now = time.time() if now is None else now
    mark = {"id": uuid.uuid4().hex, "started": now, "main": str(config.paths.data_dir), "reason": reason}
    path = marker_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{MARKER}.tmp")
    tmp.write_text(json.dumps(mark), encoding="utf-8")
    tmp.replace(path)
    return mark


def pending(config: Config) -> dict[str, Any] | None:
    """A standby brain with something to fold into the real one (its marker and its database), or None."""
    mark = read_marker(config)
    if mark is None:
        return None
    return mark if standby_config(config).paths.db_path.exists() else None


def discard(config: Config) -> None:
    """Remove the standby brain's files (the directory itself stays: systemd owns it)."""
    root = Path(config.storage.standby_dir)
    if not root.is_dir():
        return
    for entry in root.iterdir():
        with contextlib.suppress(OSError):
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()


def status(config: Config, db: Any = None) -> dict[str, Any] | None:
    """For the snapshot (feed, face, doctor): since when it runs on the standby brain, and how much it read."""
    if not is_standby(config):
        return None
    mark = read_marker(config) or {}
    out: dict[str, Any] = {"since": mark.get("started"), "dir": str(config.paths.data_dir)}
    if db is not None:
        with contextlib.suppress(Exception):
            out["documents"] = int(db.scalar("SELECT COUNT(*) FROM documents WHERE state != 'duplicate'") or 0)
    return out


# ------------------------------------------------------------------ in the real brain: the planner and the fold
def planner(agent: Any) -> None:
    """On the real brain: a standby brain waiting to be folded in gets its fold job (one at a time)."""
    if is_standby(agent.config):
        return
    mark = pending(agent.config)
    if mark is None:
        return
    folded = set(agent.db.kv_get(FOLDED_KEY) or [])
    if mark["id"] in folded:  # folded already, only its files were left (a stop right after the fold)
        discard(agent.config)
        return
    agent.scheduler.enqueue(FOLD_KIND, {"id": mark["id"]}, key=f"fold:{mark['id']}", priority=4.0)


def _fold_documents(main_db: Any, sdb: Any, after: int, deadline: float) -> tuple[int, int, bool]:
    """Documents read in standby into the real brain's document store. Returns (last id, added, finished)."""
    from polymath.memory.documents import Document, DocumentStore, load_body

    store = DocumentStore(main_db)
    added = 0
    while time.monotonic() < deadline:
        rows = sdb.query(
            "SELECT * FROM documents WHERE id > ? AND state != 'duplicate' AND codec NOT IN ('evicted', 'spilled') "
            "ORDER BY id LIMIT ?",
            (after, FOLD_BATCH),
        )
        if not rows:
            return after, added, True
        with main_db.transaction():
            for r in rows:
                body, ok = load_body(r)
                after = int(r["id"])
                if not ok or not body.get("text"):
                    continue
                try:
                    meta = json.loads(r["meta"] or "{}")
                except ValueError:
                    meta = {}
                doc = Document(
                    source=r["source"], external_id=r["external_id"], title=r["title"], text=body["text"],
                    license=r["license"], url=r["url"], license_url=r["license_url"], lang=r["lang"],
                    published=r["published"], links=[tuple(x) for x in body.get("links") or []],
                    meta=meta if isinstance(meta, dict) else {}, extra=body.get("extra") or {},
                )  # fmt: skip
                _id, state = store.add(doc)
                added += state in {"new", "updated"}
    return after, added, False


def _fold_rest(main_db: Any, sdb: Any, mark: dict[str, Any], added: int) -> dict[str, int]:
    """Sources, Wikidata records, the crawler's progress and the problems noted, then the record of the fold."""
    n: dict[str, int] = {}
    with main_db.transaction():
        for r in sdb.query("SELECT * FROM sources"):
            main_db.execute(
                "INSERT OR IGNORE INTO sources(name, license, license_url, homepage, description) VALUES(?,?,?,?,?)",
                (r["name"], r["license"], r["license_url"], r["homepage"], r["description"]),
            )
        n["wikidata"] = 0
        for r in sdb.query("SELECT qid, label, description, aliases, enwiki, claims, fetched FROM wd_entities"):
            cur = main_db.execute(
                "INSERT OR IGNORE INTO wd_entities(qid, label, description, aliases, enwiki, claims, state, fetched) "
                "VALUES(?,?,?,?,?,?,'new',?)",
                (r["qid"], r["label"], r["description"], r["aliases"], r["enwiki"], r["claims"], r["fetched"]),
            )
            n["wikidata"] += cur.rowcount or 0
        n["pages"] = 0
        for r in sdb.query("SELECT url, fetched_at, http_status, etag, last_modified FROM frontier WHERE state='done'"):
            cur = main_db.execute(
                "UPDATE frontier SET state='done', fetched_at=?, http_status=?, etag=?, last_modified=? "
                "WHERE url=? AND state='new'",
                (r["fetched_at"], r["http_status"], r["etag"], r["last_modified"], r["url"]),
            )  # read in standby: not fetched again
            n["pages"] += cur.rowcount or 0
        n["issues"] = 0
        for r in sdb.query("SELECT at, text, detail FROM events WHERE kind = 'issue' ORDER BY id"):
            main_db.execute("INSERT INTO events(at, kind, text, detail) VALUES(?, 'issue', ?, ?)",
                            (r["at"], "(on standby) " + str(r["text"]), r["detail"]))  # fmt: skip
            n["issues"] += 1
        n["documents"] = added
        since = float(mark.get("started") or time.time())
        away = _duration(time.time() - since)
        text = (f"back on its drive after {away} on the standby brain: {added:,} documents it read there are now "
                f"in the real brain (it reads them again with everything it knows)")  # fmt: skip
        main_db.execute("INSERT INTO events(at, kind, text, detail) VALUES(?, 'standby', ?, ?)",
                        (time.time(), text, json.dumps({"status": "folded", **n})))  # fmt: skip
        folded = list(main_db.kv_get(FOLDED_KEY) or [])
        main_db.kv_set(FOLDED_KEY, [*folded, mark["id"]][-20:])
    return n


def _duration(seconds: float) -> str:
    from polymath.core.netstate import _duration as fmt

    return fmt(seconds)


def fold_job(ctx: Any) -> Any:
    """Fold the standby brain into the real one, a slice at a time; then remove the standby brain."""
    from polymath.core.db import Database
    from polymath.core.jobs import JobOutcome

    mark = read_marker(ctx.config)
    if mark is None or mark.get("id") != ctx.job.payload.get("id"):
        return JobOutcome(done=True)  # gone already, or a different one (its own job comes)
    if mark["id"] in set(ctx.db.kv_get(FOLDED_KEY) or []):
        discard(ctx.config)
        return JobOutcome(done=True)
    path = standby_config(ctx.config).paths.db_path
    if not path.exists():
        discard(ctx.config)
        return JobOutcome(done=True)
    cp = dict(ctx.job.checkpoint or {})
    sdb = Database(path, readonly=True)
    try:
        deadline = time.monotonic() + max(1.0, ctx.time_left() - 2.0)
        after, added, finished = _fold_documents(ctx.db, sdb, int(cp.get("after", 0)), deadline)
        added += int(cp.get("added", 0))
        if not finished:
            return JobOutcome(done=False, checkpoint={"after": after, "added": added}, value=float(added))
        n = _fold_rest(ctx.db, sdb, mark, added)
    finally:
        sdb.close()
    discard(ctx.config)
    log.warning("standby brain folded in", extra={"standby": mark["id"], **n})
    return JobOutcome(done=True, value=float(added))


def note(db: Any, text: str) -> None:
    """Into the caretaker's inbox (and so the feed's ``problem`` line) of whichever brain ``db`` is."""
    from polymath.core import issues

    with contextlib.suppress(Exception):
        issues.report(db, "storage", "standby", text, every=3600.0)
