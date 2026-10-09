"""Phase 11: systemd units, idempotent install / uninstall (staged root), dashboard autostart launcher."""

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
    assert svc["ReadWritePaths"] == "/srv/polymath" and svc["ProtectSystem"] == "strict"
    env = (ROOT / "deploy" / "polymath.service").read_text()
    assert "OPENBLAS_NUM_THREADS=2" in env and "OMP_NUM_THREADS=2" in env
    assert u["Install"]["WantedBy"] == "multi-user.target"
    assert svc["ExecStart"] == "/opt/polymath/venv/bin/polymath run"


def test_dashboard_unit_and_desktop_entry():
    u = unit("polymath-dashboard.service")
    assert u["Service"]["ExecStart"] == "/opt/polymath/venv/bin/polymath dashboard"
    assert u["Unit"]["RequiresMountsFor"] == "/srv/polymath" and u["Service"]["ReadOnlyPaths"] == "/srv/polymath"
    d = unit("polymath-dashboard.desktop")["Desktop Entry"]
    assert d["Type"] == "Application" and d["Exec"] == "/opt/polymath/src/scripts/open-dashboard.sh"
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
        stage / "home/tester/.config/autostart/polymath-dashboard.desktop",
        stage / "srv/polymath",
    ):
        assert p.exists(), p
    launcher = stage / "opt/polymath/src/scripts/open-dashboard.sh"
    assert launcher.stat().st_mode & stat.S_IXUSR
    assert not list((stage / "opt/polymath/src").rglob("__pycache__"))
    conf = etc / "polymath/polymath.toml"
    conf.write_text(conf.read_text() + "\n# my change\n")
    second = sh(str(ROOT / "install.sh"), "--root", str(stage), "--user", "tester", "--skip-venv")
    assert "keeping existing configuration" in second.stdout and "# my change" in conf.read_text()
    labwc = (stage / "home/tester/.config/labwc/autostart").read_text()
    wayfire = (stage / "home/tester/.config/wayfire.ini").read_text()
    assert labwc.count("open-dashboard.sh") == 1 and wayfire.count("polymath =") == 1
    dry = sh(str(ROOT / "install.sh"), "--root", str(tmp_path / "dry"), "--user", "tester", "--skip-venv", "--dry-run")
    assert "+ install -D" in dry.stdout and not (tmp_path / "dry/etc").exists()  # nothing written

    (stage / "srv/polymath/learned.sqlite3").write_text("knowledge")
    sh(str(ROOT / "uninstall.sh"), "--root", str(stage), "--user", "tester")
    assert not (etc / "systemd/system/polymath.service").exists() and not (stage / "opt/polymath").exists()
    assert not (stage / "home/tester/.config/autostart/polymath-dashboard.desktop").exists()
    assert "open-dashboard" not in (stage / "home/tester/.config/labwc/autostart").read_text()
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
