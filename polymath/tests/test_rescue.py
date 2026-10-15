"""v0.6.3: scripts/rescue.sh, the one command that brings a stuck Pi back and installs the checkout.

The real script runs here against stand-ins: system commands (systemctl, umount, dd, ps, dpkg, apt-get, udevadm,
polymath) that log their calls and act out a frozen or healthy drive, a fake sysfs USB tree and a fake
mountinfo. Every test checks what was done, in what order, and what the person is told.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "rescue.sh"
VERSION = next(line.split('"')[1] for line in (ROOT / "pyproject.toml").read_text().splitlines()
               if line.startswith("version = "))  # fmt: skip

STUBS = {
    "systemctl": """
echo "systemctl $*" >>"$CALLS"
if [ "$1" = is-active ]; then cat "$STATE/active" 2>/dev/null || echo active; exit 0; fi
case "$*" in *"restart --no-block polymath-volume@"*)
    [ -f "$STATE/comes_back" ] && echo "40 25 8:2 /polymath-brain/home $DATA rw - ext4 /dev/sdb2 rw" >>"$MOUNTINFO" ;;
esac
exit 0""",
    "umount": """
echo "umount $*" >>"$CALLS"
target="${@: -1}"
awk -v m="$target" '$5 != m' "$MOUNTINFO" >"$MOUNTINFO.new" && mv "$MOUNTINFO.new" "$MOUNTINFO"
rm -f "$STATE/stuck"   # the lazy unmount + reset free what was stuck
exit 0""",
    "dd": """
echo "dd $*" >>"$CALLS"
[ -f "$STATE/frozen" ] && exec sleep 5
exit 0""",
    "ps": """
[ -f "$STATE/stuck" ] && printf '  195 D    [usb-storage]\\n 4237 DNs  /opt/polymath/venv/bin/python3 /opt/polymath/venv/bin/polymath init\\n'
printf '    1 Ss   /sbin/init\\n'
exit 0""",
    "dpkg": 'echo "dpkg $*" >>"$CALLS"; exit 0',
    "apt-get": 'echo "apt-get $*" >>"$CALLS"; exit "${APT_RC:-0}"',
    "udevadm": 'echo "udevadm $*" >>"$CALLS"; exit 0',
    "uhubctl": None,  # not installed
    "fake-install": """
