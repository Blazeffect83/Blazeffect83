"""Plugged-in drives become brain space: the privileged half of the storage pool (runs as root).

udev starts ``polymath-volume@<device>.service`` when a drive (or partition) appears — and at boot for
drives already plugged in — which runs ``polymath storage attach /dev/<device>``. Unplugging stops the unit,
which runs ``polymath storage detach``. The agent itself never mounts anything; it only discovers what is
mounted under ``storage.mount_root`` (:mod:`polymath.memory.pool`).

Safety rules, in order:

1. Never the system disk (the one holding ``/``, ``/boot`` or the data directory), never internal or
   virtual devices (SD card, loop, zram, device-mapper, RAID), never an ignored or retired drive.
2. Never format anything that holds data. Only a *completely blank* device — no partition table, no
   filesystem signature, its first and last MiB all zeros — is formatted (GPT, one ext4 partition labelled
   POLYMATH), and only with ``storage.format_blank_disks``.
3. Never touch files outside ``polymath-brain/``. On a drive that already holds files, Polymath uses a share
   of the free space (``storage.shared_drive_share``) and always leaves ``storage.reserve_fraction`` free.
4. Mount with ``nodev,nosuid,noexec``; FAT/exFAT/NTFS drives are mounted for the ``polymath`` user.
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

from polymath.core.config import Config
from polymath.core.logging import get_logger
from polymath.memory.pool import BRAIN, MANIFEST, ignored_ids, read_manifest

log = get_logger("volumes")
POSIX_FS = {"ext4", "ext3", "ext2", "btrfs", "xfs", "f2fs"}
FOREIGN_FS = {"vfat", "exfat", "ntfs"}  # no Unix owners: mounted for the polymath user
INTERNAL = ("loop", "ram", "zram", "dm-", "md", "sr", "mmcblk", "nbd")
MIN_BUDGET = 1_000_000_000  # below 1 GB a drive is not worth adopting
EDGE = 1 << 20
LOCK = Path("/run/polymath-storage.lock")

Run = Callable[..., "subprocess.CompletedProcess[str]"]


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
    ) -> None:
        self.config = config
        self.run = run
        self.edges = edges
        self._owner = owner
        self.mountinfo = mountinfo
        self.lock = lock
        self.clock = clock

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
            return Outcome("already", f"{dev} is already part of the brain", vol)
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
        return self._adopt(dev, mnt, vol, pr, inf, formatted)

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
        for m, src in self.mounts_of():
            if m == mp:
                r = self.run(["systemctl", "stop", f"polymath-volume@{Path(src).name}.service"])
                if r.returncode != 0 or self.mounts_of(src):
                    self.detach(src)
                return Outcome("ejected", f"{src} can be unplugged now", vol)
        return Outcome("skipped", f"no plugged-in drive {vol}", vol)
