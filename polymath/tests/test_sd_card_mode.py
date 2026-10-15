"""v0.7.0: scripts/sd-card-mode.sh, the switch to SD-card mode and back.

The real script runs here against stand-ins only: system commands (systemctl, umount, udevadm, ps) that log their
calls and act out an SSD that is frozen, healthy, coming back or not, a fake sysfs USB tree, a fake mountinfo, and
temporary directories for /etc/polymath, /var/lib/polymath, /srv and /run. Nothing touches the machine the tests run
on (they run as root in the development container: every path and command is replaced).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "sd-card-mode.sh"
VERSION = next(line.split('"')[1] for line in (ROOT / "pyproject.toml").read_text().splitlines()
               if line.startswith("version = "))  # fmt: skip

STUBS = {
    "systemctl": """
echo "systemctl $*" >>"$CALLS"
if [ "$1" = is-active ]; then cat "$STUB/active" 2>/dev/null || echo active; exit 0; fi
case "$*" in *"restart --no-block polymath-volume@"*)
    [ -f "$STUB/comes_back" ] && echo "40 25 8:2 /polymath-brain/home $DATA rw - ext4 /dev/sda2 rw" >>"$MOUNTINFO" ;;
esac
exit 0""",
    "umount": """
echo "umount $*" >>"$CALLS"
target="${@: -1}"
if [ "$1" != -l ] && [ -f "$STUB/busy" ]; then echo "umount: $target: target is busy" >&2; exit 32; fi
awk -v m="$target" '$5 != m' "$MOUNTINFO" >"$MOUNTINFO.new" && mv "$MOUNTINFO.new" "$MOUNTINFO"
rm -f "$STUB/stuck"   # whatever was stuck on the drive is let go of
exit 0""",
    "ps": """
[ -f "$STUB/stuck" ] && printf '  195 D    [usb-storage]\\n 4237 DNs  /opt/polymath/venv/bin/python3 /opt/polymath/venv/bin/polymath init\\n'
printf '    1 Ss   /sbin/init\\n'
exit 0""",
    "udevadm": 'echo "udevadm $*" >>"$CALLS"; exit 0',
    "fake-install": """
echo "install $*" >>"$CALLS"
echo "installing… (output of install.sh)"
[ "${INSTALL_RC:-0}" = 0 ] || { echo "error: the install stopped at line 99"; exit "$INSTALL_RC"; }
printf '{"version": "%s", "commit": "abc1234", "date": "2026-10-15"}\\n' "$WANT" >"$RESCUE_OPT/build.json"
# the agent starts: in SD-card mode its snapshot says where its brain is
if grep -qs '^sd_only = true' "$RESCUE_ETC/polymath.d/sd-card.toml" && [ -z "${AGENT_SILENT:-}" ]; then
    mkdir -p "$RESCUE_RUN"
    printf '{"status": {"sd_card": {"dir": "%s"}}}\\n' "$RESCUE_SD_DIR" >"$RESCUE_RUN/feed.json"
