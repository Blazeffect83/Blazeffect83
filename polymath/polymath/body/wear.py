"""SD card wear guard: how much is written to the disks, how long the SD card would last at that rate, and
what to do about it.

An SD card wears out from writes: each cell survives a limited number of erase cycles, and cards do extra
internal writes when they get many small random writes, which is how databases write. Nothing on an SD card
reports its own wear, so the guard measures writes and estimates:

* **what is written**: the whole device's counter (``/sys/dev/block/<major:minor>/stat``, sectors written, which
  includes the OS and its journal), and the agent's own share (``/proc/self/io``, ``write_bytes``). Both are
  sampled every ``SAMPLE_EVERY`` seconds into ``disk_writes``, for the data disk and, when different, the system
  disk;
* **how long it lasts**: the rated life is ``capacity × body.sd_endurance_cycles`` full-card writes divided by
  ``body.sd_write_amplification``. The daily budget that makes it last ``body.sd_target_years`` is that divided
  by the days. At the last 7 days' rate, the card would last ``rated life ÷ (rate × 365)`` years. This is a rate,
  not the card's remaining life, because writes from before Polymath was installed are unknown;
* **what it does**:
  * on an SD card above budget, *saver* mode doubles job slices: each slice is one transaction, so fewer and
    bigger commits write less;
  * the feed and the digest say so, once a day;
  * a data disk that the kernel has turned read-only (the classic sign of a failing card) pauses the agent with
    a clear reason;
  * the permanent fix is a drive. Plug one in and the storage helper moves the whole brain onto it
    (``body.volumes.Helper.move_home``), so the SD card only boots the Pi.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from polymath.core.config import Config
from polymath.core.db import Database

SAMPLE_EVERY = 900.0
KEEP_DAYS = 400
DAY = 86400.0


@dataclass(frozen=True)
class Device:
    name: str  # the partition (or disk) holding the path, e.g. mmcblk0p2
    disk: str  # the whole device, e.g. mmcblk0
    size_bytes: int
    kind: str  # sd | mmc | ssd | hdd | other
    stat: Path

    @property
    def is_sd(self) -> bool:
        return self.kind in {"sd", "mmc"}


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def device_of(path: Path, *, sys_root: Path = Path("/sys")) -> Device | None:
    """The block device a path lives on, from sysfs (None for network or virtual filesystems)."""
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        st = os.stat(probe)
    except OSError:
        return None
    node = sys_root / "dev" / "block" / f"{os.major(st.st_dev)}:{os.minor(st.st_dev)}"
    try:
        here = node.resolve(strict=True)
    except OSError:
        return None
    disk_dir = here.parent if (here / "partition").exists() else here
    try:
        size = int(_read(disk_dir / "size") or 0) * 512
    except ValueError:
        size = 0
    disk = disk_dir.name
    if disk.startswith("mmcblk"):
        kind = "sd" if _read(disk_dir / "device" / "type").upper() in {"SD", ""} else "mmc"
    elif _read(disk_dir / "queue" / "rotational") == "0":
        kind = "ssd"
    elif _read(disk_dir / "queue" / "rotational") == "1":
        kind = "hdd"
    else:
        kind = "other"
    return Device(name=here.name, disk=disk, size_bytes=size, kind=kind, stat=here / "stat")


def device_written(dev: Device) -> int | None:
    """Bytes written to the device since boot (sectors written × 512)."""
    fields = _read(dev.stat).split()
    try:
        return int(fields[6]) * 512
    except (IndexError, ValueError):
        return None


def process_written(path: Path = Path("/proc/self/io")) -> int | None:
    """Bytes this process caused to be written to storage (``write_bytes`` − ``cancelled_write_bytes``)."""
    try:
        rows = dict(line.split(":", 1) for line in path.read_text().splitlines() if ":" in line)
        return int(rows["write_bytes"]) - int(rows.get("cancelled_write_bytes", 0))
    except (OSError, KeyError, ValueError):
        return None


def read_only(path: Path, *, mounts: Path = Path("/proc/self/mounts")) -> bool:
    """True when the filesystem holding ``path`` is mounted read-only (the kernel does this on disk errors)."""
    target = str(path.resolve()) if path.exists() else str(path)
    best, ro = "", False
    try:
        lines = mounts.read_text().splitlines()
    except OSError:
        return False
    for line in lines:
        parts = line.split()
        if len(parts) < 4:
            continue
        mp = parts[1].replace("\\040", " ")
        inside = target == mp or target.startswith(mp.rstrip("/") + "/")
        if inside and len(mp) >= len(best):
            best, ro = mp, "ro" in parts[3].split(",")
    return ro


def rated_bytes(config: Config, size_bytes: int) -> float:
    b = config.body
    return size_bytes * b.sd_endurance_cycles / b.sd_write_amplification


def daily_budget(config: Config, size_bytes: int) -> float:
    return rated_bytes(config, size_bytes) / (365.0 * config.body.sd_target_years)


class WearMeter:
    """Samples the write counters (called from the body guard every cycle; does real work every 15 minutes)."""

    def __init__(
        self,
        config: Config,
        db: Database,
        *,
        sys_root: Path = Path("/sys"),
        proc_io: Path = Path("/proc/self/io"),
        mounts: Path = Path("/proc/self/mounts"),
    ) -> None:
        self.config = config
        self.db = db
        self.sys_root = sys_root
        self.proc_io = proc_io
        self.mounts = mounts
        self._at = -1e18
        self._last: dict[str, int] = {}

    def devices(self) -> list[tuple[str, Device]]:
        out: list[tuple[str, Device]] = []
        data = device_of(self.config.paths.data_dir, sys_root=self.sys_root)
        system = device_of(Path("/"), sys_root=self.sys_root)
        if data is not None:
            out.append(("data", data))
        if system is not None and (data is None or system.name != data.name):
            out.append(("system", system))
        return out

    def _delta(self, key: str, value: int | None) -> int:
        if value is None:
            return 0
        last = self._last.get(key)
        if last is None:
            stored = self.db.kv_get(f"wear:{key}")
            last = int(stored) if isinstance(stored, int | float) else None
        self._last[key] = value
        self.db.kv_set(f"wear:{key}", value)
        if last is None:
            return 0  # first sight of this counter: nothing to compare with yet
        return value - last if value >= last else value  # a counter reset (reboot / restart) starts from 0

    def sample(self, now: float, *, force: bool = False) -> bool:
        if not force and now - self._at < SAMPLE_EVERY:
            return False
        self._at = now
        agent = self._delta("agent", process_written(self.proc_io))
        for role, dev in self.devices():
            written = self._delta(f"dev:{dev.name}", device_written(dev))
            self.db.execute(
                "INSERT OR REPLACE INTO disk_writes(at, device, role, kind, size_bytes, bytes, agent_bytes) "
                "VALUES(?,?,?,?,?,?,?)",
                (now, dev.name, role, dev.kind, dev.size_bytes, written, agent if role == "data" else 0),
            )
        self.db.execute("DELETE FROM disk_writes WHERE at < ?", (now - KEEP_DAYS * DAY,))
        return True

    def data_read_only(self) -> bool:
        return read_only(self.config.paths.data_dir, mounts=self.mounts)


def report(db: Database, config: Config, now: float | None = None) -> list[dict[str, Any]]:
    """Per device: writes in the last day and 7 days, the budget, and how long an SD card lasts at this rate."""
    now = time.time() if now is None else now
    out: list[dict[str, Any]] = []
    devices = db.query(
        "SELECT device, role, kind, size_bytes, MIN(at) AS first FROM disk_writes "
        "WHERE at >= ? GROUP BY device ORDER BY role",
        (now - 30 * DAY,),
    )
    for d in devices:

        def total(since: float, column: str = "bytes", name: str = str(d["device"])) -> int:
            return int(
                db.scalar(
                    f"SELECT COALESCE(SUM({column}), 0) FROM disk_writes WHERE device = ? AND at > ?",
                    (name, since),
                    default=0,
                )
            )

        day = total(now - DAY)
        span = min(7 * DAY, max(now - float(d["first"]), 3600.0))
        per_day = total(now - 7 * DAY) * DAY / span
        size = int(d["size_bytes"] or 0)
        entry: dict[str, Any] = {
            "device": str(d["device"]),
            "role": str(d["role"]),
            "kind": str(d["kind"]),
            "size_bytes": size,
            "last_day_bytes": day,
            "per_day_bytes": int(per_day),
            "agent_last_day_bytes": total(now - DAY, "agent_bytes"),
            "measured_days": round(span / DAY, 2),
        }
        if entry["kind"] in {"sd", "mmc"} and size:
            budget = daily_budget(config, size)
            years = rated_bytes(config, size) / (per_day * 365.0) if per_day > 0 else None
            entry |= {
                "budget_bytes": int(budget),
                "years_at_this_rate": None if years is None else round(years, 1),
                "status": "ok" if per_day <= budget else ("critical" if years is not None and years < 1 else "high"),
            }
        out.append(entry)
    return out


def sd_summary(db: Database, config: Config, now: float | None = None) -> dict[str, Any] | None:
    """The SD card's line (data disk first, else the system disk), or None when no SD card is measured."""
    for e in report(db, config, now):
        if e["kind"] in {"sd", "mmc"}:
            return e
    return None


def gb(n: float) -> str:
    return f"{n / 1e9:.1f} GB" if n >= 1e8 else f"{n / 1e6:.0f} MB"


def describe(e: dict[str, Any]) -> str:
    """One plain line about a device's writes, for the feed, the digest and the CLI."""
    where = "SD card" if e["kind"] in {"sd", "mmc"} else f"{e['kind'].upper()} {e['device']}"
    text = f"{where} ({e['role']} disk): {gb(e['last_day_bytes'])} written in the last day"
    if "budget_bytes" in e:
        text += f", budget {gb(e['budget_bytes'])}/day"
        if e.get("years_at_this_rate") is not None:
            text += f"; at this rate it lasts about {e['years_at_this_rate']:g} years"
    return text
