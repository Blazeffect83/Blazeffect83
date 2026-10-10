"""Plugged-in drives become brain space: the privileged half of the storage pool (runs as root).

udev starts ``polymath-volume@<device>.service`` when a drive (or partition) appears — and at boot for
drives already plugged in — which runs ``polymath storage attach /dev/<device>``. Unplugging stops the unit,
which runs ``polymath storage detach``. The agent itself never mounts anything; it only discovers what is
mounted under ``storage.mount_root`` (:mod:`polymath.memory.pool`).

Safety rules, in order:

1. Never the system disk (the one holding ``/``, ``/boot`` or the data directory), never internal or
   virtual devices (SD card, loop, zram, device-mapper, RAID), never an ignored or retired drive.
2. Never format anything that holds data. Two kinds of drive are formatted on their own (ext4, labelled
   POLYMATH):

   * a *completely blank* device: no partition table, no filesystem signature, its first and last MiB all zeros
     (with ``storage.format_blank_disks``);
   * a *fresh* drive as it comes from the shop (with ``storage.format_fresh_drives``). It is formatted for Windows
     or macOS (exFAT, NTFS, FAT), at least ``storage.home_min_gb`` big, and holds nothing but the maker's
     installers and manuals (``factory_files``) and system clutter. No folder may be anything but the maker's,
     the maker's files total under 1 GB, and nothing of Polymath's is stored there.

   **Once per drive, ever.** Before a fresh drive is formatted, its identity (the disk's serial number, or model
   and size) is written to ``REGISTRY`` on the SD card, outside the brain. A drive in that list is never formatted
   on its own again: not when it is unplugged and plugged back in, not after a power cut, and not if you later
   reformat it on a laptop. A drive Polymath formatted is ext4 with a ``polymath-brain/`` folder, so it is
   recognised by its filesystem whatever the list says.
3. Never touch files outside ``polymath-brain/``. On a drive that already holds files, Polymath uses a share
   of the free space (``storage.shared_drive_share``) and always leaves ``storage.reserve_fraction`` free.
4. Mount with ``nodev,nosuid,noexec``; FAT/exFAT/NTFS drives are mounted for the ``polymath`` user.

**Moving the brain off the SD card.** When the data directory is on the SD card and a *dedicated* Linux-formatted
drive with room arrives (``storage.move_home``), the helper moves the whole brain onto it, so the SD card only
boots the Pi:

1. it stops the agent and the dashboard (each commits and exits cleanly);
2. it copies the data directory to ``polymath-brain/home`` on the drive, and checks the copy: the same files and
   bytes, and SQLite's ``quick_check`` on the database;
3. it moves the SD card's copy aside (``/srv/polymath-sd-copy``, kept as a safety net), leaves a
   ``MOVED-TO-DRIVE.json`` marker in the empty directory, and bind-mounts the drive's copy over it;
4. it starts everything again, now writing to the drive.

At every boot (or replug) the bind mount is made again. While the drive is away, the agent sees the marker and
waits instead of starting on the old SD copy. Anything that fails before step 3 leaves the brain on the SD card
and starts the agent again. A fresh shop drive (rule 2) is formatted first and then becomes the brain's home. A
Windows or macOS drive that holds your files is never reformatted on its own: ``sudo polymath storage format
<id> --yes --erase-files`` does that, on request only.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import pwd
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from polymath.core.config import HOME_FILE, MOVED_FILE, Config
from polymath.core.logging import get_logger
from polymath.memory.pool import BRAIN, MANIFEST, ignored_ids, read_manifest

log = get_logger("volumes")
POSIX_FS = {"ext4", "ext3", "ext2", "btrfs", "xfs", "f2fs"}
FOREIGN_FS = {"vfat", "exfat", "ntfs"}  # no Unix owners: mounted for the polymath user
INTERNAL = ("loop", "ram", "zram", "dm-", "md", "sr", "mmcblk", "nbd")
MIN_BUDGET = 1_000_000_000  # below 1 GB a drive is not worth adopting
EDGE = 1 << 20
LOCK = Path("/run/polymath-storage.lock")
REGISTRY = Path("/var/lib/polymath/formatted-drives.json")  # on the SD card: drives already formatted once
FACTORY_MAX_BYTES = 1_000_000_000  # the maker's installers and manuals are small; more than this is user data
# names of drive makers and their bundled apps: a fresh drive's folders must carry one, its files one of these or a
# FACTORY_WORDS word, and every file must have an installer or manual extension
MAKERS = ("samsung", "sandisk", "western digital", "wd discovery", "wddiscovery", "wd apps", "seagate", "lacie",
          "crucial", "kingston", "transcend", "toshiba", "lexar", "adata", "sabrent", "silicon power", "verbatim",
          "intenso", "teamgroup", "corsair", "portable ssd", "memory zone", "secureaccess", "backup plus",
          "my passport", "dashboard setup")  # fmt: skip
FACTORY_WORDS = ("setup", "install", "manual", "user guide", "quick start", "readme", "warranty", "start here")
FACTORY_EXT = (".exe", ".msi", ".pkg", ".dmg", ".app", ".apk", ".pdf", ".txt", ".rtf", ".htm", ".html", ".url",
               ".ico", ".inf", ".zip")  # fmt: skip
HOME = "home"  # polymath-brain/home on a drive: the whole data directory, when the brain lives there
SERVICES = ("polymath.service", "polymath-dashboard.service")
# what an "empty" drive may hold and still be reformatted on request (OS housekeeping, never user files)
JUNK = {"lost+found", "System Volume Information", "$RECYCLE.BIN", ".Trashes", ".Spotlight-V100", ".fseventsd",
        ".TemporaryItems", "Thumbs.db", ".DS_Store", "desktop.ini", "autorun.inf", "IndexerVolumeGuid"}  # fmt: skip


def tree_stats(root: Path) -> tuple[int, int]:
    """(files, bytes) under a directory, symlinks not followed."""
    files = total = 0
    for base, _dirs, names in os.walk(root):
        for name in names:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(base, name)).st_size
                files += 1
    return files, total


Run = Callable[..., "subprocess.CompletedProcess[str]"]


def factory_files(root: Path) -> list[str] | None:
    """What a fresh drive holds besides clutter: the maker's installers and manuals (None if anything else).

    Every top-level file must be an installer or manual (by extension) named after a maker or as one ("Setup",
    "User Guide"…), every folder must carry a maker's name, and everything together must stay under 1 GB.
    """
    found, total = [], 0
    try:
        entries = [e for e in os.listdir(root) if e not in JUNK and e != BRAIN and not e.startswith("._")]
    except OSError:
        return None
    for name in entries:
        path = root / name
        low = name.lower()
        maker = any(m in low for m in MAKERS)
        if path.is_symlink():
            return None
        if path.is_dir():
            if not maker:
                return None
            total += tree_stats(path)[1]
        elif path.is_file():
            if not low.endswith(FACTORY_EXT) or not (maker or any(w in low for w in FACTORY_WORDS)):
                return None
            total += path.stat().st_size
        else:
            return None
        found.append(name)
        if total > FACTORY_MAX_BYTES:
            return None
    return sorted(found)


def run_cmd(args: list[str], *, input: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, timeout=900, input=input, check=False)


def read_edges(dev: str) -> tuple[bytes, bytes]:
    """The first and last MiB of a block device (what a 'blank' check looks at)."""
    with open(dev, "rb") as f:
        head = f.read(EDGE)
        size = f.seek(0, os.SEEK_END)
        f.seek(max(0, size - EDGE))
        tail = f.read(EDGE)
    return head, tail


@dataclass
class Outcome:
    action: str  # attached | already | formatted | skipped | detached | ejected
    detail: str
    volume: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"action": self.action, "detail": self.detail, "volume": self.volume}


class Helper:
    def __init__(
        self,
        config: Config,
        run: Run = run_cmd,
        *,
        edges: Callable[[str], tuple[bytes, bytes]] = read_edges,
        owner: tuple[int, int] | None = None,
        mountinfo: Path = Path("/proc/self/mountinfo"),
        lock: Path = LOCK,
        clock: Callable[[], float] = time.time,
        registry: Path = REGISTRY,
    ) -> None:
        self.config = config
        self.run = run
        self.edges = edges
        self._owner = owner
        self.mountinfo = mountinfo
        self.lock = lock
        self.clock = clock
        self.registry = registry

    # ------------------------------------------------------------------ facts about devices
    def owner(self) -> tuple[int, int]:
        if self._owner is None:
            pw = pwd.getpwnam("polymath")
            self._owner = (pw.pw_uid, pw.pw_gid)
        return self._owner

    def probe(self, dev: str) -> dict[str, str]:
        """Low-level signature probe (``blkid -p``): TYPE, UUID, LABEL, PTTYPE… Empty when nothing is found."""
        r = self.run(["blkid", "-p", "-o", "export", dev])
        if r.returncode != 0:
            return {}
        return dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)

    def info(self, dev: str) -> dict[str, Any]:
        r = self.run(["lsblk", "-J", "-b", "-o", "NAME,PATH,TYPE,SIZE,MOUNTPOINTS,PKNAME,MODEL,SERIAL,LABEL", dev])
        if r.returncode != 0:
            raise FileNotFoundError(r.stderr.strip() or f"{dev} not found")
        devices = json.loads(r.stdout).get("blockdevices") or []
        if not devices:
            raise FileNotFoundError(f"{dev} not found")
        d: dict[str, Any] = devices[0]
        d["mountpoints"] = [m for m in d.get("mountpoints") or [] if m]
        return d

    def disk_of(self, dev: str) -> str:
        d = self.info(dev)
        return f"/dev/{d['pkname']}" if d.get("pkname") else str(d.get("path") or dev)

    def system_disks(self) -> set[str]:
        out = set()
        for path in ("/", "/boot", "/boot/firmware", str(self.config.paths.data_dir)):
            r = self.run(["findmnt", "-n", "-o", "SOURCE", "--target", path])
            src = r.stdout.strip().split("[")[0]
            if r.returncode == 0 and src.startswith("/dev/"):
                with contextlib.suppress(FileNotFoundError, ValueError):
                    out.add(self.disk_of(src))
        return out

    def identity(self, dev: str) -> str:
        """Who the physical drive is: its serial number, or its model and size ("" when it reports neither)."""
        try:
            d = self.info(self.disk_of(dev))
        except (FileNotFoundError, ValueError):
            return ""
        serial, model = str(d.get("serial") or "").strip(), str(d.get("model") or "").strip()
        if serial:
            return f"serial:{serial}"
        return f"model:{model}|{d.get('size') or ''}" if model else ""

    def formatted_drives(self) -> dict[str, Any]:
        try:
            data = json.loads(self.registry.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def remember_formatted(self, ident: str, entry: dict[str, Any]) -> None:
        """Written *before* formatting, atomically, so a crash or power cut can never lead to a second format."""
        if not ident:
            return
        drives = self.formatted_drives()
        drives[ident] = {**drives.get(ident, {}), **entry, "at": self.clock()}
        self.registry.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.registry.with_suffix(".tmp")
        tmp.write_text(json.dumps(drives, indent=2, sort_keys=True), encoding="utf-8")
        with open(tmp, "rb+") as f:
            os.fsync(f.fileno())
        tmp.replace(self.registry)

    def is_blank(self, dev: str) -> bool:
        try:
            head, tail = self.edges(dev)
        except OSError:
            return False
        return not head.strip(b"\0") and not tail.strip(b"\0")

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        with self.lock.open("w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            yield

    # ------------------------------------------------------------------ attach
    def attach(self, dev: str) -> Outcome:
        with self._locked():
            try:
                out = self._attach(dev)
            except FileNotFoundError as exc:
                out = Outcome("skipped", f"{dev} went away: {exc}")
        log.info("storage attach", extra={"device": dev, **out.to_dict()})
        return out

    def _attach(self, dev: str) -> Outcome:
        st = self.config.storage
        name = Path(dev).name
        if not st.adopt:
            return Outcome("skipped", "storage.adopt is off")
        if name.startswith(INTERNAL) and not (st.allow_virtual and name.startswith("loop")):
            return Outcome("skipped", f"{name} is an internal or virtual device")
        inf = self.info(dev)
        if self.disk_of(dev) in self.system_disks():
            return Outcome("skipped", f"{dev} is on the system disk")
        pr = self.probe(dev)
        if pr.get("UUID") and pr["UUID"] in ignored_ids(self.config):
            return Outcome("skipped", f"{dev} is on the ignore list", pr["UUID"])
        formatted = False
        if inf.get("type") == "disk" and (pr.get("PTTYPE") or inf.get("children")):
            return Outcome("skipped", "the disk has partitions; each one is handled on its own")
        if not pr.get("TYPE"):
            if not self.is_blank(dev):
                return Outcome("skipped", f"{dev} holds data Polymath does not recognise; not touching it")
            if not st.format_blank_disks:
                return Outcome("skipped", f"{dev} is blank, but storage.format_blank_disks is off")
            if inf.get("type") == "disk":
                dev = self._partition(dev)
            r = self.run(["mkfs.ext4", "-q", "-F", "-L", "POLYMATH", "-m", "0", dev])
            if r.returncode != 0:
                return Outcome("skipped", f"formatting {dev} failed: {r.stderr.strip()}")
            formatted = True
            pr = self.probe(dev)
            inf = self.info(dev)
        fstype = pr.get("TYPE", "")
        if fstype not in POSIX_FS | FOREIGN_FS:
            return Outcome("skipped", f"{fstype or 'no'} filesystem: not one Polymath can use")
        uuid = pr.get("UUID", "")
        vol = re.sub(r"[^A-Za-z0-9-]", "", uuid)
        if not vol:
            return Outcome("skipped", f"{dev} has no filesystem UUID")
        mnt = self.config.storage.mount_root / vol
        if str(mnt) in inf["mountpoints"]:
            return self._home(Outcome("already", f"{dev} is already part of the brain", vol), mnt)
        mnt.mkdir(parents=True, exist_ok=True)
        if inf["mountpoints"]:
            if fstype not in POSIX_FS:
                self._cleanup(mnt, mounted=False)
                where = inf["mountpoints"][0]
                return Outcome(
                    "skipped", f"mounted at {where} with permissions Polymath cannot use; unmount and replug"
                )
            r = self.run(["mount", "--bind", inf["mountpoints"][0], str(mnt)])
        else:
            r = self.run(["mount", *self._mount_args(fstype), dev, str(mnt)])
        if r.returncode != 0:
            self._cleanup(mnt, mounted=False)
            return Outcome("skipped", f"mounting {dev} failed: {r.stderr.strip()}", vol)
        if fstype in FOREIGN_FS:
            fresh = self._format_fresh(dev, mnt, pr)
            if fresh is not None:
                return fresh
        return self._home(self._adopt(dev, mnt, vol, pr, inf, formatted), mnt)

    def fresh_reason(self, dev: str, mnt: Path) -> tuple[str, list[str]]:
        """("", factory files) when a mounted Windows/macOS drive is fresh from the shop, else why it is not."""
        st = self.config.storage
        if not st.format_fresh_drives:
            return "storage.format_fresh_drives is off", []
        if shutil.disk_usage(mnt).total < st.home_min_gb * 1e9:
            return f"smaller than {st.home_min_gb:g} GB", []
        brain = mnt / BRAIN
        manifest = read_manifest(brain) or {}
        if manifest.get("retired") or manifest.get("home"):
            return "retired" if manifest.get("retired") else "it holds the brain", []
        if brain.exists() and any(e not in {MANIFEST, MANIFEST + ".tmp"} for e in os.listdir(brain)):
            return f"{BRAIN}/ holds Polymath's documents", []
        files = factory_files(mnt)
        if files is None:
            return "it holds files", []
        ident = self.identity(dev)
        if not ident:
            return "the drive reports no serial number or model, so a second format could not be ruled out", []
        if ident in self.formatted_drives():
            return "it was formatted once before (it is in the list of formatted drives)", []
        return "", files

    def _format_fresh(self, dev: str, mnt: Path, pr: dict[str, str]) -> Outcome | None:
        """Format a fresh shop drive once (ext4, POLYMATH) and adopt it; None when it is not fresh (left as is)."""
        why, files = self.fresh_reason(dev, mnt)
        if why:
            if why not in {"it holds files", "storage.format_fresh_drives is off"}:
                log.info("storage fresh drive not formatted", extra={"device": dev, "why": why})
            return None
        ident = self.identity(dev)
        old = pr.get("TYPE", "")
        self.remember_formatted(ident, {"was": old, "uuid_before": pr.get("UUID", ""), "factory_files": files,
                                        "device": dev})  # fmt: skip
        self.run(["umount", str(mnt)])
        if any(m == str(mnt) for m, _s in self.mounts_of()):
            return Outcome("skipped", f"{mnt} is busy; the fresh drive was not formatted", pr.get("UUID", ""))
        self._cleanup(mnt, mounted=False)
        r = self.run(["mkfs.ext4", "-q", "-F", "-L", "POLYMATH", "-m", "0", dev])
        if r.returncode != 0:
            return Outcome("skipped", f"formatting the fresh drive {dev} failed: {r.stderr.strip()}")
        log.info("storage formatted a fresh drive", extra={"device": dev, "was": old, "identity": ident})
        out = self._attach(dev)
        note = f"formatted a fresh {old} drive as ext4 (once; it is never formatted on its own again)"
        if files:
            note += f"; the maker's files it held: {', '.join(files[:4])}{' …' if len(files) > 4 else ''}"
        return Outcome("formatted" if out.action == "attached" else out.action, f"{note}; {out.detail}", out.volume)

    def _partition(self, disk: str) -> str:
        """One GPT partition across a blank disk; returns the partition's device path."""
        r = self.run(["sfdisk", "--quiet", "--label", "gpt", disk], input="type=linux,name=polymath\n")
        if r.returncode != 0:
            raise FileNotFoundError(f"partitioning {disk} failed: {r.stderr.strip()}")
        self.run(["udevadm", "settle"])
        children = self.info(disk).get("children") or []
        if not children:
            self.run(["partprobe", disk])
            self.run(["udevadm", "settle"])
            children = self.info(disk).get("children") or []
        if not children:
            raise FileNotFoundError(f"the new partition on {disk} did not appear")
        return str(children[0]["path"])

    def _mount_args(self, fstype: str) -> list[str]:
        base = "nodev,nosuid,noexec,noatime"
        if fstype in POSIX_FS:
            return ["-t", fstype, "-o", base]
        uid, gid = self.owner()
        return ["-t", "ntfs3" if fstype == "ntfs" else fstype, "-o", f"uid={uid},gid={gid},umask=0027,{base}"]

    def _cleanup(self, mnt: Path, *, mounted: bool) -> None:
        if mounted:
            self.run(["umount", str(mnt)])
        with contextlib.suppress(OSError):
            mnt.rmdir()

    def _adopt(
        self, dev: str, mnt: Path, vol: str, pr: dict[str, str], inf: dict[str, Any], formatted: bool
    ) -> Outcome:
        st = self.config.storage
        brain = mnt / BRAIN
        created = not brain.exists()
        brain.mkdir(exist_ok=True)
        posix = pr.get("TYPE") in POSIX_FS
        if posix:
            uid, gid = self.owner()
            os.chown(brain, uid, gid)
            os.chmod(brain, 0o750)
        manifest = read_manifest(brain)
        if manifest is not None and manifest.get("retired"):
            self._cleanup(mnt, mounted=True)
            return Outcome("skipped", "this drive was retired", vol)
        if manifest is None:
            usage = shutil.disk_usage(mnt)
            others = [e for e in os.listdir(mnt) if e not in {BRAIN, "lost+found"}]
            dedicated = formatted or pr.get("LABEL", "").upper().startswith("POLYMATH") or not others
            reserve = usage.total * st.reserve_fraction
            if dedicated:
                budget = usage.free - reserve
            else:
                budget = min(usage.free * st.shared_drive_share, usage.free - reserve)
            budget = max(0, int(budget))
            if budget < MIN_BUDGET:
                if created:
                    with contextlib.suppress(OSError):
                        brain.rmdir()
                self._cleanup(mnt, mounted=True)
                return Outcome("skipped", f"only {usage.free / 1e9:.1f} GB free: too little to use", vol)
            manifest = {
                "id": vol,
                "label": pr.get("LABEL") or inf.get("label") or "",
                "fstype": pr.get("TYPE", ""),
                "model": (inf.get("model") or "").strip(),
                "serial": (inf.get("serial") or "").strip(),
                "size_bytes": int(usage.total),
                "budget_bytes": budget,
                "dedicated": dedicated,
                "adopted_at": self.clock(),
            }
        manifest["device"] = dev
        manifest["attached_at"] = self.clock()
        tmp = brain / (MANIFEST + ".tmp")
        tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        if posix:
            os.chown(tmp, *self.owner())
        tmp.replace(brain / MANIFEST)
        what = "dedicated drive" if manifest["dedicated"] else "shared drive"
        return Outcome(
            "attached",
            f"{dev} → {mnt} ({what}; {int(manifest['budget_bytes']) / 1e9:.1f} GB of brain space)",
            vol,
        )

    # ------------------------------------------------------------------ the brain's home drive
    def data_on_system_disk(self) -> bool:
        def disk(path: str) -> str:
            r = self.run(["findmnt", "-n", "-o", "SOURCE", "--target", path])
            src = r.stdout.strip().split("[")[0]
            if r.returncode != 0 or not src.startswith("/dev/"):
                return ""
            try:
                return self.disk_of(src)
            except (FileNotFoundError, ValueError):
                return ""

        root = disk("/")
        return bool(root) and disk(str(self.config.paths.data_dir)) == root

    def _home(self, out: Outcome, mnt: Path) -> Outcome:
        """After a drive is attached: make it the brain's home again (boot, replug), or move the brain onto it."""
        if out.action not in {"attached", "already"}:
            return out
        brain = mnt / BRAIN
        manifest = read_manifest(brain) or {}
        if manifest.get("home"):
            return self.mount_home(brain, manifest, out)
        if self.should_move(brain, manifest):
            return self.move_home(brain, manifest, out)
        hint = self.home_hint(brain, manifest)
        if hint != manifest.get("home_hint", ""):
            manifest["home_hint"] = hint
            self._write_manifest(brain, manifest)
        return Outcome(out.action, f"{out.detail}; {hint}", out.volume) if hint else out

    def home_hint(self, brain: Path, manifest: dict[str, Any]) -> str:
        """Why a big drive did not become the brain's home, and the one thing to do about it ("" if none)."""
        st, data = self.config.storage, self.config.paths.data_dir
        if not st.move_home or (data / MOVED_FILE).exists() or not self.data_on_system_disk():
            return ""
        if int(manifest.get("size_bytes") or 0) < st.home_min_gb * 1e9:
            return ""
        vol = manifest.get("id", "")
        if manifest.get("fstype") in FOREIGN_FS:
            fs = manifest.get("fstype")
            return f"to move the brain onto it (now {fs}, erased!): sudo polymath storage format {vol} --yes"
        if not manifest.get("dedicated"):
            return "it holds other files: empty it and plug it in again to move the brain onto it"
        return ""

    def should_move(self, brain: Path, manifest: dict[str, Any]) -> bool:
        st, data = self.config.storage, self.config.paths.data_dir
        if not st.move_home or not manifest.get("dedicated") or manifest.get("fstype") not in POSIX_FS:
            return False
        if (data / MOVED_FILE).exists() or (data / HOME_FILE).exists() or not self.data_on_system_disk():
            return False  # the brain already lives on a drive (this one or another), or not on the SD card
        need = max(st.home_min_gb * 1e9, 3 * tree_stats(data)[1])
        return shutil.disk_usage(brain).free >= need

    def _services(self, verb: str) -> None:
        if verb == "start":
            self.run(["systemctl", "reset-failed", *SERVICES])
        self.run(["systemctl", verb, *SERVICES])

    def _chown_tree(self, root: Path) -> None:
        uid, gid = self.owner()
        for base, dirs, names in os.walk(root):
            for name in [*dirs, *names]:
                with contextlib.suppress(OSError):
                    os.lchown(os.path.join(base, name), uid, gid)
        with contextlib.suppress(OSError):
            os.chown(root, uid, gid)

    def move_home(self, brain: Path, manifest: dict[str, Any], out: Outcome) -> Outcome:
        """Copy the data directory onto the drive, verify it, and switch the brain over (agent stopped meanwhile)."""
        import sqlite3

        data, home = self.config.paths.data_dir, brain / HOME
        name = manifest.get("label") or manifest.get("model") or manifest.get("id", "")[:8]
        self._services("stop")
        try:
            if home.exists():
                shutil.rmtree(home)  # a copy left over from an interrupted attempt (only ever ours)
            shutil.copytree(data, home, symlinks=True)
            src_stats, dst_stats = tree_stats(data), tree_stats(home)
            if src_stats != dst_stats:
                raise OSError(f"copy differs: {src_stats} files/bytes on the SD card, {dst_stats} on the drive")
            db = home / self.config.paths.db_path.relative_to(data)
            if db.exists():
                conn = sqlite3.connect(str(db))
                try:
                    check = conn.execute("PRAGMA quick_check").fetchone()[0]
                    if check != "ok":
                        raise OSError(f"database check failed on the copy: {check}")
                    with contextlib.suppress(sqlite3.Error):  # tell the feed (the table exists once migrated)
                        conn.execute(
                            "INSERT INTO events(at, kind, text, detail) VALUES(?, 'home', ?, ?)",
                            (self.clock(), f"moved the brain onto {name}: {src_stats[1] / 1e9:.1f} GB copied and "
                             "checked; the SD card now only boots the Pi", json.dumps({"status": "moved"})),
                        )  # fmt: skip
                        conn.commit()
                finally:
                    conn.close()
            usage = shutil.disk_usage(brain)
            budget_gb = round(usage.total * (1 - self.config.storage.reserve_fraction) / 1e9, 1)
            (home / HOME_FILE).write_text(
                json.dumps({"id": manifest.get("id"), "name": name, "budget_gb": budget_gb, "moved_at": self.clock(),
                            "from": str(data)}, indent=2), encoding="utf-8")  # fmt: skip
            self._chown_tree(home)
        except (OSError, shutil.Error, sqlite3.Error) as exc:
            with contextlib.suppress(OSError):
                shutil.rmtree(home)
            self._services("start")
            log.warning("moving the brain failed", extra={"error": str(exc)})
            return Outcome(out.action, f"{out.detail}; moving the brain onto it failed ({exc}): it stays on the "
                           "SD card", out.volume)  # fmt: skip
        # switch over: the SD card's copy is moved aside (not deleted) and the drive's copy mounted in its place
        aside = data.with_name(f"{data.name}-sd-copy")
        if aside.exists():
            aside = data.with_name(f"{data.name}-sd-copy-{int(self.clock())}")
        aside.mkdir()
        for entry in list(data.iterdir()):
            entry.rename(aside / entry.name)
        (data / MOVED_FILE).write_text(
            json.dumps({"id": manifest.get("id"), "name": name, "moved_at": self.clock(), "sd_copy": str(aside)},
                       indent=2), encoding="utf-8")  # fmt: skip
        manifest |= {"home": True, "budget_bytes": 0}  # the drive is the main disk now: nothing spills to it
        self._write_manifest(brain, manifest)
        r = self.run(["mount", "--bind", str(home), str(data)])
        self._services("start")
        if r.returncode != 0:
            return Outcome("moved", f"the brain was copied to {name}, but mounting it failed ({r.stderr.strip()}); "
                           "replug the drive", out.volume)  # fmt: skip
        return Outcome(
            "moved",
            f"the brain moved onto {name} ({tree_stats(home)[1] / 1e9:.1f} GB, {budget_gb:.0f} GB of room); the SD "
            f"card only boots the Pi now. Its old copy is kept at {aside}",
            out.volume,
        )

    def mount_home(self, brain: Path, manifest: dict[str, Any], out: Outcome) -> Outcome:
        """The home drive is back (boot or replug): mount it over the data directory and start the agent."""
        data, home = self.config.paths.data_dir, brain / HOME
        if not (home / HOME_FILE).exists():
            return Outcome(out.action, f"{out.detail}; it should hold the brain, but {home} is missing", out.volume)
        if any(mp == str(data) for mp, _src in self.mounts_at(data)):
            return Outcome(out.action, f"{out.detail}; it holds the brain (already mounted)", out.volume)
        r = self.run(["mount", "--bind", str(home), str(data)])
        if r.returncode != 0:
            return Outcome(out.action, f"{out.detail}; mounting the brain failed: {r.stderr.strip()}", out.volume)
        self._services("start")
        return Outcome("home", f"{out.detail}; the brain is back on this drive and the agent is starting", out.volume)

    def _write_manifest(self, brain: Path, manifest: dict[str, Any]) -> None:
        tmp = brain / (MANIFEST + ".tmp")
        tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chown(tmp, *self.owner())
        tmp.replace(brain / MANIFEST)

    def format_empty(self, vol: str, *, erase_files: bool = False) -> Outcome:
        """Reformat an adopted drive (only on request): Linux ext4, so the brain can move there. A drive holding
        files is refused unless ``erase_files`` (the user listed them and typed --erase-files)."""
        with self._locked():
            mp = str(self.config.storage.mount_root / vol)
            src = next((s for m, s in self.mounts_of() if m == mp), "")
            if not src:
                return Outcome("skipped", f"no plugged-in drive {vol}", vol)
            brain = Path(mp) / BRAIN
            manifest = read_manifest(brain) or {}
            if manifest.get("home"):
                return Outcome("skipped", "this drive holds the brain; it is never reformatted", vol)
            files = [e for e in os.listdir(mp) if e not in JUNK and e != BRAIN]
            inside = [e for e in os.listdir(brain) if e not in {MANIFEST, MANIFEST + ".tmp"}] if brain.exists() else []
            if inside:  # spilled documents live here: erasing them would lose what it learned
                return Outcome("skipped", f"{BRAIN}/ holds Polymath's documents; retire the drive first", vol)
            if files and not erase_files:
                shown = ", ".join(sorted(files)[:5]) + (" …" if len(files) > 5 else "")
                return Outcome("skipped", f"the drive holds files ({shown}); it is not reformatted. To erase them "
                               f"too: sudo polymath storage format {vol} --yes --erase-files", vol)  # fmt: skip
            self.remember_formatted(self.identity(src), {"was": manifest.get("fstype", ""), "uuid_before": vol,
                                                         "by": "polymath storage format", "device": src})  # fmt: skip
            self.run(["umount", mp])
            if any(m == mp for m, _s in self.mounts_of()):
                return Outcome("skipped", f"{mp} is busy; nothing was changed", vol)
            with contextlib.suppress(OSError):
                Path(mp).rmdir()
            r = self.run(["mkfs.ext4", "-q", "-F", "-L", "POLYMATH", "-m", "0", src])
            if r.returncode != 0:
                return Outcome("skipped", f"formatting {src} failed: {r.stderr.strip()}", vol)
        return self.attach(src)

    def mounts_at(self, path: Path) -> list[tuple[str, str]]:
        """``(mountpoint, source)`` of mounts exactly at ``path``."""
        try:
            lines = self.mountinfo.read_text().splitlines()
        except OSError:
            return []
        out = []
        for line in lines:
            left, _, right = line.partition(" - ")
            fields, rfields = left.split(), right.split()
            if len(fields) >= 5 and len(rfields) >= 2 and fields[4].replace("\\040", " ") == str(path):
                out.append((fields[4], rfields[1]))
        return out

    # ------------------------------------------------------------------ detach / eject
    def mounts_of(self, dev: str | None = None) -> list[tuple[str, str]]:
        """``(mountpoint, source)`` of the pool's mounts (optionally only those of ``dev``)."""
        root = str(self.config.storage.mount_root).rstrip("/") + "/"
        out = []
        try:
            lines = self.mountinfo.read_text().splitlines()
        except OSError:
            return []
        for line in lines:
            left, _, right = line.partition(" - ")
            fields, rfields = left.split(), right.split()
            if len(fields) < 5 or len(rfields) < 2:
                continue
            mp = fields[4].replace("\\040", " ")
            if mp.startswith(root) and (dev is None or rfields[1] == dev):
                out.append((mp, rfields[1]))
        return out

    def detach(self, dev: str) -> Outcome:
        """Unmount a drive that is being (or has been) unplugged. Lazy, so a vanished device never hangs."""
        with self._locked():
            mps = self.mounts_of(dev)
            home = [(mp, src) for mp, src in self.mounts_at(self.config.paths.data_dir) if src == dev]
            if home:  # the brain's drive went away: stop the agent (it waits for the drive) and release it
                self.run(["systemctl", "stop", *SERVICES])
                mps = home + mps
            for mp, _src in mps:
                self.run(["umount", "-l", mp])
                with contextlib.suppress(OSError):
                    Path(mp).rmdir()
        out = Outcome("detached" if mps else "skipped", f"{dev}: {len(mps)} mount(s) released")
        log.info("storage detach", extra={"device": dev, **out.to_dict()})
        return out

    def eject(self, vol: str) -> Outcome:
        """Cleanly unmount a drive so it can be unplugged (stops its unit, which detaches it)."""
        mp = str(self.config.storage.mount_root / vol)
        home = (read_manifest(Path(mp) / BRAIN) or {}).get("home")
        if home and self.run(["systemctl", "is-active", "--quiet", SERVICES[0]]).returncode == 0:
            return Outcome("skipped", "this drive holds the brain: it stays plugged in. To unplug it anyway, "
                           "`sudo systemctl stop polymath polymath-dashboard` first", vol)  # fmt: skip
        for m, src in self.mounts_of():
            if m == mp:
                r = self.run(["systemctl", "stop", f"polymath-volume@{Path(src).name}.service"])
                if r.returncode != 0 or self.mounts_of(src):
                    self.detach(src)
                return Outcome("ejected", f"{src} can be unplugged now", vol)
        return Outcome("skipped", f"no plugged-in drive {vol}", vol)