fi
exit 0""",
    "fake-polymath": 'echo "polymath $*" >>"$CALLS"; echo "✓ agent      running: reading"; exit 0',
}


def usb_tree(sysroot: Path, disk: str) -> Path:
    dev = sysroot / "devices" / "platform" / "usb2" / "2-1"
    (dev / "2-1:1.0" / "host0" / "block" / disk).mkdir(parents=True)
    (dev / "authorized").write_text("1")
    (dev / "idVendor").write_text("31db")
    (dev / "idProduct").write_text("9210")
    (sysroot / "block").mkdir()
    (sysroot / "block" / disk).symlink_to(dev / "2-1:1.0" / "host0" / "block" / disk)
    (sysroot / "bus" / "usb" / "devices").mkdir(parents=True)
    (sysroot / "bus" / "usb" / "devices" / "2-1").symlink_to(dev)
    return dev


def brain_db(path: Path, *, good: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not good:
        path.write_bytes(b"SQLite format 3\0" + b"\x07" * 4096)  # a damaged database
        return
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE documents(id INTEGER PRIMARY KEY, title TEXT)")
    con.execute("INSERT INTO documents(title) VALUES('Page 1')")
    con.commit()
    con.close()


class Pi:
    """A stand-in Pi: the SD card's /srv/polymath (with the marker the move left), its old copy, the SSD's mounts."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.bin, self.state, self.opt = tmp / "bin", tmp / "state", tmp / "opt"
        for d in (self.bin, self.state, self.opt):
            d.mkdir()
        for name, body in STUBS.items():
            f = self.bin / name
            f.write_text("#!/usr/bin/env bash\n" + body.strip() + "\n")
            f.chmod(f.stat().st_mode | stat.S_IEXEC)
        self.data = tmp / "srv" / "polymath"
        self.data.mkdir(parents=True)
        self.copy = tmp / "srv" / "polymath-sd-copy"
        self.etc = tmp / "etc" / "polymath"
        self.etc.mkdir(parents=True)
        self.varlib = tmp / "var" / "lib" / "polymath"
        self.varlib.mkdir(parents=True)
        self.sd_dir = self.varlib / "brain"
        self.mountinfo = tmp / "mountinfo"
        self.mountinfo.write_text("")
        self.usb = usb_tree(tmp / "sys", "sda")
        self.calls = tmp / "calls.log"
        self.calls.write_text("")
        (self.opt / "build.json").write_text('{"version": "0.5.6", "commit": "ca43ce6", "date": "2026-10-13"}\n')
        self.env = {
            **{k: v for k, v in os.environ.items() if k != "SUDO_USER"},
            "PATH": f"{self.bin}:{os.environ['PATH']}", "CALLS": str(self.calls), "STUB": str(self.state),
            "DATA": str(self.data), "MOUNTINFO": str(self.mountinfo), "WANT": VERSION, "RESCUE_NOT_ROOT_OK": "1",
            "RESCUE_SYS": str(tmp / "sys"), "RESCUE_DATA": str(self.data), "RESCUE_MOUNT_ROOT": str(tmp / "mnt"),
            "RESCUE_OPT": str(self.opt), "RESCUE_MOUNTINFO": str(self.mountinfo), "RESCUE_ETC": str(self.etc),
            "RESCUE_SD_DIR": str(self.sd_dir), "RESCUE_STATE": str(self.varlib / "sd-card-mode.json"),
            "RESCUE_RUN": str(tmp / "run"), "RESCUE_INSTALL": str(self.bin / "fake-install"),
            "RESCUE_POLYMATH": str(self.bin / "fake-polymath"), "RESCUE_LOG": str(tmp / "sd-card.log"),
            "RESCUE_STUCK_WAIT_S": "2", "RESCUE_MOUNT_WAIT_S": "2", "RESCUE_CHECK_S": "2", "RESCUE_LOOK_S": "0.05",
            "RESCUE_SETTLE_S": "0", "RESCUE_STOP_WAIT_S": "1",
        }  # fmt: skip

    def ssd_mounted(self) -> None:
        (self.tmp / "mnt" / "26ffc49f").mkdir(parents=True, exist_ok=True)
        self.mountinfo.write_text(f"40 25 8:2 /polymath-brain/home {self.data} rw - ext4 /dev/sda2 rw\n"
                                  f"36 25 8:2 / {self.tmp / 'mnt' / '26ffc49f'} rw - ext4 /dev/sda2 rw\n")  # fmt: skip

    def moved_to_ssd(self, *, good: bool = True) -> None:
        """What the move onto the SSD left on the SD card: the marker, and the old brain kept aside."""
        brain_db(self.copy / "db" / "polymath.sqlite3", good=good)
        (self.copy / "raw").mkdir()
        (self.data / "MOVED-TO-DRIVE.json").write_text(json.dumps({"name": "DockCase", "sd_copy": str(self.copy)}))

    def run(self, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(SCRIPT), *args], env={**self.env, **extra}, capture_output=True,
                              text=True, timeout=120)  # fmt: skip

    @property
    def dropin(self) -> Path:
        return self.etc / "polymath.d" / "sd-card.toml"

    def state_json(self) -> dict[str, object]:
        return dict(json.loads((self.varlib / "sd-card-mode.json").read_text()))


@pytest.fixture()
def pi(tmp_path: Path) -> Pi:
    return Pi(tmp_path)


def order(calls: Path) -> list[str]:
    return [" ".join(line.split()[:2]) for line in calls.read_text().splitlines()]


