"""v0.10.0: the SSD watch: SD-card mode notices the SSD, checks it, moves back onto it by itself, and falls back."""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from polymath.body import ssdwatch as sw
from polymath.core import fsprobe, sdcard
from polymath.core.config import HOME_FILE, MOVED_FILE, ConfigError, load_config
from tests.conftest import make_config

UUID = "1b2c3d4e-0000-4000-8000-00000000abcd"
USB_ID = "31db:9210"


class Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        return self.t


class Pi:
    """A Pi in SD-card mode with the SSD's enclosure on USB: sysfs, lsblk, systemctl and the brain's mount."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.clock = Clock()
        for sub in ("home", "sd"):
            (tmp / sub).mkdir()
        self.home = make_config(tmp / "home")
        self.home.paths.data_dir = tmp / "srv"
        self.home.paths.data_dir.mkdir(parents=True)
        (self.home.paths.data_dir / MOVED_FILE).write_text(json.dumps({"id": UUID, "sd_copy": "/srv/polymath-sd-copy"}))
        self.home.storage.mount_root = tmp / "mnt"
        self.home.storage.mount_root.mkdir()
        self.config = make_config(tmp / "sd")
        self.config.storage.sd_only = True
        self.config.paths.run_dir = tmp / "run"
        self.config.paths.run_dir.mkdir()
        self.dropin = tmp / "etc" / "polymath.d" / sdcard.DROPIN
        self.dropin.parent.mkdir(parents=True)
        self.dropin.write_text("[storage]\nsd_only = true\n")
        old = self.clock.t - 5 * 86400  # switched on days ago: no first-hour hold
        os.utime(self.dropin, (old, old))
        self.sd_state = tmp / "sd-card-mode.json"
        self.sd_state.write_text(json.dumps({"usb": "", "id": USB_ID, "disk": "sda", "at": int(old)}))
        self.sys = tmp / "sys"
        self.usb = self.sys / "devices" / "platform" / "usb2" / "2-1"
        self.usb.mkdir(parents=True)
        for name, value in {"authorized": "1", "idVendor": "31db", "idProduct": "9210", "speed": "5000",
                            "busnum": "2", "devnum": "3"}.items():  # fmt: skip
            (self.usb / name).write_text(value + "\n")
        (self.sys / "bus" / "usb" / "devices").mkdir(parents=True)
        (self.sys / "bus" / "usb" / "devices" / "2-1").symlink_to(self.usb)
        (self.sys / "block").mkdir()
        self.disk: dict[str, Any] | None = None  # what lsblk shows of the SSD (None: no drive)
        self.others: list[dict[str, Any]] = []
        self.calls: list[list[str]] = []
        self.mounted = False
        self.mount_works = True
        self.probe_result: tuple[bool, float, str] = (True, 300.0, "")
        self.probes = 0
        self.heal: list[dict[str, Any]] = []

    # the SSD as the kernel shows it
    def plug(self, *, size: int = 1_000_204_886_016, speed: str = "5000", devnum: str = "3") -> None:
        (self.usb / "speed").write_text(speed + "\n")
        (self.usb / "devnum").write_text(devnum + "\n")
        (self.usb / "authorized").write_text("1\n")
        host = self.usb / "2-1:1.0" / "host0" / "target0:0:0" / "0:0:0:0"
        (host / "block" / "sda").mkdir(parents=True, exist_ok=True)
        (host / "ioerr_cnt").write_text("0x0\n")
        link = self.sys / "block" / "sda"
        if not link.is_symlink():
            link.symlink_to(host / "block" / "sda")
        if not (host / "block" / "sda" / "device").exists():
            (host / "block" / "sda" / "device").symlink_to(host)
        part = {"name": "sda2", "path": "/dev/sda2", "type": "part", "size": size // 2, "fstype": "ext4",
                "uuid": UUID, "mountpoints": [None]}  # fmt: skip
        self.disk = {"name": "sda", "path": "/dev/sda", "type": "disk", "size": size, "tran": "usb",
                     "children": [part] if size else []}  # fmt: skip

    def io_errors(self, n: int) -> None:
        (self.usb / "2-1:1.0" / "host0" / "target0:0:0" / "0:0:0:0" / "ioerr_cnt").write_text(hex(n) + "\n")

    def unplug(self) -> None:
        self.disk = None
        if (self.sys / "block" / "sda").is_symlink():
            (self.sys / "block" / "sda").unlink()

    def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        out = ""
        if args[0] == "lsblk":
            mmc = {"name": "mmcblk0", "type": "disk", "size": 64_000_000_000, "tran": None,
                   "children": [{"name": "mmcblk0p2", "type": "part", "size": 63_000_000_000, "uuid": "root"}]}  # fmt: skip
            devs = [mmc, *([self.disk] if self.disk else []), *self.others]
            out = json.dumps({"blockdevices": devs})
        elif args[:2] == ["systemctl", "is-active"]:
            out = "inactive\n"
        elif args[:3] == ["systemctl", "restart", "--no-block"] and args[3].startswith("polymath-volume@"):
            if self.mount_works:
                self.mounted = True
                (self.home.paths.data_dir / HOME_FILE).write_text("{}")
        elif args[:2] == ["umount", "-l"]:
            self.mounted = False
        return subprocess.CompletedProcess(args, 0, out, "")

    def mounts_at(self, path: Path) -> list[tuple[str, str]]:
        return [(str(path), "/dev/sda2")] if self.mounted and path == self.home.paths.data_dir else []

    def probe(self, dev: str) -> tuple[bool, float, str]:
        self.probes += 1
        return self.probe_result

    def watch(self) -> sw.SSDWatch:
        return sw.SSDWatch(self.config, self.home, self.run, dropin=self.dropin, mounts_at=self.mounts_at,
                           heal_actions=lambda: self.heal, probe=self.probe, sys_root=self.sys,
                           state_path=self.tmp / "ssd-watch.json", sd_state=self.sd_state, clock=self.clock,
                           sleep=lambda s: None)  # fmt: skip

    def tick(self, minutes: float = 1.0) -> dict[str, Any]:
        self.clock.t += minutes * 60
        return self.watch().tick()

    def public(self) -> dict[str, Any]:
        return sw.read_public(self.config) or {}

    def ran(self, *prefix: str) -> list[list[str]]:
        return [c for c in self.calls if c[: len(prefix)] == list(prefix)]


@pytest.fixture()
def pi(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Pi:
    monkeypatch.setattr(fsprobe, "ISOLATE", False)
    return Pi(tmp_path)


# ------------------------------------------------------------------ what it sees, in words
def test_an_ssd_that_is_not_there_is_said_plainly_and_costs_no_sd_card_writes(pi):
    (pi.usb / "idProduct").write_text("0001\n")  # the enclosure is not on USB either
    assert pi.tick()["ssd"] == "absent"
    rec = pi.public()
    assert rec["short"] == "SSD not plugged in" and "powered USB 3 hub" in rec["fix"]
    before = (pi.tmp / "ssd-watch.json").stat().st_mtime_ns
    time.sleep(0.01)
    pi.tick()
    assert (pi.tmp / "ssd-watch.json").stat().st_mtime_ns == before  # a steady state writes nothing to the card
    assert pi.public()["checked_at"] == pi.clock.t  # the copy in memory (/run) is fresh


def test_an_enclosure_without_a_drive_and_a_drive_with_zero_bytes_need_power(pi):
    assert pi.tick()["ssd"] == "enclosure_only"
    assert "replug" in pi.public()["short"] and "powered USB 3 hub" in pi.public()["fix"]
    pi.plug(size=0)
    pi.disk = {"name": "sda", "path": "/dev/sda", "type": "disk", "size": 0, "tran": "usb"}
    assert pi.tick()["ssd"] == "no_power"
    assert [e["event"] for e in pi.public()["events"]] == ["usb_reset"]  # one soft reset (a replug in software)
    assert (pi.usb / "authorized").read_text().strip() == "1"
    pi.tick()
    assert [e["event"] for e in pi.public()["events"]] == ["usb_reset"]  # not again within ten minutes
    pi.tick(11)
    assert [e["event"] for e in pi.public()["events"]] == ["usb_reset", "usb_reset"]
    assert "more power" in pi.public()["fix"]


def test_another_drive_is_not_mistaken_for_the_brain(pi):
    (pi.usb / "idProduct").write_text("0001\n")
    pi.others = [{"name": "sdb", "type": "disk", "size": 64_000_000_000, "tran": "usb",
                  "children": [{"name": "sdb1", "type": "part", "uuid": "AAAA-BBBB", "fstype": "exfat"}]}]  # fmt: skip
    assert pi.tick()["ssd"] == "other_drive" and "/dev/sdb" in pi.public()["detail"]
    assert pi.probes == 0


def test_usb_2_is_refused_and_never_checked(pi):
    pi.plug(speed="480")
    assert pi.tick()["ssd"] == "usb2"
    assert "480 Mbit/s" in pi.public()["detail"] and "blue USB 3 port" in pi.public()["fix"] and pi.probes == 0
    pi.config.storage.sd_return_allow_usb2 = True
    assert pi.tick()["ssd"] == "checking"


# ------------------------------------------------------------------ switched off by sd-card-mode.sh on
def test_a_switched_off_ssd_is_switched_on_to_test_it_and_off_again_if_nothing_shows_up(pi):
    (pi.usb / "authorized").write_text("0\n")
    assert pi.tick()["ssd"] == "waking"
    assert (pi.usb / "authorized").read_text().strip() == "1" and pi.public()["events"][-1]["event"] == "woke"
    assert pi.tick()["ssd"] == "waking"  # it has three minutes to show up
    assert pi.tick(3)["ssd"] == "switched_off"
    assert (pi.usb / "authorized").read_text().strip() == "0"  # off again
    rec = pi.public()
    assert rec["events"][-1]["event"] == "wake_failed" and rec["next_try"] == pytest.approx(pi.clock.t + 3600)
    assert pi.tick(30)["ssd"] == "switched_off" and "switches it on to test it at" in pi.public()["detail"]
    assert pi.tick(31)["ssd"] == "waking"  # an hour later: tried again
    pi.plug()  # this time the drive shows up
    assert pi.tick()["ssd"] == "checking" and pi.probes == 1


# ------------------------------------------------------------------ checked, then back onto the SSD
def test_a_healthy_ssd_is_checked_for_ten_minutes_then_the_brain_moves_back(pi):
    pi.plug()
    for i in range(10):
        out = pi.tick()
        assert out["ssd"] == "checking", (i, out)
    assert "healthy for 9 min of 10 min" in pi.public()["detail"] and pi.public()["short"] == "SSD back: checking it"
    assert pi.tick()["ssd"] == "returned"
    assert pi.mounted and not pi.dropin.exists() and not pi.dropin.with_name(pi.dropin.name + ".off").exists()
    assert not pi.sd_state.exists()
    stop = pi.ran("systemctl", "stop", "--no-block")[0]
    assert stop[3:] == list(sw.SERVICES)
    assert pi.ran("systemctl", "restart", "--no-block", "polymath-volume@sda2.service")
    assert pi.ran("systemctl", "start", "--no-block")
    order = [c[1] for c in pi.calls if c[0] == "systemctl" and c[1] in {"stop", "restart", "start"}]
    assert order.index("stop") < order.index("restart") < order.index("start")
    assert [e["event"] for e in pi.public()["events"]][-2:] == ["returning", "returned"]
    # the drive helper now runs without SD-card mode: the SSD is on probation for a day
    pi.config.storage.sd_only = False
    assert pi.tick()["ssd"] == "probation" and "0 drop-out(s)" in pi.public()["detail"]
    pi.clock.t += sw.PROBATION_S + 60
    assert pi.watch().tick()["ssd"] == "home" and pi.public()["events"][-1]["event"] == "trusted"


def test_a_failed_check_backs_off_and_a_replug_tries_again_at_once(pi):
    pi.plug()
    pi.tick()
    pi.probe_result = (False, 0.0, "it did not answer within 30 s (frozen)")
    assert pi.tick()["ssd"] == "unhealthy" and "frozen" in pi.public()["detail"]
    probes = pi.probes
    assert pi.tick()["ssd"] == "waiting" and pi.probes == probes  # half an hour of rest first
    pi.probe_result = (True, 300.0, "")
    pi.plug(devnum="7")  # a person plugged it in again
    assert pi.tick()["ssd"] == "checking" and pi.probes == probes + 1
    assert pi.public()["probes_ok"] == 1  # and the ten minutes start over


def test_io_errors_while_checking_count_as_unhealthy(pi):
    pi.plug()
    pi.tick()
    pi.io_errors(3)
    assert pi.tick()["ssd"] == "unhealthy" and "3 I/O error(s)" in pi.public()["detail"]


def test_a_new_stretch_of_sd_card_mode_is_left_alone_for_an_hour(pi):
    pi.dropin.write_text("[storage]\nsd_only = true\n")  # sd-card-mode.sh on, just now
    os.utime(pi.dropin, (pi.clock.t, pi.clock.t))
    pi.plug()
    assert pi.tick()["ssd"] == "waiting" and pi.probes == 0
    assert pi.tick(30)["ssd"] == "waiting"
    assert pi.tick(31)["ssd"] == "checking"


def test_a_return_that_does_not_mount_puts_everything_back(pi):
    pi.plug()
    pi.mount_works = False
    for _ in range(10):
        pi.tick()
    assert pi.tick()["ssd"] == "return_failed"
    assert pi.dropin.exists() and "sd_only = true" in pi.dropin.read_text()  # SD-card mode is on again
    assert pi.ran("systemctl", "start", "--no-block")  # and everything runs, on the SD card
    rec = pi.public()
    assert rec["events"][-1]["event"] == "return_failed" and rec["next_try"] == pytest.approx(pi.clock.t + 1800)
    assert pi.tick()["ssd"] == "waiting"  # the same stretch: no first-hour hold, just the backoff


def test_switching_sd_card_mode_off_failing_is_safe(pi, monkeypatch):
    pi.plug()
    for _ in range(10):
        pi.tick()

    real = os.replace

    def refuse(src: Any, dst: Any, **kw: Any) -> None:
        if Path(str(src)) == pi.dropin:
            raise PermissionError(13, "Permission denied")
        real(src, dst, **kw)

    monkeypatch.setattr("os.replace", refuse)
    assert pi.tick()["ssd"] == "return_failed" and pi.dropin.exists()


def test_a_move_cut_short_is_finished_or_undone_and_everything_runs_again(pi):
    aside = pi.dropin.with_name(pi.dropin.name + ".off")
    pi.dropin.rename(aside)  # killed after setting SD-card mode aside, before the SSD was mounted
    pi.config.storage.sd_only = False  # (the configuration now reads without the drop-in)
    assert pi.tick()["ssd"] == "return_failed"
    assert pi.dropin.exists() and not aside.exists() and pi.ran("systemctl", "start", "--no-block")
    assert pi.public()["next_try"] > pi.clock.t
    pi.dropin.rename(aside)  # cut short again, this time after the SSD was mounted
    pi.mounted = True
    (pi.home.paths.data_dir / HOME_FILE).write_text("{}")
    assert pi.tick()["ssd"] == "returned" and not aside.exists() and not pi.dropin.exists()
    assert not pi.sd_state.exists() and pi.public()["returned_at"] == pi.clock.t


# ------------------------------------------------------------------ and back to the SD card if it fails again
def test_an_ssd_that_drops_out_again_within_a_day_sends_the_brain_back_to_the_sd_card(pi):
    pi.plug()
    for _ in range(11):
        pi.tick()
    assert pi.public()["state"] == "returned"
    pi.config.storage.sd_only = False
    pi.heal = [{"at": pi.clock.t - 3600, "action": "usb_reset"},  # before the return: not counted
               {"at": pi.clock.t + 600, "action": "dead_mount"}]  # fmt: skip
    assert pi.tick(20)["ssd"] == "probation" and "1 drop-out(s)" in pi.public()["detail"]
    pi.heal.append({"at": pi.clock.t + 60, "action": "usb_power"})
    assert pi.tick(2)["ssd"] == "fallback"
    assert pi.dropin.exists() and "sd_only = true" in pi.dropin.read_text()
    assert "switched on by the SSD watch" in pi.dropin.read_text()
    assert (pi.usb / "authorized").read_text().strip() == "0"  # the SSD is let go of
    assert json.loads(pi.sd_state.read_text())["id"] == USB_ID and not pi.mounted
    assert pi.ran("umount", "-l") and pi.ran("systemctl", "start", "--no-block")
    rec = pi.public()
    assert rec["events"][-1]["event"] == "fallback" and rec["next_try"] == pytest.approx(pi.clock.t + 86400)
    # SD-card mode again: the same stretch, so it waits the day before testing it again
    pi.config.storage.sd_only = True
    pi.unplug()
    assert pi.tick()["ssd"] == "switched_off"
    pi.clock.t += 86400
    assert pi.watch().tick()["ssd"] == "waking"


def test_no_fallback_when_switched_off(pi):
    pi.config.storage.sd_only = False
    pi.config.storage.sd_auto_fallback = False
    st = sw.WatchState(returned_at=pi.clock.t, state="returned")
    (pi.tmp / "ssd-watch.json").write_text(json.dumps(asdict(st)))
    pi.heal = [{"at": pi.clock.t + 1, "action": "dead_mount"}, {"at": pi.clock.t + 2, "action": "dead_mount"}]
    assert pi.tick()["ssd"] == "probation"


# ------------------------------------------------------------------ when it does nothing
def test_it_stands_aside_when_it_should(pi):
    pi.config.storage.sd_auto_return = False
    assert pi.tick()["ssd"] == "off" and "sd-card-mode.sh off" in pi.public()["fix"]
    pi.config.storage.sd_auto_return = True
    pi.dropin.unlink()
    assert pi.tick()["ssd"] == "manual"
    pi.dropin.write_text("[storage]\nsd_only = true\n")
    (pi.home.paths.data_dir / MOVED_FILE).unlink()
    assert pi.tick()["ssd"] == "no_ssd"
    (pi.tmp / "ssd-watch.json").write_text("not json")
    assert pi.watch().load() == sw.WatchState()
    pi.plug()
    assert [d["name"] for d in pi.watch().devices()] == ["sda", "sda2"]  # the SD card is never a candidate
    broken = SimpleNamespace(run=lambda a: SimpleNamespace(returncode=1, stdout=""))
    assert sw.SSDWatch.devices(broken) == []  # type: ignore[arg-type]


# ------------------------------------------------------------------ the read check itself
def test_the_read_check_reads_three_places_and_never_waits_on_a_frozen_drive(tmp_path, monkeypatch):
    dev = tmp_path / "disk.img"
    dev.write_bytes(os.urandom(1 << 20) * 64)  # 64 MiB
    pid = tmp_path / "probe.pid"
    ok, mbps, why = sw.read_check(str(dev), pidfile=pid)
    assert ok and mbps > 0 and why == "" and not pid.exists()
    ok, _m, why = sw.read_check(str(tmp_path / "missing"), pidfile=pid)
    assert not ok and "reading it failed" in why
    small = tmp_path / "small.img"
    small.write_bytes(b"x" * 1000)
    assert "only 1000 bytes" in sw.read_check(str(small), pidfile=pid)[2]
    pid.write_text(str(os.getpid()))  # a check still stuck from before: no second one
    assert "still stuck" in sw.read_check(str(dev), pidfile=pid)[2]
    pid.unlink()
    monkeypatch.setattr(sw, "_CHILD", "import time; time.sleep(30)")
    t0 = time.monotonic()
    ok, _m, why = sw.read_check(str(dev), timeout=1.0, pidfile=pid)
    assert not ok and "frozen" in why and time.monotonic() - t0 < 10
    monkeypatch.setattr(sw, "_CHILD", "print('nonsense')")
    pid.unlink()
    assert "said 'nonsense'" in sw.read_check(str(dev), pidfile=pid)[2]
    monkeypatch.setattr(sw, "MIN_MBPS", 1e9)
    monkeypatch.setattr(sw, "_CHILD", "print(64 << 20)")
    assert "reads only" in sw.read_check(str(dev), pidfile=pid)[2]


# ------------------------------------------------------------------ everyone sees it
def test_the_feed_doctor_dashboard_and_inbox_show_what_it_sees(pi, tmp_path):
    from polymath.core.db import open_database
    from polymath.interface import cli, doctor
    from polymath.interface.dashboard import DashboardData
    from polymath.interface.feed import now_line

    pi.plug(speed="480")
    pi.tick()
    st = sdcard.status(pi.config)
    assert st is not None and st["ssd"]["state"] == "usb2"
    text = "".join(t for t, _s in now_line({"sd_card": st, "state": "running", "online": True}))
    assert "SSD on USB 2: move it to a blue USB 3 port" in text
    check = doctor.sd_card_check(pi.config, {"sd_card": st})
    assert "SSD watch:" in check.detail and "blue USB 3 port" in check.fix
    (pi.usb / "idProduct").write_text("0001\n")
    pi.unplug()
    pi.tick()
    assert "Polymath moves back onto it by itself" not in doctor.sd_card_check(pi.config, {}).fix  # its own fix
    pi.config.paths.data_dir.mkdir(parents=True, exist_ok=True)
    db = open_database(pi.config.paths.db_path)
    try:
        lines = DashboardData(pi.config).storage(db)["lines"]
        assert any(line.startswith("SSD: the SSD is not plugged in") for line in lines)
        pi.plug(speed="480")
        pi.tick()
        pi.unplug()
        (pi.usb / "authorized").write_text("0\n")
        pi.tick()
        n = cli.report_ssd_watch(db, pi.config)
        assert n == len(pi.public()["events"]) >= 1 and cli.report_ssd_watch(db, pi.config) == 0
        row = db.one("SELECT detail FROM events WHERE kind = 'issue' ORDER BY id DESC LIMIT 1")
        assert row is not None and "ssd_watch:" in str(row["detail"])
    finally:
        db.close()


def test_the_cli_builds_the_watch_from_the_configuration_without_sd_card_mode(tmp_path):
    from polymath.interface import cli

    cfg = make_config(tmp_path)
    path = tmp_path / "polymath.toml"
    dropins = tmp_path / "polymath.d"
    dropins.mkdir()
    (dropins / sdcard.DROPIN).write_text("[storage]\nsd_only = true\n")
    (dropins / "other.toml").write_text("[storage]\nsd_return_stable_min = 20.0\n")
    assert load_config(path, env={}).storage.sd_only
    home = load_config(path, env={}, skip_dropins=(sdcard.DROPIN,))
    assert not home.storage.sd_only and home.storage.sd_return_stable_min == 20.0  # only that drop-in is left out
    helper = SimpleNamespace(mounts_at=lambda p: [])
    healer = SimpleNamespace(load=lambda: SimpleNamespace(actions=[{"at": 1.0, "action": "usb_reset"}]))
    w = cli.ssd_watch(cfg, SimpleNamespace(config=str(path)), helper, healer)  # type: ignore[arg-type]
    assert w.dropin == dropins / sdcard.DROPIN and not w.home.storage.sd_only and w.heal_actions()[0]["at"] == 1.0
    cfg.storage.sd_return_stable_min = 0
    with pytest.raises(ConfigError, match="sd_return_stable_min"):
        cfg.validate()


def test_the_drive_helper_unit_gives_it_time_to_move_the_brain():
    from tests.test_deploy import unit

    assert unit("polymath-heal.service")["Service"]["TimeoutStartSec"] == "10min"
