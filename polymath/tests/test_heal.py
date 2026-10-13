"""`polymath storage heal`: bring the brain's drive back by itself (dead mount, 0-byte USB drive, stuck enclosure)."""

from __future__ import annotations

import errno
import json
import time
from pathlib import Path
from typing import Any

import pytest

from polymath.body import heal
from polymath.core.config import HOME_FILE, MOVED_FILE
from polymath.core.db import open_database
from polymath.interface import cli, doctor
from tests.conftest import make_config

SERVICES = ("polymath.service", "polymath-dashboard.service")


class Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        return self.t


def usb_tree(root: Path, size: str = "0") -> Path:
    """/sys/block/sda → …/usb2/2-1/2-1:1.0/host0/…/block/sda, with the USB device's authorized switch."""
    usbdev = root / "devices" / "usb2" / "2-1"
    (usbdev / "2-1:1.0" / "host0" / "block" / "sda").mkdir(parents=True)
    (usbdev / "authorized").write_text("1")
    (usbdev / "idVendor").write_text("31db")
    (usbdev / "2-1:1.0" / "host0" / "block" / "sda" / "size").write_text(size)
    block = root / "block"
    block.mkdir()
    (block / "sda").symlink_to(usbdev / "2-1:1.0" / "host0" / "block" / "sda")
    return block


@pytest.fixture()
def setup(tmp_path):
    cfg = make_config(tmp_path)
    cfg.paths.run_dir = tmp_path / "run"
    cfg.paths.run_dir.mkdir()
    cfg.paths.data_dir.mkdir(parents=True, exist_ok=True)
    calls: list[list[str]] = []
    clock = Clock()
    block = usb_tree(tmp_path / "sys")

    def make(**kw: Any) -> heal.Healer:
        return heal.Healer(cfg, lambda args, **_: calls.append(list(args)), services=SERVICES,
                           mounts_at=lambda p: [], sys_block=block, state_path=tmp_path / "heal.json",
                           clock=clock, sleep=lambda s: None, which=kw.get("which", lambda name: "/usr/bin/" + name))  # fmt: skip

    return cfg, calls, clock, block, make


def test_brain_state(setup, monkeypatch):
    cfg, *_ = setup
    data = cfg.paths.data_dir
    assert heal.brain_state(cfg) == "local"  # the brain lives on this disk: nothing to heal
    (data / MOVED_FILE).write_text("{}")
    assert heal.brain_state(cfg) == "missing"
    (data / HOME_FILE).write_text("{}")
    assert heal.brain_state(cfg) == "healthy"
    real = Path.stat

    def dead(self: Path, *a: Any, **k: Any) -> Any:
        if self.name == HOME_FILE:
            raise OSError(errno.EIO, "Input/output error")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "stat", dead)
    assert heal.brain_state(cfg) == "dead"


def test_nothing_happens_while_the_brain_is_healthy(setup):
    cfg, calls, _clock, _block, make = setup
    (cfg.paths.data_dir / HOME_FILE).write_text("{}")
    assert make().heal() == {"brain": "healthy", "action": "none"}
    assert calls == []


def test_escalation_reset_then_power_then_limits(setup):
    cfg, calls, clock, block, make = setup
    (cfg.paths.data_dir / MOVED_FILE).write_text("{}")
    h = make()
    # 1. the drive reports 0 bytes: a soft reset of just that USB device
    out = h.heal()
    assert out["action"] == "usb_reset" and "/dev/sda" in out["detail"]
    usbdev = (block / "sda").resolve().parent.parent.parent.parent
    assert (usbdev / "authorized").read_text() == "1"  # switched off and on again
    assert ["udevadm", "trigger", "--action=add", "--subsystem-match=block"] in calls
    public = json.loads((cfg.paths.run_dir / "heal.json").read_text())
    assert public["actions"][-1]["action"] == "usb_reset"  # readable by doctor and the agent
    # 2. a minute later it is still missing: not again yet, not the power either (missing < POWER_AFTER)
    calls.clear()
    clock.t += 60
    assert h.heal()["action"] in {"waiting", "retrigger"}
    assert not [c for c in calls if c[0] == "uhubctl"]
    # 3. still missing after POWER_AFTER and the reset window: the reset comes first again, then the power
    clock.t += heal.POWER_AFTER
    assert h.heal()["action"] == "usb_reset"
    out = h.heal()
    assert out["action"] == "usb_power" and "cut the USB power for 20 s" in out["detail"]
    uh = [c for c in calls if c[0] == "uhubctl"]
    assert uh == [["uhubctl", "-l", "2", "-a", "0"], ["uhubctl", "-l", "1-1", "-a", "0"],
                  ["uhubctl", "-l", "1-1", "-a", "1"], ["uhubctl", "-l", "2", "-a", "1"]]  # fmt: skip
    # 4. not more often than POWER_EVERY, and at most POWER_PER_DAY a day
    calls.clear()
    clock.t += heal.RESET_EVERY
    h.heal()  # the reset again
    assert h.heal()["action"] != "usb_power"
    st = h.load()
    st.power_count, st.last_power, st.last_reset = heal.POWER_PER_DAY, 0.0, clock.t
    h.save(st)
    assert h.heal()["action"] != "usb_power"


