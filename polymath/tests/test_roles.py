"""v0.11.0: drive roles: Polymath decides which drive holds the brain, which keeps the backups, which are extra."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from polymath.body import roles as rl
from polymath.body import volumes as vols
from polymath.core.config import HOME_FILE, MOVED_FILE, ConfigError
from polymath.memory import pool as storage_pool
from tests.test_storage import GB, Drives, FakeSystem

NIGHT = time.mktime((2026, 10, 27, 5, 0, 0, 0, 0, -1))  # 05:00 local the next day: after the 03:00 backup
DAY = time.mktime((2026, 10, 26, 14, 0, 0, 0, 0, -1))


class Clock:
    def __init__(self, t: float = DAY) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeHelper:
    """What the roles need from the drive helper: where the brain is mounted, and moving it (recorded)."""

    def __init__(self, data: Path) -> None:
        self.data = data
        self.mounted = False
        self.on_sd = True
        self.moves: list[tuple[str, ...]] = []
        self.result = "moved"
        self.manifests: dict[str, dict[str, Any]] = {}

    def mounts_at(self, path: Path) -> list[tuple[str, str]]:
        return [(str(path), "/dev/sda2")] if self.mounted and path == self.data else []

    def data_on_system_disk(self) -> bool:
        return self.on_sd

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        yield

    def move_home(self, brain: Path, manifest: dict[str, Any], out: Any) -> vols.Outcome:
        self.moves.append(("home", manifest["id"]))
        if self.result == "moved":
            manifest["home"] = True
            (brain / storage_pool.MANIFEST).write_text(json.dumps(manifest))
            self.mounted = True
        return vols.Outcome(self.result, f"{self.result}: {manifest['id']}", manifest["id"])

    def move_brain(self, src: Path, sm: dict[str, Any], dst: Path, dm: dict[str, Any]) -> vols.Outcome:
        self.moves.append(("between", sm["id"], dm["id"]))
        if self.result == "moved":
            sm["home"], dm["home"], sm["budget_bytes"] = False, True, 2 * GB
            (src / storage_pool.MANIFEST).write_text(json.dumps(sm))
            (dst / storage_pool.MANIFEST).write_text(json.dumps(dm))
        return vols.Outcome(self.result, f"{self.result}: {dm['id']}", dm["id"])

    def _write_manifest(self, brain: Path, manifest: dict[str, Any]) -> None:
        (brain / storage_pool.MANIFEST).write_text(json.dumps(manifest))

    def format_empty(self, vol: str, *, erase_files: bool = False, by: str = "") -> vols.Outcome:
        self.moves.append(("format", vol, by))
        return vols.Outcome("attached", f"{vol} formatted ext4", vol)


# what lsblk and sysfs would say about each drive
SSD = {"disk": "sda", "tran": "usb", "rotational": False, "speed": 5000, "power_ma": 896, "usb": "/sys/u/2-1"}
HDD = {"disk": "sdb", "tran": "usb", "rotational": True, "speed": 5000, "power_ma": 896, "usb": "/sys/u/2-2"}
STICK = {"disk": "sdc", "tran": "usb", "rotational": False, "speed": 480, "power_ma": 200, "usb": "/sys/u/1-1.3"}
NVME = {"disk": "nvme0n1", "tran": "nvme", "rotational": False, "speed": 0, "power_ma": 0, "usb": ""}


class World:
    def __init__(self, config: Any, tmp: Path) -> None:
        self.config = config
        self.drives = Drives(config)
        config.paths.data_dir.mkdir(parents=True, exist_ok=True)
        config.paths.run_dir = tmp / "run"
        config.paths.run_dir.mkdir()
        config.storage.home_min_gb = 0.000001
        self.helper = FakeHelper(config.paths.data_dir)
        self.clock = Clock()
        self.facts: dict[str, dict[str, Any]] = {}
        self.probes: list[str] = []
        self.probe_result: tuple[bool, float, str] = (True, 300.0, "")
        self.heal: list[dict[str, Any]] = []
        self.state = tmp / "drive-roles.json"

    def plug(self, vol: str, facts: dict[str, Any], **manifest: Any) -> Path:
        dev = f"/dev/{facts['disk']}1"
        self.facts[dev] = facts
        return self.drives.plug(vol, device=dev, **manifest)

    def roles(self) -> rl.DriveRoles:
        pool = storage_pool.StoragePool(self.config, ismount=lambda p: p in self.drives.mounted)
        return rl.DriveRoles(self.config, self.helper, pool=pool, facts=lambda d: self.facts.get(d, {}),
                             probe=self.probe, heal_actions=lambda: self.heal, state_path=self.state,
                             clock=self.clock)  # fmt: skip

    def probe(self, dev: str) -> tuple[bool, float, str]:
        self.probes.append(dev)
        return self.probe_result

    def tick(self, minutes: float = 1.0) -> dict[str, Any]:
        self.clock.t += minutes * 60
        return self.roles().tick()

    def public(self) -> dict[str, Any]:
        return rl.read_public(self.config) or {}

    def role(self, vol: str) -> str:
        return next(str(d["role"]) for d in self.public()["drives"] if d["id"] == vol)


@pytest.fixture()
def w(config: Any, tmp_path: Path) -> World:
    return World(config, tmp_path)


# ------------------------------------------------------------------ what makes a good brain drive
def test_scores_follow_the_connection_the_kind_the_speed_and_the_drop_outs():
    def drive(**kw: Any) -> rl.Drive:
        return rl.Drive(id="x", name="x", brain=Path("/x"), fstype="ext4", dedicated=True, home=False, **kw)

    nvme = drive(tran="nvme", rotational=False, free=500 * GB)
    usb3_ssd = drive(tran="usb", speed=5000, rotational=False, free=500 * GB)
    usb3_hdd = drive(tran="usb", speed=5000, rotational=True, free=2000 * GB)
    usb2_ssd = drive(tran="usb", speed=480, rotational=False, free=500 * GB)
    scores = [rl.brain_score(d) for d in (nvme, usb3_ssd, usb3_hdd, usb2_ssd)]
    assert scores == sorted(scores, reverse=True), scores
    assert rl.brain_score(usb3_ssd) - rl.brain_score(usb3_hdd) >= rl.MOVE_MARGIN - 0.5  # an SSD is a class better
    fast = drive(tran="usb", speed=5000, rotational=False, free=500 * GB, mbps=400.0)
    assert rl.brain_score(fast) == pytest.approx(rl.brain_score(usb3_ssd) + 2.0)
    flaky = drive(tran="usb", speed=5000, rotational=False, free=500 * GB, drops=2)
    assert rl.brain_score(flaky) == pytest.approx(rl.brain_score(usb3_ssd) - 2.0)
    assert usb3_ssd.kind() == "USB 3 SSD" and usb3_hdd.kind() == "USB 3 hard disk" and nvme.kind() == "NVMe SSD"
    assert usb2_ssd.kind() == "USB 2 SSD" and drive(tran="usb").kind() == "USB drive" and drive().kind() == "drive"
    assert drive(tran="sata", rotational=True).kind() == "SATA hard disk"


def test_what_can_never_hold_the_brain(w):
    w.plug("ssd", SSD)
    w.plug("exfat", HDD, fstype="exfat")
    w.plug("shared", {**HDD, "disk": "sdd"}, dedicated=False)
    w.plug("stick", STICK)
    w.tick()
    w.tick(11)
    why = {d["id"]: d["why_not"] for d in w.public()["drives"]}
    assert why["ssd"] == "" and "Windows/macOS" in why["exfat"] and "your files" in why["shared"]
    assert "USB 2" in why["stick"]
    w.config.storage.brain_allow_usb2 = True
    w.helper.on_sd = False  # (nothing to move: only the reasons)
    w.tick()
    assert {d["id"]: d["why_not"] for d in w.public()["drives"]}["stick"] == ""


# ------------------------------------------------------------------ off the SD card: the best drive, not the first
def test_with_several_drives_plugged_in_the_best_becomes_the_brain_and_another_keeps_the_backups(w):
    w.plug("stick", STICK, label="STICK", budget=1 * GB)
    w.plug("hdd", HDD, label="BIG HDD", budget=4 * GB)
    w.plug("ssd", SSD, label="T7", budget=3 * GB)
    out = w.tick()
    assert w.helper.moves == [] and "becomes its home once it has settled" in out["decision"]
    w.tick(5)
    assert w.helper.moves == []  # still settling: waiting is what lets it pick the best, not the first
    out = w.tick(6)
    assert w.helper.moves == [("home", "ssd")] and out["moved"]
    events = [e["event"] for e in w.public()["events"]]
    assert events.index("moving") + 1 == events.index("moved") and "backups" in events
    assert w.role("ssd") == "brain" and w.role("hdd") == "backup" and w.role("stick") == "extra"
    out = w.tick()  # the next minute, from where the brain is now
    assert "the brain is on T7 (USB 3 SSD), the best drive plugged in" in out["decision"]
    assert w.public()["backup"] == "hdd" and w.public()["extra"] == ["stick"]
    assert "/dev/sda1" in w.probes  # every drive that could hold the brain gets its read speed measured


def test_the_brain_stays_on_the_sd_card_when_moving_it_is_switched_off(w):
    w.config.storage.move_home = False
    w.plug("ssd", SSD, label="T7")
    w.tick()
    w.tick(11)
    assert w.helper.moves == [] and w.role("ssd") == "backup"  # still used: for the backups


def test_no_drive_fit_for_the_brain_says_what_to_plug_in(w):
    w.plug("stick", STICK)
    out = w.tick(20)
    assert w.helper.moves == [] and "plug in an SSD of Polymath's own on a blue USB 3 port" in out["decision"]


# ------------------------------------------------------------------ onto a better drive, at night
def test_a_better_drive_takes_the_brain_at_night_and_only_once_a_day(w):
    w.helper.on_sd, w.helper.mounted = False, True
    w.plug("hdd", HDD, label="HDD", home=True, budget=0)
    w.plug("ssd", SSD, label="SSD", budget=900 * GB)
    w.tick()
    out = w.tick(11)
    assert w.helper.moves == [] and "moves onto SSD" in out["decision"] and "tonight, after 04:00" in out["decision"]
    w.clock.t = NIGHT
    out = w.tick()
    assert w.helper.moves == [("between", "hdd", "ssd")] and out["moved"]
    w.tick()
    assert w.role("ssd") == "brain" and w.role("hdd") == "backup"  # the old brain drive keeps the backups now
    w.plug("nvme", NVME, label="NVME", budget=900 * GB)  # an even better one, the same night
    w.tick()
    w.tick(11)
    assert len(w.helper.moves) == 1  # at most one move between drives a day
    w.clock.t = NIGHT + 86400
    w.tick()
    assert w.helper.moves[-1] == ("between", "ssd", "nvme")


def test_a_drive_that_keeps_dropping_out_loses_the_brain_at_once(w):
    w.helper.on_sd, w.helper.mounted = False, True
    w.plug("ssd1", SSD, label="OLD", home=True, budget=0)
    w.plug("ssd2", {**SSD, "disk": "sdb", "usb": "/sys/u/2-2"}, label="NEW", budget=900 * GB)
    w.tick()
    w.tick(11)
    assert w.helper.moves == []  # as good as the old one: no reason to move
    now = w.clock.t
    w.heal = [{"at": now - 3600 * h, "action": a} for h, a in ((1, "dead_mount"), (2, "usb_reset"), (3, "usb_power"))]
    out = w.tick()  # daytime, but the brain's drive is failing
    assert w.helper.moves == [("between", "ssd1", "ssd2")] and out["moved"]
    assert "keeps dropping out" in w.public()["events"][-3]["detail"]


def test_a_failed_move_waits_six_hours(w):
    w.helper.result = "skipped"
    w.plug("ssd", SSD, label="T7")
    w.tick()
    w.tick(11)
    assert w.helper.moves == [("home", "ssd")] and w.public()["events"][-1]["event"] == "move_failed"
    w.tick(60)
    assert len(w.helper.moves) == 1
    w.tick(6 * 60)
    assert len(w.helper.moves) == 2


def test_nothing_moves_while_the_brain_is_missing_on_its_own_disk_or_in_sd_card_mode(w):
    w.helper.on_sd = False
    (w.config.paths.data_dir / MOVED_FILE).write_text("{}")  # the brain's drive is away
    w.plug("ssd", SSD)
    assert "not there" in w.tick(20)["decision"] and w.helper.moves == []
    (w.config.paths.data_dir / MOVED_FILE).unlink()
    assert w.tick()["decision"] == "the brain lives on its own disk" and w.helper.moves == []
    w.config.storage.sd_only = True
    assert w.tick() == {"roles": "sd-card"}
    w.config.storage.sd_only, w.config.storage.auto_roles = False, False
    assert w.tick() == {"roles": "off"}


# ------------------------------------------------------------------ what it remembers about each drive
def test_drop_outs_are_counted_and_a_replugged_drive_settles_again(w):
    w.helper.on_sd, w.helper.mounted = False, True
    w.plug("home", SSD, home=True, budget=0)
    w.plug("hdd", HDD, budget=900 * GB)
    w.tick(30)
    for _ in range(2):
        w.drives.unplug("hdd")
        w.tick()
        w.drives.plug("hdd", device="/dev/sdb1", budget=900 * GB)
        w.tick()
    row = next(d for d in w.public()["drives"] if d["id"] == "hdd")
    assert row["drops"] == 2 and row["plugged_min"] < 2
    state = json.loads(w.state.read_text())
    assert len(state["seen"]["hdd"]["drops"]) == 2
    w.clock.t += rl.FLAKY_WINDOW_S + 60
    w.roles().tick()
    assert next(d for d in w.public()["drives"] if d["id"] == "hdd")["drops"] == 0  # a week later, forgotten


def test_read_speed_is_measured_once_a_day_and_a_failed_read_counts(w):
    w.plug("ssd", SSD)
    w.plug("hdd", HDD)
    w.tick()
    w.tick()
    w.tick()
    assert sorted(w.probes) == ["/dev/sda1", "/dev/sdb1"]  # one a run, each once
    assert {d["id"]: d["mbps"] for d in w.public()["drives"]} == {"ssd": 300.0, "hdd": 300.0}
    w.probe_result = (False, 0.0, "it did not answer within 30 s (frozen)")
    w.tick(60 * 24)
    failed = [e for e in w.public()["events"] if e["event"] == "read_failed"]
    assert failed and "frozen" in failed[-1]["detail"]


def test_it_warns_about_power_and_about_backups_on_the_brains_own_drive(w):
    w.helper.on_sd, w.helper.mounted = False, True
    w.plug("ssd", SSD, home=True, budget=0)
    w.tick()
    assert any("plug in a second drive" in x for x in w.public()["warnings"])
    w.plug("hdd", HDD, budget=900 * GB)
    w.tick()
    warnings = w.public()["warnings"]
    assert any("1792 mA" in x and "powered USB 3 hub" in x for x in warnings)
    assert not any("second drive" in x for x in warnings)


def test_the_old_brain_copy_goes_after_a_steady_week(w):
    w.helper.on_sd, w.helper.mounted = False, True
    w.plug("ssd", SSD, home=True, budget=0)
    old = w.plug("hdd", HDD, budget=0, previous_home={"dir": "home-previous-1", "at": w.clock.t})
    (old / "home-previous-1" / "db").mkdir(parents=True)
    (old / "home-previous-1" / "db" / "polymath.sqlite3").write_bytes(b"x" * 1000)
    w.tick(60 * 24)
    assert (old / "home-previous-1").exists()  # a day: kept
    w.tick(60 * 24 * 7)
    assert not (old / "home-previous-1").exists()
    manifest = storage_pool.read_manifest(old) or {}
    assert "previous_home" not in manifest and manifest["budget_bytes"] > 0
    assert w.public()["events"][-1]["event"] == "cleaned"


def test_the_record_on_the_sd_card_is_written_only_when_something_changed(w):
    w.helper.on_sd, w.helper.mounted = False, True
    w.plug("ssd", SSD, home=True, budget=0)
    w.tick()
    w.tick()
    before = w.state.stat().st_mtime_ns
    time.sleep(0.01)
    w.tick()
    assert w.state.stat().st_mtime_ns == before
    assert w.public()["checked_at"] == w.clock.t


# ------------------------------------------------------------------ the agent follows the decision
def test_backups_go_where_the_roles_say_and_spills_fill_extra_drives_first(w):
    w.plug("a", HDD, budget=5 * GB)
    w.plug("b", SSD, budget=3 * GB)
    pool = w.drives.pool()
    assert (pool.choose() or pytest.fail("no drive")).id == "a"  # no decision yet: the roomiest
    (w.config.paths.run_dir / storage_pool.ROLES_FILE).write_text(json.dumps({"backup": "a"}))
    pool = storage_pool.StoragePool(w.config, ismount=lambda p: p in w.drives.mounted)
    assert pool.backup_dir() == w.drives.config.storage.mount_root / "a" / storage_pool.BRAIN / "backups"
    assert (pool.choose() or pytest.fail("no drive")).id == "b"  # extra room first, the backup drive last
    assert pool.backup_dir(need=10**15) == w.config.paths.backup_dir  # nowhere has room


def test_attach_leaves_the_choice_to_the_roles(config, tmp_path):
    config.storage.mount_root = tmp_path / "mnt"
    h = vols.Helper(config, FakeSystem(tmp_path), owner=(os.getuid(), os.getgid()), mountinfo=tmp_path / "mi",
                    lock=tmp_path / "lock", registry=tmp_path / "reg.json")  # fmt: skip
    brain = tmp_path / "b"
    assert not h.should_move(brain, {"dedicated": True, "fstype": "ext4"})
    config.storage.brain_settle_min = 0
    with pytest.raises(ConfigError, match="brain_settle_min"):
        config.validate()


# ------------------------------------------------------------------ the move between drives itself (root helper)
@pytest.fixture()
def two_drives(config, db, tmp_path):
    config.storage.mount_root = tmp_path / "mnt"
    config.storage.reserve_fraction = 0.05
    fake = FakeSystem(tmp_path)
    h = vols.Helper(config, fake, owner=(os.getuid(), os.getgid()), mountinfo=tmp_path / "mountinfo",
                    lock=tmp_path / "lock", registry=tmp_path / "reg.json")  # fmt: skip
    old = config.storage.mount_root / "old" / storage_pool.BRAIN
    new = config.storage.mount_root / "new" / storage_pool.BRAIN
    for b, vid, home in ((old, "old", True), (new, "new", False)):
        b.mkdir(parents=True)
        (b / storage_pool.MANIFEST).write_text(json.dumps({"id": vid, "label": vid.upper(), "home": home,
                                                           "fstype": "ext4", "dedicated": True}))  # fmt: skip
    (old / vols.HOME).mkdir()
    data = config.paths.data_dir  # stands for the bind mount of old/home: what the agent sees
    (data / "raw").mkdir(parents=True, exist_ok=True)
    (data / "raw" / "dump.bz2").write_bytes(b"y" * 3000)
    (data / MOVED_FILE).write_text(json.dumps({"id": "old", "sd_copy": "/srv/polymath-sd-copy"}))
    db.execute("INSERT INTO events(at, kind, text) VALUES(1, 'site', 'before')")
    return h, fake, old, new, data


def test_the_brain_moves_between_drives_and_the_old_one_keeps_a_copy(two_drives, config):
    h, fake, old, new, data = two_drives
    src, dst = storage_pool.read_manifest(old) or {}, storage_pool.read_manifest(new) or {}
    out = h.move_brain(old, src, new, dst)
    assert out.action == "moved" and "OLD is extra room now" in out.detail, out
    assert (new / vols.HOME / "raw" / "dump.bz2").read_bytes() == b"y" * 3000
    assert json.loads((new / vols.HOME / HOME_FILE).read_text())["from"] == "old"
    m_new = storage_pool.read_manifest(new) or {}
    assert m_new["home"] is True and m_new["budget_bytes"] == 0
    m_old = storage_pool.read_manifest(old) or {}
    assert m_old["home"] is False and m_old["previous_home"]["dir"].startswith("home-previous-")
    assert (old / m_old["previous_home"]["dir"]).is_dir() and not (old / vols.HOME).exists()
    assert json.loads((data / MOVED_FILE).read_text())["id"] == "new"  # the SD card names the new brain drive
    calls = [c for c in fake.calls if c[0] in {"systemctl", "mount", "umount"}]
    assert calls[0] == ["systemctl", "stop", *vols.SERVICES] and ["umount", str(data)] in calls
    assert ["mount", "--bind", str(new / vols.HOME), str(data)] in calls and calls[-1][:2] == ["systemctl", "start"]
    import sqlite3

    con = sqlite3.connect(str(new / vols.HOME / "db" / "polymath.sqlite3"))
    texts = [r[0] for r in con.execute("SELECT text FROM events ORDER BY id")]
    con.close()
    assert texts[0] == "before" and "moved the brain from OLD onto NEW" in texts[-1]


def test_a_move_between_drives_that_fails_leaves_the_brain_where_it_was(two_drives, monkeypatch):
    h, fake, old, new, data = two_drives
    real = vols.tree_stats
    calls = iter([(1, 1)])
    monkeypatch.setattr(vols, "tree_stats", lambda root: next(calls, None) or real(root))
    out = h.move_brain(old, storage_pool.read_manifest(old) or {}, new, storage_pool.read_manifest(new) or {})
    assert out.action == "skipped" and "stays on OLD" in out.detail
    assert not (new / vols.HOME).exists() and (old / vols.HOME).exists()
    assert (storage_pool.read_manifest(old) or {})["home"] is True
    monkeypatch.setattr(vols, "tree_stats", real)

    def busy(args: list[str], *, input: str | None = None) -> subprocess.CompletedProcess[str]:
        fake.calls.append(args)
        if args[0] == "umount":
            return subprocess.CompletedProcess(args, 32, "", "target is busy")
        return subprocess.CompletedProcess(args, 0, "", "")

    h.run = busy
    h.mountinfo.write_text(f"40 25 8:17 /polymath-brain/home {data} rw - ext4 /dev/sdb1 rw\n")
    out = h.move_brain(old, storage_pool.read_manifest(old) or {}, new, storage_pool.read_manifest(new) or {})
    assert out.action == "skipped" and "busy" in out.detail and (old / vols.HOME).exists()


# ------------------------------------------------------------------ facts from lsblk and sysfs
def test_disk_facts_reads_lsblk_and_sysfs(tmp_path):
    usb = tmp_path / "sys" / "devices" / "usb2" / "2-1"
    (usb / "2-1:1.0" / "host0" / "block" / "sda").mkdir(parents=True)
    for k, v in {"authorized": "1", "idVendor": "31db", "speed": "5000", "bMaxPower": "896mA"}.items():
        (usb / k).write_text(v + "\n")
    (tmp_path / "sys" / "block").mkdir()
    (tmp_path / "sys" / "block" / "sda").symlink_to(usb / "2-1:1.0" / "host0" / "block" / "sda")

    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        node: dict[str, Any]
        if args[-1] == "/dev/sda2":
            node = {"name": "sda2", "pkname": "sda", "tran": None, "rota": None}
        else:
            node = {"name": "sda", "pkname": None, "tran": "usb", "rota": False}
        return subprocess.CompletedProcess(args, 0, json.dumps({"blockdevices": [node]}), "")

    f = rl.disk_facts(run, tmp_path / "sys", "/dev/sda2")
    assert f["disk"] == "sda" and f["tran"] == "usb" and f["rotational"] is False
    assert f["speed"] == 5000 and f["power_ma"] == 896 and f["usb"].endswith("2-1")
    assert rl.disk_facts(run, tmp_path / "sys", "")["disk"] == ""
    broken = rl.disk_facts(lambda a: subprocess.CompletedProcess(a, 1, "", ""), tmp_path / "sys", "/dev/sdz1")
    assert broken["disk"] == "sdz1" and broken["rotational"] is None


# ------------------------------------------------------------------ everyone sees the decision
def test_the_cli_doctor_dashboard_and_inbox_show_the_roles(w, db, capsys, monkeypatch):
    from polymath.interface import cli, doctor
    from polymath.interface.dashboard import DashboardData

    assert "no decision yet" in cli.roles_lines(None)[0]
    w.plug("stick", STICK, label="STICK", budget=1 * GB)
    w.plug("hdd", HDD, label="BIG HDD", budget=4 * GB)
    w.plug("ssd", SSD, label="T7", budget=3 * GB)
    w.tick()
    w.tick(11)
    w.tick()
    lines = cli.roles_lines(w.public())
    assert lines[0].startswith("decision: the brain is on T7") and any(x.lstrip().startswith("brain") for x in lines)
    assert any("not the brain: on USB 2" in x for x in lines) and any("moved" in x for x in lines)
    w.drives.pool()  # the agent's pool sees the same drives
    rows = {r["id"]: r.get("role") for r in cli.storage_rows(w.config)}
    assert rows["ssd"] == "brain" and rows["hdd"] == "backup" and rows["stick"] == "extra"
    monkeypatch.setattr("polymath.body.sensors.slow_usb_disks", lambda *a: [("sdc", 480)])
    drives = [c.detail for c in doctor.run_checks(w.config, "http://x", health=lambda u: ("refused", 0.0, None))
              if c.name == "drives"]  # fmt: skip
    summary = next(d for d in drives if d.startswith("brain:"))
    assert "brain: T7 (USB 3 SSD)" in summary and "backups: BIG HDD" in summary and "extra room: STICK" in summary
    assert any("mA" in d for d in drives)  # and the power warning
    assert "USB 2 speed" not in " ".join(c.detail for c in doctor.run_checks(
        w.config, "http://x", health=lambda u: ("refused", 0.0, None)))  # the stick is extra room: fine  # fmt: skip
    lines = DashboardData(w.config).storage(db)["lines"]
    assert any(x.startswith("T7: USB 3 SSD, holds the brain") for x in lines)
    assert any("STICK" in x and "not the brain: on USB 2" in x for x in lines)
    n = cli.report_drive_roles(db, w.config)
    assert n == len(w.public()["events"]) and cli.report_drive_roles(db, w.config) == 0
    assert cli.cmd_storage(w.config, argparse.Namespace(op="roles")) == 0
    assert "decision:" in capsys.readouterr().out


# ------------------------------------------------------------------ drive care: an empty Windows drive is made Linux
def test_an_empty_windows_formatted_drive_is_reformatted_for_linux_once_it_settles(w):
    w.helper.on_sd, w.helper.mounted = False, True
    w.plug("ssd", SSD, home=True, budget=0)
    stick = w.plug("stick", STICK, label="STICK", fstype="exfat", budget=1 * GB)
    (stick / "inventory.json").write_text("{}")  # Polymath's own note does not count as a file
    (stick.parent / "System Volume Information").mkdir()  # nor does Windows' clutter
    w.tick()
    assert not [m for m in w.helper.moves if m[0] == "format"]  # still settling
    w.tick(11)
    assert ("format", "stick", "drive roles (empty drive)") in w.helper.moves
    events = [e["event"] for e in w.public()["events"]]
    assert events[-2:] == ["formatting", "formatted"]
    w.tick(60)
    assert len([m for m in w.helper.moves if m[0] == "format"]) == 1  # not again the same day


def test_a_drive_with_any_file_on_it_is_never_reformatted(w):
    w.helper.on_sd, w.helper.mounted = False, True
    w.plug("ssd", SSD, home=True, budget=0)
    photos = w.plug("photos", HDD, label="PHOTOS", fstype="vfat", dedicated=False, budget=1 * GB)
    (photos.parent / "DCIM").mkdir()
    (photos.parent / "DCIM" / "img.jpg").write_bytes(b"jpg")
    spilled = w.plug("ntfs", {**HDD, "disk": "sdd"}, fstype="ntfs", budget=1 * GB)
    (spilled / "bodies").mkdir()
    (spilled / "bodies" / "pack-000001.bin").write_bytes(b"p")  # Polymath's documents: never erased either
    w.tick()
    w.tick(11)
    assert not [m for m in w.helper.moves if m[0] == "format"]
    assert any("PHOTOS is FAT32 and holds your files" in x for x in w.public()["warnings"])
    w.config.storage.care_format_empty = False
    (photos.parent / "DCIM" / "img.jpg").unlink()
    (photos.parent / "DCIM").rmdir()
    w.tick(60 * 25)
    assert not [m for m in w.helper.moves if m[0] == "format"]  # switched off


def test_format_empty_ignores_polymaths_notes_but_never_erases_its_documents(two_drives, config):
    h, fake, _old, new, _data = two_drives
    (config.storage.mount_root / "new").mkdir(exist_ok=True)
    fake.add("/dev/sdc")
    fake.add("/dev/sdc1", disk="sdc", probe={"TYPE": "exfat", "UUID": "new"})
    h.mountinfo.write_text(f"36 25 8:33 / {config.storage.mount_root / 'new'} rw - exfat /dev/sdc1 rw\n")
    (new / "inventory.json").write_text("{}")
    (new / "backups").mkdir()
    (new / "backups" / "polymath-1.sqlite3.xz").write_bytes(b"b")
    out = h.format_empty("new", by="drive roles (empty drive)")
    assert out.action == "skipped" and "holds Polymath's documents" in out.detail
    (new / "backups" / "polymath-1.sqlite3.xz").unlink()
    real = h.run

    def run(args: list[str], *, input: str | None = None) -> subprocess.CompletedProcess[str]:
        if args[0] == "umount":
            h.mountinfo.write_text("")  # released
        out: subprocess.CompletedProcess[str] = real(args, input=input)
        return out

    h.run = run
    h.format_empty("new", by="drive roles (empty drive)")
    assert ["mkfs.ext4", "-q", "-F", "-L", "POLYMATH", "-m", "0", "/dev/sdc1"] in fake.calls
    reg = json.loads(h.registry.read_text())
    assert any(v.get("by") == "drive roles (empty drive)" for v in reg.values())


def test_an_old_brain_drive_coming_back_never_covers_the_brain_that_lives_on(two_drives, config):
    h, fake, old, _new, data = two_drives
    (data / MOVED_FILE).write_text(json.dumps({"id": "somewhere-else"}))  # the brain moved on (or was rebuilt)
    (old / vols.HOME / HOME_FILE).write_text("{}")
    manifest = storage_pool.read_manifest(old) or {}
    out = h.mount_home(old, manifest, vols.Outcome("attached", "/dev/sdb1 → x", "old"))
    assert "older copy of the brain" in out.detail and not any(c[:2] == ["mount", "--bind"] for c in fake.calls)
    m = storage_pool.read_manifest(old) or {}
    assert m["home"] is False and (old / m["previous_home"]["dir"]).is_dir()


def test_the_sd_card_remembers_which_drive_holds_the_brain(two_drives, config, monkeypatch):
    h, _fake, old, new, data = two_drives
    monkeypatch.setattr(h, "drive_identity", lambda m: {"serial": "S-NEW", "model": "T7", "usb": "04e8:4001"})
    h.move_brain(old, storage_pool.read_manifest(old) or {}, new, storage_pool.read_manifest(new) or {})
    marker = json.loads((data / MOVED_FILE).read_text())
    assert marker["id"] == "new" and marker["serial"] == "S-NEW" and marker["usb"] == "04e8:4001"
    assert vols.Helper.drive_identity(h, {"serial": " S1 ", "model": "M"})["serial"] == "S1"
