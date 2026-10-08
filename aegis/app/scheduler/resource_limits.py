"""Resource guard: decides whether expensive work may run right now."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from ..config import Settings
from ..observability.metrics import Snapshot


@dataclass
class ResourceVerdict:
    ok: bool
    reasons: list[str] = field(default_factory=list)


def evaluate(settings: Settings, snap: Snapshot) -> ResourceVerdict:
    reasons = []
    if snap.cpu_temp_c is not None and snap.cpu_temp_c >= settings.max_cpu_temp_c:
        reasons.append(f"CPU temperature {snap.cpu_temp_c}°C ≥ {settings.max_cpu_temp_c}°C")
    if snap.disk_free_mb is not None and snap.disk_free_mb < settings.min_free_disk_mb:
        reasons.append(f"free disk {snap.disk_free_mb:.0f} MB < {settings.min_free_disk_mb} MB")
    if snap.memory_percent is not None and snap.memory_percent >= settings.max_memory_percent:
        reasons.append(f"memory use {snap.memory_percent}% ≥ {settings.max_memory_percent}%")
    cpus = os.cpu_count() or 1
    if snap.load_1m is not None and snap.load_1m / cpus >= settings.max_load_per_cpu:
        reasons.append(f"load {snap.load_1m} ≥ {settings.max_load_per_cpu}/cpu")
    return ResourceVerdict(ok=not reasons, reasons=reasons)
