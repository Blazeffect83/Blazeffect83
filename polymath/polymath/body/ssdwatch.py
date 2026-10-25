"""The SSD watch: back onto the SSD by itself once it is healthy, and back to the SD card if it fails again.

Runs as root every minute with the drive helper (``polymath storage heal``, ``polymath-heal.timer``), so it works
with nobody at the Pi.

**In SD-card mode** (``core.sdcard``) the brain runs on the SD card and no drive is used. The watch looks for the
brain's SSD (the drive whose filesystem the SD card's ``MOVED-TO-DRIVE.json`` names) and says what it sees, in the
feed, on the dashboard and in doctor:

* not plugged in, or no power (nothing on USB);
* the enclosure on USB but no drive behind it, or a drive reporting 0 bytes (too little power: one soft reset of
  that USB device every ``RESET_EVERY`` seconds, like a replug);
* plugged in at USB 2 speed (what made it freeze before: not trusted unless ``storage.sd_return_allow_usb2``);
* switched off by ``sd-card-mode.sh on`` (its USB ``authorized`` switch is 0): it is switched on to test it, at
  most once per backoff step (1 h, 3 h, 6 h, 12 h, then daily), and off again if no drive shows up;
* plugged in at USB 3: **checked** every minute. Each check reads 16 MiB from the start, the middle and the end of
  the partition, past the page cache, in a child process with a time limit (a frozen drive never blocks the
  watch), and the drive's own I/O error counter must not grow. A replug (another USB connection) starts over.

Once it has been healthy for ``storage.sd_return_stable_min`` minutes (10), it moves the brain back, exactly as
``sd-card-mode.sh off`` does: it stops the agent and the dashboard, sets SD-card mode's drop-in aside, and has the
drive helper mount the SSD (``polymath-volume@``, which binds the brain and starts everything). The agent starts on
its real brain and moves what it learned on the SD card onto it by itself (``core.standby.fold_job``). If the SSD is
not mounted within ``MOUNT_WAIT_S``, the drop-in comes back, everything starts on the SD card again, and the watch
waits (backoff) before it tries again. A failed check waits the same way. A replug starts over at once.

**After an automatic return** the SSD is on probation for a day. If the drive helper has to bring it back
``PROBATION_FAILS`` times in that day (a dead or frozen mount, a USB reset or power cycle), the watch goes back to
SD-card mode by itself: the drop-in, the SSD's mounts dropped (lazily: never waits on it), its USB device switched off,
everything started again on the SD card. The SSD's brain is never touched; what is learned on the SD card meanwhile
moves onto it when it comes back. It is tested again a day later.

It never formats, never deletes a brain, and never touches a drive that is not the brain's. Its record is
``/var/lib/polymath/ssd-watch.json``, copied to ``/run/polymath/ssd-watch.json`` for everyone to read.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from polymath.body.heal import usb_device_dir
from polymath.core import fsprobe
from polymath.core.config import HOME_FILE, MOVED_FILE, Config
from polymath.core.logging import get_logger
from polymath.core.safefs import safe_write

log = get_logger("ssdwatch")

STATE = Path("/var/lib/polymath/ssd-watch.json")
PUBLIC = "ssd-watch.json"  # in the runtime directory (/run/polymath), readable by everyone
SD_STATE = Path("/var/lib/polymath/sd-card-mode.json")  # what sd-card-mode.sh (or the watch) switched off
PROBE_PID = Path("/run/polymath-ssd-probe.pid")
SERVICES = ("polymath.service", "polymath-dashboard.service")
HOLD_S = 3600.0  # a new stretch of SD-card mode: nothing is tried in its first hour (unless the SSD is replugged)
WAKE_AFTER = (3600.0, 3 * 3600.0, 6 * 3600.0, 12 * 3600.0, 86400.0)  # switching a switched-off SSD on to test it
RETRY_AFTER = (1800.0, 3600.0, 3 * 3600.0, 6 * 3600.0, 12 * 3600.0, 86400.0)  # after a failed check or return
WAKE_WAIT_S = 180.0  # after switching it on: this long for the drive to show up
RESET_EVERY = 600.0  # a drive reporting 0 bytes is soft-reset at most this often
MIN_PROBES = 3  # at least this many good checks, however short the stable time
PROBE_MB = 16  # read at each of three places
PROBE_TIMEOUT_S = 30.0
MIN_MBPS = 2.0  # slower than this is not healthy (a healthy USB 3 SSD reads over 100 MB/s)
STOP_WAIT_S = 60.0
MOUNT_WAIT_S = 180.0
PROBATION_S = 86400.0
PROBATION_FAILS = 2
FAIL_ACTIONS = {"dead_mount", "usb_reset", "usb_power"}  # what the drive helper records when it brings it back
KEEP_EVENTS = 30
INTERNAL = ("loop", "ram", "zram", "dm-", "md", "sr", "mmcblk", "nbd")

# Read the start, the middle and the end of a device past the page cache (O_DIRECT, page-aligned buffer):
# prints the bytes read; exit 3 with the error on failure
_CHILD = (
    "import mmap,os,sys\n"
    "p,mb=sys.argv[1],int(sys.argv[2])\n"
    "try:\n"
    "    try: fd=os.open(p,os.O_RDONLY|getattr(os,'O_DIRECT',0))\n"
    "    except OSError as e:\n"
    "        if e.errno!=22: raise\n"
    "        fd=os.open(p,os.O_RDONLY)\n"
    "    try:\n"
    "        size=os.lseek(fd,0,os.SEEK_END)\n"
    "        chunk=mb<<20\n"
    "        buf=mmap.mmap(-1,1<<20)\n"
    "        total=0\n"
    "        for start in sorted({0,max(0,size//2)>>20<<20,max(0,size-chunk)>>20<<20}):\n"
    "            off=start\n"
    "            while off<min(size,start+chunk):\n"
    "                n=os.preadv(fd,[buf],off)\n"
    "                if n<=0: break\n"
    "                total+=n; off+=n\n"
    "    finally:\n"
    "        os.close(fd)\n"
    "    print(total)\n"
    "except OSError as e:\n"
    "    print(e.strerror or e); sys.exit(3)\n"
)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_check(dev: str, *, timeout: float = PROBE_TIMEOUT_S, pidfile: Path = PROBE_PID) -> tuple[bool, float, str]:
    """(healthy, MB/s, why not) for reading ``dev``. In a child process: one stuck in the kernel on a frozen drive
    is left behind (it ends once the drive is reset or unplugged), and while it is stuck no second one is started."""
    with contextlib.suppress(OSError, ValueError):
        old = int(pidfile.read_text().strip())
        if _alive(old):
            return False, 0.0, "the last read check is still stuck on the drive (it froze)"
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(
            [sys.executable, "-I", "-c", _CHILD, dev, str(PROBE_MB)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError as exc:
        return False, 0.0, f"could not start the read check: {exc}"
    with contextlib.suppress(OSError):
        pidfile.write_text(str(proc.pid))
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()  # never waited for: a task stuck in the kernel cannot be killed
        return False, 0.0, f"it did not answer within {timeout:.0f} s (frozen)"
    with contextlib.suppress(OSError):
        pidfile.unlink()
    secs = max(time.monotonic() - t0, 1e-3)
    text = (out or "").strip()
    if proc.returncode != 0:
        return False, 0.0, f"reading it failed: {text or 'error ' + str(proc.returncode)}"
    try:
        total = int(text)
    except ValueError:
        return False, 0.0, f"the read check said {text!r}"
    mbps = total / 1e6 / secs
    if total < 3 * (1 << 20):
        return False, mbps, f"only {total} bytes could be read"
    if mbps < MIN_MBPS:
        return False, mbps, f"it reads only {mbps:.1f} MB/s"
    return True, mbps, ""


def run_cmd(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, timeout=120, check=False)


def _clock_str(t: float) -> str:
    """A time of day, with the weekday when it is more than 20 hours away."""
    return time.strftime("%H:%M" if abs(t - time.time()) < 20 * 3600 else "%a %H:%M", time.localtime(t))


def _minutes(s: float) -> str:
    return f"{s / 60:.0f} min" if s >= 60 else f"{s:.0f} s"


@dataclass
class WatchState:
    episode: str = ""  # which stretch of SD-card mode (the drop-in's time): a new one starts fresh
    state: str = ""  # what it saw last (see SHORT)
    detail: str = ""
    fix: str = ""
    since: float = 0.0  # when this state began
    checked_at: float = 0.0
    streak_since: float = 0.0  # healthy since (0: not now)
    streak_key: str = ""  # the connection being checked (disk and USB address): a replug starts over
    streak_errors: int = -1  # the disk's I/O error count when the streak began
    probes_ok: int = 0
    mbps: float = 0.0
    tries: int = 0  # failed checks and returns in a row (the backoff step)
    next_try: float = 0.0  # nothing is tried before this
    wakes: int = 0  # times it switched a switched-off SSD on to test it, this stretch
    woke_at: float = 0.0
    woke_dev: str = ""
    returned_at: float = 0.0  # the last automatic return (the day of probation starts here)
    last_reset: float = 0.0
    events: list[dict[str, Any]] = field(default_factory=list)  # newest last: {at, event, detail}


SHORT = {
    "absent": "SSD not plugged in",
    "no_power": "SSD plugged in but not answering: it needs more power",
    "enclosure_only": "SSD enclosure on USB but no drive: replug it, or more power",
    "usb2": "SSD on USB 2: move it to a blue USB 3 port",
    "other_drive": "a drive is plugged in, but not the brain's SSD",
    "switched_off": "SSD switched off; tested again later",
    "waking": "switched the SSD on to test it",
    "checking": "SSD back: checking it",
    "unhealthy": "SSD failed a check; tried again later",
    "waiting": "SSD plugged in; tried again later",
    "returning": "moving back onto the SSD",
    "returned": "back on the SSD",
    "return_failed": "moving back onto the SSD failed; tried again later",
    "probation": "back on the SSD (watched for a day)",
    "fallback": "the SSD failed again: back on the SD card",
    "home": "on the SSD",
    "no_ssd": "the brain never lived on a drive",
    "manual": "SD-card mode set in polymath.toml",
    "off": "automatic return switched off",
}


class SSDWatch:
    def __init__(
        self,
        config: Config,
        home: Config,
        run: Callable[[list[str]], Any] = run_cmd,
        *,
        dropin: Path,
        mounts_at: Callable[[Path], list[tuple[str, str]]],
        heal_actions: Callable[[], list[dict[str, Any]]] = lambda: [],
        probe: Callable[[str], tuple[bool, float, str]] = read_check,
        sys_root: Path = Path("/sys"),
        state_path: Path = STATE,
        sd_state: Path = SD_STATE,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config  # as it runs now (SD-card mode or not)
        self.home = home  # as it is without SD-card mode: where the brain lives on the SSD
        self.run = run
        self.dropin = dropin
        self.mounts_at = mounts_at
        self.heal_actions = heal_actions
        self.probe = probe
        self.sys_root = sys_root
        self.state_path = state_path
        self.sd_state = sd_state
        self.clock = clock
        self.sleep = sleep

    # ---------------------------------------------------------------- record
    def load(self) -> WatchState:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return WatchState()
        known = WatchState.__dataclass_fields__
        return WatchState(**{k: v for k, v in raw.items() if k in known}) if isinstance(raw, dict) else WatchState()

    def save(self, st: WatchState, *, persist: bool = True) -> None:
        """The public copy every time (``/run`` is in memory); the record on the SD card only when ``persist``."""
        record = asdict(st) | {"short": SHORT.get(st.state, st.state)}
        payload = json.dumps(record, indent=1)
        for target in (self.state_path, Path(self.config.paths.run_dir) / PUBLIC):
            if target == self.state_path and not persist:
                continue
            if target != self.state_path and not target.parent.is_dir():
                continue
            with contextlib.suppress(OSError):
                target.parent.mkdir(parents=True, exist_ok=True)
                safe_write(target.parent, target.name, payload, mode=0o644)

    def _set(self, st: WatchState, state: str, detail: str, fix: str = "") -> dict[str, Any]:
        now = self.clock()
        if state != st.state:
            st.since = now
            log.info("ssd watch", extra={"state": state, "detail": detail})
        st.state, st.detail, st.fix, st.checked_at = state, detail, fix, now
        return {"ssd": state, "detail": detail}

    def _event(self, st: WatchState, event: str, detail: str) -> None:
        entry = {"at": self.clock(), "event": event, "detail": detail}
        st.events = [*st.events, entry][-KEEP_EVENTS:]
        log.warning("ssd watch", extra=entry)

    def _reset_streak(self, st: WatchState) -> None:
        st.streak_since, st.probes_ok, st.streak_key, st.streak_errors = 0.0, 0, "", -1

    def _backoff(self, st: WatchState, why: str, event: str = "check_failed") -> None:
        st.tries += 1
        st.next_try = self.clock() + RETRY_AFTER[min(st.tries, len(RETRY_AFTER)) - 1]
        self._reset_streak(st)
        self._event(st, event, f"{why}; tried again at {_clock_str(st.next_try)}")

    # ---------------------------------------------------------------- facts
    def _episode(self) -> str:
        try:
            return str(int(self.dropin.stat().st_mtime))
        except OSError:
            return ""

    def target(self) -> str:
        """The brain's SSD: the filesystem id the SD card's own ``MOVED-TO-DRIVE.json`` names ("" when none)."""
        marker = Path(self.home.paths.data_dir) / MOVED_FILE
        if fsprobe.probe(marker, read=True, timeout=8.0) != fsprobe.OK:
            return ""
        try:
            raw = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        return str(raw.get("id") or "") if isinstance(raw, dict) else ""

    def devices(self) -> list[dict[str, Any]]:
        """Every block device (disks and partitions, flattened); a partition carries its disk's transport."""
        r = self.run(["lsblk", "-J", "-b", "-o", "NAME,PATH,TYPE,SIZE,FSTYPE,UUID,TRAN,PKNAME,MOUNTPOINTS"])
        if getattr(r, "returncode", 1) != 0:
            return []
        try:
            tree = json.loads(r.stdout).get("blockdevices") or []
        except (ValueError, AttributeError):
            return []
        out: list[dict[str, Any]] = []

        def walk(nodes: list[dict[str, Any]], parent: dict[str, Any] | None) -> None:
            for n in nodes:
                d = dict(n)
                d["tran"] = d.get("tran") or (parent or {}).get("tran")
                d["pkname"] = d.get("pkname") or (parent or {}).get("name")
                d["mountpoints"] = [m for m in d.get("mountpoints") or [] if m]
                if not str(d.get("name") or "").startswith(INTERNAL):
                    out.append(d)
                walk(list(n.get("children") or []), d)

        walk(list(tree), None)
        return out

    def _read(self, path: Path) -> str:
        with contextlib.suppress(OSError):
            return path.read_text().strip()
        return ""

    def known_usb(self) -> list[Path]:
        """USB devices with the vendor:product SD-card mode recorded for the SSD."""
        want = self._sd_record().get("id") or ""
        if not want:
            return []
        out = []
        with contextlib.suppress(OSError):
            for d in sorted((self.sys_root / "bus" / "usb" / "devices").iterdir()):
                if (
                    d / "authorized"
                ).exists() and f"{self._read(d / 'idVendor')}:{self._read(d / 'idProduct')}" == want:
                    out.append(d)
        return out

    def _sd_record(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.sd_state.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _usb_of(self, disk: str) -> Path | None:
        return usb_device_dir(self.sys_root / "block" / disk)

    def _io_errors(self, disk: str) -> int:
        raw = self._read(self.sys_root / "block" / disk / "device" / "ioerr_cnt")
        try:
            return int(raw, 16) if raw else -1
        except ValueError:
            return -1

    def _authorize(self, usb: Path, on: bool) -> bool:
        try:
            (usb / "authorized").write_text("1" if on else "0")
        except OSError as exc:
            log.warning("ssd watch: usb switch failed", extra={"usb": str(usb), "error": str(exc)})
            return False
        return True

    def _brain_mounted(self) -> bool:
        data = Path(self.home.paths.data_dir)
        return bool(self.mounts_at(data)) and fsprobe.probe(data / HOME_FILE, read=True, timeout=8.0) == fsprobe.OK

    # ---------------------------------------------------------------- the check
    def tick(self) -> dict[str, Any]:
        st = self.load()
        before = {k: v for k, v in asdict(st).items() if k != "checked_at"}
        try:
            interrupted = self._interrupted(st)
            if interrupted is not None:
                out = interrupted
            else:
                out = self._in_sd_mode(st) if self.config.storage.sd_only else self._on_ssd(st)
        finally:  # the SD card is written only when something changed (a steady state costs nothing)
            self.save(st, persist={k: v for k, v in asdict(st).items() if k != "checked_at"} != before)
        return out

    def _interrupted(self, st: WatchState) -> dict[str, Any] | None:
        """A move cut short (power cut, the helper's time limit): the drop-in is still set aside. Finish it if the
        brain is mounted, else put SD-card mode back; either way everything is started again. None when not."""
        aside = self.dropin.with_name(self.dropin.name + ".off")
        if not aside.exists() or self.dropin.exists():
            return None
        if self._brain_mounted():
            with contextlib.suppress(OSError):
                aside.unlink()
            with contextlib.suppress(OSError):
                self.sd_state.unlink()
            self._start_services()
            st.returned_at, st.tries, st.next_try, st.episode = self.clock(), 0, 0.0, ""
            self._reset_streak(st)
            self._event(st, "returned", "the move back onto the SSD was cut short but had finished: the brain is on it")
            return self._set(st, "returned", "the brain is back on the SSD (the move was cut short, then finished)")
        with contextlib.suppress(OSError):
            os.replace(aside, self.dropin)
        st.episode = self._episode()
        self._drop_ssd_mounts(None)
        self._start_services()
        self._backoff(st, "the move back onto the SSD was cut short: it stays on the SD card", "return_failed")
        return self._set(st, "return_failed", "the move back onto the SSD was cut short, so the brain stays on the SD "
                         f"card; it tries again at {_clock_str(st.next_try)}")  # fmt: skip

    def _in_sd_mode(self, st: WatchState) -> dict[str, Any]:
        now = self.clock()
        if not self.config.storage.sd_auto_return:
            return self._set(
                st,
                "off",
                "automatic return to the SSD is switched off ([storage] sd_auto_return)",
                "to go back: cd ~/polymath-src/polymath && sudo ./scripts/sd-card-mode.sh off",
            )
        if not self.dropin.exists():
            return self._set(
                st,
                "manual",
                f"SD-card mode is set in polymath.toml, not by its switch ({self.dropin})",
                "to go back, set sd_only = false there (or use scripts/sd-card-mode.sh)",
            )
        episode = self._episode()
        if episode != st.episode:  # a new stretch of SD-card mode: start fresh, nothing tried in its first hour
            st.episode, st.tries, st.wakes, st.woke_at, st.returned_at = episode, 0, 0, 0.0, 0.0
            st.next_try = max(st.next_try if st.state == "fallback" else 0.0, float(episode or now) + HOLD_S)
            self._reset_streak(st)
        target = self.target()
        if not target:
            return self._set(st, "no_ssd", "the brain has never lived on a drive, so there is no SSD to go back to")
        devs = self.devices()

        def vol(d: dict[str, Any]) -> str:
            return re.sub(r"[^A-Za-z0-9-]", "", str(d.get("uuid") or ""))

        part = next((d for d in devs if d.get("type") == "part" and vol(d) == target), None)
        if part is None:
            part = next((d for d in devs if vol(d) == target), None)
        if part is None:
            return self._absent(st, devs, now)
        return self._present(st, part, devs, now)

    def _absent(self, st: WatchState, devs: list[dict[str, Any]], now: float) -> dict[str, Any]:
        was_present = st.state in {"checking", "waiting", "unhealthy", "usb2"}
        self._reset_streak(st)
        known = self.known_usb()
        if st.woke_at:
            if now - st.woke_at < WAKE_WAIT_S:
                return self._set(st, "waking", "switched the SSD's USB device on to test it; waiting for the drive")
            if st.woke_dev:
                self._authorize(Path(st.woke_dev), False)
            st.woke_at, st.woke_dev = 0.0, ""
            st.next_try = now + WAKE_AFTER[min(st.wakes, len(WAKE_AFTER)) - 1]
            self._event(
                st,
                "wake_failed",
                "switched the SSD on to test it, but no drive showed up within "
                f"{WAKE_WAIT_S / 60:.0f} min: switched it off again; tested again at {_clock_str(st.next_try)}",
            )
        off = [d for d in known if self._read(d / "authorized") == "0"]
        if off:
            if now >= st.next_try and self._authorize(off[0], True):
                st.wakes += 1
                st.woke_at, st.woke_dev = now, str(off[0])
                self._event(st, "woke", "switched the SSD's USB device on to test it")
                return self._set(st, "waking", "switched the SSD's USB device on to test it; waiting for the drive")
            return self._set(
                st,
                "switched_off",
                "the SSD's USB device is switched off (SD-card mode); Polymath "
                f"switches it on to test it at {_clock_str(st.next_try)}",
                "nothing to do (or unplug it and plug it into a blue USB 3 port to test it now)",
            )
        usb_disks = [d for d in devs if d.get("type") == "disk" and d.get("tran") == "usb"]
        zero = [d for d in usb_disks if not int(d.get("size") or 0)]
        if zero:
            disk = str(zero[0].get("name"))
            usb = self._usb_of(disk)
            if usb is not None and now - st.last_reset >= RESET_EVERY:
                st.last_reset = now
                if self._authorize(usb, False):
                    self.sleep(2.0)
                    self._authorize(usb, True)
                    self._event(st, "usb_reset", f"/dev/{disk} reported 0 bytes: reset its USB device (a replug)")
            return self._set(
                st,
                "no_power",
                f"the SSD's enclosure is connected (/dev/{disk}), but the drive inside does "
                "not answer: it reports 0 bytes",
                "it needs more power: a powered USB 3 hub, or the enclosure's own power supply; then plug it in again",
            )
        if [d for d in known if self._read(d / "authorized") == "1"]:
            return self._set(
                st,
                "enclosure_only",
                "the SSD's enclosure is on USB, but no drive shows up behind it",
                "unplug it for 10 s and plug it into a blue USB 3 port; if it stays like this, it needs "
                "more power (a powered USB 3 hub)",
            )
        others = [d for d in usb_disks if int(d.get("size") or 0)]
        if others:
            return self._set(
                st,
                "other_drive",
                f"a drive is plugged in (/dev/{others[0].get('name')}), but it does not hold Polymath's brain",
                "plug in the SSD that holds the brain",
            )
        if was_present:
            self._event(st, "gone", "the SSD went away while it was being checked")
        return self._set(
            st,
            "absent",
            "the SSD is not plugged in, or gets no power (it does not show up on USB)",
            "plug it into a blue USB 3 port; if it does not show up, give it a powered USB 3 hub or its own power",
        )

    def _present(self, st: WatchState, part: dict[str, Any], devs: list[dict[str, Any]], now: float) -> dict[str, Any]:
        disk = str(part.get("pkname") or part.get("name"))
        disk_info = next((d for d in devs if d.get("name") == disk), part)
        usb = self._usb_of(disk)
        was_away = st.state in {"absent", "no_power", "enclosure_only", "other_drive", "switched_off", ""}
        if st.woke_at:
            st.woke_at, st.woke_dev = 0.0, ""
        if not int(disk_info.get("size") or 0):
            self._reset_streak(st)
            return self._set(
                st,
                "no_power",
                f"the SSD (/dev/{disk}) reports 0 bytes: the drive inside does not answer",
                "it needs more power: a powered USB 3 hub or its own power supply",
            )
        speed = self._read(usb / "speed") if usb is not None else ""
        key = f"{disk}|{self._read(usb / 'busnum') if usb else ''}/{self._read(usb / 'devnum') if usb else ''}"
        if key != st.streak_key:
            if (st.streak_key or was_away) and st.state not in {"", "waking"}:
                st.next_try, st.tries = 0.0, 0  # plugged in again by a person: try at once
            self._reset_streak(st)
            st.streak_key = key
            st.streak_errors = self._io_errors(disk)
        try:
            usb2 = bool(speed) and float(speed) < 5000
        except ValueError:
            usb2 = False
        if usb2 and not self.config.storage.sd_return_allow_usb2:
            st.streak_since, st.probes_ok = 0.0, 0
            return self._set(
                st,
                "usb2",
                f"the SSD is plugged in (/dev/{disk}), but at USB 2 speed ({speed} Mbit/s), "
                "which is what made it freeze before: Polymath stays on the SD card",
                "move it to a blue USB 3 port, with a USB 3 cable and no USB 2 hub",
            )
        if now < st.next_try:
            return self._set(
                st,
                "waiting",
                f"the SSD is plugged in (/dev/{disk}); it is checked from {_clock_str(st.next_try)}",
                "nothing to do (or plug it out and in again to check now)",
            )
        ok, mbps, why = self.probe(str(part.get("path") or f"/dev/{part.get('name')}"))
        errors = self._io_errors(disk)
        if ok and st.streak_errors >= 0 and errors > st.streak_errors:
            ok, why = False, f"the drive reported {errors - st.streak_errors} I/O error(s)"
        if not ok:
            self._backoff(st, f"the SSD failed a read check: {why}")
            return self._set(
                st,
                "unhealthy",
                f"the SSD is plugged in (/dev/{disk}) but failed a read check: {why}; "
                f"checked again at {_clock_str(st.next_try)}",
                "if it keeps failing: a powered USB 3 hub or its own power, and another cable",
            )
        st.probes_ok += 1
        st.mbps = round(mbps, 1)
        st.streak_since = st.streak_since or now
        healthy = now - st.streak_since
        need = self.config.storage.sd_return_stable_min * 60
        if healthy < need or st.probes_ok < MIN_PROBES:
            return self._set(
                st,
                "checking",
                f"the SSD is back (/dev/{disk}, USB {speed or '?'} Mbit/s, {mbps:.0f} MB/s) "
                f"and healthy for {_minutes(healthy)} of {_minutes(need)}: then the brain moves back "
                "onto it by itself",
            )
        return self._return_home(st, part, healthy)

    # ---------------------------------------------------------------- moving
    def _stop_services(self) -> None:
        self.run(["systemctl", "stop", "--no-block", *SERVICES])
        waited = 0.0
        while waited < STOP_WAIT_S:
            r = self.run(["systemctl", "is-active", SERVICES[0]])
            if str(getattr(r, "stdout", "")).strip() not in {"active", "deactivating", "activating"}:
                break
            self.sleep(2.0)
            waited += 2.0
        self.run(["systemctl", "kill", "--signal=KILL", *SERVICES])

    def _start_services(self) -> None:
        self.run(["systemctl", "reset-failed", *SERVICES])
        self.run(["systemctl", "start", "--no-block", *SERVICES])

    def _return_home(self, st: WatchState, part: dict[str, Any], healthy: float) -> dict[str, Any]:
        name = str(part.get("name"))
        self._event(
            st, "returning", f"the SSD (/dev/{name}) was healthy for {_minutes(healthy)}: moving the brain back onto it"
        )
        self._set(st, "returning", "moving the brain back onto the SSD")
        self.save(st)
        aside = self.dropin.with_name(self.dropin.name + ".off")
        self._stop_services()
        try:
            os.replace(self.dropin, aside)
        except OSError as exc:
            self._start_services()
            self._backoff(st, f"could not switch SD-card mode off ({exc})", "return_failed")
            return self._set(st, "return_failed", f"could not switch SD-card mode off: {exc}")
        # every drive's unit again, not only the SSD's: in SD-card mode each drive plugged in was left alone (its unit
        # ran and stays "active"), so the extra drives plugged in meanwhile are adopted now too, not at the next replug
        self.run(["systemctl", "reset-failed", "polymath-volume@*.service"])
        self.run(["systemctl", "restart", "--no-block", "polymath-volume@*.service"])
        self.run(["udevadm", "trigger", "--action=add", "--subsystem-match=block"])
        waited = 0.0
        while not self._brain_mounted() and waited < MOUNT_WAIT_S:
            self.sleep(2.0)
            waited += 2.0
        if self._brain_mounted():
            with contextlib.suppress(OSError):
                aside.unlink()
            with contextlib.suppress(OSError):
                self.sd_state.unlink()
            self._start_services()  # the drive helper has started them already: this only makes sure
            st.returned_at, st.tries, st.next_try, st.episode = self.clock(), 0, 0.0, ""
            self._reset_streak(st)
            data = self.home.paths.data_dir
            self._event(
                st,
                "returned",
                f"the brain is back on the SSD ({data}, /dev/{name}); what it learned on the "
                "SD card moves onto it by itself",
            )
            return self._set(
                st,
                "returned",
                f"the brain is back on the SSD (/dev/{name}); what it learned on the SD card moves onto it by itself",
            )
        # not mounted in time: everything back as it was, on the SD card
        with contextlib.suppress(OSError):
            os.replace(aside, self.dropin)
        st.episode = self._episode()
        self._drop_ssd_mounts(part)
        self._start_services()
        self._backoff(
            st, f"the SSD was not mounted within {MOUNT_WAIT_S / 60:.0f} min: it stays on the SD card", "return_failed"
        )
        return self._set(
            st,
            "return_failed",
            "moving the brain back onto the SSD did not work (it was not mounted "
            f"within {MOUNT_WAIT_S / 60:.0f} min); it stays on the SD card and tries again at "
            f"{_clock_str(st.next_try)}",
            "details: journalctl -u 'polymath-volume@*' -n 30",
        )

    def _drop_ssd_mounts(self, part: dict[str, Any] | None) -> None:
        """Release the brain's mount and the SSD's own mount, lazily (never waits on a frozen drive)."""
        data = Path(self.home.paths.data_dir)
        for _mp, _src in self.mounts_at(data):
            self.run(["umount", "-l", str(data)])
        root = Path(self.home.storage.mount_root)
        with contextlib.suppress(OSError):
            for mnt in sorted(root.iterdir()):
                if self.mounts_at(mnt):
                    self.run(["umount", "-l", str(mnt)])
        if part is not None:
            self.run(["systemctl", "stop", "--no-block", f"polymath-volume@{part.get('name')}.service"])

    # ---------------------------------------------------------------- after a return
    def _on_ssd(self, st: WatchState) -> dict[str, Any]:
        now = self.clock()
        if not st.returned_at or now - st.returned_at > PROBATION_S:
            if st.returned_at:
                self._event(st, "trusted", "the SSD stayed fine for a day after coming back")
                st.returned_at = 0.0
            return self._set(st, "home", "the brain lives on its SSD")
        fails = [
            a
            for a in self.heal_actions()
            if float(a.get("at") or 0) > st.returned_at and a.get("action") in FAIL_ACTIONS
        ]
        if len(fails) < PROBATION_FAILS or not self.config.storage.sd_auto_fallback:
            return self._set(
                st,
                "probation",
                f"back on the SSD since {_clock_str(st.returned_at)}; watched for a day "
                f"({len(fails)} drop-out(s) so far)",
            )
        return self._fall_back(st, fails)

    def _fall_back(self, st: WatchState, fails: list[dict[str, Any]]) -> dict[str, Any]:
        """The SSD failed again soon after coming back: SD-card mode, by itself (what sd-card-mode.sh on does to the
        drive; the SSD's brain is not touched)."""
        now = self.clock()
        self.save(st)
        self._stop_services()
        self.dropin.parent.mkdir(parents=True, exist_ok=True)
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(now))
        safe_write(
            self.dropin.parent,
            self.dropin.name,
            f"# SD-card mode, switched on by the SSD watch on {when}: the SSD dropped out {len(fails)} times\n"
            "# within a day of coming back. Polymath goes back onto the SSD by itself once it is healthy again\n"
            "# (body/ssdwatch.py); by hand: cd ~/polymath-src/polymath && sudo ./scripts/sd-card-mode.sh off\n"
            "[storage]\nsd_only = true\n",
            mode=0o644,
        )
        target = self.target()
        devs = self.devices()
        part = next((d for d in devs if re.sub(r"[^A-Za-z0-9-]", "", str(d.get("uuid") or "")) == target), None)
        self._drop_ssd_mounts(part)
        disk = str((part or {}).get("pkname") or (part or {}).get("name") or "")
        usb = self._usb_of(disk) if disk else None
        if usb is not None and self._authorize(usb, False):
            ident = f"{self._read(usb / 'idVendor')}:{self._read(usb / 'idProduct')}"
            with contextlib.suppress(OSError):
                self.sd_state.parent.mkdir(parents=True, exist_ok=True)
                self.sd_state.write_text(
                    json.dumps({"usb": str(usb), "id": ident, "disk": disk, "at": int(now), "by": "ssd-watch"}) + "\n"
                )
        self._start_services()
        st.episode = self._episode()
        st.returned_at, st.tries, st.wakes = 0.0, len(RETRY_AFTER) - 1, 0
        st.next_try = now + 86400.0
        self._reset_streak(st)
        self._event(
            st,
            "fallback",
            f"the SSD dropped out {len(fails)} times within a day of coming back: the brain runs "
            "on the SD card again (SD-card mode); the SSD's brain is untouched; it is tested again "
            f"{_clock_str(st.next_try)}",
        )
        return self._set(
            st,
            "fallback",
            f"the SSD dropped out {len(fails)} times within a day of coming back, so the "
            "brain runs on the SD card again; the SSD's brain is untouched and is tried again in a day",
            "for good: a powered USB 3 hub or the enclosure's own power, a USB 3 cable, a blue port",
        )


def read_public(config: Config) -> dict[str, Any] | None:
    """The watch's record as everyone may read it (``/run/polymath/ssd-watch.json``), or None."""
    with contextlib.suppress(OSError, ValueError):
        raw = json.loads((Path(config.paths.run_dir) / PUBLIC).read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else None
    return None
