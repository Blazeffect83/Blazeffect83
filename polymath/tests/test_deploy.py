"""Phase 11: systemd units, idempotent install / uninstall (staged root), live feed terminal and dashboard launchers."""

from __future__ import annotations

import configparser
import os
import stat
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def unit(name: str) -> configparser.ConfigParser:
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str  # type: ignore[assignment,method-assign]
    cp.read(ROOT / "deploy" / name)
    return cp


def test_agent_unit_matches_the_spec():
    u = unit("polymath.service")
    svc = u["Service"]
    assert svc["Type"] == "notify" and svc["WatchdogSec"] == "120"
    assert svc["CPUQuota"] == "200%" and svc["MemoryMax"] == "3G" and svc["Nice"] == "10"
    assert u["Unit"]["RequiresMountsFor"] == "/srv/polymath"
    assert svc["Restart"] == "always" and svc["User"] == "polymath"
    assert svc["ReadWritePaths"] == "/srv/polymath -/mnt/polymath" and svc["ProtectSystem"] == "strict"
    env = (ROOT / "deploy" / "polymath.service").read_text()
    assert "OPENBLAS_NUM_THREADS=2" in env and "OMP_NUM_THREADS=2" in env
    assert u["Install"]["WantedBy"] == "multi-user.target"
    assert svc["ExecStart"] == "/opt/polymath/venv/bin/polymath run"


def test_dashboard_unit_and_desktop_entry():
    u = unit("polymath-dashboard.service")
    assert u["Service"]["ExecStart"] == "/opt/polymath/venv/bin/polymath dashboard"
    assert (
        u["Unit"]["RequiresMountsFor"] == "/srv/polymath"
        and u["Service"]["ReadOnlyPaths"] == "/srv/polymath -/mnt/polymath"
    )
    d = unit("polymath-dashboard.desktop")["Desktop Entry"]
    assert d["Type"] == "Application" and d["Exec"] == "/opt/polymath/src/scripts/open-dashboard.sh"
    assert "X-GNOME-Autostart-enabled" not in d  # a menu entry now; the live feed is what opens at login
    f = unit("polymath-feed.desktop")["Desktop Entry"]
    assert f["Exec"] == "/opt/polymath/src/scripts/open-feed.sh" and f["X-GNOME-Autostart-enabled"] == "true"
    m = unit("polymath-feed-menu.desktop")["Desktop Entry"]
    assert m["Exec"] == "/opt/polymath/src/scripts/open-feed.sh --new" and m["Name"] == f["Name"]
    j = unit("journald-polymath.conf")["Journal"]
    assert j["SystemMaxUse"] == "300M"


def sh(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=check, timeout=120)


def test_staged_install_is_idempotent_and_uninstall_keeps_data(tmp_path):
    stage = tmp_path / "stage"
    first = sh(str(ROOT / "install.sh"), "--root", str(stage), "--user", "tester", "--skip-venv")
    assert "writing default configuration" in first.stdout
    etc = stage / "etc"
    for p in (
        etc / "systemd/system/polymath.service",
        etc / "systemd/system/polymath-dashboard.service",
        etc / "systemd/journald.conf.d/polymath.conf",
        etc / "polymath/polymath.toml",
        stage / "opt/polymath/src/polymath/core/loop.py",
        stage / "home/tester/.config/autostart/polymath-feed.desktop",
        stage / "home/tester/.local/share/applications/polymath-feed.desktop",
        stage / "home/tester/.local/share/applications/polymath-dashboard.desktop",
        stage / "srv/polymath",
    ):
        assert p.exists(), p
    for name in ("open-dashboard.sh", "open-feed.sh"):
        assert (stage / "opt/polymath/src/scripts" / name).stat().st_mode & stat.S_IXUSR
    assert (stage / "usr/local/bin/polymath").stat().st_mode & stat.S_IXOTH  # `polymath` on everyone's PATH
    assert not (stage / "home/tester/.config/autostart/polymath-dashboard.desktop").exists()
    assert not list((stage / "opt/polymath/src").rglob("__pycache__"))
    conf = etc / "polymath/polymath.toml"
    conf.write_text(conf.read_text() + "\n# my change\n")
    second = sh(str(ROOT / "install.sh"), "--root", str(stage), "--user", "tester", "--skip-venv")
    assert "keeping existing configuration" in second.stdout and "# my change" in conf.read_text()
    labwc = (stage / "home/tester/.config/labwc/autostart").read_text()
    wayfire = (stage / "home/tester/.config/wayfire.ini").read_text()
    assert labwc.count("open-feed.sh") == 1 and "open-dashboard" not in labwc
    assert wayfire.count("polymath =") == 1 and "polymath = /opt/polymath/src/scripts/open-feed.sh" in wayfire
    dry = sh(str(ROOT / "install.sh"), "--root", str(tmp_path / "dry"), "--user", "tester", "--skip-venv", "--dry-run")
    assert "+ install -D" in dry.stdout and not (tmp_path / "dry/etc").exists()  # nothing written

    (stage / "srv/polymath/learned.sqlite3").write_text("knowledge")
    sh(str(ROOT / "uninstall.sh"), "--root", str(stage), "--user", "tester")
    assert not (etc / "systemd/system/polymath.service").exists() and not (stage / "opt/polymath").exists()
    assert not (stage / "usr/local/bin/polymath").exists()
    assert not (stage / "home/tester/.config/autostart/polymath-feed.desktop").exists()
    assert not list((stage / "home/tester/.local/share/applications").glob("polymath-*"))
    assert "open-feed" not in (stage / "home/tester/.config/labwc/autostart").read_text()
    assert "polymath =" not in (stage / "home/tester/.config/wayfire.ini").read_text()
    assert (stage / "srv/polymath/learned.sqlite3").exists() and conf.exists()  # learned data is kept
    refused = sh(str(ROOT / "uninstall.sh"), "--root", str(stage), "--purge", check=False)
    assert refused.returncode == 2 and (stage / "srv/polymath/learned.sqlite3").exists()
    sh(str(ROOT / "uninstall.sh"), "--root", str(stage), "--purge", "--yes")
    assert (stage / "srv/polymath").is_dir() and not any((stage / "srv/polymath").iterdir())
    assert not (etc / "polymath").exists()


