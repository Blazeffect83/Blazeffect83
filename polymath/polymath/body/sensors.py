"""Hardware/OS sensors read straight from /sys and /proc (no psutil)."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Vitals:
    temp_c: float | None
    load1: float
    cpus: int
    mem_available_mb: float | None
    disk_free_gb: float | None
    disk_used_gb: float | None


def read_temperature(zone: Path) -> float | None:
    """CPU temperature in °C from a thermal zone (millidegrees), or None."""
    try:
        return int(zone.read_text().strip()) / 1000.0
    except (OSError, ValueError):
        return None


def read_meminfo(path: Path = Path("/proc/meminfo")) -> float | None:
    try:
        for line in path.read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024.0
    except (OSError, ValueError, IndexError):
        return None
    return None


def disk_usage(path: Path) -> tuple[float | None, float | None]:
    """(free GB, used-by-path GB is computed elsewhere) — here free/total-used of the filesystem."""
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        usage = shutil.disk_usage(probe)
    except OSError:
        return None, None
    return usage.free / 1e9, usage.used / 1e9


def read_vitals(thermal_zone: Path, data_dir: Path) -> Vitals:
    try:
        load1 = os.getloadavg()[0]
    except OSError:  # pragma: no cover - always available on Linux
        load1 = 0.0
    free, used = disk_usage(data_dir)
    return Vitals(
        temp_c=read_temperature(thermal_zone),
        load1=load1,
        cpus=os.cpu_count() or 1,
        mem_available_mb=read_meminfo(),
        disk_free_gb=free,
        disk_used_gb=used,
    )


def same_filesystem_as_root(path: Path) -> bool:
    """True when ``path`` lives on the root filesystem (on the Pi: the SD card)."""
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return os.stat(probe).st_dev == os.stat("/").st_dev
