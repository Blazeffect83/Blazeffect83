"""Drive roles: Polymath decides by itself which drive holds the brain, which keeps the backups, and which are extra.

Runs as root every minute with the drive helper (``polymath storage heal``), after it, whenever the brain is not in
SD-card mode (there the SSD watch, ``body.ssdwatch``, decides). The Pi 4 has two USB 3 ports (blue) and two USB 2
ports (black); any mix of SSDs, hard disks and sticks may be plugged in.

**What it knows about each adopted drive:** its filesystem and whether it is Polymath's own (dedicated) or shares
your files; how it is connected (NVMe, USB 3 at 5 or 10 Gbit/s, USB 2) and whether it spins (``queue/rotational``);
its size and free space; how fast it really reads (16 MiB at its start, middle and end, past the cache, once a day:
``ssdwatch.read_check``); how long it has been plugged in; and how often it dropped out this week (it went away while
it was in use, or the drive helper had to bring it back).

**The brain** goes on the best *eligible* drive: Linux-formatted, dedicated, on USB 3 or NVMe (USB 2 only with
``storage.brain_allow_usb2``), room for three times the brain and ``storage.home_min_gb``, plugged in for
``storage.brain_settle_min`` minutes, and fewer than ``FLAKY_DROPS`` drop-outs this week. The score: the connection
(NVMe 6, 10 Gbit/s 5, 5 Gbit/s 4), +2 for an SSD, up to +2 for the measured speed, up to +1 for room, −1 per
drop-out. Never a drive with your files on it, and never a Windows/macOS drive (a fresh shop drive is formatted for
Linux once by the drive helper first; one with files never is).

* **Off the SD card:** once the best drive has settled, the brain moves onto it (``Helper.move_home``: copied,
  checked, then switched; the SD card keeps its copy). Waiting for every drive plugged in at boot to settle is what
  lets it pick the best one rather than the first one.
* **Onto a better drive:** when an eligible drive scores ``MOVE_MARGIN`` more than the brain's drive (a class
  better: a hard disk → an SSD, USB 3 → NVMe), the brain moves at night, in the three hours after the nightly
  backup (from ``body.backup_hour`` + 1; ``Helper.move_brain``: the agent stops for the copy). At most once a day.
* **Off a failing drive:** when the brain's drive dropped out ``FLAKY_DROPS`` times this week and a healthy
  eligible drive is there, the brain moves at once, whatever the hour.
* The old drive keeps its copy as ``polymath-brain/home-previous-<time>`` and becomes extra room. The copy is deleted
  after ``PREVIOUS_KEEP_S`` (a week) if the new brain drive has not dropped out since.

**The backups** go on another physical drive than the brain's: the steadiest one with the most room for them (a
hard disk or a USB 2 drive is fine). Without one they stay on the brain's drive, and it says so. The agent reads the
choice (``memory.pool.backup_dir``). **Every other drive is extra room** for document bodies and downloads.

It also warns when the drives on the Pi's own ports ask for more power than the Pi 4 gives them (``USB_POWER_MA``):
the usual reason a bus-powered SSD reports 0 bytes or drops out.

Its record is ``/var/lib/polymath/drive-roles.json``, copied to ``/run/polymath/drive-roles.json`` for everyone:
``polymath storage roles``, ``polymath doctor``, the dashboard, and the caretaker's inbox show it.
"""

from __future__ import annotations

import contextlib
import json
import math
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from polymath.body.heal import usb_device_dir
from polymath.body.ssdwatch import read_check
from polymath.core.config import MOVED_FILE, Config
from polymath.core.logging import get_logger
from polymath.core.safefs import safe_write
from polymath.memory.pool import StoragePool, Volume, read_manifest

log = get_logger("roles")

