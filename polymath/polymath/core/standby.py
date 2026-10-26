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

The same fold brings back the **SD-card brain** (``core.sdcard``): the brain Polymath ran on while SD-card mode
was on. Once the mode is off and the drive is back, it is folded in like a standby brain, from the first document
it read in that mode (what it was seeded with is in the real brain already), and then removed.

Nothing in here rewrites the real brain's existing knowledge: folding only adds.
"""

from __future__ import annotations

import contextlib
import copy
import json
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from polymath.core.config import Config, PathsConfig
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
    "body.verify_backup",
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
    discard_dir(Path(config.storage.standby_dir))


def discard_dir(root: Path) -> None:
    """Remove everything in a brain kept while the real one was away (the directory itself stays: systemd owns
    it)."""
    if not root.is_dir():
        return
    for entry in root.iterdir():
        with contextlib.suppress(OSError):
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()


@dataclass(frozen=True)
class Away:
    """A brain Polymath learned on while its real one was away: the standby brain, or the SD-card brain."""

    kind: str  # "standby" | "sd-card"
    root: Path  # its data directory
    marker: str  # its marker file in ``root``
    where: str  # for the feed: "on the standby brain", "on the SD card"

    @property
    def db_path(self) -> Path:
        return PathsConfig(data_dir=self.root).db_path

    def read_marker(self) -> dict[str, Any] | None:
        try:
            raw = json.loads((self.root / self.marker).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return raw if isinstance(raw, dict) and raw.get("id") else None


def away_brains(config: Config) -> list[Away]:
    """The brains that may wait to be folded into this one. Never this brain itself: the standby brain folds
    nothing, and the SD-card brain only ever folds a standby brain."""
    from polymath.core import sdcard

    if is_standby(config):
        return []
    out = [Away("standby", Path(config.storage.standby_dir), MARKER, "on the standby brain")]
    sd = Path(config.storage.sd_dir)
    if not config.storage.sd_only and sd != Path(config.paths.data_dir):
        out.append(Away("sd-card", sd, sdcard.MARKER, "on the SD card"))
    return out


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
    """On the real brain: a standby or SD-card brain waiting to be folded in gets its fold job."""
    folded: set[str] | None = None
    for away in away_brains(agent.config):
        mark = away.read_marker()
        if mark is None or not away.db_path.exists():
            continue
        if folded is None:
            folded = set(agent.db.kv_get(FOLDED_KEY) or [])
        if mark["id"] in folded:  # folded already, only its files were left (a stop right after the fold)
            discard_dir(away.root)
            continue
        agent.scheduler.enqueue(FOLD_KIND, {"id": mark["id"], "kind": away.kind}, key=f"fold:{mark['id']}",
                                priority=4.0)  # fmt: skip


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


def _fold_rest(main_db: Any, sdb: Any, mark: dict[str, Any], added: int, away: Away | None = None) -> dict[str, int]:
    """Sources, Wikidata records, the crawler's progress and the problems noted, then the record of the fold.

    From an SD-card brain only what happened in SD-card mode (since its marker's ``started``): what it was seeded
    with came from the real brain."""
    away = away or Away("standby", Path(), MARKER, "on the standby brain")
    since = float(mark.get("started") or 0.0) if away.kind == "sd-card" else 0.0
    n: dict[str, int] = {}
    with main_db.transaction():
        for r in sdb.query("SELECT * FROM sources"):
            main_db.execute(
                "INSERT OR IGNORE INTO sources(name, license, license_url, homepage, description) VALUES(?,?,?,?,?)",
                (r["name"], r["license"], r["license_url"], r["homepage"], r["description"]),
            )
        n["wikidata"] = 0
        for r in sdb.query("SELECT qid, label, description, aliases, enwiki, claims, fetched FROM wd_entities "
                           "WHERE fetched >= ?", (since,)):  # fmt: skip
            cur = main_db.execute(
                "INSERT OR IGNORE INTO wd_entities(qid, label, description, aliases, enwiki, claims, state, fetched) "
                "VALUES(?,?,?,?,?,?,'new',?)",
                (r["qid"], r["label"], r["description"], r["aliases"], r["enwiki"], r["claims"], r["fetched"]),
            )
            n["wikidata"] += cur.rowcount or 0
        n["pages"] = 0
        for r in sdb.query("SELECT url, fetched_at, http_status, etag, last_modified FROM frontier "
                           "WHERE state='done' AND COALESCE(fetched_at, 0) >= ?", (since,)):  # fmt: skip
            cur = main_db.execute(
                "UPDATE frontier SET state='done', fetched_at=?, http_status=?, etag=?, last_modified=? "
                "WHERE url=? AND state='new'",
                (r["fetched_at"], r["http_status"], r["etag"], r["last_modified"], r["url"]),
            )  # read in standby: not fetched again
            n["pages"] += cur.rowcount or 0
        n["issues"] = 0
        tag = "(on standby) " if away.kind == "standby" else "(on the SD card) "
        for r in sdb.query("SELECT at, text, detail FROM events WHERE kind = 'issue' AND at >= ? ORDER BY id",
                           (since,)):  # fmt: skip
            main_db.execute("INSERT INTO events(at, kind, text, detail) VALUES(?, 'issue', ?, ?)",
                            (r["at"], tag + str(r["text"]), r["detail"]))  # fmt: skip
            n["issues"] += 1
        n["agents"] = _fold_agents(main_db, sdb, since)
        n["documents"] = added
        took = _duration(time.time() - float(mark.get("started") or time.time()))
        agents = f", and {n['agents']} agent(s) you set up there" if n["agents"] else ""
        text = (f"back on its drive after {took} {away.where}: {added:,} documents it read there are now "
                f"in the real brain (it reads them again with everything it knows){agents}")  # fmt: skip
        main_db.execute("INSERT INTO events(at, kind, text, detail) VALUES(?, 'standby', ?, ?)",
                        (time.time(), text, json.dumps({"status": "folded", "from": away.kind, **n})))  # fmt: skip
        folded = list(main_db.kv_get(FOLDED_KEY) or [])
        main_db.kv_set(FOLDED_KEY, [*folded, mark["id"]][-20:])
    return n


def _fold_agents(main_db: Any, sdb: Any, since: float) -> int:
    """Agents Christian set up while the real brain was away (``origin = 'user'``, created since ``since``) are set
    up again on the real brain from their directive, so their scope is resolved against its knowledge, with the
    same name and status. One with that name or directive there already is left as it is. Evolved agents stay
    behind: the real brain's society evolves its own, from its own track record."""
    from polymath.agents import society, store

    if not sdb.scalar("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'agents'"):
        return 0
    names = {str(r["name"]) for r in main_db.query("SELECT name FROM agents")}
    known = {str(r["directive"]) for r in main_db.query("SELECT directive FROM agents WHERE status != 'retired'")}
    added = 0
    for r in sdb.query("SELECT name, directive, status FROM agents WHERE origin = 'user' AND created >= ? "
                       "ORDER BY id", (since,)):  # fmt: skip
        if str(r["name"]) in names or str(r["directive"]) in known:
            continue
        agent = society.spawn(main_db, str(r["directive"]), name=str(r["name"]))
        if r["status"] != "active":
            store.set_status(main_db, agent.id, str(r["status"]), "as it was set while the drive was away")
        names.add(agent.name)
        known.add(agent.directive)
        added += 1
    return added


def _duration(seconds: float) -> str:
    from polymath.core.netstate import _duration as fmt

    return fmt(seconds)


def fold_job(ctx: Any) -> Any:
    """Fold a standby or SD-card brain into the real one, a slice at a time; then remove it."""
    from polymath.core.db import Database
    from polymath.core.jobs import JobOutcome

    kind = str(ctx.job.payload.get("kind") or "standby")
    away = next((a for a in away_brains(ctx.config) if a.kind == kind), None)
    if away is None:
        return JobOutcome(done=True)  # not one this brain folds (SD-card mode switched on again meanwhile)
    mark = away.read_marker()
    if mark is None or mark.get("id") != ctx.job.payload.get("id"):
        return JobOutcome(done=True)  # gone already, or a different one (its own job comes)
    if mark["id"] in set(ctx.db.kv_get(FOLDED_KEY) or []):
        discard_dir(away.root)
        return JobOutcome(done=True)
    path = away.db_path
    if not path.exists():
        discard_dir(away.root)
        return JobOutcome(done=True)
    cp = dict(ctx.job.checkpoint or {})
    start = int(cp.get("after", mark.get("fold_after") or 0))  # an SD-card brain: from its first new document
    sdb = Database(path, readonly=True)
    try:
        deadline = time.monotonic() + max(1.0, ctx.time_left() - 2.0)
        after, added, finished = _fold_documents(ctx.db, sdb, start, deadline)
        added += int(cp.get("added", 0))
        if not finished:
            return JobOutcome(done=False, checkpoint={"after": after, "added": added}, value=float(added))
        n = _fold_rest(ctx.db, sdb, mark, added, away)
    finally:
        sdb.close()
    discard_dir(away.root)
    log.warning("brain folded in", extra={"from": away.kind, "id": mark["id"], **n})
    return JobOutcome(done=True, value=float(added))


def note(db: Any, text: str) -> None:
    """Into the caretaker's inbox (and so the feed's ``problem`` line) of whichever brain ``db`` is."""
    from polymath.core import issues

    with contextlib.suppress(Exception):
        issues.report(db, "storage", "standby", text, every=3600.0)
