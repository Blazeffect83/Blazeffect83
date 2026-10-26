"""Clock trouble on a Pi without a real-time clock (v0.13.0).

A Raspberry Pi 4 has no battery-backed clock. After a power cut it boots with the time saved at the last shutdown
(fake-hwclock), or even 1970, and only gets the real time once it reaches the internet. With the clock behind,
every job waiting for a time that has "not come yet" waits that much longer: offline, it could sit idle for days.

The agent remembers the latest time it has seen (``kv clock_high``, written with every heartbeat). When the clock
is more than ``JUMP_S`` behind it, the clock went back: every queued job's time is moved back by the same amount,
so the work keeps flowing, and it is said once (inbox ``clock/backwards``). When the real time comes back (a jump
forward), the jobs simply become due as usual.
"""

from __future__ import annotations

from typing import Any

JUMP_S = 300.0


def check(db: Any, now: float, high: float) -> tuple[float, float]:
    """(the high-water mark to keep, seconds the clock went back — 0 when it did not). Shifts queued jobs."""
    if high <= 0 or now >= high - JUMP_S:
        return max(high, now), 0.0
    back = high - now
    from polymath.core import issues

    with db.transaction():
        cur = db.execute(
            "UPDATE jobs SET not_before = not_before - ? WHERE state = 'queued' AND not_before > ?", (back, now)
        )
        issues.report(db, "clock", "backwards", f"the clock went back {back / 3600:.1f} h (a power cut without the "
                      f"internet: the Pi has no clock of its own); {cur.rowcount or 0} waiting jobs were moved back "
                      "with it, so nothing stalls", severity="warn", now=now, every=3600.0)  # fmt: skip
        db.kv_set("clock_high", now)
    return now, back