def test_power_cycle_without_uhubctl_says_so(setup):
    cfg, _calls, clock, block, make = setup
    (cfg.paths.data_dir / MOVED_FILE).write_text("{}")
    (block / "sda").resolve().joinpath("size").write_text("1953525168")  # not 0 bytes: no soft reset to try
    h = make(which=lambda name: None)
    h.heal()
    clock.t += heal.POWER_AFTER + 1
    out = h.heal()
    assert out["action"] == "usb_power" and "uhubctl is not installed" in out["detail"]
    assert h.load().power_count == 0


def test_a_dead_mount_is_dropped_and_the_drive_asked_for_again(setup, monkeypatch):
    cfg, calls, _clock, _block, make = setup
    monkeypatch.setattr(heal, "brain_state", lambda c: "dead")
    out = make().heal()
    assert out["action"] == "dead_mount"
    assert calls[0] == ["systemctl", "stop", *SERVICES]
    assert ["umount", "-l", str(cfg.paths.data_dir)] in calls
    assert ["systemctl", "start", *SERVICES] in calls and calls[-1][0] == "udevadm"


def test_heal_can_be_switched_off(setup):
    cfg, calls, clock, _block, make = setup
    (cfg.paths.data_dir / MOVED_FILE).write_text("{}")
    cfg.storage.heal = False
    assert make().heal()["action"] == "none" and calls == []
    cfg.storage.heal = True
    cfg.storage.heal_power_cycle = False
    h = make()
    h.heal()
    clock.t += 10_000
    h.heal()
    assert h.heal()["action"] != "usb_power"


def test_what_it_did_reaches_the_caretakers_inbox_once(setup):
    cfg, _calls, _clock, _block, make = setup
    (cfg.paths.data_dir / MOVED_FILE).write_text("{}")
    make().heal()
    (cfg.paths.data_dir / MOVED_FILE).unlink()
    db = open_database(cfg.paths.db_path)
    assert cli.report_heal_actions(db, cfg) == 1
    assert cli.report_heal_actions(db, cfg) == 0  # once each
    text = db.scalar("SELECT text FROM events WHERE kind = 'issue'")
    assert text.startswith("the drive helper brought the brain's drive back: reset /dev/sda")
    db.close()
    st = json.loads((cfg.paths.run_dir / "heal.json").read_text())
    st["actions"][-1]["at"] = time.time()
    (cfg.paths.run_dir / "heal.json").write_text(json.dumps(st))
    checks = {c.name: c for c in doctor.run_checks(cfg, "http://x", health=lambda u: ("answered", 0.1, {}))}
    assert "brought the brain's drive back 1 time" in checks["drive helper"].detail
    assert "powered USB hub" in checks["drive helper"].fix


def test_units_and_installer():
    root = Path(__file__).resolve().parent.parent
    timer = (root / "deploy/polymath-heal.timer").read_text()
    assert "OnBootSec=45s" in timer and "OnUnitActiveSec=60s" in timer
    assert "polymath storage heal" in (root / "deploy/polymath-heal.service").read_text()
    script = (root / "install.sh").read_text()
    assert "uhubctl" in script and "enable --now polymath-heal.timer" in script
    assert "polymath-heal.timer" in (root / "uninstall.sh").read_text()