def test_install_refuses_without_the_nvme_mount_or_root():
    if os.geteuid() == 0:
        if os.path.ismount("/srv/polymath"):
            pytest.skip("a real /srv/polymath mount exists here")
        out = sh(str(ROOT / "install.sh"), check=False)
        assert out.returncode == 1 and "not a mounted filesystem" in out.stderr
    else:
        out = sh(str(ROOT / "install.sh"), check=False)
        assert out.returncode == 1 and "run as root" in out.stderr


def test_open_dashboard_waits_for_the_server_then_opens_an_app_window(tmp_path):
    hits = []

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            return

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "chromium"
    fake.write_text(f'#!/bin/sh\necho "$@" > {tmp_path}/args\n')
    fake.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "POLYMATH_DASHBOARD_URL": f"http://127.0.0.1:{srv.server_address[1]}/",
        "POLYMATH_DASHBOARD_WAIT": "5",
        "XDG_RUNTIME_DIR": str(tmp_path),
        "HOME": str(tmp_path),
    }
    try:
        subprocess.run([str(ROOT / "scripts/open-dashboard.sh")], env=env, check=True, timeout=30)
    finally:
        srv.shutdown()
    args = (tmp_path / "args").read_text()
    assert f"--app=http://127.0.0.1:{srv.server_address[1]}/" in args and "--start-maximized" in args
    assert hits == ["/"]
    # a second launcher while the first holds the lock exits quietly
    with open(tmp_path / "polymath-dashboard.lock", "w") as lock:
        import fcntl

        fcntl.flock(lock, fcntl.LOCK_EX)
        (tmp_path / "args").unlink()
        subprocess.run([str(ROOT / "scripts/open-dashboard.sh")], env=env, check=True, timeout=30)
        assert not (tmp_path / "args").exists()


def test_upgrade_moves_the_dashboard_kiosk_to_the_live_feed_and_retires_minecraft_settings(tmp_path):
    stage = tmp_path / "stage"
    home = stage / "home/tester"
    (home / ".config/autostart").mkdir(parents=True)
    (home / ".config/labwc").mkdir(parents=True)
    (home / ".config/autostart/polymath-dashboard.desktop").write_text("[Desktop Entry]\n")
    (home / ".config/labwc/autostart").write_text("swaybg &\n/opt/polymath/src/scripts/open-dashboard.sh &\n")
    (home / ".config/wayfire.ini").write_text("[autostart]\npolymath = /opt/polymath/src/scripts/open-dashboard.sh\n")
    conf = stage / "etc/polymath/polymath.toml"
    conf.parent.mkdir(parents=True)
    old = (
        (ROOT / "config/polymath.toml")
        .read_text()
        .replace("[body]\n", '[body]\nminecraft_host = "127.0.0.1"\nminecraft_port = 25565\nminecraft_check = true\n')
    )
    conf.write_text(old)
    out = sh(str(ROOT / "install.sh"), "--root", str(stage), "--user", "tester", "--skip-venv").stdout
    assert "retired minecraft_* settings" in out
    assert (conf.parent / "polymath.toml.bak").read_text() == old  # the original is kept
    text = conf.read_text()
    assert (
        "\nminecraft_" not in text and text.count("# retired (the Minecraft player check was removed): minecraft_") == 3
    )
    from polymath.core.config import load_config

    load_config(conf, env={})  # still a valid configuration
    assert not (home / ".config/autostart/polymath-dashboard.desktop").exists()
    labwc = (home / ".config/labwc/autostart").read_text()
    assert labwc == "swaybg &\n/opt/polymath/src/scripts/open-feed.sh &\n"
    assert (
        home / ".config/wayfire.ini"
    ).read_text() == "[autostart]\npolymath = /opt/polymath/src/scripts/open-feed.sh\n"
    again = sh(str(ROOT / "install.sh"), "--root", str(stage), "--user", "tester", "--skip-venv").stdout
    assert "retired" not in again and (home / ".config/labwc/autostart").read_text() == labwc  # idempotent


