"""v0.5.7: a frozen drive never freezes Polymath.

A FIFO with no writer blocks ``open`` exactly the way a frozen USB mount blocks ``read``: the caller sleeps until
something else happens. These tests freeze on purpose and check that every part that looks at the brain's storage
answers within its time limit, and that nothing stuck is left that would keep a program from exiting.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from polymath.body import heal
from polymath.body import volumes as vols
from polymath.core import app, fsprobe
from polymath.core.config import HOME_FILE
from polymath.interface import cli, doctor
from polymath.interface.dashboard import make_server
from tests.conftest import make_config
from tests.test_heal import SERVICES, Clock, usb_tree


def release(fifo: Path) -> None:
    """Unfreeze whatever waits on ``fifo``: a writer appears and leaves, so the reader gets end-of-file."""
    try:
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return  # nobody is waiting on it (any more)
    os.close(fd)


def release_others(fifo: Path) -> None:
    """Unfreeze readers in other processes (a stuck probe left behind by a program that has exited)."""
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        release(fifo)
        time.sleep(0.05)


@pytest.fixture()
def frozen(tmp_path: Path) -> Iterator[Path]:
    fifo = tmp_path / "frozen"
    os.mkfifo(fifo)
    yield fifo
    deadline = time.monotonic() + 5
    while fsprobe.stuck() and time.monotonic() < deadline:
        release(fifo)
        time.sleep(0.05)
    fsprobe._inflight.clear()


def wait_unstuck(fifo: Path) -> None:
    deadline = time.monotonic() + 5
    while fsprobe.stuck() and time.monotonic() < deadline:
        release(fifo)
        time.sleep(0.05)
    assert fsprobe.stuck() == 0


# ------------------------------------------------------------------------------------------- the probe itself
def test_answers_for_ordinary_paths(tmp_path):
    f = tmp_path / "f"
    f.write_text("x")
    assert fsprobe.probe(f) == fsprobe.OK
    assert fsprobe.probe(f, read=True) == fsprobe.OK
    assert fsprobe.probe(tmp_path / "nope", read=True) == fsprobe.MISSING
    assert fsprobe.probe(tmp_path) == fsprobe.OK


def test_an_io_error_counts_as_dead():
    # reading /proc/self/mem from its start fails with EIO: the same error a dead mount gives
    assert fsprobe.probe(Path("/proc/self/mem"), read=True) == fsprobe.DEAD
    assert fsprobe.unhealthy(fsprobe.DEAD) and fsprobe.describe(fsprobe.DEAD) == "Input/output error"


@pytest.mark.skipif(os.geteuid() == 0, reason="root may read anything")
def test_no_permission_is_not_a_drive_problem(tmp_path):
    f = tmp_path / "secret"
    f.write_text("x")
    f.chmod(0)
    assert fsprobe.probe(f, read=True) == fsprobe.DENIED
    assert not fsprobe.unhealthy(fsprobe.DENIED)


def test_a_frozen_path_answers_hang_in_time_and_the_next_probe_at_once(frozen):
    t0 = time.monotonic()
    assert fsprobe.probe(frozen, read=True, timeout=0.5) == fsprobe.HANG
    assert time.monotonic() - t0 < 3
    assert fsprobe.stuck() == 1
    t1 = time.monotonic()
    assert fsprobe.probe(frozen, read=True, timeout=5) == fsprobe.HANG  # still stuck: no second waiter
    assert time.monotonic() - t1 < 0.5 and fsprobe.stuck() == 1
    assert fsprobe.describe(fsprobe.HANG) == "it does not answer at all"
    wait_unstuck(frozen)  # the drive answers again: the stuck probe ends by itself
    frozen.unlink()
    frozen.write_text("back")
    assert fsprobe.probe(frozen, read=True) == fsprobe.OK  # asked afresh


def test_a_stuck_probe_never_keeps_a_program_from_exiting(frozen):
    """The reason probes run in a child process: a thread stuck in the kernel would pin the whole program, and
    `polymath init` would still hang systemd's start (the timeout the Pi showed)."""
    code = (
        "import sys, time; from pathlib import Path; from polymath.core import fsprobe\n"
        f"print(fsprobe.probe(Path({str(frozen)!r}), read=True, timeout=0.3))\n"
    )
    t0 = time.monotonic()
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20,
                         cwd=Path(__file__).resolve().parent.parent)  # fmt: skip
    release_others(frozen)
    assert out.stdout.strip() == "hang" and out.returncode == 0
    assert time.monotonic() - t0 < 10


def test_without_a_child_process_a_thread_asks(frozen, tmp_path, monkeypatch):
    def no_fork(*a: Any, **k: Any) -> Any:
        raise OSError(11, "Resource temporarily unavailable")

    monkeypatch.setattr(subprocess, "Popen", no_fork)
    f = tmp_path / "f"
    f.write_text("x")
    assert fsprobe.probe(f, read=True) == fsprobe.OK
    assert fsprobe.probe(frozen, read=True, timeout=0.3) == fsprobe.HANG
    assert fsprobe.stuck() == 1 and isinstance(fsprobe._inflight[f"{frozen}|True"], threading.Thread)
    wait_unstuck(frozen)


