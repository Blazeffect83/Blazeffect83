"""The hourly mirror (v0.12.0): a copy of the brain's database on the backup drive, at most an hour old.

The nightly backup is a compressed, verified copy taken once a day; a drive that dies at 23:00 loses a day of
learning. The mirror closes that gap without copying the whole database every hour: the database file is read in
1 MiB chunks, each chunk's hash is compared with the one stored at the last run, and only the chunks that changed are
written into the mirror file on the backup drive (``<drive>/polymath-brain/backups/mirror/``).

**A consistent copy while the agent keeps learning.** Between two job slices (no transaction open on the agent's
connection) the write-ahead log is checkpointed and truncated (``PRAGMA wal_checkpoint(TRUNCATE)``); a second,
read-only connection then opens a read transaction. Its snapshot has no frames in the log, so SQLite keeps every
later commit in the log and never copies one into the database file while that transaction is open: the file
stays exactly as of the snapshot while a background thread reads it. If anything else committed in between (the log
is not empty any more), the run is skipped and tried again at the next pause between slices.

**Safety.** The mirror is marked incomplete (``mirror.json``) before its first changed chunk is written and complete
only after it is flushed to the drive, so a mirror cut short by a power loss is never taken for a good one. Only on
a drive other than the database's (a mirror on the same disk dies with it), not in SD-card mode, not on the standby
brain. Every ``body.mirror_every_min`` minutes (60; 0: off), and not when the database has not changed.
``restore_mirror`` puts it back (``body.ssdwatch``'s rebuild uses it when it is newer than the newest backup).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from polymath.core.logging import get_logger
from polymath.core.safefs import safe_write

log = get_logger("mirror")
CHUNK = 1 << 20
MIRROR_DIR = "mirror"
MIRROR_FILE = "polymath-mirror.sqlite3"
STATE_FILE = "mirror.json"
RETRY_S = 300.0  # a run that could not start (a reader or writer in the way) is tried again after this
SPARE = 1e9  # the mirror leaves this much free on its drive


@dataclass
class MirrorResult:
    ok: bool
    detail: str
    at: float = 0.0
    changed: int = 0  # chunks written
    chunks: int = 0
    bytes: int = 0
    took_s: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "detail": self.detail, "at": self.at, "changed": self.changed, "chunks": self.chunks,
                "bytes": self.bytes, "took_s": round(self.took_s, 1), **self.extra}  # fmt: skip


def read_state(out: Path) -> dict[str, Any]:
    try:
        raw = json.loads((out / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_state(out: Path, state: dict[str, Any]) -> None:
    safe_write(out, STATE_FILE, json.dumps(state), mode=0o640)


def copy_changed(src: Path, out: Path, tick: Callable[[], None] = lambda: None) -> MirrorResult:
    """Bring ``out/MIRROR_FILE`` up to date with ``src`` chunk by chunk (``src`` must not change meanwhile)."""
    t0 = time.monotonic()
    out.mkdir(parents=True, exist_ok=True)
    state = read_state(out)
    old: list[str] = list(state.get("hashes") or []) if state.get("complete") and state.get("chunk") == CHUNK else []
    target = out / MIRROR_FILE
    if not target.exists():
        old = []
    size = src.stat().st_size
    hashes: list[str] = []
    changed = 0
    marked = False
    with src.open("rb") as fin, open(target, "r+b" if target.exists() else "w+b") as fout:
        i = 0
        while chunk := fin.read(CHUNK):
            h = hashlib.blake2b(chunk, digest_size=16).hexdigest()
            hashes.append(h)
            if i >= len(old) or old[i] != h:
                if not marked:  # from the first write on, the mirror is not a whole copy until it is flushed
                    _write_state(out, {**state, "complete": False})
                    marked = True
                fout.seek(i * CHUNK)
                fout.write(chunk)
                changed += 1
            i += 1
            if i % 64 == 0:
                tick()
        if fout.seek(0, os.SEEK_END) != size:
            if not marked:
                _write_state(out, {**state, "complete": False})
            fout.truncate(size)
            changed += 0 if marked else 1  # a shrunken file is a change too
        fout.flush()
        os.fsync(fout.fileno())
    now = time.time()
    _write_state(out, {"complete": True, "chunk": CHUNK, "hashes": hashes, "size": size, "at": now,
                       "source": str(src)})  # fmt: skip
    took = time.monotonic() - t0
    return MirrorResult(True, f"{changed} of {len(hashes)} MiB changed, written in {took:.0f} s", now, changed,
                        len(hashes), size, took)  # fmt: skip


def restore_mirror(out: Path, db_path: Path) -> Path:
    """Put a complete mirror back as the database (agent stopped). Returns where an old database was moved.
    Raises RuntimeError when the mirror is incomplete or damaged."""
    state = read_state(out)
    target = out / MIRROR_FILE
    if not state.get("complete") or not target.is_file():
        raise RuntimeError("the mirror is not a whole copy")
    tmp = db_path.with_name(db_path.name + ".restoring")
    shutil.copyfile(target, tmp)
    con = sqlite3.connect(str(tmp))
    try:
        check = str(con.execute("PRAGMA quick_check").fetchone()[0])
    finally:
        con.close()
    if check != "ok":
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"the mirror is damaged: {check[:200]}")
    old = db_path.with_name(db_path.name + f".pre-restore-{int(time.time())}")
    if db_path.exists():
        db_path.replace(old)
    for suffix in ("-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    tmp.replace(db_path)
    return old


def mirrors_on(root: Path) -> list[tuple[float, Path]]:
    """Complete mirrors on the drives mounted under ``root``: (when, folder), newest first."""
    found: list[tuple[float, Path]] = []
    with contextlib.suppress(OSError):
        for mnt in root.iterdir():
            out = mnt / "polymath-brain" / "backups" / MIRROR_DIR
            state = read_state(out)
            if state.get("complete") and (out / MIRROR_FILE).is_file():
                found.append((float(state.get("at") or 0), out))
    return sorted(found, reverse=True)


def _same_disk(a: Path, b: Path) -> bool:
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


class Mirror:
    """Runs the mirror from the agent's loop, between slices (``Agent.cycle``)."""

    def __init__(self, config: Any, db: Any, *, clock: Callable[[], float] = time.time) -> None:
        self.config = config
        self.db = db
        self.clock = clock
        self.next_at = 0.0
        self.thread: threading.Thread | None = None
        self.last: MirrorResult | None = None
        self._reported: MirrorResult | None = None
        self._seen: tuple[int, int, int, int] | None = None  # the database and its log at the last mirror

    def every_s(self) -> float:
        return float(self.config.body.mirror_every_min) * 60.0

    def target(self) -> Path | None:
        """Where the mirror goes: the backup drive's backups folder, never the database's own disk."""
        from polymath.core import standby
        from polymath.memory import pool as storage_pool

        if self.config.storage.sd_only or standby.is_standby(self.config):
            return None
        pool = storage_pool.for_config(self.config)
        if pool is None:
            return None
        db_path = Path(self.config.paths.db_path)
        try:
            size = db_path.stat().st_size
        except OSError:
            return None
        base = pool.backup_dir(need=size)
        if base == self.config.paths.backup_dir or _same_disk(base.parent, db_path):
            return None
        return base / MIRROR_DIR

    def _signature(self, db_path: Path) -> tuple[int, int, int, int] | None:
        try:
            a, wal = db_path.stat(), Path(f"{db_path}-wal")
            b = wal.stat() if wal.exists() else None
        except OSError:
            return None
        return (a.st_size, a.st_mtime_ns, b.st_size if b else 0, b.st_mtime_ns if b else 0)

    def busy(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def maybe_start(self, *, wait: bool = False) -> str:
        """Called between slices with no transaction open on the agent's connection. Returns what it did."""
        now = self.clock()
        every = self.every_s()
        if every <= 0 or now < self.next_at or self.busy():
            return "not due"
        out = self.target()
        if out is None:
            self.next_at = now + every
            return "no drive for it"
        db_path = Path(self.config.paths.db_path)
        sig = self._signature(db_path)
        if sig is not None and sig == self._seen:
            self.next_at = now + every
            return "unchanged"
        size = db_path.stat().st_size
        have = (out / MIRROR_FILE).stat().st_size if (out / MIRROR_FILE).exists() else 0
        out.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(out).free + have < size + SPARE:
            self.next_at = now + every
            self.last = MirrorResult(False, f"no room for the mirror on {out.parent.parent.parent.name}", now)
            return "no room"
        row = self.db.one("PRAGMA wal_checkpoint(TRUNCATE)")
        if row is None or int(row[0]) != 0:
            self.next_at = now + RETRY_S
            return "log busy"
        reader = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
        try:
            reader.execute("BEGIN")
            reader.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()  # the snapshot: an empty log
            wal = Path(f"{db_path}-wal")
            if wal.exists() and wal.stat().st_size > 0:
                raise sqlite3.OperationalError("something wrote in between")
        except sqlite3.Error:
            reader.close()
            self.next_at = now + RETRY_S
            return "log busy"
        self._seen = self._signature(db_path)
        self.next_at = now + every

        def run() -> None:
            try:
                self.last = copy_changed(db_path, out)
            except OSError as exc:
                self.last = MirrorResult(False, f"the mirror failed: {exc}", self.clock())
                log.warning("mirror failed", extra={"error": str(exc)})
            finally:
                with contextlib.suppress(sqlite3.Error):
                    reader.rollback()
                reader.close()

        if wait:
            run()
            return "mirrored"
        self.thread = threading.Thread(target=run, name="polymath-mirror", daemon=True)
        self.thread.start()
        return "started"

    def record(self) -> None:
        """Write the last result to the brain (``kv mirror``) once, from the loop's thread."""
        res = self.last
        if res is None or res is self._reported or self.busy():
            return
        self._reported = res
        try:
            with self.db.transaction():
                self.db.kv_set("mirror", res.as_dict())
                if not res.ok:
                    from polymath.core import issues

                    issues.report(self.db, "body", "mirror_failed", res.detail, severity="warn", every=86400.0)
        except Exception:  # the record of a mirror never stops the loop
            log.exception("could not record the mirror")