STATE = Path("/var/lib/polymath/drive-roles.json")
PUBLIC = "drive-roles.json"  # in the runtime directory (/run/polymath)
POSIX = {"ext4", "ext3", "ext2", "btrfs", "xfs", "f2fs"}
FOREIGN = {"vfat", "exfat", "ntfs"}  # Windows/macOS: never the brain; reformatted when empty (drive care)
FLAKY_WINDOW_S = 7 * 86400.0
FLAKY_DROPS = 3  # drop-outs in a week: not trusted with the brain
MOVE_MARGIN = 2.0  # a better drive must score this much more (a class better) before the brain moves
LARGER_BY = 1.25  # with brain_prefer = "largest": a drive must be this much larger before the brain moves onto it
MOVE_EVERY_S = 86400.0  # at most one move between drives a day (a failing brain drive excepted)
RETRY_MOVE_S = 6 * 3600.0  # after a move that failed
MEASURE_EVERY_S = 86400.0  # each drive's read speed, once a day
NIGHT_HOURS = 3  # moves between drives happen in the hours after body.backup_hour
PREVIOUS_KEEP_S = 7 * 86400.0
USB_POWER_MA = 1200  # what a Pi 4 gives all its USB ports together
FAIL_ACTIONS = {"dead_mount", "usb_reset", "usb_power"}
KEEP_EVENTS = 40


@dataclass
class Drive:
    """One adopted, mounted drive and what Polymath knows about it."""

    id: str
    name: str
    brain: Path  # its polymath-brain folder
    fstype: str
    dedicated: bool
    home: bool
    device: str = ""
    disk: str = ""
    tran: str = ""  # usb | nvme | sata | ""
    speed: int = 0  # Mbit/s of its USB link (0: not USB, or unknown)
    rotational: bool | None = None
    size: int = 0
    free: int = 0
    room: int = 0  # what Polymath may still put there (its budget and the free space)
    power_ma: int = 0
    usb: str = ""  # the USB device in sysfs (several partitions can share one)
    mbps: float = 0.0  # measured read speed (0: not measured yet)
    drops: int = 0  # this week
    plugged_s: float = 0.0  # how long it has been plugged in without a break
    score: float = 0.0
    why_not: str = ""  # why it cannot hold the brain ("" when it can)
    role: str = "extra"

    def kind(self) -> str:
        """In words: ``USB 3 SSD``, ``USB 2 hard disk``, ``NVMe SSD``…"""
        link = {"nvme": "NVMe", "sata": "SATA"}.get(self.tran, "")
        if not link:
            link = "USB 3" if self.speed >= 5000 else "USB 2" if self.speed else "USB" if self.tran == "usb" else ""
        what = "SSD" if self.rotational is False else "hard disk" if self.rotational else "drive"
        return f"{link} {what}".strip()


@dataclass
class RolesState:
    seen: dict[str, dict[str, Any]] = field(default_factory=dict)  # id → since, last, drops[], mbps, measured
    brain: str = ""  # where the brain is: a drive id, "sd" or "missing"
    backup: str = ""
    extra: list[str] = field(default_factory=list)
    decision: str = ""
    warnings: list[str] = field(default_factory=list)
    drives: list[dict[str, Any]] = field(default_factory=list)
    last_move: float = 0.0
    next_move: float = 0.0
    brain_bytes: int = 0  # the brain's size (measured at most hourly: it walks the whole data directory)
    brain_bytes_at: float = 0.0
    blocked: str = ""  # which drives cannot take the brain off the SD card, and why (reported when it changes)
    pending: list[str] = field(default_factory=list)  # Windows/macOS drives drive care reformats: no data meanwhile
    events: list[dict[str, Any]] = field(default_factory=list)  # newest last: {at, event, detail}


def disk_facts(run: Callable[[list[str]], Any], sys_root: Path, device: str) -> dict[str, Any]:
    """The physical disk behind a partition: its name, transport, whether it spins, its USB link and power draw."""
    out: dict[str, Any] = {"disk": "", "tran": "", "rotational": None, "speed": 0, "power_ma": 0, "usb": ""}
    if not device:
        return out
    r = run(["lsblk", "-J", "-b", "-d", "-o", "NAME,PKNAME,TRAN,ROTA", device])
    try:
        node = (json.loads(r.stdout).get("blockdevices") or [{}])[0] if getattr(r, "returncode", 1) == 0 else {}
    except (ValueError, AttributeError, IndexError):
        node = {}
    disk = str(node.get("pkname") or node.get("name") or Path(device).name)
    if node.get("pkname"):
        r = run(["lsblk", "-J", "-b", "-d", "-o", "NAME,TRAN,ROTA", f"/dev/{disk}"])
        with contextlib.suppress(ValueError, AttributeError, IndexError):
            node = (json.loads(r.stdout).get("blockdevices") or [{}])[0] if getattr(r, "returncode", 1) == 0 else {}
    out["disk"], out["tran"] = disk, str(node.get("tran") or "")
    rota = node.get("rota")
    out["rotational"] = None if rota is None else str(rota).lower() in {"1", "true"}
    usb = usb_device_dir(sys_root / "block" / disk)
    if usb is not None:
        out["usb"] = str(usb)
        with contextlib.suppress(OSError, ValueError):
            out["speed"] = int(float((usb / "speed").read_text().strip()))
        with contextlib.suppress(OSError, ValueError):
            out["power_ma"] = int(re.sub(r"\D", "", (usb / "bMaxPower").read_text()) or 0)
        out["tran"] = out["tran"] or "usb"
    return out