# ------------------------------------------------------------------------------------------- on
def test_on_lets_go_of_a_frozen_ssd_and_starts_the_brain_from_the_sd_cards_copy(pi):
    pi.ssd_mounted()
    pi.moved_to_ssd()
    (pi.state / "stuck").touch()
    (pi.state / "busy").touch()  # a frozen drive cannot be unmounted cleanly: lazily, then
    out = pi.run("on")
    print(out.stdout)  # what Christian sees (pytest -s shows it)
    o, log = out.stdout, pi.calls.read_text()
    assert out.returncode == 0, o + out.stderr
    assert 'installed now: {"version": "0.5.6"' in o and f"will install v{VERSION} and run it on the SD card" in o
    assert "sd_only = true" in pi.dropin.read_text() and "✓ SD-card mode is on" in o
    assert f"umount -l {pi.data}" in log and pi.mountinfo.read_text() == ""  # nothing of the SSD is mounted
    assert pi.usb.joinpath("authorized").read_text() == "0"  # switched off, like unplugging it
    assert pi.state_json()["id"] == "31db:9210" and pi.state_json()["usb"] == str(pi.usb)
    assert "switched its USB device off (31db:9210)" in o
    assert (pi.sd_dir / "db" / "polymath.sqlite3").exists() and not pi.copy.exists()  # renamed, not copied
    assert json.loads((pi.sd_dir / "seeded-from.json").read_text())["from"] == str(pi.copy)
    assert "the brain starts from the SD card's copy" in o
    assert (pi.data / "MOVED-TO-DRIVE.json").exists()  # the SSD's brain is still where it belongs, untouched
    seq = order(pi.calls)
    assert seq.index("systemctl stop") < seq.index("umount -l") < seq.index("install --allow-sd-card")
    assert f'✓ installed: {{"version": "{VERSION}"' in o and f"✓ the agent runs on the SD card ({pi.sd_dir})" in o
    assert "installing… (output of install.sh)" in o and "✓ agent      running" in o
    assert "everything worked" in o


def test_on_with_a_healthy_ssd_unmounts_it_cleanly(pi):
    pi.ssd_mounted()
    out = pi.run("on")
    log = pi.calls.read_text()
    assert out.returncode == 0, out.stdout
    assert f"umount {pi.data}" in log and "umount -l" not in log  # its data written out, nothing lazy
    assert "the SD card holds no copy of the brain: it starts with an empty brain" in out.stdout
    assert (pi.sd_dir / "db").is_dir()


def test_on_fresh_keeps_the_old_copy_and_starts_empty(pi):
    pi.moved_to_ssd()
    out = pi.run("on", "--fresh", "--keep-ssd-on")
    assert out.returncode == 0, out.stdout
    assert "--fresh: it starts with an empty brain" in out.stdout and pi.copy.exists()
    assert not (pi.sd_dir / "db" / "polymath.sqlite3").exists() and (pi.sd_dir / "db").is_dir()
    assert pi.usb.joinpath("authorized").read_text() == "1"  # nothing was mounted, and it was asked to stay on


def test_a_damaged_copy_is_kept_and_not_used(pi):
    pi.moved_to_ssd(good=False)
    out = pi.run("on")
    assert out.returncode == 1  # worth knowing: marked ✗
    assert "✗ the SD card's copy of the brain was not used: it failed the database check" in out.stdout
    assert pi.copy.exists() and not (pi.sd_dir / "db" / "polymath.sqlite3").exists()
    assert "✓ the agent runs on the SD card" in out.stdout  # Polymath runs anyway, on an empty brain


def test_a_copy_on_another_filesystem_is_never_copied(pi, tmp_path):
    shm = Path("/dev/shm")
    if not shm.is_dir() or shm.stat().st_dev == tmp_path.stat().st_dev:
        pytest.skip("needs a second filesystem (/dev/shm)")
    other = shm / f"polymath-test-{os.getpid()}-{tmp_path.name}"
    try:
        brain_db(other / "db" / "polymath.sqlite3")
        (pi.data / "MOVED-TO-DRIVE.json").write_text(json.dumps({"sd_copy": str(other)}))
        out = pi.run("on")
        assert "is on another filesystem than" in out.stdout and other.exists()  # no room for a second copy
        assert not (pi.sd_dir / "db" / "polymath.sqlite3").exists()
    finally:
        import shutil

        shutil.rmtree(other, ignore_errors=True)


def test_running_on_again_carries_on_and_switches_a_reawakened_ssd_off(pi):
    pi.ssd_mounted()
    pi.moved_to_ssd()
    assert pi.run("on").returncode == 0
    (pi.sd_dir / "db" / "new.txt").write_text("learned on the SD card")
    pi.usb.joinpath("authorized").write_text("1")  # a reboot switched it on again; it is not mounted
    out = pi.run("on")
    assert out.returncode == 0, out.stdout
    assert "the SD-card brain is there already" in out.stdout and (pi.sd_dir / "db" / "new.txt").exists()
    assert "switched its USB device off again (31db:9210)" in out.stdout
    assert pi.usb.joinpath("authorized").read_text() == "0"


