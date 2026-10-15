"""Bring the brain's drive back by itself: ``polymath storage heal`` (root, every minute from a systemd timer).

Why: some enclosures (DockCase, with a capacitor for a safe power-off) do not power-cycle when the Pi reboots.
Afterwards the drive is missing, or the enclosure answers with ``sda 0B usb``, and only a replug used to help.
Or the drive freezes and comes back as a new device while the brain's mount still points at the dead one.

What it does, only while the brain's drive is missing or dead (a healthy brain costs one ``stat``):

1. **Dead or frozen mount** (Input/output error on ``/srv/polymath``, or no answer at all): drop the mount
   (lazily: never waits), reset the USB drive behind it, restart the services without waiting on them, and ask
   for the drive again (``mount_home`` binds it and restarts the services).
2. **A USB drive reporting 0 bytes**: a soft reset of just that device (its ``authorized`` switch in sysfs, off
   and on: a replug in software). At most once every ``RESET_EVERY`` seconds.
3. **Still missing** after ``POWER_AFTER`` seconds: cut the power of the USB ports for ``heal_power_off_s``
   (``uhubctl``), long enough for a capacitor-backed enclosure to really switch off, then switch it back on. A
   real replug, at most once every ``POWER_EVERY`` seconds and ``POWER_PER_DAY`` times a day.

Every action is written to ``/var/lib/polymath/heal.json`` and copied to ``/run/polymath/heal.json`` (readable
by everyone): ``polymath doctor`` shows it, and the agent reports it to the caretaker's inbox when it starts.
It never formats, never touches a drive that is not the brain's, and does nothing while the brain is healthy.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from polymath.body.sensors import unresponsive_usb_disks
from polymath.core import fsprobe
from polymath.core.config import HOME_FILE, MOVED_FILE, Config
from polymath.core.logging import get_logger

log = get_logger("heal")

STATE = Path("/var/lib/polymath/heal.json")
PUBLIC = "heal.json"  # in the runtime directory (/run/polymath)
RESET_EVERY = 120.0  # soft USB resets at most this often
POWER_AFTER = 90.0  # the brain must be missing this long before the USB power is cut
POWER_EVERY = 900.0  # power cycles at most this often
POWER_PER_DAY = 6
RETRIGGER_EVERY = 120.0  # asking udev to attach drives again
KEEP_ACTIONS = 30
PROBE_TIMEOUT_S = 8.0


@dataclass
class HealState:
    missing_since: float = 0.0
    last_reset: float = 0.0
    last_power: float = 0.0
    last_retrigger: float = 0.0
    power_day: str = ""
    power_count: int = 0
    actions: list[dict[str, Any]] = field(default_factory=list)  # newest last: {at, action, detail}


def brain_state(config: Config) -> str:
    """``healthy`` | ``dead`` (the mount gives I/O errors or does not answer at all) | ``missing`` (lives on a drive
    that is not there) | ``local`` (the brain lives on this disk, nothing to heal). Never hangs (``core.fsprobe``)."""
    data = config.paths.data_dir
    home = fsprobe.probe(data / HOME_FILE, timeout=PROBE_TIMEOUT_S)
    if home in {fsprobe.OK, fsprobe.DENIED, fsprobe.ERROR}:
        return "healthy"  # permission trouble is not a drive problem
    if fsprobe.unhealthy(home):
        return "dead"
    moved = fsprobe.probe(data / MOVED_FILE, timeout=PROBE_TIMEOUT_S)
    if fsprobe.unhealthy(moved):
        return "dead"
    return "missing" if moved == fsprobe.OK else "local"


def usb_device_dir(block_dir: Path) -> Path | None:
    """The USB device a block device hangs off (the sysfs directory with ``authorized`` and ``idVendor``)."""
    try:
        cur = block_dir.resolve()
    except OSError:
        return None
    while cur != cur.parent:
        if (cur / "authorized").exists() and (cur / "idVendor").exists():
            return cur
        cur = cur.parent
    return None


class Healer:
    def __init__(
        self,
        config: Config,
        run: Callable[..., Any],
        *,
        services: tuple[str, ...],
        mounts_at: Callable[[Path], list[tuple[str, str]]],
        sys_block: Path = Path("/sys/block"),
        state_path: Path = STATE,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        which: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self.config = config
        self.run = run
        self.services = services
        self.mounts_at = mounts_at
        self.sys_block = sys_block
        self.state_path = state_path
        self.clock = clock
        self.sleep = sleep
        self.which = which

    # ---------------------------------------------------------------- state
    def load(self) -> HealState:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return HealState()
        known = HealState.__dataclass_fields__
        return HealState(**{k: v for k, v in raw.items() if k in known}) if isinstance(raw, dict) else HealState()

    def save(self, st: HealState) -> None:
        payload = json.dumps(asdict(st), indent=1)
        for target in (self.state_path, Path(self.config.paths.run_dir) / PUBLIC):
            if target != self.state_path and not target.parent.is_dir():
                continue
            with contextlib.suppress(OSError):
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(f".{target.name}.tmp")
                tmp.write_text(payload, encoding="utf-8")
                os.chmod(tmp, 0o644)
                os.replace(tmp, target)

    def _did(self, st: HealState, action: str, detail: str) -> dict[str, Any]:
        entry = {"at": self.clock(), "action": action, "detail": detail}
        st.actions = [*st.actions, entry][-KEEP_ACTIONS:]
        log.warning("storage heal", extra=entry)
        return entry

    # ---------------------------------------------------------------- actions
    def _retrigger(self, st: HealState) -> None:
        """Attach the drives again. A drive's unit that already ran (``polymath-volume@sda``, RemainAfterExit)
        ignores a new udev "add", so the units that exist are restarted (detach, then attach: a fresh mount and
        the brain bound onto it), and udev is asked for the devices that have no unit yet."""
        self.run(["systemctl", "restart", "--no-block", "polymath-volume@*.service"])
        self.run(["udevadm", "trigger", "--action=add", "--subsystem-match=block"])
        st.last_retrigger = self.clock()

    def reset_usb(self, disk: str) -> bool:
        """A software replug of one USB device: its ``authorized`` switch off, then on."""
        dev = usb_device_dir(self.sys_block / disk)
        if dev is None:
            return False
        try:
            (dev / "authorized").write_text("0")
            self.sleep(2.0)
            (dev / "authorized").write_text("1")
        except OSError as exc:
            log.warning("usb reset failed", extra={"disk": disk, "error": str(exc)})
            return False
        return True

    def release_dead_drives(self) -> list[str]:
        """Drives under the mount root whose own mount is dead or frozen: lazy-unmount (never blocks) and reset
        the USB device behind each. Returns the disks reset."""
        root = self.config.storage.mount_root
        reset: list[str] = []
        try:
            entries = sorted(root.iterdir())
        except OSError:
            return reset
        for mnt in entries:
            sources = [src for _mp, src in self.mounts_at(mnt)]
            if not sources or not fsprobe.unhealthy(fsprobe.probe(mnt, timeout=PROBE_TIMEOUT_S)):
                continue
            self.run(["umount", "-l", str(mnt)])
            disk = re.sub(r"p?\d+$", "", Path(sources[-1]).name) if sources[-1].startswith("/dev/sd") else ""
            if disk and self.reset_usb(disk):
                reset.append(disk)
        return reset

    def power_cycle(self) -> str:
        """Cut the USB ports' power long enough for an enclosure with a capacitor to switch off, then restore it."""
        st = self.config.storage
        if self.which("uhubctl") is None:
            return "cannot cut the USB power: uhubctl is not installed (sudo apt install uhubctl)"
        for hub in st.heal_hubs:
            self.run(["uhubctl", "-l", hub, "-a", "0"])
        self.sleep(st.heal_power_off_s)
        for hub in reversed(st.heal_hubs):
            self.run(["uhubctl", "-l", hub, "-a", "1"])
        return f"cut the USB power for {st.heal_power_off_s:.0f} s and restored it"

    # ---------------------------------------------------------------- the check
    def heal(self) -> dict[str, Any]:
        """One check (and at most one kind of action). Returns what it found and did."""
        cfg = self.config.storage
        st = self.load()
        now = self.clock()
        if not cfg.heal:  # switched off (SD-card mode switches it off: no drive is touched, not even looked at)
            if st.missing_since:
                st.missing_since = 0.0
                self.save(st)
            return {"brain": "sd-card" if cfg.sd_only else "not watched", "action": "none"}
        state = brain_state(self.config)
        if state in {"healthy", "local"}:
            if st.missing_since:
                st.missing_since = 0.0
                self.save(st)
            return {"brain": state, "action": "none"}
        st.missing_since = st.missing_since or now
        data = self.config.paths.data_dir
        did: dict[str, Any] | None = None
        if state == "dead":
            # Nothing here waits on the frozen drive: a lazy unmount returns at once, and resetting the drive makes
            # whatever hangs on it (the agent, the dashboard) fail and exit. Only then are they restarted, without
            # waiting (`--no-block`): a service stuck in the kernel can take minutes to stop.
            for _mp in [mp for mp, _src in self.mounts_at(data) if mp == str(data)] or [str(data)]:
                self.run(["umount", "-l", str(data)])
            reset = self.release_dead_drives()  # the drive's own mount is just as dead: release it, reset the drive
            self.run(["systemctl", "reset-failed", *self.services])
            self.run(["systemctl", "restart", "--no-block", *self.services])  # they wait for the drive, visibly
            self._retrigger(st)  # the drive's unit restarts: mounted fresh, the brain bound, the services restarted
            extra = f"; reset {', '.join('/dev/' + d for d in reset)}" if reset else ""
            did = self._did(st, "dead_mount", f"dropped the dead mount at {data}{extra} and asked for the drive again")
        else:
            dead = unresponsive_usb_disks(self.sys_block)
            day = time.strftime("%Y-%m-%d", time.localtime(now))
            if st.power_day != day:
                st.power_day, st.power_count = day, 0
            if dead and now - st.last_reset >= RESET_EVERY:
                done = [d for d in dead if self.reset_usb(d)]
                st.last_reset = now
                self.sleep(5.0)
                self._retrigger(st)
                did = self._did(st, "usb_reset", f"reset {', '.join('/dev/' + d for d in done) or 'nothing'} "
                                f"(reported 0 bytes)")  # fmt: skip
            elif (cfg.heal_power_cycle and now - st.missing_since >= POWER_AFTER
                  and now - st.last_power >= POWER_EVERY and st.power_count < POWER_PER_DAY):  # fmt: skip
                detail = self.power_cycle()
                st.last_power = now
                if not detail.startswith("cannot"):
                    st.power_count += 1
                    self.sleep(10.0)
                    self._retrigger(st)
                did = self._did(st, "usb_power", detail)
            elif now - st.last_retrigger >= RETRIGGER_EVERY:
                self._retrigger(st)
                did = {"action": "retrigger"}
        self.save(st)
        return {"brain": state, "action": (did or {}).get("action", "waiting"), "detail": (did or {}).get("detail")}


def read_public(config: Config) -> dict[str, Any] | None:
    """The helper's record as everyone may read it (``/run/polymath/heal.json``), or None."""
    with contextlib.suppress(OSError, ValueError):
        raw = json.loads((Path(config.paths.run_dir) / PUBLIC).read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else None
    return None