# ------------------------------------------------------------------------------------------- the agent
def test_a_frozen_brain_makes_the_agent_wait_not_hang(frozen, tmp_path, capsys, monkeypatch):
    cfg = make_config(tmp_path)
    cfg.paths.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.db_path.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(cfg.paths.db_path)  # reading the database blocks, as on a frozen drive
    monkeypatch.setattr(app, "PROBE_TIMEOUT_S", 0.4)
    t0 = time.monotonic()
    with pytest.raises(app.BrainDriveMissing, match="does not answer at all"):
        app.check_storage(cfg)
    assert time.monotonic() - t0 < 3
    # `polymath init` (ExecStartPre) passes at once, so systemd starts `run`, which waits for the drive visibly
    assert cli.main(["--config", str(tmp_path / "polymath.toml"), "init"]) == 0
    assert "does not answer at all" in json.loads(capsys.readouterr().out)["waiting"]
    assert time.monotonic() - t0 < 4
    wait_unstuck(cfg.paths.db_path)


def test_the_agent_unit_never_enters_the_data_directory():
    unit = (Path(__file__).resolve().parent.parent / "deploy/polymath.service").read_text()
    assert "WorkingDirectory=/\n" in unit  # systemd would hang entering a frozen mount before Polymath starts


# ------------------------------------------------------------------------------------------- dashboard, doctor
def test_the_dashboard_answers_503_on_a_frozen_drive(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    cfg.paths.run_dir = tmp_path / "run"
    cfg.paths.run_dir.mkdir()
    data = cfg.paths.data_dir
    monkeypatch.setattr(fsprobe, "probe", lambda p, **k: fsprobe.HANG if str(p).startswith(str(data))
                        else fsprobe.OK)  # fmt: skip
    from tests.test_always_on import req

    server = make_server(cfg, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        t0 = time.monotonic()
        status, _h, body = req(server.server_address[1], "GET", "/health")
        assert status == 503 and "not answering" in json.loads(body)["error"]
        assert time.monotonic() - t0 < 3
    finally:
        server.shutdown()
        server.server_close()


def test_doctor_names_a_frozen_drive(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    monkeypatch.setattr(fsprobe, "probe", lambda p, **k: fsprobe.HANG)
    c = doctor.brain_check(cfg.paths.data_dir)
    assert c.level == doctor.BAD and "does not answer at all" in c.detail and "storage heal" in c.fix


# ------------------------------------------------------------------------------------------- the drive helpers
def test_a_frozen_brain_is_dead_to_the_healer(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    monkeypatch.setattr(fsprobe, "probe", lambda p, **k: fsprobe.HANG if p.name == HOME_FILE else fsprobe.MISSING)
    assert heal.brain_state(cfg) == "dead"


def test_frozen_drives_are_released_and_reset(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    cfg.paths.run_dir = tmp_path / "run"
    block = usb_tree(tmp_path / "sys", size="1953525168")
    root = cfg.storage.mount_root = tmp_path / "mnt"
    (root / "26ffc49f").mkdir(parents=True)
    (root / "healthy").mkdir()
    calls: list[list[str]] = []
    mounts = {str(root / "26ffc49f"): "/dev/sda1", str(root / "healthy"): "/dev/sdb1"}
    monkeypatch.setattr(fsprobe, "probe", lambda p, **k: fsprobe.HANG if p.name == "26ffc49f" else fsprobe.OK)
    h = heal.Healer(cfg, lambda args, **_: calls.append(list(args)), services=SERVICES,
                    mounts_at=lambda p: [(str(p), mounts[str(p)])] if str(p) in mounts else [], sys_block=block,
                    state_path=tmp_path / "heal.json", clock=Clock(), sleep=lambda s: None)  # fmt: skip
    assert h.release_dead_drives() == ["sda"]
    assert calls == [["umount", "-l", str(root / "26ffc49f")]]  # the healthy drive is left alone
    assert (block / "sda").resolve().parents[3].joinpath("authorized").read_text() == "1"  # off, then on again


def test_the_volume_helper_treats_a_frozen_brain_as_not_there(frozen):
    t0 = time.monotonic()
    assert vols._answers(frozen) is False  # a read that never answers: replace the mount, do not trust it
    assert time.monotonic() - t0 < 12
    wait_unstuck(frozen)


def test_never_moves_the_brain_while_its_storage_does_not_answer(monkeypatch):
    monkeypatch.setattr(fsprobe, "probe", lambda p, **k: fsprobe.HANG)
    assert vols._there(Path("/srv/polymath/.moved")) is None
    monkeypatch.setattr(fsprobe, "probe", lambda p, **k: fsprobe.MISSING)
    assert vols._there(Path("/srv/polymath/.moved")) is False


def test_the_installer_carries_on_past_a_frozen_drive(frozen):
    """install.sh's `bounded` gives up on a command stuck on the drive (here: reading a FIFO nobody writes)."""
    script = (Path(__file__).resolve().parent.parent / "install.sh").read_text()
    func = script[script.index("bounded() {") : script.index("\n}\n", script.index("bounded() {")) + 3]
    sh = (
        f"DRY=0\nBOUNDED_TICKS=2\nwarn() {{ :; }}\n{func}bounded true; a=$?\nbounded false; b=$?\n"
        f'bounded cat {frozen}; c=$?\necho "$a $b $c"\n'
    )
    t0 = time.monotonic()
    out = subprocess.run(["bash", "-c", sh], capture_output=True, text=True, timeout=20)
    release_others(frozen)
    assert out.stdout.split() == ["0", "1", "124"]
    assert time.monotonic() - t0 < 9
