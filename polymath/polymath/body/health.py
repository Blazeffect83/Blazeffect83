"""Drive health (v0.12.0): read each drive's own SMART report once a day and act before it dies.

``smartctl`` (package smartmontools, installed by ``install.sh`` when apt works) answers in JSON. USB enclosures
often hide SMART behind their bridge; ``-d sat`` asks through it. What matters:

* **failing**: the drive says so itself (``smart_status.passed`` false), an NVMe drive raises a critical warning or
  has used up its spare blocks, or the counts that mean lost data grew since the last look: sectors waiting to be
  remapped (197), sectors that could not be read (198), errors reported to the computer (187), 10 or more newly
  remapped sectors (5) in a week, NVMe media errors.
* **watch**: remapped or waiting sectors that are not growing, 90 % of its write life used, or 60 °C and hotter.
* **ok** / **unknown** (no smartctl, or the enclosure does not pass SMART through).

The drive roles (``body.roles``) treat a failing drive as one that keeps dropping out: it never takes the brain or
the backups, and the brain moves off it at once when another drive can hold it. It is said once (inbox
``storage/roles:health:<time>``) with what to do. Read-only: smartctl only reads the report.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from typing import Any

WEEK_S = 7 * 86400.0
REALLOC_GROWTH = 10  # newly remapped sectors in a week that count as failing
HOT_C = 60
WORN_PCT = 90
ATTR = {5: "reallocated", 187: "reported_uncorrect", 197: "pending", 198: "uncorrectable"}
WEAR_ATTRS = {177: "normalized", 231: "normalized", 233: "normalized", 202: "used"}  # SSD life left / used


def run_smartctl(args: list[str]) -> tuple[int, str]:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 255, json.dumps({"error": str(exc)})
    return r.returncode, r.stdout


def read_report(disk: str, run: Callable[[list[str]], tuple[int, str]] = run_smartctl) -> dict[str, Any] | None:
    """The drive's SMART report as smartctl's JSON, through a USB bridge if needed; None when it cannot be read."""
    for extra in ([], ["-d", "sat"]):
        code, out = run(["smartctl", "-j", "-a", *extra, f"/dev/{disk}"])
        try:
            raw = json.loads(out) if out else {}
        except ValueError:
            raw = {}
        # bits 0-1: the command line or opening the device failed; anything else is a report (perhaps a bad one)
        if isinstance(raw, dict) and raw.get("device") and not code & 0b11:
            return raw
    return None


def counts_of(report: dict[str, Any]) -> dict[str, Any]:
    """The numbers that matter, from an ATA or NVMe report."""
    out: dict[str, Any] = {}
    status = report.get("smart_status") or {}
    if "passed" in status:
        out["passed"] = bool(status["passed"])
    for row in (report.get("ata_smart_attributes") or {}).get("table") or []:
        aid = int(row.get("id") or 0)
        if aid in ATTR:
            out[ATTR[aid]] = int((row.get("raw") or {}).get("value") or 0)
        elif aid in WEAR_ATTRS and "wear_pct" not in out:
            value = int(row.get("value") or 0)
            out["wear_pct"] = 100 - value if WEAR_ATTRS[aid] == "normalized" else value
    nvme = report.get("nvme_smart_health_information_log") or {}
    if nvme:
        out["critical_warning"] = int(nvme.get("critical_warning") or 0)
        out["media_errors"] = int(nvme.get("media_errors") or 0)
        out["wear_pct"] = int(nvme.get("percentage_used") or 0)
        spare, low = nvme.get("available_spare"), nvme.get("available_spare_threshold")
        if spare is not None and low is not None:
            out["spare_low"] = int(spare) <= int(low)
    temp = (report.get("temperature") or {}).get("current")
    if temp is not None:
        out["temp_c"] = int(temp)
    return out


def verdict(now: dict[str, Any], before: dict[str, Any] | None, *, days: float = 1.0) -> tuple[str, str]:
    """(status, why) from today's counts and the last ones (``before``, ``days`` ago)."""
    prev = before or {}

    def grew(key: str) -> int:
        return int(now.get(key) or 0) - int(prev.get(key) or 0) if before else 0

    if now.get("passed") is False:
        return "failing", "the drive reports itself as failing (SMART overall health: FAILED)"
    if now.get("critical_warning"):
        return "failing", f"the NVMe drive raises a critical warning ({now['critical_warning']})"
    if now.get("spare_low"):
        return "failing", "the NVMe drive has used up its spare blocks"
    for key, words in (("pending", "sectors waiting to be remapped"), ("uncorrectable", "sectors that cannot be read"),
                       ("reported_uncorrect", "read errors reported"), ("media_errors", "media errors")):  # fmt: skip
        if grew(key) > 0:
            return "failing", f"{words} went from {prev.get(key, 0)} to {now.get(key)}"
    if before and days <= 7 and grew("reallocated") >= REALLOC_GROWTH:
        return "failing", f"{grew('reallocated')} more sectors remapped in {days:.0f} day(s)"
    if int(now.get("pending") or 0) or int(now.get("reallocated") or 0):
        return "watch", (f"{now.get('reallocated', 0)} remapped and {now.get('pending', 0)} waiting sectors (not "
                         "growing)")  # fmt: skip
    if int(now.get("wear_pct") or 0) >= WORN_PCT:
        return "watch", f"{now['wear_pct']} % of its write life used"
    if int(now.get("temp_c") or 0) >= HOT_C:
        return "watch", f"it runs at {now['temp_c']} °C"
    return "ok", ""


def check(disk: str, last: dict[str, Any] | None, now_s: float,
          run: Callable[[list[str]], tuple[int, str]] = run_smartctl) -> dict[str, Any]:  # fmt: skip
    """Today's look at a drive: {status, why, counts, at}, compared with ``last`` (a previous result)."""
    report = read_report(disk, run)
    if report is None:
        return {"status": "unknown", "why": "no SMART report (smartctl missing, or the enclosure hides it)",
                "counts": (last or {}).get("counts") or {}, "at": now_s}  # fmt: skip
    counts = counts_of(report)
    base = (last or {}).get("counts") if (last or {}).get("status") != "unknown" else None
    days = (now_s - float((last or {}).get("at") or now_s)) / 86400.0
    status, why = verdict(counts, base, days=max(days, 1.0))
    keep = base if base is not None and status == "failing" else counts  # failing stays measured against the past
    return {"status": status, "why": why, "counts": counts if status != "failing" else keep, "at": now_s,
            "now": counts}  # fmt: skip
