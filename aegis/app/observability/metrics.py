"""Host metrics read directly from /proc and /sys (no extra dependencies)."""
from __future__ import annotations

import os
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from ..db import Database, now_iso


@dataclass
class Snapshot:
    cpu_percent: float | None
    load_1m: float | None
    memory_percent: float | None
    memory_available_mb: float | None
    disk_free_mb: float | None
    disk_percent: float | None
    cpu_temp_c: float | None

    def as_dict(self) -> dict:
        return asdict(self)


def _cpu_times() -> tuple[int, int] | None:
    try:
        with open("/proc/stat") as f:
            parts = f.readline().split()[1:]
        values = [int(x) for x in parts]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return idle, sum(values)
    except (OSError, ValueError, IndexError):
        return None


def cpu_percent(interval: float = 0.25) -> float | None:
    a = _cpu_times()
    if a is None:
        return None
    time.sleep(interval)
    b = _cpu_times()
    if b is None or b[1] == a[1]:
        return None
    return round(100.0 * (1 - (b[0] - a[0]) / (b[1] - a[1])), 1)


def memory() -> tuple[float | None, float | None]:
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k] = int(v.strip().split()[0])
        total, avail = info["MemTotal"], info.get("MemAvailable", info.get("MemFree", 0))
        return round(100.0 * (1 - avail / total), 1), round(avail / 1024, 1)
    except (OSError, KeyError, ValueError, ZeroDivisionError):
        return None, None


def cpu_temperature() -> float | None:
    for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*/temp")):
        try:
            return round(int(zone.read_text().strip()) / 1000.0, 1)
        except (OSError, ValueError):
            continue
    return None


def load_1m() -> float | None:
    try:
        return round(os.getloadavg()[0], 2)
    except OSError:
        return None


def disk(path: Path) -> tuple[float | None, float | None]:
    try:
        u = shutil.disk_usage(path)
        return round(u.free / 1024 / 1024, 1), round(100.0 * u.used / u.total, 1)
    except OSError:
        return None, None


def snapshot(data_dir: Path, cpu_interval: float = 0.25) -> Snapshot:
    mem_pct, mem_avail = memory()
    free, dpct = disk(data_dir)
    return Snapshot(
        cpu_percent=cpu_percent(cpu_interval),
        load_1m=load_1m(),
        memory_percent=mem_pct,
        memory_available_mb=mem_avail,
        disk_free_mb=free,
        disk_percent=dpct,
        cpu_temp_c=cpu_temperature(),
    )


def record(db: Database, snap: Snapshot) -> None:
    db.insert("system_metrics", {**snap.as_dict(), "created_at": now_iso()})


def latest(db: Database) -> dict | None:
    return db.one("SELECT * FROM system_metrics ORDER BY id DESC LIMIT 1")