echo "install $*" >>"$CALLS"
echo "installing… (output of install.sh)"
[ "${INSTALL_RC:-0}" = 0 ] || { echo "error: the install stopped at line 99"; exit "$INSTALL_RC"; }
printf '{"version": "%s", "commit": "abc1234", "date": "2026-10-14"}\\n' "$WANT" >"$RESCUE_OPT/build.json"
exit 0""",
    "fake-polymath": 'echo "polymath $*" >>"$CALLS"; echo "✓ agent      running: reading"; exit 0',
}


def usb_tree(sysroot: Path, disk: str) -> Path:
    dev = sysroot / "devices" / "usb2" / "2-1"
    (dev / "2-1:1.0" / "host0" / "block" / disk).mkdir(parents=True)
    (dev / "authorized").write_text("1")
    (dev / "idVendor").write_text("31db")
    (dev / "idProduct").write_text("9210")
    (sysroot / "block").mkdir()
    (sysroot / "block" / disk).symlink_to(dev / "2-1:1.0" / "host0" / "block" / disk)
    return dev


@pytest.fixture()
def pi(tmp_path: Path):
    """A stand-in Pi; returns (run, calls, state, usbdev)."""
    bin_dir, state, opt = tmp_path / "bin", tmp_path / "state", tmp_path / "opt"
    for d in (bin_dir, state, opt):
        d.mkdir()
    for name, body in STUBS.items():
        if body is None:
            continue
        f = bin_dir / name
        f.write_text("#!/usr/bin/env bash\n" + body.strip() + "\n")
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    data = tmp_path / "srv" / "polymath"
    (data / "db").mkdir(parents=True)
    (data / "db" / "polymath.sqlite3").write_bytes(b"SQLite format 3\0")
    mountinfo = tmp_path / "mountinfo"
    mnt = tmp_path / "mnt" / "26ffc49f"
    mnt.mkdir(parents=True)
    mountinfo.write_text(f"40 25 8:2 /polymath-brain/home {data} rw - ext4 /dev/sda2 rw\n"
                         f"36 25 8:2 / {mnt} rw - ext4 /dev/sda2 rw\n")  # fmt: skip
    (opt / "build.json").write_text('{"version": "0.5.6", "commit": "ca43ce6", "date": "2026-10-13"}\n')
    usbdev = usb_tree(tmp_path / "sys", "sda")
    calls = tmp_path / "calls.log"
    calls.write_text("")
    env = {
        **{k: v for k, v in os.environ.items() if k not in {"SUDO_USER"}},
        "PATH": f"{bin_dir}:{os.environ['PATH']}", "CALLS": str(calls), "STATE": str(state), "DATA": str(data),
        "MOUNTINFO": str(mountinfo), "WANT": VERSION, "RESCUE_NOT_ROOT_OK": "1", "RESCUE_SYS": str(tmp_path / "sys"),
        "RESCUE_DATA": str(data), "RESCUE_MOUNT_ROOT": str(tmp_path / "mnt"), "RESCUE_OPT": str(opt),
        "RESCUE_MOUNTINFO": str(mountinfo), "RESCUE_INSTALL": str(bin_dir / "fake-install"),
        "RESCUE_POLYMATH": str(bin_dir / "fake-polymath"), "RESCUE_LOG": str(tmp_path / "rescue.log"),
        "RESCUE_PROBE_S": "1", "RESCUE_STUCK_WAIT_S": "2", "RESCUE_MOUNT_WAIT_S": "2", "RESCUE_CHECK_S": "2",
        "RESCUE_LOOK_S": "0.05", "RESCUE_UHUBCTL": "no-such-uhubctl",  # never what this machine has
        "RESCUE_ETC": str(tmp_path / "etc"),  # never this machine's /etc/polymath
    }  # fmt: skip

    def run(*args: str, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(SCRIPT), *args], env={**env, **extra}, capture_output=True, text=True,
                              timeout=60)  # fmt: skip

    return run, calls, state, usbdev


def test_a_frozen_drive_is_freed_then_the_checkout_installed(pi):
    run, calls, state, usbdev = pi
    (state / "stuck").touch()
    (state / "comes_back").touch()
    out = run()
    log = calls.read_text()
    print(out.stdout)  # what Christian sees (pytest -s shows it)
    assert out.returncode == 0, out.stdout + out.stderr
    o = out.stdout
    assert 'installed now: {"version": "0.5.6"' in o and f"will install v{VERSION}" in o
    assert "processes stuck on the drive:" in o and "[usb-storage]" in o
    assert "reset the USB device of /dev/sda (31db:9210)" in o and usbdev.joinpath("authorized").read_text() == "1"
    assert "✓ freed the drive" in o and "✓ the drive is mounted again" in o
    assert f'✓ installed: {{"version": "{VERSION}"' in o and "everything worked" in o
    assert "installing… (output of install.sh)" in o  # the install's own output is shown live
    assert "✓ agent      running" in o  # doctor's report is shown
    order = [line.split()[0] + " " + line.split()[1] for line in log.splitlines()]
    assert order.index("systemctl stop") < order.index("umount -l") < order.index("install --allow-sd-card")
    assert "systemctl stop --no-block polymath.service polymath-dashboard.service polymath-heal.timer" in log
    assert "dpkg --configure -a" in log and "apt-get install -y uhubctl" in log
    assert "install --allow-sd-card" in log  # how this Pi is installed, by default


def test_a_frozen_drive_seen_only_by_reading_past_the_cache(pi):
    run, calls, state, _ = pi
    (state / "frozen").touch()  # nothing in state D yet, but a direct read does not answer
    out = run()
    assert "did not answer within 1 s" in out.stdout and "umount -l" in calls.read_text()
    assert "iflag=direct" in calls.read_text()


def test_a_healthy_drive_is_left_alone(pi):
    run, calls, _state, _ = pi
    out = run("--some-option")
    log = calls.read_text()
    assert out.returncode == 0, out.stdout
    assert "– the drive answers (/dev/sda): nothing to free" in out.stdout
    assert "umount" not in log and "reset the USB" not in out.stdout
    assert "install --some-option" in log  # options go to install.sh as given


def test_a_failed_install_is_shown_with_its_last_lines(pi):
    run, _calls, _state, _ = pi
    out = run(INSTALL_RC="1")
    assert out.returncode == 1
    assert "✗ install.sh stopped with exit status 1" in out.stdout
    assert "error: the install stopped at line 99" in out.stdout
    assert '✗ still installed: {"version": "0.5.6"' in out.stdout
    assert "something above is marked ✗: send the log" in out.stdout


def test_a_drive_that_does_not_come_back_is_named(pi):
    run, _calls, state, _ = pi
    (state / "stuck").touch()  # freed, but it stays away
    out = run(APT_RC="100")
    assert "✗ the drive did not come back within 2 s" in out.stdout and "standby brain" in out.stdout
    assert "✗ could not install uhubctl (optional" in out.stdout
    assert out.returncode == 1


def test_in_sd_card_mode_a_frozen_drive_is_let_go_of_not_waited_for(pi, tmp_path):
    run, calls, state, usbdev = pi
    (tmp_path / "etc" / "polymath.d").mkdir(parents=True)
    (tmp_path / "etc" / "polymath.d" / "sd-card.toml").write_text("[storage]\nsd_only = true\n")
    (state / "stuck").touch()
    out = run()
    assert out.returncode == 0, out.stdout
    assert "switched off the USB device of /dev/sda (31db:9210): SD-card mode leaves it alone" in out.stdout
    assert usbdev.joinpath("authorized").read_text() == "0"  # stays off: not reset and switched back on
    assert "✓ SD-card mode: let go of the drive" in out.stdout and "did not come back" not in out.stdout
    assert "polymath-volume@" not in calls.read_text()  # the drive is not asked back


def test_in_sd_card_mode_with_no_drive_there_is_nothing_to_do(pi, tmp_path):
    run, calls, _state, _ = pi
    (tmp_path / "etc" / "polymath.d").mkdir(parents=True)
    (tmp_path / "etc" / "polymath.d" / "sd-card.toml").write_text("[storage]\nsd_only = true\n")
    (tmp_path / "mountinfo").write_text("")
    out = run()
    assert "– SD-card mode: the brain is on the SD card and no drive is mounted" in out.stdout
    assert "umount" not in calls.read_text()


def test_it_refuses_to_run_without_root(tmp_path):
    """As any user but root it stops before doing anything (as root this would act on the real machine: never)."""
    script = SCRIPT.read_text()
    guard = script.index('if [ "$(id -u)" != 0 ]')
    first_action = min(m.start() for m in re.finditer(r"^\s*(limited|shown|systemctl|umount|dpkg|apt-get) ", script,
                                                       re.M))  # fmt: skip
    assert guard < first_action  # the root check comes before anything that acts
    if os.geteuid() == 0:
        return  # only the order can be checked here; running it as root would touch this machine
    out = subprocess.run(["bash", str(SCRIPT)], env={**os.environ, "RESCUE_LOG": str(tmp_path / "x.log")},
                         capture_output=True, text=True, timeout=30)  # fmt: skip
    assert out.returncode == 2 and "sudo" in out.stderr


def test_the_installer_never_blocks_on_a_frozen_agent_and_names_a_stop():
    script = (ROOT / "install.sh").read_text()
    assert "systemctl restart --no-block polymath.service" in script
    assert "trap '" in script and "the install stopped at line" in script
    assert "mountpoint -q" not in script  # mounts come from mountinfo: stat() on a frozen mount hangs
