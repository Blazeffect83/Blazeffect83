"""v0.6.2: what the Pi itself showed on 2026-10-14, each turned into a test.

* the probes passed from the page cache while the drive was frozen, then opening the database hung;
* a process stuck on the frozen drive kept the database locked, so every restart crashed ('database is locked');
* the SSD linked at USB 2 (480M) and nothing said so.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from polymath.body import sensors
from polymath.core import app, fsprobe
from polymath.interface import cli, doctor
from tests.conftest import make_config
from tests.test_fsprobe import frozen, release_others  # noqa: F401  (fixture)


def test_a_direct_probe_reads_a_real_file(tmp_path):
    f = tmp_path / "db.sqlite3"
    f.write_bytes(os.urandom(16384))
    assert fsprobe.probe(f, read=True, direct=True) == fsprobe.OK  # O_DIRECT where it works, else a plain read
    assert fsprobe.probe(tmp_path / "gone", read=True, direct=True) == fsprobe.MISSING
    assert fsprobe.probe(Path("/proc/self/mem"), read=True, direct=True) == fsprobe.DEAD


def test_a_direct_probe_still_catches_a_frozen_path(frozen):  # noqa: F811
    t0 = time.monotonic()
    assert fsprobe.probe(frozen, read=True, direct=True, timeout=0.5) == fsprobe.HANG
    assert time.monotonic() - t0 < 3
    assert fsprobe.probe(frozen, read=True, timeout=0.5) == fsprobe.HANG  # its own key: a plain probe asks too
    release_others(frozen)


def test_the_storage_check_reads_past_the_cache(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    seen: list[tuple[str, bool, bool]] = []
    real = fsprobe.probe

    def spy(path: Path, *, read: bool = False, direct: bool = False, timeout: float = 5.0) -> str:
        seen.append((path.name, read, direct))
        return real(path, read=read, direct=direct, timeout=timeout)

    monkeypatch.setattr(fsprobe, "probe", spy)
    app.check_storage(cfg)
    assert (cfg.paths.db_path.name, True, True) in seen  # the database is read from the drive, not the cache


@pytest.mark.parametrize("message", ["database is locked", "disk I/O error"])
def test_a_locked_brain_waits_instead_of_crash_looping(tmp_path, monkeypatch, capsys, message):
    make_config(tmp_path)

    def stuck(_path: Any) -> Any:
        raise sqlite3.OperationalError(message)

    monkeypatch.setattr(app, "open_database", stuck)
    cfg = make_config(tmp_path)
    with pytest.raises(app.BrainDriveMissing, match="a process is stuck on its drive"):
        app.open_brain(cfg)
    # `polymath init` (ExecStartPre) passes, so `run` waits visibly and then learns on the standby brain
    assert cli.main(["--config", str(tmp_path / "polymath.toml"), "init"]) == 0
    assert "stuck on its drive" in json.loads(capsys.readouterr().out)["waiting"]
    with pytest.raises(app.BrainDriveMissing):
        app.build_agent(cfg)


def test_other_database_errors_are_not_hidden(tmp_path, monkeypatch):
    def broken(_path: Any) -> Any:
        raise sqlite3.OperationalError("no such table: jobs")

    monkeypatch.setattr(app, "open_database", broken)
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        app.open_brain(make_config(tmp_path))


def usb_disk(root: Path, speed: str) -> Path:
    dev = root / "devices" / "usb1" / "1-1" / "1-1.2"
    (dev / "1-1.2:1.0" / "host0" / "block" / "sda").mkdir(parents=True)
    (dev / "speed").write_text(speed + "\n")
    (dev / "idVendor").write_text("31db")
    (dev / "1-1.2:1.0" / "host0" / "block" / "sda" / "size").write_text("1953525168")
    block = root / "block"
    block.mkdir()
    (block / "sda").symlink_to(dev / "1-1.2:1.0" / "host0" / "block" / "sda")
    (root / "devices" / "platform" / "mmc").mkdir(parents=True)
    (block / "mmcblk0").symlink_to(root / "devices" / "platform" / "mmc")  # the SD card: not USB, never listed
    return block


def test_a_drive_at_usb_2_speed_is_named(tmp_path):
    assert sensors.slow_usb_disks(usb_disk(tmp_path / "a", "480")) == [("sda", 480)]
    assert sensors.slow_usb_disks(usb_disk(tmp_path / "b", "5000")) == []
    assert sensors.slow_usb_disks(tmp_path / "nowhere") == []


def test_doctor_says_move_it_to_a_blue_port(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    monkeypatch.setattr(sensors, "slow_usb_disks", lambda: [("sda", 480)])
    checks = [c for c in doctor.run_checks(cfg, "http://x", health=lambda u: ("answered", 0.1, {}))
              if c.name == "drives" and "USB 2" in c.detail]  # fmt: skip
    assert checks and checks[0].level == doctor.WARN and "blue USB 3 port" in checks[0].fix
    from polymath.agents import faults

    assert "brain-drive-usb2" in {f.id for f in faults.for_text(checks[0].detail)}  # the caretaker knows it