def test_an_agent_that_does_not_report_the_sd_card_is_named(pi):
    out = pi.run("on", AGENT_SILENT="1")
    assert out.returncode == 1
    assert "✗ the agent runs, but has not said it is on the SD card within 2 s" in out.stdout


def test_a_failed_install_is_shown(pi):
    out = pi.run("on", INSTALL_RC="1")
    assert out.returncode == 1
    assert "✗ install.sh stopped with exit status 1" in out.stdout
    assert "error: the install stopped at line 99" in out.stdout
    assert pi.dropin.exists()  # SD-card mode stays on: the old agent waits, the next run installs


# ------------------------------------------------------------------------------------------- off
def test_off_switches_the_ssd_back_on_and_the_mode_off(pi):
    pi.ssd_mounted()
    assert pi.run("on").returncode == 0
    (pi.state / "comes_back").touch()
    out = pi.run("off")
    log = pi.calls.read_text()
    assert out.returncode == 0, out.stdout
    assert pi.usb.joinpath("authorized").read_text() == "1" and "switched the SSD's USB device back on" in out.stdout
    assert not pi.dropin.exists() and not (pi.etc / "polymath.d" / "sd-card.toml.off").exists()
    assert not (pi.varlib / "sd-card-mode.json").exists()
    assert "✓ the SSD is back: the brain is on it again" in out.stdout
    assert "what it learned on the SD card moves onto it by itself" in out.stdout
    assert "udevadm trigger --action=add --subsystem-match=block" in log
    assert "systemctl start polymath-heal.timer" in log


def test_off_keeps_sd_card_mode_when_the_ssd_does_not_come_back(pi):
    pi.ssd_mounted()
    assert pi.run("on").returncode == 0
    out = pi.run("off")
    assert out.returncode == 1
    assert "✗ the SSD did not come back within 2 s: SD-card mode stays on" in out.stdout
    assert "sd_only = true" in pi.dropin.read_text()  # straight back
    assert pi.usb.joinpath("authorized").read_text() == "0"
    assert "the agent runs, still on the SD card" in out.stdout


# ------------------------------------------------------------------------------------------- status, safety
def test_status_needs_no_sudo(pi):
    out = pi.run("status", RESCUE_NOT_ROOT_OK="")
    assert out.returncode == 0 and "SD-card mode: off" in out.stdout and "SSD: not mounted" in out.stdout
    pi.ssd_mounted()
    assert pi.run("on").returncode == 0
    out = pi.run("status")
    assert "SD-card mode: on (since" in out.stdout and f"brain: {pi.sd_dir}" in out.stdout
    assert "SSD: switched off (31db:9210" in out.stdout and "sd-card-mode.sh off" in out.stdout


def test_it_refuses_to_change_anything_without_root(tmp_path):
    """As any user but root it stops before doing anything (as root this would act on the real machine: never)."""
    script = SCRIPT.read_text()
    guard = script.index('if [ "$(id -u)" != 0 ]')
    # at the top level (function bodies only run when called; the status block before the guard only reads)
    acting = r"^(limited|shown|systemctl|umount|mv|mkdir|printf|drop_drive_mounts|stop_everything|open_log)\b(?! *\(\))"
    first_action = min(m.start() for m in re.finditer(acting, script, re.M))
    assert guard < first_action  # the root check comes before anything that acts
    assert script.index("status (no root needed)") < guard  # status only reads
    if os.geteuid() == 0:
        return  # only the order can be checked here; running it as root would touch this machine
    out = subprocess.run(["bash", str(SCRIPT), "on"], env={**os.environ, "RESCUE_LOG": str(tmp_path / "x.log")},
                         capture_output=True, text=True, timeout=30)  # fmt: skip
    assert out.returncode == 2 and "sudo" in out.stderr


def test_it_never_formats_or_deletes():
    script = SCRIPT.read_text() + (ROOT / "scripts" / "ops-lib.sh").read_text()
    assert not re.search(r"\b(mkfs|wipefs|rm -rf|shred|dd of=)", script)
    assert "rm -f" not in script.replace('rm -f "$DROPIN.off" "$STATE"', "").replace('rm -f "$rcfile"', "")