def feed_env(tmp_path: Path, terminals: tuple[str, ...]) -> dict[str, str]:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for name in terminals:
        fake = bindir / name
        fake.write_text(
            f'#!/bin/sh\nprintf "%s\\n" "$@" > {tmp_path}/{name}.args\n'
            f'printf "%s\\n" "$XDG_CONFIG_HOME" > {tmp_path}/{name}.env\n'
        )
        fake.chmod(0o755)
    return {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "XDG_RUNTIME_DIR": str(tmp_path),
        "HOME": str(tmp_path),
    }


def test_open_feed_prefers_lxterminal_and_opens_one_window(tmp_path):
    env = feed_env(tmp_path, ("lxterminal", "xterm"))
    script = str(ROOT / "scripts/open-feed.sh")
    subprocess.run([script], env=env, check=True, timeout=30)
    args = (tmp_path / "lxterminal.args").read_text().splitlines()
    assert args == ["--no-remote", "--title=Polymath — live feed", "--geometry=120x36", "-e", f"{script} --inside"]
    conf = tmp_path / ".config/polymath-feed/lxterminal/lxterminal.conf"
    assert "fontname=Monospace 14" in conf.read_text()  # a bigger font for this window only
    assert (tmp_path / "lxterminal.env").read_text().strip() == str(tmp_path / ".config/polymath-feed")
    conf.write_text("[general]\nfontname=Monospace 18\n")  # the user's own change survives the next launch
    assert not (tmp_path / "xterm.args").exists()
    import fcntl

    with open(tmp_path / "polymath-feed.lock", "w") as lock:  # a window is already open (it holds the lock)
        fcntl.flock(lock, fcntl.LOCK_EX)
        (tmp_path / "lxterminal.args").unlink()
        subprocess.run([script], env=env, check=True, timeout=30)
        assert not (tmp_path / "lxterminal.args").exists()  # autostart: nothing more
        subprocess.run([script, "--new"], env=env, check=True, timeout=30)
        assert (tmp_path / "lxterminal.args").exists()  # the menu entry always opens a window
    assert "Monospace 18" in conf.read_text()


@pytest.mark.parametrize(
    ("terminal", "expected"),
    [
        ("foot", ["--title=Polymath — live feed", "--window-size-chars=120x36", "--font=monospace:size=14", "{script}",
                  "--inside"]),
        ("xterm", ["-T", "Polymath — live feed", "-geometry", "120x36", "-fa", "Monospace", "-fs", "14", "-e",
                   "{script}", "--inside"]),
        ("x-terminal-emulator", ["-T", "Polymath — live feed", "-e", "{script}", "--inside"]),
    ],
)  # fmt: skip
def test_open_feed_other_terminals(tmp_path, terminal, expected):
    script = str(ROOT / "scripts/open-feed.sh")
    subprocess.run([script, "--new"], env=feed_env(tmp_path, (terminal,)), check=True, timeout=30)
    assert (tmp_path / f"{terminal}.args").read_text().splitlines() == [a.format(script=script) for a in expected]


def test_open_feed_without_a_terminal_and_inside_the_window(tmp_path):
    script = str(ROOT / "scripts/open-feed.sh")
    none = subprocess.run([script, "--new"], env=feed_env(tmp_path, ()), capture_output=True, text=True, timeout=30)
    assert none.returncode == 1 and "no terminal emulator found" in none.stderr
    env = feed_env(tmp_path, ()) | {"POLYMATH_FEED_CMD": "echo feeding"}
    inside = subprocess.run([script, "--inside"], env=env, input="\n", capture_output=True, text=True, timeout=30)
    assert inside.returncode == 0 and "feeding" in inside.stdout
    assert "The live feed stopped (exit status 0). Press Enter to close this window." in inside.stdout
    assert "\033]0;Polymath — live feed\007" in inside.stdout


