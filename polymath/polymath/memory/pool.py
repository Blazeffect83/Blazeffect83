"""The storage pool: the main data directory plus every drive that was plugged in and adopted.

Drives are mounted by the privileged helper (:mod:`polymath.body.volumes`, started by udev when a drive is
plugged in) under ``storage.mount_root``. Each adopted drive holds a ``polymath-brain/`` folder with a
manifest (``.polymath-volume.json``: id, label, size, the budget Polymath may use). Nothing outside that
folder is ever touched. The agent only *discovers* drives here; it never mounts anything itself.

What goes on the drives:

* **document bodies** (the bulk of the database) are *spilled* to append-only pack files when the main disk
  fills up (``storage.spill_at`` of its budget). Each record is ``magic, doc id, length, crc32`` and the
  compressed body; the database keeps a pointer (``documents.codec = 'spilled'``, ``meta.spill``). A record
  is written and fsynced before the pointer commits, so a crash or an unplugged drive never loses a body:
  at worst a pack holds an orphan record.
* **downloads** (``raw/``) and **backups** (on a different drive from the database: safer).

The database itself, the indexes and the reports stay on the main disk, so unplugging a drive never stops
the agent. While a drive is away, its bodies read as unavailable (facts, metadata and the index stay) and
come back when it is plugged in again.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import struct
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from polymath.core.config import Config
from polymath.core.db import Database
from polymath.core.logging import get_logger

log = get_logger("storage")
BRAIN = "polymath-brain"
MANIFEST = ".polymath-volume.json"
IGNORE_FILE = "storage-ignore.json"  # in data_dir: drives the user retired (never re-adopted)
PACK_MAGIC = b"PMB1"
PACK_HEADER = struct.Struct(">4sQII")  # magic, doc id, length, crc32
PACK_LIMIT = 256 * 1024 * 1024
SCAN_TTL = 15.0
USED_TTL = 300.0


def directory_bytes(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(root, name)).st_size
    return total


@dataclass
class Volume:
    id: str
    label: str
    fstype: str
    model: str
    size_bytes: int
    budget_bytes: int
    dedicated: bool
    path: Path  # the polymath-brain folder

    @property
    def name(self) -> str:
        return self.label or self.model or self.id[:8]


def read_manifest(brain: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((brain / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("id") else None


def ignored_ids(config: Config) -> set[str]:
    out = {str(x) for x in config.storage.ignore}
    try:
        data = json.loads((config.paths.data_dir / IGNORE_FILE).read_text(encoding="utf-8"))
        out |= {str(x) for x in data} if isinstance(data, list) else set()
    except (OSError, ValueError):
        pass
    return out


class StoragePool:
    def __init__(
        self,
        config: Config,
        *,
        clock: Callable[[], float] = time.time,
        ismount: Callable[[str], bool] = os.path.ismount,
    ) -> None:
        self.config = config
        self.clock = clock
        self.ismount = ismount
        self._scan: list[Volume] = []
        self._scan_at = -1e18
        self._used: dict[str, tuple[float, int]] = {}

    # ---------------------------------------------------------------- discovery
    def volumes(self, *, fresh: bool = False) -> list[Volume]:
        """Adopted drives that are mounted and readable right now."""
        now = self.clock()
        if not fresh and now - self._scan_at < SCAN_TTL:
            return self._scan
        found: list[Volume] = []
        root = self.config.storage.mount_root
        try:
            entries = sorted(root.iterdir())
        except OSError:
            entries = []
        for mnt in entries:
            brain = mnt / BRAIN
            if not self.ismount(str(mnt)):
                continue
            m = read_manifest(brain)
            if m is None or m.get("retired"):
                continue
            try:
                found.append(
                    Volume(
                        id=str(m["id"]),
                        label=str(m.get("label") or ""),
                        fstype=str(m.get("fstype") or ""),
                        model=str(m.get("model") or ""),
                        size_bytes=int(m.get("size_bytes") or 0),
                        budget_bytes=int(m.get("budget_bytes") or 0),
                        dedicated=bool(m.get("dedicated")),
                        path=brain,
                    )
                )
            except (TypeError, ValueError):
                log.warning("unreadable volume manifest", extra={"path": str(brain)})
        self._scan, self._scan_at = found, now
        return found

    def find(self, vol_id: str) -> Volume | None:
        return next((v for v in self.volumes() if v.id == vol_id), None)

    def used(self, v: Volume) -> int:
        now = self.clock()
        hit = self._used.get(v.id)
        if hit is None or now - hit[0] >= USED_TTL:
            hit = (now, directory_bytes(v.path))
            self._used[v.id] = hit
        return hit[1]

    def _grew(self, v: Volume, n: int) -> None:
        at, used = self._used.get(v.id, (self.clock(), 0))
        self._used[v.id] = (at, used + n)

    def room(self, v: Volume) -> int:
        """Bytes Polymath may still put on this drive: its budget, and the drive's real free space."""
        try:
            usage = shutil.disk_usage(v.path)
        except OSError:
            return 0
        reserve = int(usage.total * self.config.storage.reserve_fraction)
        return max(0, min(v.budget_bytes - self.used(v), usage.free - reserve))

    def choose(self, need: int = 0) -> Volume | None:
        """The online drive with the most room that can take ``need`` bytes."""
        fits = [(r, v.id, v) for v in self.volumes() if (r := self.room(v)) > 0 and r >= need]
        return max(fits, key=lambda t: (t[0], t[1]))[2] if fits else None

    def totals(self) -> dict[str, Any]:
        vols = self.volumes()
        return {
            "primary_budget_bytes": int(self.config.body.disk_budget_gb * 1e9),
            "volumes": len(vols),
            "volume_budget_bytes": sum(v.budget_bytes for v in vols),
            "volume_used_bytes": sum(self.used(v) for v in vols),
            "brain_bytes": int(self.config.body.disk_budget_gb * 1e9) + sum(v.budget_bytes for v in vols),
        }

    # ---------------------------------------------------------------- bodies
    def _pack(self, v: Volume, size: int) -> Path:
        d = v.path / "bodies"
        d.mkdir(parents=True, exist_ok=True)
        packs = sorted(d.glob("pack-*.bin"))
        if packs and packs[-1].stat().st_size + size <= PACK_LIMIT:
            return packs[-1]
        n = int(packs[-1].stem.split("-")[1]) + 1 if packs else 1
        return d / f"pack-{n:06d}.bin"

    def write_body(self, v: Volume, doc_id: int, blob: bytes, codec: str) -> dict[str, Any]:
        """Append one body to the drive and fsync it. Returns the pointer the database keeps."""
        pack = self._pack(v, PACK_HEADER.size + len(blob))
        crc = zlib.crc32(blob)
        with pack.open("ab") as f:
            off = f.tell()
            f.write(PACK_HEADER.pack(PACK_MAGIC, doc_id, len(blob), crc))
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        self._grew(v, PACK_HEADER.size + len(blob))
        return {"vol": v.id, "pack": pack.name, "off": off, "len": len(blob), "crc": crc, "codec": codec}

    def read_body(self, ref: dict[str, Any], doc_id: int) -> bytes | None:
        """The body a pointer refers to, or None while its drive is away (or the record is damaged)."""
        v = self.find(str(ref.get("vol", "")))
        if v is None:
            return None
        try:
            with (v.path / "bodies" / str(ref["pack"])).open("rb") as f:
                f.seek(int(ref["off"]))
                magic, rid, length, crc = PACK_HEADER.unpack(f.read(PACK_HEADER.size))
                blob = f.read(length)
        except (OSError, KeyError, ValueError, struct.error):
            return None
        if magic != PACK_MAGIC or rid != doc_id or length != int(ref["len"]) or zlib.crc32(blob) != crc:
            log.warning("damaged body record", extra={"volume": v.id, "doc": doc_id, "pack": ref.get("pack")})
            return None
        return blob

    # ---------------------------------------------------------------- files
    def _safe(self, base: Path, rel: str) -> Path:
        path = (base / rel).resolve()
        if base.resolve() not in path.parents:
            raise ValueError(f"refusing path outside {base}: {rel}")
        return path

    def raw_path(self, rel: str) -> Path:
        """Where a download lives: wherever it already is (complete or partial), else on the roomiest drive."""
        bases = [self.config.paths.raw_dir] + [v.path / "raw" for v in self.volumes()]
        for base in bases:
            p = self._safe(base, rel)
            if p.exists() or p.with_name(p.name + ".part").exists():
                return p
        v = self.choose()
        return self._safe(v.path / "raw" if v is not None else self.config.paths.raw_dir, rel)

    def raw_dirs(self) -> list[Path]:
        return [self.config.paths.raw_dir] + [v.path / "raw" for v in self.volumes()]

    def backup_dir(self, need: int = 0) -> Path:
        """Backups go to a drive other than the database's when one has room (a dead disk then loses less)."""
        v = self.choose(need)
        return v.path / "backups" if v is not None else self.config.paths.backup_dir

    def backup_dirs(self) -> list[Path]:
        return [self.config.paths.backup_dir] + [v.path / "backups" for v in self.volumes()]