def brain_score(d: Drive) -> float:
    """How good a home this drive is for the brain (higher is better; eligibility is checked separately)."""
    link = {"nvme": 6.0, "sata": 4.0}.get(d.tran, 0.0)
    if not link:
        link = 5.0 if d.speed >= 10000 else 4.0 if d.speed >= 5000 else 1.0 if d.speed else 2.0
    ssd = 2.0 if d.rotational is False else 0.0
    speed = min(d.mbps / 150.0, 2.0)
    room = min(math.log2(d.free / 32e9 + 1.0) / 4.0, 1.0) if d.free > 0 else 0.0
    return round(link + ssd + speed + room - d.drops, 2)


class DriveRoles:
    def __init__(
        self,
        config: Config,
        helper: Any,
        *,
        pool: StoragePool | None = None,
        facts: Callable[[str], dict[str, Any]] | None = None,
        probe: Callable[[str], tuple[bool, float, str]] = read_check,
        heal_actions: Callable[[], list[dict[str, Any]]] = lambda: [],
        sys_root: Path = Path("/sys"),
        state_path: Path = STATE,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.helper = helper  # body.volumes.Helper: mounts, moves, manifests (and its lock)
        self.pool = pool or StoragePool(config)
        self.facts = facts or (lambda dev: disk_facts(helper.run, sys_root, dev))
        self.probe = probe
        self.heal_actions = heal_actions
        self.state_path = state_path
        self.clock = clock

    # ---------------------------------------------------------------- record
    def load(self) -> RolesState:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return RolesState()
        known = RolesState.__dataclass_fields__
        return RolesState(**{k: v for k, v in raw.items() if k in known}) if isinstance(raw, dict) else RolesState()

    def save(self, st: RolesState, *, persist: bool) -> None:
        payload = json.dumps(asdict(st) | {"checked_at": self.clock()}, indent=1, default=str)
        targets = [Path(self.config.paths.run_dir) / PUBLIC] + ([self.state_path] if persist else [])
        for target in targets:
            if target != self.state_path and not target.parent.is_dir():
                continue
            with contextlib.suppress(OSError):
                target.parent.mkdir(parents=True, exist_ok=True)
                safe_write(target.parent, target.name, payload, mode=0o644)

    def _event(self, st: RolesState, event: str, detail: str) -> None:
        entry = {"at": self.clock(), "event": event, "detail": detail}
        st.events = [*st.events, entry][-KEEP_EVENTS:]
        log.warning("drive roles", extra=entry)

    # ---------------------------------------------------------------- facts
    def drives(self, st: RolesState) -> list[Drive]:
        now = self.clock()
        out: list[Drive] = []
        vols: list[Volume] = self.pool.volumes(fresh=True)
        heal_drops = [
            a
            for a in self.heal_actions()
            if a.get("action") in FAIL_ACTIONS and now - float(a.get("at") or 0) < FLAKY_WINDOW_S
        ]
        for v in vols:
            manifest = read_manifest(v.path) or {}
            device = str(manifest.get("device") or "")
            f = self.facts(device)
            try:
                usage = shutil.disk_usage(v.path)
                size, free = int(v.size_bytes or usage.total), int(usage.free)
            except OSError:
                size, free = v.size_bytes, 0
            seen = st.seen.setdefault(v.id, {"since": now, "drops": []})
            seen["drops"] = [t for t in seen.get("drops") or [] if now - float(t) < FLAKY_WINDOW_S]
            drops = len(seen["drops"]) + (len(heal_drops) if v.home else 0)
            d = Drive(
                id=v.id,
                name=v.name,
                brain=v.path,
                fstype=v.fstype,
                dedicated=v.dedicated,
                home=v.home,
                device=device,
                disk=str(f.get("disk") or ""),
                tran=str(f.get("tran") or ""),
                speed=int(f.get("speed") or 0),
                rotational=f.get("rotational"),
                size=size,
                free=free,
                room=self.pool.room(v) if not v.home else 0,
                power_ma=int(f.get("power_ma") or 0),
                usb=str(f.get("usb") or ""),
                mbps=float(seen.get("mbps") or 0.0),
                drops=drops,
                plugged_s=now - float(seen.get("since") or now),
            )
            out.append(d)
        present = {d.id for d in out}
        for vid, seen in list(st.seen.items()):
            seen["drops"] = [t for t in seen.get("drops") or [] if now - float(t) < FLAKY_WINDOW_S]
            if vid in present:
                seen["last"] = now
            elif seen.get("last") is not None and now - float(seen["last"]) < 180:
                # it went away: a drop-out (or an unplug); it starts its settling time again when it is back
                seen["drops"] = [*(seen.get("drops") or []), now]
                seen["last"] = None
                seen["since"] = None
            elif seen.get("since") is None and not seen.get("drops"):
                del st.seen[vid]
        for d in out:
            if st.seen[d.id].get("since") is None:
                st.seen[d.id]["since"] = now
                d.plugged_s = 0.0
        return out

    def brain_bytes(self, st: RolesState) -> int:
        from polymath.body.volumes import tree_stats

        now = self.clock()
        if now - st.brain_bytes_at >= 3600:
            with contextlib.suppress(OSError):
                st.brain_bytes, st.brain_bytes_at = tree_stats(self.config.paths.data_dir)[1], now
        return st.brain_bytes

    def _better(self, best: Drive, home: Drive) -> str:
        """Why ``best`` is worth moving the brain onto from ``home`` ("" when it is not): a quarter larger, with
        ``brain_prefer = "largest"``; a class better (``MOVE_MARGIN``) with "fastest"."""
        if self.config.storage.brain_prefer == "largest":
            if best.size >= home.size * LARGER_BY:
                return (f"{best.name} is the larger drive ({best.kind()}, {best.size / 1e9:.0f} GB against "
                        f"{home.size / 1e9:.0f} GB)")  # fmt: skip
            if home.size >= best.size * LARGER_BY:
                return ""  # the brain's drive is the larger one
        if best.score - home.score >= MOVE_MARGIN:
            return f"{best.name} is the better drive ({best.kind()}, {best.score:g} against {home.score:g})"
        return ""

    def _words(self, d: Drive) -> str:
        """``T7 (USB 3 SSD, 1000 GB), the largest drive plugged in`` (or ``…, the best drive plugged in``)."""
        if self.config.storage.brain_prefer == "largest":
            return f"{d.name} ({d.kind()}, {d.size / 1e9:.0f} GB), the largest drive plugged in"
        return f"{d.name} ({d.kind()}), the best drive plugged in"

    def reasons(self, d: Drive, need: int) -> list[tuple[str, str]]:
        """Every reason ``d`` cannot hold the brain, as (kind, words); none when it can."""
        st = self.config.storage
        out = []
        if d.fstype not in POSIX:
            out.append(("foreign", f"formatted for Windows/macOS ({d.fstype or '?'})"))
        if not d.dedicated:
            out.append(("files", "it holds your files"))
        if d.tran == "usb" and d.speed and d.speed < 5000 and not st.brain_allow_usb2:
            out.append(("usb2", "on USB 2 (move it to a blue USB 3 port)"))
        if d.drops >= FLAKY_DROPS:
            out.append(("flaky", f"dropped out {d.drops} times this week"))
        if not d.home and d.free < need:
            out.append(("small", f"too small: the brain needs {need / 1e9:.0f} GB free there"))
        if not d.home and d.plugged_s < st.brain_settle_min * 60:
            out.append(("settling", f"plugged in {d.plugged_s / 60:.0f} min ago (it settles for "
                                    f"{st.brain_settle_min:.0f})"))  # fmt: skip
        return out

    def eligible(self, d: Drive, need: int) -> str:
        """Why ``d`` cannot hold the brain ("" when it can): the first reason."""
        why = self.reasons(d, need)
        return why[0][1] if why else ""

    def contents(self, d: Drive) -> tuple[list[str], list[str]] | None:
        """(your files on the drive, Polymath's data in its folder), or None when the drive cannot be read."""
        from polymath.body.volumes import loose_files, own_files

        try:
            return loose_files(d.brain.parent), own_files(d.brain)
        except OSError:
            return None

    def _pending(self, d: Drive, on_sd: bool) -> bool:
        """Drive care will reformat this Windows/macOS drive: none of your files on it, and nothing of Polymath's
        either, or the brain is stuck on the SD card (then what Polymath put there is worth less than freeing it)."""
        if not self.config.storage.care_format_empty or d.home or d.fstype not in FOREIGN:
            return False
        got = self.contents(d)
        return got is not None and not got[0] and (not got[1] or on_sd)

    def _blocked(self, d: Drive, need: int, on_sd: bool) -> str:
        """Why ``d`` cannot take the brain off the SD card, every reason, in words for the feed."""
        out = []
        first = {"usb2": 0, "files": 1, "small": 2, "flaky": 3, "foreign": 4}  # what only you can fix comes first
        for kind, words in sorted(self.reasons(d, need), key=lambda r: first.get(r[0], 9)):
            if kind == "settling" or (kind == "files" and d.fstype in FOREIGN):
                continue
            if kind == "foreign":
                if self._pending(d, on_sd):
                    words += ": reformatted for Linux by itself (none of your files are on it)"
                elif self.config.storage.care_format_empty:
                    words += " with your files on it: never reformatted while they are there"
            out.append(words)
        return "; ".join(out)

    def _measure(self, st: RolesState, drives: list[Drive]) -> None:
        """One read-speed measurement per run: the brain's drive and the drives that could hold it, once a day."""
        now = self.clock()
        due = [
            d
            for d in drives
            if d.device
            and (d.home or (d.fstype in POSIX and d.dedicated))
            and now - float(st.seen[d.id].get("measured") or 0) >= MEASURE_EVERY_S
        ]
        if not due:
            return
        d = sorted(due, key=lambda x: (not x.home, x.id))[0]
        ok, mbps, why = self.probe(d.device)
        seen = st.seen[d.id]
        seen["measured"] = now
        if ok:
            seen["mbps"] = d.mbps = round(mbps, 1)
        else:
            seen["drops"] = [*(seen.get("drops") or []), now]
            d.drops += 1
            self._event(st, "read_failed", f"{d.name} failed its read check ({why})")

    def _where(self, drives: list[Drive]) -> tuple[str, Drive | None]:
        """Where the brain is now: ("drive", its drive) | ("sd", None) | ("missing", None) | ("own", None)."""
        data = Path(self.config.paths.data_dir)
        home = next((d for d in drives if d.home), None)
        if self.helper.mounts_at(data):
            return ("drive", home) if home is not None else ("own", None)
        with contextlib.suppress(OSError):
            if (data / MOVED_FILE).exists():
                return "missing", None
        return ("sd", None) if self.helper.data_on_system_disk() else ("own", None)

    def _night(self, now: float) -> bool:
        """In the hours after the nightly backup (which then has finished): the quietest time to stop the agent."""
        hour = time.localtime(now).tm_hour
        return (hour - self.config.body.backup_hour - 1) % 24 < NIGHT_HOURS

    # ---------------------------------------------------------------- the decision
    def _stable(self, st: RolesState) -> str:
        """What is worth keeping on the SD card: the decision, not the minutes and megabytes that tick by."""
        keep = {k: v for k, v in asdict(st).items() if k not in {"seen", "drives", "brain_bytes", "brain_bytes_at"}}
        keep["drives"] = [(d.get("id"), d.get("role"), d.get("kind"), bool(d.get("why_not"))) for d in st.drives]
        keep["seen"] = {k: (v.get("drops"), v.get("measured"), v.get("since") is None) for k, v in st.seen.items()}
        return json.dumps(keep, sort_keys=True, default=str)

    def tick(self) -> dict[str, Any]:
        if self.config.storage.sd_only or not self.config.storage.auto_roles:
            return {"roles": "off" if not self.config.storage.auto_roles else "sd-card"}
        st = self.load()
        before = self._stable(st)
        drives = self.drives(st)
        self._measure(st, drives)
        out = self._decide(st, drives)
        if not out.get("moved"):
            self._care(st, drives)
        self._cleanup_previous(st, drives)
        # the public copy every run (in memory); the record on the SD card only when a decision or a drive changed
        self.save(st, persist=self._stable(st) != before)
        return out

    def _decide(self, st: RolesState, drives: list[Drive]) -> dict[str, Any]:
        now = self.clock()
        where, home = self._where(drives)
        others = [d for d in drives if not d.home and d.fstype in POSIX and d.dedicated]
        need = max(int(self.config.storage.home_min_gb * 1e9), 3 * self.brain_bytes(st) if others else 0)
        for d in drives:
            d.score = brain_score(d)
            d.why_not = self.eligible(d, need)
        largest = self.config.storage.brain_prefer == "largest"
        cands = sorted([d for d in drives if not d.why_not and not d.home], key=lambda d: (-d.score, d.id))
        if largest and cands:  # the largest, and among drives about as large (within a quarter) the best
            top = max(d.size for d in cands)
            cands = [d for d in cands if d.size * LARGER_BY > top] + [d for d in cands if d.size * LARGER_BY <= top]
        best = cands[0] if cands else None
        decision = ""
        # ---- the brain
        if where == "sd":
            if best is not None and now >= st.next_move and self.config.storage.move_home:
                return self._move_from_sd(st, drives, best)
            waiting = [d for d in drives if d.why_not.startswith("plugged in")]
            blocked = [(d, why) for d in drives if not d.home and d.why_not and (why := self._blocked(d, need, True))]
            if waiting:
                decision = f"the brain is on the SD card; {waiting[0].name} becomes its home once it has settled"
            elif blocked:
                decision = (
                    "the brain stays on the SD card: "
                    + "; ".join(f"{d.name} cannot hold it ({why})" for d, why in blocked[:3])
                    + ". Or plug in an SSD of Polymath's own on a blue USB 3 port and the brain moves onto it"
                )
            else:
                decision = (
                    "the brain is on the SD card: plug in an SSD of Polymath's own on a blue "
                    "USB 3 port and the brain moves onto it by itself"
                )
            sig = "|".join(
                f"{d.id}:" + ",".join(k for k, _ in self.reasons(d, need) if k != "settling") for d, _ in blocked
            )
            if sig and sig != st.blocked and not waiting:
                self._event(st, "blocked", decision)
            st.blocked = sig if not waiting else st.blocked
        elif where == "drive" and home is not None:
            flaky = home.drops >= FLAKY_DROPS
            better = self._better(best, home) if best is not None else ""
            if (
                best is not None
                and now >= st.next_move
                and (flaky or (better and self._night(now) and now - st.last_move >= MOVE_EVERY_S))
            ):
                return self._move_between(st, drives, home, best, "it keeps dropping out" if flaky else better)
            if best is not None and better:
                decision = (
                    f"the brain moves onto {best.name} tonight, after "
                    f"{(self.config.body.backup_hour + 1) % 24:02d}:00: {better}"
                )
            elif flaky:
                decision = (
                    f"the brain's drive {home.name} dropped out {home.drops} times this week; plug in "
                    "another SSD of Polymath's own and the brain moves onto it"
                )
            else:
                decision = f"the brain is on {self._words(home)}"
        elif where == "missing":
            decision = "the brain's drive is not there: the drive helper brings it back (nothing moves meanwhile)"
        else:
            decision = "the brain lives on its own disk"
        if where != "sd":
            st.blocked = ""
        # ---- backups and extra room
        st.pending = sorted(d.id for d in drives if self._pending(d, where == "sd"))
        self._assign(st, drives, home if where == "drive" else None)
        st.brain = home.id if where == "drive" and home is not None else where
        st.decision = decision
        st.warnings = self._warnings(drives, home if where == "drive" else None, st)
        st.drives = [self._row(d) for d in sorted(drives, key=lambda d: (d.role != "brain", d.role != "backup", d.id))]
        return {"roles": st.brain, "backup": st.backup, "decision": decision}

    def _assign(self, st: RolesState, drives: list[Drive], home: Drive | None) -> None:
        db = self.config.paths.db_path
        try:
            need = int(1.5 * db.stat().st_size)
        except OSError:
            need = 0
        for d in drives:
            d.role = "brain" if home is not None and d.id == home.id else "pending" if d.id in st.pending else "extra"
        other_disk = [d for d in drives if d.role == "extra" and (home is None or not d.disk or d.disk != home.disk)]
        steady = [d for d in other_disk if d.drops < FLAKY_DROPS and d.room > 0 and d.room >= need]
        pick = max(steady, key=lambda d: (-d.drops, d.room, d.id)) if steady else None
        if pick is not None:
            pick.role = "backup"
        if (pick.id if pick else "") != st.backup:
            if pick is not None:
                self._event(
                    st,
                    "backups",
                    f"backups go to {pick.name} ({pick.kind()}, {pick.room / 1e9:.0f} GB of "
                    "room), a different drive from the brain's",
                )
            elif st.backup in st.pending:
                name = next((d.name for d in drives if d.id == st.backup), st.backup)
                self._event(st, "backups", f"backups stay with the brain until {name} is reformatted for Linux")
            elif st.backup:
                self._event(st, "backups", "backups are back on the brain's own drive (no other drive has room)")
        st.backup = pick.id if pick else ""
        st.extra = sorted(d.id for d in drives if d.role == "extra")

    def _warnings(self, drives: list[Drive], home: Drive | None, st: RolesState) -> list[str]:
        out = []
        draw: dict[str, int] = {}
        for d in drives:
            if d.usb:
                draw[d.usb] = max(draw.get(d.usb, 0), d.power_ma)
        if sum(draw.values()) > USB_POWER_MA:
            out.append(
                f"the drives ask for {sum(draw.values())} mA, more than the {USB_POWER_MA} mA a Pi 4 gives its "
                "USB ports: give them a powered USB 3 hub (or their own power), or they may drop out"
            )
        if home is not None and drives and not st.backup:
            out.append("the backups are on the brain's own drive: plug in a second drive to keep them safe")
        for d in drives:
            if d.fstype == "vfat" and not self._empty(d):
                out.append(f"{d.name} is FAT32 and holds your files: no file over 4 GB fits on it, so backups and "
                           "big downloads go elsewhere (it is never reformatted while it holds files)")  # fmt: skip
        if home is not None and home.why_not.startswith("on USB 2"):
            out.append(f"the brain's drive {home.name} is on USB 2: move it to a blue USB 3 port")
        return out

    def _row(self, d: Drive) -> dict[str, Any]:
        return {
            "id": d.id,
            "name": d.name,
            "role": d.role,
            "kind": d.kind(),
            "score": d.score,
            "disk": d.disk,
            "why_not": d.why_not,
            "mbps": d.mbps,
            "size_gb": round(d.size / 1e9, 1),
            "free_gb": round(d.free / 1e9, 1),
            "drops": d.drops,
            "fstype": d.fstype,
            "dedicated": d.dedicated,
            "speed": d.speed,
            "plugged_min": round(d.plugged_s / 60, 1),
        }

    # ---------------------------------------------------------------- moving the brain
    def _outcome(self, vol: str) -> Any:
        from polymath.body.volumes import Outcome

        return Outcome("attached", "", vol)

    def _move_from_sd(self, st: RolesState, drives: list[Drive], best: Drive) -> dict[str, Any]:
        manifest = read_manifest(best.brain) or {}
        self._event(
            st,
            "moving",
            f"moving the brain off the SD card onto {self._words(best)}",
        )
        self.save(st, persist=True)
        with self.helper._locked():
            res = self.helper.move_home(best.brain, manifest, self._outcome(best.id))
        return self._after_move(st, drives, best, res)

    def _move_between(self, st: RolesState, drives: list[Drive], home: Drive, best: Drive, why: str) -> dict[str, Any]:
        src, dst = read_manifest(home.brain) or {}, read_manifest(best.brain) or {}
        self._event(st, "moving", f"moving the brain from {home.name} onto {best.name}: {why}")
        self.save(st, persist=True)
        with self.helper._locked():
            res = self.helper.move_brain(home.brain, src, best.brain, dst)
        return self._after_move(st, drives, best, res)

    def _after_move(self, st: RolesState, drives: list[Drive], best: Drive, res: Any) -> dict[str, Any]:
        now = self.clock()
        if res.action == "moved":
            st.last_move, st.next_move, st.brain = now, 0.0, best.id
            self._event(st, "moved", res.detail)
            for d in drives:
                d.home = d.id == best.id
            self._assign(st, drives, best)
            st.decision = f"the brain is on {self._words(best)}"
        else:
            st.next_move = now + RETRY_MOVE_S
            self._event(st, "move_failed", f"{res.detail}; tried again in {RETRY_MOVE_S / 3600:.0f} h")
            st.decision = f"moving the brain onto {best.name} failed; tried again later"
        st.drives = [self._row(d) for d in drives]
        return {"roles": st.brain, "moved": res.action == "moved", "detail": res.detail}

    def _empty(self, d: Drive) -> bool:
        """Nothing of yours and nothing of Polymath's on it (its own manifest and inventory aside)."""
        got = self.contents(d)
        return got is not None and not got[0] and not got[1]

    def _care(self, st: RolesState, drives: list[Drive]) -> None:
        """Drive care: a Windows/macOS-formatted drive with none of your files on it is reformatted for Linux (one a
        run, once a day at most per drive), so it can hold the brain, backups over 4 GB and full extra room. What
        Polymath itself put there is dropped only when the brain is stuck on the SD card (it is read again). Never one
        with a file of yours on it (said once, with what is on it), never the brain's drive."""
        now = self.clock()
        if not self.config.storage.care_format_empty:
            return
        on_sd = st.brain == "sd"
        for d in drives:
            if d.home or d.fstype not in FOREIGN:
                continue
            seen = st.seen.setdefault(d.id, {"since": now, "drops": []})
            got = self.contents(d)
            if got is None:
                continue
            theirs, mine = got
            if theirs:
                note = "|".join(theirs[:5])
                if seen.get("care_note") != note:
                    seen["care_note"] = note
                    shown = ", ".join(theirs[:5]) + (" …" if len(theirs) > 5 else "")
                    self._event(st, "format_skipped", f"{d.name} is formatted for Windows/macOS ({d.fstype}) and "
                                f"holds files ({shown}): it is never reformatted while they are there, so it cannot "
                                "hold the brain; it serves as extra room")  # fmt: skip
                continue
            if mine and not on_sd:
                continue  # the brain has a drive: Polymath's data there is worth more than reformatting it
            if d.plugged_s < self.config.storage.brain_settle_min * 60:
                continue
            if now - float(seen.get("care_tried") or 0) < 86400:
                continue
            seen["care_tried"] = now
            dropped = (f"; the {len(mine)} file(s) Polymath put there go (its documents are read again, its "
                       "backups taken again), to free the brain from the SD card") if mine else ""  # fmt: skip
            self._event(st, "formatting", f"{d.name} holds none of your files and is formatted for Windows/macOS "
                        f"({d.fstype}): reformatting it for Linux (ext4) so Polymath can use all of it"
                        + dropped)  # fmt: skip
            self.save(st, persist=True)
            res = self.helper.format_empty(d.id, drop_own=bool(mine), by="drive roles (empty drive)")
            if res.action in {"attached", "formatted", "already", "moved", "home"}:
                self._event(st, "formatted", f"{d.name} is formatted for Linux now: {res.detail}")
                new = str(getattr(res, "volume", "") or "")
                if new and new != d.id:  # the same drive under its new name: it has settled already
                    st.seen[new] = {"since": seen.get("since") or now, "drops": list(seen.get("drops") or [])}
            else:
                self._event(st, "format_skipped", f"{d.name} was not reformatted: {res.detail}")
            return

    def _cleanup_previous(self, st: RolesState, drives: list[Drive]) -> None:
        """The old brain drive's copy goes after a week, if the brain's drive has not dropped out since."""
        now = self.clock()
        home = next((d for d in drives if d.home), None)
        for d in drives:
            manifest = read_manifest(d.brain) or {}
            prev = manifest.get("previous_home") or {}
            if d.home or not isinstance(prev, dict) or not prev.get("dir"):
                continue
            at = float(prev.get("at") or now)
            home_drops = [t for t in (st.seen.get(home.id, {}).get("drops") or []) if float(t) > at] if home else [1]
            if now - at < PREVIOUS_KEEP_S or home_drops:
                continue
            target = d.brain / str(prev["dir"])
            if not str(prev["dir"]).startswith("home-previous-") or not target.is_dir() or target.is_symlink():
                continue
            shutil.rmtree(target, ignore_errors=True)
            manifest.pop("previous_home", None)
            with contextlib.suppress(OSError):
                usage = shutil.disk_usage(d.brain)
                manifest["budget_bytes"] = max(0, int(usage.free - usage.total * self.config.storage.reserve_fraction))
            self.helper._write_manifest(d.brain, manifest)
            self._event(
                st,
                "cleaned",
                f"removed the old copy of the brain from {d.name} (a week old; the brain's drive has been steady)",
            )


def read_public(config: Config) -> dict[str, Any] | None:
    """The roles as everyone may read them (``/run/polymath/drive-roles.json``), or None."""
    with contextlib.suppress(OSError, ValueError):
        raw = json.loads((Path(config.paths.run_dir) / PUBLIC).read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else None
    return None