def test_storage_pool_udev_rule_and_volume_unit():
    rule = (ROOT / "deploy" / "90-polymath-storage.rules").read_text()
    active = [line for line in rule.splitlines() if line and not line.startswith("#")]
    assert 'KERNEL!="sd*|nvme*|vd*", GOTO="polymath_storage_end"' in active  # never the SD card, loop or zram
    assert all('ENV{SYSTEMD_WANTS}+="polymath-volume@%k.service"' in line for line in active if "SYSTEMD_WANTS" in line)
    assert any('ENV{UDISKS_AUTO}="0"' in line for line in active)  # Polymath mounts it, not the desktop
    u = unit("polymath-volume@.service")
    assert u["Unit"]["BindsTo"] == "dev-%i.device"  # pulling the drive out stops the unit → detach
    assert u["Service"]["ExecStart"] == "/opt/polymath/venv/bin/polymath storage attach /dev/%I"
    assert u["Service"]["ExecStop"] == "/opt/polymath/venv/bin/polymath storage detach /dev/%I"
    assert u["Service"]["Type"] == "oneshot" and u["Service"]["RemainAfterExit"] == "yes"


def test_sd_card_install_and_storage_pool_files(tmp_path):
    stage = tmp_path / "stage"
    out = sh(str(ROOT / "install.sh"), "--root", str(stage), "--user", "tester", "--skip-venv", "--allow-sd-card")
    conf = stage / "etc/polymath/polymath.toml"
    text = conf.read_text()
    assert "SD-card mode" in out.stdout and "require_separate_mount = false" in text
    budget = float(text.split("\ndisk_budget_gb = ")[1].split()[0])
    assert 2.0 <= budget <= 15.0  # sized to the card, never the 200 GB NVMe default
    from polymath.core.config import load_config

    cfg = load_config(conf, env={})
    assert not cfg.paths.require_separate_mount and cfg.body.disk_budget_gb == budget
    assert cfg.storage.mount_root == Path("/mnt/polymath") and cfg.storage.adopt
    for p in (
        stage / "etc/udev/rules.d/90-polymath-storage.rules",
        stage / "etc/systemd/system/polymath-volume@.service",
        stage / "mnt/polymath",
    ):
        assert p.exists(), p
    again = sh(str(ROOT / "install.sh"), "--root", str(stage), "--user", "tester", "--skip-venv", "--allow-sd-card")
    assert "SD-card mode" not in again.stdout and conf.read_text() == text  # idempotent
    plain = tmp_path / "plain"
    sh(str(ROOT / "install.sh"), "--root", str(plain), "--user", "tester", "--skip-venv")
    assert "require_separate_mount = true" in (plain / "etc/polymath/polymath.toml").read_text()
    sh(str(ROOT / "uninstall.sh"), "--root", str(stage), "--user", "tester")
    assert not (stage / "etc/udev/rules.d/90-polymath-storage.rules").exists()
    assert not (stage / "etc/systemd/system/polymath-volume@.service").exists()


def test_polymath_command_runs_as_the_polymath_user(tmp_path):
    """The /usr/local/bin/polymath wrapper: feed as you, data commands as `polymath`, drive mounting as root."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("sudo", "runuser"):
        (bindir / name).write_text(f'#!/bin/sh\necho "{name} $*"\n')
        (bindir / name).chmod(0o755)
    (bindir / "id").write_text('#!/bin/sh\nif [ "$1" = -u ]; then echo "$FAKE_UID"; else echo "$FAKE_USER"; fi\n')
    (bindir / "id").chmod(0o755)
    real = tmp_path / "polymath"
    real.write_text('#!/bin/sh\necho "polymath $*"\n')
    real.chmod(0o755)

    def run(*args: str, uid: str = "1000", user: str = "christian") -> str:
        env = {"PATH": f"{bindir}:/usr/bin:/bin", "FAKE_UID": uid, "FAKE_USER": user, "POLYMATH_BIN": str(real)}
        out = subprocess.run(["sh", str(ROOT / "deploy" / "polymath-cli"), *args], env=env, capture_output=True,
                             text=True, timeout=30)  # fmt: skip
        return out.stdout.strip()

    assert run("feed") == "polymath feed" and run("--help") == "polymath --help"
    assert run("ask", "x") == f"sudo -u polymath --preserve-env=POLYMATH_CONFIG {real} ask x"
    assert run("storage", "eject", "v1") == f"sudo {real} storage eject v1"
    assert run("status", uid="0", user="root") == f"runuser -u polymath -- {real} status"
    assert run("storage", "attach", "/dev/sda1", uid="0", user="root") == "polymath storage attach /dev/sda1"
    assert run("report", uid="999", user="polymath") == "polymath report"
