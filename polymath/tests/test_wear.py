"""SD card wear guard: measuring writes, saver mode, read-only disks, and moving the brain onto a drive."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from polymath.body import volumes as vols
from polymath.body import wear
from polymath.body.guard import Guard
from polymath.body.sensors import Vitals
from polymath.core.app import StorageError, check_storage
from polymath.core.config import HOME_FILE, MOVED_FILE, ConfigError, apply_home, load_config
from polymath.core.loop import MAX_SLICE, Agent
from polymath.interface import cli
from polymath.interface.face import Face
from polymath.interface.feed import render_event
from polymath.memory import pool as storage_pool
from tests.test_storage import FakeSystem

GB = 1_000_000_000


def text_of(segs) -> str:
    return "".join(t for t, _ in segs)


# ------------------------------------------------------------------ fake sysfs
def fake_sys(root: Path, path: Path, *, disk: str = "mmcblk0", part: str = "mmcblk0p2", sd: bool = True,
             sectors_written: int = 1000, size_sectors: int = 122_880_000) -> Path:  # fmt: skip
    """A /sys tree in which ``path`` lives on ``part`` of ``disk``."""
    st = os.stat(path)
    disk_dir = root / "block" / disk
    part_dir = disk_dir / part
    part_dir.mkdir(parents=True, exist_ok=True)
    (part_dir / "partition").write_text("2\n")
    (part_dir / "stat").write_text(f"100 0 2000 50 7 0 {sectors_written} 30 0 60 80 0 0 0 0 0 0\n")
    (disk_dir / "size").write_text(f"{size_sectors}\n")
    if sd:
        (disk_dir / "device").mkdir(exist_ok=True)
        (disk_dir / "device" / "type").write_text("SD\n")
    else:
        (disk_dir / "queue").mkdir(exist_ok=True)
        (disk_dir / "queue" / "rotational").write_text("0\n")
    link = root / "dev" / "block" / f"{os.major(st.st_dev)}:{os.minor(st.st_dev)}"
    link.parent.mkdir(parents=True, exist_ok=True)
    if not link.exists():
        link.symlink_to(part_dir)
    return part_dir / "stat"


def set_written(stat: Path, sectors: int) -> None:
    fields = stat.read_text().split()
    fields[6] = str(sectors)
    stat.write_text(" ".join(fields) + "\n")


def proc_io(path: Path, written: int) -> Path:
    path.write_text(f"rchar: 1\nwchar: 2\nwrite_bytes: {written}\ncancelled_write_bytes: 0\n")
    return path


# ------------------------------------------------------------------ sensors
def test_device_of_reads_sysfs(tmp_path):
    sysroot = tmp_path / "sys"
    data = tmp_path / "data"
    data.mkdir()
    stat = fake_sys(sysroot, data, sectors_written=2048)
    dev = wear.device_of(data / "missing" / "deeper", sys_root=sysroot)
    assert dev is not None and dev.name == "mmcblk0p2" and dev.disk == "mmcblk0" and dev.kind == "sd" and dev.is_sd
    assert dev.size_bytes == 122_880_000 * 512 and wear.device_written(dev) == 2048 * 512
    stat.write_text("garbage\n")
    assert wear.device_written(dev) is None
    assert wear.device_of(data, sys_root=tmp_path / "nowhere") is None
    other = tmp_path / "ssd"
    ssd_sys = tmp_path / "sys2"
    other.mkdir()
    fake_sys(ssd_sys, other, disk="sda", part="sda1", sd=False)
    d2 = wear.device_of(other, sys_root=ssd_sys)
    assert d2 is not None and d2.kind == "ssd" and not d2.is_sd
    (ssd_sys / "block/sda/queue/rotational").write_text("1\n")
    assert wear.device_of(other, sys_root=ssd_sys).kind == "hdd"  # type: ignore[union-attr]
    (ssd_sys / "block/sda/queue/rotational").unlink()
    assert wear.device_of(other, sys_root=ssd_sys).kind == "other"  # type: ignore[union-attr]
    (ssd_sys / "block/sda/size").write_text("x")
    assert wear.device_of(other, sys_root=ssd_sys).size_bytes == 0  # type: ignore[union-attr]
    (sysroot / "block/mmcblk0/device/type").write_text("MMC\n")
    assert wear.device_of(data, sys_root=sysroot).kind == "mmc"  # type: ignore[union-attr]


def test_process_written_and_read_only(tmp_path):
    assert wear.process_written(proc_io(tmp_path / "io", 12345)) == 12345
    assert wear.process_written(tmp_path / "missing") is None
    assert wear.process_written() is not None  # this process, on Linux
    mounts = tmp_path / "mounts"
    data = tmp_path / "srv" / "polymath"
    data.mkdir(parents=True)
    mounts.write_text(f"/dev/root / ext4 rw,noatime 0 0\n/dev/sda1 {tmp_path}/srv ext4 ro,relatime 0 0\nshort\n")
    assert wear.read_only(data, mounts=mounts) is True
    mounts.write_text(f"/dev/root / ext4 ro 0 0\n/dev/sda1 {data} ext4 rw 0 0\n")
    assert wear.read_only(data, mounts=mounts) is False  # the closest mount wins
    assert wear.read_only(data, mounts=tmp_path / "nope") is False
    assert wear.read_only(tmp_path / "not-there", mounts=mounts) is True  # under "/" (ro)


# ------------------------------------------------------------------ meter and report
def meter(config, db, tmp_path, **kw):
    sysroot = tmp_path / "sys"
    config.paths.data_dir.mkdir(parents=True, exist_ok=True)
    stat = fake_sys(sysroot, config.paths.data_dir, **kw)
    io = proc_io(tmp_path / "io", 0)
    m = wear.WearMeter(config, db, sys_root=sysroot, proc_io=io, mounts=tmp_path / "mounts")
    return m, stat, io


def test_meter_samples_deltas_handles_resets_and_reports(config, db, tmp_path):
    m, stat, io = meter(config, db, tmp_path, sectors_written=0)
    devices = m.devices()
    assert devices[0][0] == "data" and devices[0][1].name == "mmcblk0p2"
    t = 1_000_000.0
    assert m.sample(t) and not m.sample(t + 10)  # every 15 minutes
    rows = db.query("SELECT * FROM disk_writes WHERE device = 'mmcblk0p2'")
    assert len(rows) == 1 and rows[0]["bytes"] == 0  # first sight: nothing to compare with
    for i in range(1, 97):  # one day of samples: 1 GB per 15 minutes
        set_written(stat, i * (GB // 512))
        proc_io(io, i * GB // 2)
        assert m.sample(t + i * wear.SAMPLE_EVERY)
    now = t + 96 * wear.SAMPLE_EVERY
    rep = {r["device"]: r for r in wear.report(db, config, now)}["mmcblk0p2"]
    assert rep["last_day_bytes"] == 96 * GB - (GB // 512 * 512 - GB) * 0 or rep["last_day_bytes"] > 95 * GB
    assert rep["agent_last_day_bytes"] == pytest.approx(48 * GB, rel=0.02)
    assert rep["kind"] == "sd" and rep["status"] in {"high", "critical"} and rep["years_at_this_rate"] < 5
    size = 122_880_000 * 512
    assert rep["budget_bytes"] == int(size * 1000 / 3 / (365 * 5))  # 62.9 GB × 333 full writes over 5 years
    assert "SD card (data disk)" in wear.describe(rep) and "lasts about" in wear.describe(rep)
    assert wear.sd_summary(db, config, now)["device"] == "mmcblk0p2"  # type: ignore[index]
    # a reboot resets the device counter: the new value counts from zero instead of going negative
    set_written(stat, 10)
    m2 = wear.WearMeter(config, db, sys_root=tmp_path / "sys", proc_io=io, mounts=tmp_path / "mounts")
    assert m2.sample(now + wear.SAMPLE_EVERY)
    last = db.scalar("SELECT bytes FROM disk_writes WHERE device = 'mmcblk0p2' ORDER BY at DESC LIMIT 1")
    assert last == 10 * 512
    assert wear.gb(5e7) == "50 MB" and wear.gb(2.5e9) == "2.5 GB"


def test_report_for_a_quiet_card_and_non_sd_disks(config, db):
    now = 2_000_000.0
    for i in range(24):
        db.execute("INSERT INTO disk_writes VALUES(?, 'mmcblk0p2', 'system', 'sd', ?, ?, 0)",
                   (now - i * 3600, 64 * GB, 50_000_000))  # fmt: skip
        db.execute("INSERT INTO disk_writes VALUES(?, 'sda1', 'data', 'ssd', ?, ?, ?)",
                   (now - i * 3600, 1000 * GB, 2 * GB, GB))  # fmt: skip
    rows = {r["device"]: r for r in wear.report(db, config, now)}
    sd, ssd = rows["mmcblk0p2"], rows["sda1"]
    assert sd["status"] == "ok" and sd["years_at_this_rate"] > 40  # 1.2 GB a day
    assert "budget_bytes" not in ssd and wear.describe(ssd).startswith("SSD sda1 (data disk)")
    assert wear.sd_summary(db, config, now)["role"] == "system"  # type: ignore[index]
    db.execute("DELETE FROM disk_writes WHERE device = 'mmcblk0p2'")
    assert wear.sd_summary(db, config, now) is None
    db.execute("INSERT INTO disk_writes VALUES(?, 'mmcblk0p1', 'system', 'sd', ?, 0, 0)", (now, 64 * GB))
    assert wear.sd_summary(db, config, now)["years_at_this_rate"] is None  # type: ignore[index]


# ------------------------------------------------------------------ the guard
def test_guard_saver_mode_feed_note_and_read_only_pause(config, db, tmp_path):
    m, stat, _io = meter(config, db, tmp_path, sectors_written=0)
    clock = [3_000_000.0]
    g = Guard(config, db, clock=lambda: clock[0], sensors=lambda: Vitals(50.0, 0.5, 4, 1000.0, 500.0, 10.0))
    g.wear = m
    st = g.observe()
    assert st.mode == "normal" and st.slice_factor == 1.0 and not g.saver
    for i in range(1, 100):  # 1 GB every 15 minutes: far over a 62.9 GB card's budget
        clock[0] += wear.SAMPLE_EVERY
        set_written(stat, i * (GB // 512))
        st = g.observe()
    assert g.saver and st.slice_factor == 2.0 and any("saving SD card writes" in r for r in st.reasons)
    notes = db.query("SELECT text, detail FROM events WHERE kind = 'wear'")
    assert len(notes) == 1 and "plug in a USB drive" in notes[0]["text"]  # once a day, not every sample
    (tmp_path / "mounts").write_text(f"/dev/mmcblk0p2 {config.paths.data_dir} ext4 ro 0 0\n")
    st = g.observe()
    assert st.mode == "pause" and "read-only" in st.reasons[0]
    assert db.scalar("SELECT COUNT(*) FROM events WHERE kind = 'wear' AND detail LIKE '%read-only%'") == 1
    g.observe()
    assert db.scalar("SELECT COUNT(*) FROM events WHERE kind = 'wear' AND detail LIKE '%read-only%'") == 1


def test_guard_on_a_system_sd_card_only_explains(config, db, tmp_path):
    g = Guard(config, db, sensors=lambda: Vitals(50.0, 0.5, 4, 1000.0, 500.0, 10.0))
    g.wear.sample = lambda now, force=False: True  # type: ignore[method-assign]
    now = time.time()
    for i in range(30):
        db.execute("INSERT INTO disk_writes VALUES(?, 'mmcblk0p2', 'system', 'sd', ?, ?, 0)",
                   (now - i * 3600, 8 * GB, 5 * GB))  # fmt: skip
    st = g.observe()
    assert not g.saver and st.slice_factor == 1.0  # the brain is on a drive: the OS's writes are not ours to save
    assert "the OS writes this much" in db.scalar("SELECT text FROM events WHERE kind = 'wear'")


# ------------------------------------------------------------------ the loop writes less
def test_idle_cycles_write_the_heartbeat_only_now_and_then(config, db):
    from polymath.body.systemd_notify import Notifier
    from polymath.core.app import build_components

    agent = Agent(config, db, build_components(config, db).registry, notifier=Notifier({}))
    agent.start()
    agent.cycle()
    first = db.kv_get("heartbeat")
    assert first["state"] == "idle"
    agent.cycle()
    assert db.kv_get("heartbeat")["ts"] == first["ts"]  # unchanged: no commit for an idle cycle
    agent._quiet_at -= 31
    agent.cycle()
    assert db.kv_get("heartbeat")["ts"] > first["ts"]
    agent.scheduler.enqueue("noop", {})
    agent.cycle()
    assert db.kv_get("heartbeat")["state"] == "running"
    agent.cycle()
    assert db.kv_get("heartbeat")["state"] == "idle"  # the change is written at once


def test_saver_doubles_slices_within_the_watchdog(config, db):
    from polymath.body.systemd_notify import Notifier
    from polymath.core.jobs import JobOutcome, JobRegistry
    from polymath.core.loop import BodyState

    seen = []
    reg = JobRegistry()
    reg.register("probe", lambda ctx: (seen.append(ctx.deadline - time.monotonic()), JobOutcome(done=True))[1], "probe")

    class SaverBody:
        def observe(self):
            return BodyState(slice_factor=2.0)

    config.loop.job_time_budget = 40
    agent = Agent(config, db, reg, body=SaverBody(), notifier=Notifier({}))
    agent.start()
    agent.scheduler.enqueue("probe", {})
    agent.cycle()
    assert MAX_SLICE - 1 < seen[0] <= MAX_SLICE  # 80 s asked, capped at 60 s


# ------------------------------------------------------------------ moving the brain onto a drive
@pytest.fixture()
def home_helper(config, db, tmp_path):
    config.storage.mount_root = tmp_path / "mnt"
    config.storage.reserve_fraction = 0.05
    config.storage.home_min_gb = 0.000001
    fake = FakeSystem(tmp_path)
    blank = {"/dev/sdb": (b"\0" * 64, b"\0" * 64)}
    h = vols.Helper(config, fake, edges=lambda dev: blank.get(dev, (b"data", b"")), owner=(os.getuid(), os.getgid()),
                    mountinfo=tmp_path / "mountinfo", lock=tmp_path / "lock")  # fmt: skip
    (config.paths.data_dir / "raw").mkdir(parents=True, exist_ok=True)
    (config.paths.data_dir / "raw" / "dump.bz2").write_bytes(b"x" * 5000)
    db.execute("INSERT INTO events(at, kind, text) VALUES(1, 'site', 'before the move')")
    return h, fake


def test_a_new_ssd_becomes_the_brains_home(home_helper, config, db, tmp_path):
    h, fake = home_helper
    fake.add("/dev/sdb")  # a brand-new 1 TB SSD: blank, formatted for Polymath, dedicated
    out = h.attach("/dev/sdb")
    assert out.action == "moved" and "the SD card only boots the Pi" in out.detail, out
    data = config.paths.data_dir
    brain = config.storage.mount_root / "new-uuid-1" / storage_pool.BRAIN
    home = brain / vols.HOME
    assert sorted(p.name for p in data.iterdir()) == [MOVED_FILE]  # the SD card's directory only holds the marker
    aside = data.with_name("data-sd-copy")
    assert (aside / "raw" / "dump.bz2").exists() and (aside / "db").exists()  # kept as a safety net
    assert (home / "raw" / "dump.bz2").read_bytes() == b"x" * 5000
    info = json.loads((home / HOME_FILE).read_text())
    assert info["id"] == "new-uuid-1" and info["budget_gb"] > 0 and info["from"] == str(data)
    manifest = storage_pool.read_manifest(brain)
    assert manifest is not None and manifest["home"] is True and manifest["budget_bytes"] == 0
    copy = sqlite3.connect(str(home / "db" / "polymath.sqlite3"))
    texts = [r[0] for r in copy.execute("SELECT text FROM events ORDER BY id")]
    copy.close()
    assert texts[0] == "before the move" and "moved the brain onto" in texts[-1]
    calls = [c for c in fake.calls if c[0] in {"systemctl", "mount"}]
    assert next(c for c in calls if c[0] == "systemctl") == ["systemctl", "stop", *vols.SERVICES]
    assert ["mount", "--bind", str(home), str(data)] in calls
    assert calls[-1] == ["systemctl", "start", *vols.SERVICES]
    with pytest.raises(StorageError, match="Plug it in"):
        check_storage(config)  # without the drive, the agent waits instead of using the stale SD copy
    cfg2 = load_config(tmp_path / "polymath.toml", env={"POLYMATH_DATA_DIR": str(home)})
    assert cfg2.body.disk_budget_gb == info["budget_gb"]  # the drive's room, not the SD card's
    # the next boot: the drive is mounted again and so is the brain
    fake.calls.clear()
    again = h.attach("/dev/sdb1")
    assert again.action == "home" and ["mount", "--bind", str(home), str(data)] in fake.calls
    assert fake.calls[-1] == ["systemctl", "start", *vols.SERVICES]
    (tmp_path / "mountinfo").write_text(
        f"40 25 8:17 /polymath-brain/home {data} rw - ext4 /dev/sdb1 rw\n"
        f"36 25 8:17 / {config.storage.mount_root / 'new-uuid-1'} rw - ext4 /dev/sdb1 rw\n"
    )
    assert "already mounted" in h.attach("/dev/sdb1").detail
    assert h.eject("new-uuid-1").action == "skipped"  # it holds the brain while the agent runs
    fake.calls.clear()
    out = h.detach("/dev/sdb1")  # pulled out: stop the agent, release both mounts
    assert out.action == "detached" and fake.calls[0] == ["systemctl", "stop", *vols.SERVICES]
    assert ["umount", "-l", str(data)] in fake.calls


def test_failed_copy_leaves_the_brain_on_the_sd_card(home_helper, config, monkeypatch):
    h, fake = home_helper
    real = vols.tree_stats
    calls = iter([None, (1, 1)])

    def lying(root):
        nxt = next(calls, None)
        return nxt if nxt is not None else real(root)

    monkeypatch.setattr(vols, "tree_stats", lying)
    fake.add("/dev/sdb")
    out = h.attach("/dev/sdb")
    assert out.action == "attached" and "moving the brain onto it failed" in out.detail and "copy differs" in out.detail
    assert not (config.storage.mount_root / "new-uuid-1" / storage_pool.BRAIN / vols.HOME).exists()
    assert not (config.paths.data_dir / MOVED_FILE).exists() and (config.paths.data_dir / "raw").exists()
    assert fake.ran("systemctl")[-1] == ["systemctl", "start", *vols.SERVICES]


def test_a_bad_database_copy_is_refused(home_helper, config, monkeypatch):
    h, fake = home_helper

    class Broken:
        def __init__(self, *a, **k):
            pass

        def execute(self, sql):
            class R:
                def fetchone(self):
                    return ["*** in database main ***"]

            return R()

        def close(self):
            pass

    monkeypatch.setattr(sqlite3, "connect", Broken)
    fake.add("/dev/sdb")
    out = h.attach("/dev/sdb")
    assert "database check failed" in out.detail and not (config.paths.data_dir / MOVED_FILE).exists()


def test_when_the_brain_does_not_move(home_helper, config, monkeypatch):
    h, fake = home_helper
    fake.add("/dev/sda1", disk="sda", probe={"TYPE": "exfat", "UUID": "EX-1"})  # an empty exFAT SSD
    out = h.attach("/dev/sda1")
    assert out.action == "attached"  # used for documents, but never the brain's home (no Linux permissions)
    fake.add("/dev/sdc1", disk="sdc", probe={"TYPE": "ext4", "UUID": "shared-1"})
    (config.storage.mount_root / "shared-1" / "Photos").mkdir(parents=True)
    assert h.attach("/dev/sdc1").action == "attached"  # a shared drive is never taken over
    config.storage.home_min_gb = 10**9
    fake.add("/dev/sdd1", disk="sdd", probe={"TYPE": "ext4", "UUID": "small-1", "LABEL": "POLYMATH"})
    assert h.attach("/dev/sdd1").action == "attached"  # not enough room
    config.storage.home_min_gb = 0.000001
    config.storage.move_home = False
    fake.add("/dev/sde1", disk="sde", probe={"TYPE": "ext4", "UUID": "off-1", "LABEL": "POLYMATH"})
    assert h.attach("/dev/sde1").action == "attached"
    config.storage.move_home = True
    monkeypatch.setattr(h, "data_on_system_disk", lambda: False)  # an NVMe already holds the brain
    fake.add("/dev/sdf1", disk="sdf", probe={"TYPE": "ext4", "UUID": "nvme-1", "LABEL": "POLYMATH"})
    assert h.attach("/dev/sdf1").action == "attached"
    assert not (config.paths.data_dir / MOVED_FILE).exists()
    brain = config.storage.mount_root / "x" / storage_pool.BRAIN
    brain.mkdir(parents=True)
    out = h.mount_home(brain, {"home": True}, vols.Outcome("attached", "d", "x"))
    assert "is missing" in out.detail


def test_data_on_system_disk(home_helper, config):
    h, fake = home_helper
    assert h.data_on_system_disk()
    real = fake.__call__

    def nvme(args, *, input=None):
        if args[0] == "findmnt" and args[-1] == str(config.paths.data_dir):
            return subprocess.CompletedProcess(args, 0, "/dev/nvme0n1p1", "")
        return real(args, input=input)

    h.run = nvme
    fake.add("/dev/nvme0n1p1", disk="nvme0n1")
    fake.add("/dev/nvme0n1")
    assert not h.data_on_system_disk()
    h.run = lambda args, input=None: subprocess.CompletedProcess(args, 1, "", "")
    assert not h.data_on_system_disk()


def test_format_an_empty_windows_drive_on_request(home_helper, config, tmp_path):
    h, fake = home_helper
    fake.add("/dev/sda1", disk="sda", probe={"TYPE": "exfat", "UUID": "EX-2"})
    mnt = config.storage.mount_root / "EX-2"
    assert h.attach("/dev/sda1").action == "attached"
    (mnt / "System Volume Information").mkdir()  # Windows housekeeping only: still empty
    info = tmp_path / "mountinfo"
    info.write_text(f"36 25 8:1 / {mnt} rw - exfat /dev/sda1 rw\n")
    real = fake.__call__

    def system(args, *, input=None):
        if args[0] == "umount":
            info.write_text("")
            fake.mounts["/dev/sda1"] = []
        if args[0] == "mkfs.ext4":
            fake.calls.append(args)
            fake.devices["/dev/sda1"]["probe"] = {"TYPE": "ext4", "UUID": "fresh-1", "LABEL": "POLYMATH"}
            return subprocess.CompletedProcess(args, 0, "", "")
        return real(args, input=input)

    h.run = system
    out = h.format_empty("EX-2")
    assert out.action == "moved" and fake.ran("mkfs.ext4")[0][-1] == "/dev/sda1"  # then it became the home
    assert h.format_empty("EX-2").action == "skipped"  # no longer plugged in under that id


def test_format_refuses_drives_with_files_the_home_and_busy_mounts(home_helper, config, tmp_path):
    h, fake = home_helper
    fake.add("/dev/sda1", disk="sda", probe={"TYPE": "exfat", "UUID": "EX-3"})
    mnt = config.storage.mount_root / "EX-3"
    (mnt / "Holiday.mov").parent.mkdir(parents=True, exist_ok=True)
    (mnt / "Holiday.mov").write_bytes(b"film")
    h.attach("/dev/sda1")
    (tmp_path / "mountinfo").write_text(f"36 25 8:1 / {mnt} rw - exfat /dev/sda1 rw\n")
    out = h.format_empty("EX-3")
    assert out.action == "skipped" and "Holiday.mov" in out.detail and "--erase-files" in out.detail
    assert not fake.ran("mkfs.ext4")
    assert "busy" in h.format_empty("EX-3", erase_files=True).detail  # umount did not release it: nothing changed
    (mnt / "Holiday.mov").unlink()
    (mnt / storage_pool.BRAIN / "packs").mkdir()
    assert "retire the drive first" in h.format_empty("EX-3", erase_files=True).detail
    (mnt / storage_pool.BRAIN / "packs").rmdir()
    manifest = storage_pool.read_manifest(mnt / storage_pool.BRAIN)
    assert manifest is not None
    manifest["home"] = True
    (mnt / storage_pool.BRAIN / storage_pool.MANIFEST).write_text(json.dumps(manifest))
    assert "never reformatted" in h.format_empty("EX-3").detail
    real = fake.__call__

    def failing(args, *, input=None):
        if args[0] == "umount":
            (tmp_path / "mountinfo").write_text("")
        if args[0] == "mkfs.ext4":
            return subprocess.CompletedProcess(args, 1, "", "boom")
        return real(args, input=input)

    manifest["home"] = False
    (mnt / storage_pool.BRAIN / storage_pool.MANIFEST).write_text(json.dumps(manifest))
    h.run = failing
    assert "formatting /dev/sda1 failed: boom" in h.format_empty("EX-3").detail


def test_mounts_at_and_eject_when_stopped(home_helper, config, tmp_path):
    h, fake = home_helper
    assert h.mounts_at(config.paths.data_dir) == []  # no mountinfo
    brain = config.storage.mount_root / "v9" / storage_pool.BRAIN
    brain.mkdir(parents=True)
    (brain / storage_pool.MANIFEST).write_text(json.dumps({"id": "v9", "home": True}))
    (tmp_path / "mountinfo").write_text(f"36 25 8:1 / {brain.parent} rw - ext4 /dev/sdk1 rw\nbad\n")
    real = fake.__call__
    h.run = lambda args, input=None: (subprocess.CompletedProcess(args, 3, "", "") if args[:2] == ["systemctl",
                                      "is-active"] else real(args, input=input))  # fmt: skip
    assert h.eject("v9").action == "ejected"  # the agent is stopped: the brain's drive may be unplugged


# ------------------------------------------------------------------ config, CLI, feed
def test_apply_home_and_config_validation(config, tmp_path):
    config.paths.data_dir.mkdir(parents=True, exist_ok=True)
    (config.paths.data_dir / HOME_FILE).write_text(json.dumps({"budget_gb": 850.5}))
    apply_home(config)
    assert config.body.disk_budget_gb == 850.5
    (config.paths.data_dir / HOME_FILE).write_text("not json")
    apply_home(config)
    assert config.body.disk_budget_gb == 850.5
    for key, value in (("sd_endurance_cycles", 0), ("sd_target_years", 0), ("wal_autocheckpoint", 10)):
        cfg = tmp_path / f"{key}.toml"
        cfg.write_text(f"[body]\n{key} = {value}\n")
        with pytest.raises(ConfigError):
            load_config(cfg)


def test_cli_wear_and_storage_format(config, db, tmp_path, capsys, monkeypatch):
    path = str(tmp_path / "polymath.toml")
    assert cli.main(["--config", path, "wear"]) == 0
    assert "no measurements yet" in capsys.readouterr().out
    now = time.time()
    for i in range(30):
        db.execute("INSERT INTO disk_writes VALUES(?, 'mmcblk0p2', 'data', 'sd', ?, ?, ?)",
                   (now - i * 3600, 64 * GB, 3 * GB, 2 * GB))  # fmt: skip
    assert cli.main(["--config", path, "wear"]) == 0
    out = capsys.readouterr().out
    assert "SD card (data disk)" in out and "over budget: plug in a USB drive" in out and "Polymath:" in out
    (config.paths.data_dir / HOME_FILE).write_text(json.dumps({"budget_gb": 900, "name": "T7"}))
    assert cli.main(["--config", path, "wear", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["brain_on_drive"] is True and data["devices"][0]["status"] in {"high", "critical"}
    assert cli.main(["--config", path, "wear"]) == 0
    assert "the brain lives on a drive" in capsys.readouterr().out
    assert cli.main(["--config", path, "storage", "list"]) == 0
    assert "main disk: drive T7" in capsys.readouterr().out
    assert cli.main(["--config", path, "storage", "format", "EX-1"]) == 2
    assert "add --yes" in capsys.readouterr().err
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert cli.main(["--config", path, "storage", "format", "EX-1", "--yes"]) == 2
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(vols.Helper, "format_empty", lambda self, v, erase_files: vols.Outcome("skipped", "x", v))
    assert cli.main(["--config", path, "storage", "format", "EX-1", "--yes"]) == 1


def test_feed_and_face_show_wear_and_home_notes():
    base = {"kind": "note", "at": time.time()}
    line = text_of(render_event(base | {"what": "wear", "status": "high", "text": "SD card: 14 GB"}))
    assert "SD card" in line and "14 GB" in line
    seg = render_event(base | {"what": "wear", "status": "read-only", "text": "read-only"})
    assert any(style == "red" for _t, style in seg)
    assert "storage" in text_of(render_event(base | {"what": "home", "status": "moved", "text": "moved"}))
    f = Face()
    f.update({"online": True, "state": "running", "mode": "normal", "action": "wikipedia.part"})
    f.see(base | {"what": "home"}, 10.0)
    assert f.mood(10.0) == "celebrate"
    g = Face()
    g.update({"online": True, "state": "running", "mode": "normal", "action": "wikipedia.part"})
    g.see(base | {"what": "wear", "status": "read-only"}, 10.0)
    assert g.mood(10.0) == "error"


def test_digest_reports_the_sd_card(config, db):
    from polymath.evaluation import digest

    now = time.time()
    for i in range(30):
        db.execute("INSERT INTO disk_writes VALUES(?, 'mmcblk0p2', 'data', 'sd', ?, ?, ?)",
                   (now - i * 3600, 64 * GB, 3 * GB, 2 * GB))  # fmt: skip
    lines = digest.lines(digest.collect(db, now - 86400, now, config))
    assert any(line.startswith("SD card (data disk)") and "over budget" in line for line in lines)
    assert not any("SD card" in line for line in digest.lines(digest.collect(db, now - 86400, now)))


def test_a_windows_formatted_ssd_gets_clear_instructions(home_helper, config, db):
    h, fake = home_helper
    config.storage.home_min_gb = 0.000001
    fake.add("/dev/sda1", disk="sda", probe={"TYPE": "exfat", "UUID": "AB12-CD34", "LABEL": "T7"})
    out = h.attach("/dev/sda1")
    assert "erased!): sudo polymath storage format AB12-CD34 --yes" in out.detail
    brain = config.storage.mount_root / "AB12-CD34" / storage_pool.BRAIN
    assert "storage format AB12-CD34" in storage_pool.read_manifest(brain)["home_hint"]  # type: ignore[index]
    pool = storage_pool.StoragePool(config, ismount=lambda p: True)
    storage_pool.sync(db, pool)
    from polymath.interface.feed import Feed

    evs = [e for e in Feed(db).poll()["events"] if e["kind"] == "storage"]
    lines = [text_of(render_event(e)) for e in evs]
    assert any("new drive T7" in x for x in lines) and any("storage format AB12-CD34" in x for x in lines)
    fake.add("/dev/sdc1", disk="sdc", probe={"TYPE": "ext4", "UUID": "files-1"})
    (config.storage.mount_root / "files-1" / "Music").mkdir(parents=True)
    assert "empty it and plug it in again" in h.attach("/dev/sdc1").detail
    config.storage.home_min_gb = 10**9
    fake.add("/dev/sdd1", disk="sdd", probe={"TYPE": "exfat", "UUID": "TINY-1"})
    assert "storage format" not in h.attach("/dev/sdd1").detail  # too small to be the home: no advice
    home_brain = config.storage.mount_root / "z" / storage_pool.BRAIN
    home_brain.mkdir(parents=True)
    pool2 = storage_pool.StoragePool(config, ismount=lambda p: True)
    (home_brain / storage_pool.MANIFEST).write_text(json.dumps({"id": "z", "home": True, "fstype": "ext4"}))
    storage_pool.sync(db, pool2)
    added = [e for e in Feed(db).poll()["events"] if e["kind"] == "storage" and e["event"] == "added"]
    assert any("the whole brain lives on it" in text_of(render_event(e)) for e in added)
