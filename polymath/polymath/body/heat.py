"""Heat-aware scheduling (v0.12.0): learn which jobs warm the Pi, and hold those back while it is warm.

The guard throttles at 75 °C and pauses at 82 °C: by then the Pi is already hot. This model acts earlier. Each
cycle the CPU temperature is compared with the one at the start of the cycle before; the change, per minute of that
job's slice, feeds a running average for its kind (``heat``, °C/min: positive heats, negative lets it cool). It also
learns the usual temperature of each hour of the day.

When the CPU is within ``WARM_MARGIN`` (6 °C) of the throttle point, or within 10 °C of it during one of the four
warmest hours of the day, the hottest third of the job kinds it has learned (at least ``MIN_SAMPLES`` slices each)
waits; the rest go on, maintenance and the caretaker always. So it slows down by choosing cooler work instead of
stopping, and rarely reaches the throttle point. Kept in ``kv heat`` (written at most every 10 minutes).
"""

from __future__ import annotations

import time
from typing import Any

ALPHA = 0.1  # weight of a new slice in a kind's running average
HOUR_ALPHA = 0.05
WARM_MARGIN = 6.0  # °C below the throttle point where hot work starts to wait
WARM_HOUR_MARGIN = 10.0  # …and in the warmest hours of the day
MIN_SAMPLES = 5
SAVE_EVERY_S = 600.0
EXEMPT = ("body.", "caretaker.", "storage.", "net.probe")  # never held back: they keep the Pi itself well


class HeatModel:
    def __init__(self, db: Any, *, clock: Any = time.time) -> None:
        self.db = db
        self.clock = clock
        raw = db.kv_get("heat") if db is not None else None
        raw = raw if isinstance(raw, dict) else {}
        self.kinds: dict[str, list[float]] = {
            k: [float(v[0]), float(v[1])] for k, v in (raw.get("kinds") or {}).items()
        }
        self.hours: dict[str, float] = {str(h): float(t) for h, t in (raw.get("hours") or {}).items()}
        self._last: tuple[str, float, float] | None = None  # (kind, temperature, monotonic time) at a cycle's start
        self._saved = clock()
        self._dirty = False

    def step(self, kind: str, temp: float | None, now_mono: float) -> None:
        """At the start of a cycle: what the previous cycle's job did to the temperature, then remember this one."""
        if temp is None:
            self._last = None
            return
        if self._last is not None and self._last[0]:
            prev_kind, prev_temp, prev_at = self._last
            minutes = (now_mono - prev_at) / 60.0
            if minutes > 0.05:
                rate = (temp - prev_temp) / minutes
                avg, n = self.kinds.get(prev_kind, [0.0, 0.0])
                self.kinds[prev_kind] = [rate if n == 0 else (1 - ALPHA) * avg + ALPHA * rate, n + 1]
                self._dirty = True
        hour = str(time.localtime(self.clock()).tm_hour)
        old = self.hours.get(hour)
        self.hours[hour] = temp if old is None else (1 - HOUR_ALPHA) * old + HOUR_ALPHA * temp
        self._last = (kind, temp, now_mono)

    def started(self, kind: str) -> None:
        """The kind the current cycle runs (known once a job is claimed)."""
        if self._last is not None:
            self._last = (kind, self._last[1], self._last[2])

    def warm_hours(self) -> set[int]:
        if len(self.hours) < 12:
            return set()
        return {int(h) for h, _t in sorted(self.hours.items(), key=lambda kv: -kv[1])[:4]}

    def hot_kinds(self, temp: float | None, throttle_c: float) -> set[str]:
        """The kinds that wait now (empty while it is cool)."""
        if temp is None:
            return set()
        hour = time.localtime(self.clock()).tm_hour
        margin = WARM_HOUR_MARGIN if hour in self.warm_hours() else WARM_MARGIN
        if temp < throttle_c - margin:
            return set()
        known = sorted(((v[0], k) for k, v in self.kinds.items() if v[1] >= MIN_SAMPLES and not k.startswith(EXEMPT)),
                       reverse=True)  # fmt: skip
        hot = [k for rate, k in known[: max(1, len(known) // 3)] if rate > 0]
        return set(hot)

    def save(self, *, force: bool = False) -> None:
        now = self.clock()
        if self.db is None or not self._dirty or (not force and now - self._saved < SAVE_EVERY_S):
            return
        self._saved, self._dirty = now, False
        payload = {"kinds": {k: [round(v[0], 4), int(v[1])] for k, v in self.kinds.items()},
                   "hours": {h: round(t, 2) for h, t in self.hours.items()}}  # fmt: skip
        with self.db.transaction():
            self.db.kv_set("heat", payload)