# ------------------------------------------------------------------ process-wide pool
_ACTIVE: StoragePool | None = None


def activate(config: Config, **kw: Any) -> StoragePool:
    """Make ``config``'s pool the one document reads use in this process (agent, dashboard, CLI)."""
    global _ACTIVE
    _ACTIVE = StoragePool(config, **kw)
    return _ACTIVE


def active() -> StoragePool | None:
    return _ACTIVE


def for_config(config: Config) -> StoragePool | None:
    """The active pool, if it belongs to ``config`` (placement decisions never follow a foreign pool)."""
    return _ACTIVE if _ACTIVE is not None and _ACTIVE.config is config else None


def deactivate() -> None:
    global _ACTIVE
    _ACTIVE = None


# ------------------------------------------------------------------ bookkeeping in the database
def sync(db: Database, pool: StoragePool) -> list[dict[str, Any]]:
    """Record drives arriving, leaving and coming back (``volumes`` / ``volume_events``). Returns new events."""
    now = pool.clock()
    online = {v.id: v for v in pool.volumes()}
    known = {str(r["id"]): r for r in db.query("SELECT id, online, retired FROM volumes")}
    events: list[dict[str, Any]] = []

    def event(vol: str, kind: str, detail: str) -> None:
        db.execute("INSERT INTO volume_events(at, volume_id, event, detail) VALUES(?,?,?,?)", (now, vol, kind, detail))
        events.append({"volume": vol, "event": kind, "detail": detail})

    for vid, v in online.items():
        used = pool.used(v)
        desc = json.dumps(
            {
                "name": v.name,
                "fstype": v.fstype,
                "size": v.size_bytes,
                "budget": v.budget_bytes,
                "dedicated": v.dedicated,
            }
        )
        if vid not in known:
            db.execute(
                "INSERT INTO volumes(id, label, fstype, model, size_bytes, budget_bytes, dedicated, path, online, "
                "used_bytes, first_seen, last_seen) VALUES(?,?,?,?,?,?,?,?,1,?,?,?)",
                (vid, v.label, v.fstype, v.model, v.size_bytes, v.budget_bytes, int(v.dedicated), str(v.path), used,
                 now, now),
            )  # fmt: skip
            event(vid, "added", desc)
            log.info("storage drive added", extra={"volume": vid, "name": v.name, "budget": v.budget_bytes})
        else:
            if not known[vid]["online"]:
                event(vid, "online", desc)
            db.execute(
                "UPDATE volumes SET online=1, retired=0, last_seen=?, used_bytes=?, budget_bytes=?, path=? WHERE id=?",
                (now, used, v.budget_bytes, str(v.path), vid),
            )
    for vid, r in known.items():
        if vid not in online and r["online"]:
            db.execute("UPDATE volumes SET online=0 WHERE id=?", (vid,))
            if not r["retired"]:
                event(vid, "offline", "")
                log.info("storage drive went away", extra={"volume": vid})
    return events
