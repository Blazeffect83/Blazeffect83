"""The body: keep the Pi healthy and share it politely with the Minecraft server.

Modes (strongest wins), recomputed every cycle from cheap sensors:

* **pause** — CPU ≥ ``pause_celsius`` (82 °C), held until it cools 5 °C below;
  or the data disk is almost full (< half of ``disk_min_free_gb``).
* **yield** — someone is playing on the Minecraft server (Server List Ping), or
  free space is below ``disk_min_free_gb``: only light jobs run, at half pace.
* **throttle** — CPU ≥ ``throttle_celsius`` (75 °C), held until 3 °C below: light
  jobs only, slices shortened.
* **normal**.

The guard also logs a vitals sample every five minutes (dashboard charts) and asks
for eviction when the data directory outgrows ``disk_budget_gb`` or space runs low.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path

from polymath.body.minecraft import ServerStatus, ping
from polymath.body.sensors import Vitals, read_vitals
from polymath.core.config import Config
from polymath.core.db import Database
from polymath.core.logging import get_logger
from polymath.core.loop import BodyState

log = get_logger("body")
VITALS_EVERY = 300.0
SIZE_EVERY = 600.0
PAUSE_HYSTERESIS = 5.0
THROTTLE_HYSTERESIS = 3.0
EVICT_PRIORITY = 3.5  # urgent: runs ahead of the bandit's choices


def directory_bytes(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue
    return total


def data_bytes(db: Database, data_dir: Path) -> int:
    """Bytes the agent's data really occupies: files, minus SQLite pages that are free for reuse."""
    free_pages = int(db.scalar("PRAGMA freelist_count", default=0))
    page = int(db.scalar("PRAGMA page_size", default=4096))
    return max(0, directory_bytes(data_dir) - free_pages * page)


class Guard:
    def __init__(
        self,
        config: Config,
        db: Database,
        *,
        clock: Callable[[], float] = time.time,
        pinger: Callable[[str, int], ServerStatus | None] | None = None,
        sensors: Callable[[], Vitals] | None = None,
    ) -> None:
        self.config = config
        self.db = db
        self.clock = clock
        self.pinger = pinger or (lambda host, port: ping(host, port))
        self.sensors = sensors or (lambda: read_vitals(config.body.thermal_zone, config.paths.data_dir))
        self.thermal = "normal"
        self._players: int | None = None
        self._players_at = -1e18
        self._size_gb: float | None = None
        self._size_at = -1e18
        self._vitals_at = -1e18
        self._last_mode = "normal"

    # ----------------------------------------------------------------- sensors
    def players(self) -> int | None:
        b = self.config.body
        if not b.minecraft_check:
            return None
        now = self.clock()
        if now - self._players_at >= b.minecraft_poll_seconds:
            status = self.pinger(b.minecraft_host, b.minecraft_port)
            self._players = status.online if status is not None else None
            self._players_at = now
        return self._players

    def data_gb(self) -> float:
        now = self.clock()
        if self._size_gb is None or now - self._size_at >= SIZE_EVERY:
            self._size_gb = data_bytes(self.db, self.config.paths.data_dir) / 1e9
            self._size_at = now
        return self._size_gb

    def _thermal_mode(self, temp: float | None) -> str:
        b = self.config.body
        if temp is None:
            return self.thermal
        if temp >= b.pause_celsius:
            self.thermal = "pause"
        elif self.thermal == "pause" and temp >= b.pause_celsius - PAUSE_HYSTERESIS:
            pass  # still cooling down
        elif temp >= b.throttle_celsius or (
            self.thermal in {"pause", "throttle"} and temp >= b.throttle_celsius - THROTTLE_HYSTERESIS
        ):
            self.thermal = "throttle"
        else:
            self.thermal = "normal"
        return self.thermal

    # ----------------------------------------------------------------- observe
    def observe(self) -> BodyState:
        b = self.config.body
        v = self.sensors()
        reasons: list[str] = []
        rank = {"normal": 0, "throttle": 1, "yield": 2, "pause": 3}
        mode = "normal"
        disk_critical = False

        def want(m: str, why: str) -> None:
            nonlocal mode
            reasons.append(why)
            if rank[m] > rank[mode]:
                mode = m

        thermal = self._thermal_mode(v.temp_c)
        if thermal == "pause":
            want("pause", f"CPU {v.temp_c:.1f} °C: cooling down below {b.pause_celsius - PAUSE_HYSTERESIS:.0f} °C")
        elif thermal == "throttle":
            want("throttle", f"CPU {v.temp_c:.1f} °C ≥ throttle point")
        players = self.players()
        if players:
            want("yield", f"{players} player(s) on the Minecraft server")
        if v.disk_free_gb is not None:
            if v.disk_free_gb < b.disk_min_free_gb / 2:
                want("pause", f"only {v.disk_free_gb:.1f} GB free on the data disk")
                disk_critical = True
            elif v.disk_free_gb < b.disk_min_free_gb:
                want("yield", f"only {v.disk_free_gb:.1f} GB free on the data disk")
        data_gb = self.data_gb()
        if data_gb > b.disk_budget_gb or (v.disk_free_gb is not None and v.disk_free_gb < b.disk_min_free_gb):
            self._request_eviction(data_gb, v.disk_free_gb)
        intensity = {"normal": 1.0, "throttle": 0.5, "yield": 0.5, "pause": 0.0}[mode]
        if mode != self._last_mode:
            log.info("body mode changed", extra={"from": self._last_mode, "to": mode, "reasons": reasons})
            self._last_mode = mode
        self._log_vitals(v, data_gb, players, mode)
        # a disk-full pause still lets eviction run (it is what ends the pause); a thermal pause stops everything
        allow = ["body.evict"] if mode == "pause" and disk_critical and thermal != "pause" else []
        return BodyState(mode=mode, intensity=intensity, reasons=reasons, vitals=v, players_online=players, allow=allow)

    def _request_eviction(self, data_gb: float, free_gb: float | None) -> None:
        from polymath.core.scheduler import Scheduler

        slot = int(self.clock() // 3600)
        Scheduler(self.db).enqueue(
            "body.evict",
            {"data_gb": round(data_gb, 2), "free_gb": free_gb},
            key=f"body.evict:{slot}",
            priority=EVICT_PRIORITY,
        )

    def _log_vitals(self, v: Vitals, data_gb: float, players: int | None, mode: str) -> None:
        now = self.clock()
        if now - self._vitals_at < VITALS_EVERY:
            return
        self._vitals_at = now
        self.db.execute(
            "INSERT OR REPLACE INTO vitals(at, temp_c, load1, mem_mb, disk_free_gb, data_gb, players, mode) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (now, v.temp_c, v.load1, v.mem_available_mb, v.disk_free_gb, round(data_gb, 3), players, mode),
        )
        self.db.execute("DELETE FROM vitals WHERE at < ?", (now - 30 * 86400,))
