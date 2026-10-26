"""Safe updates (scripts/update-check.sh): a new version that keeps failing is replaced by the previous one."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from polymath.core.app import build_agent
from polymath.core.loop import ROLLBACK_FILE

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "update-check.sh"


def stage(tmp: Path, *, healthy: bool, state: str, restarts: int, previous: bool = True) -> dict[str, Any]:
    opt, st, bin_ = tmp / "opt", tmp / "state", tmp / "bin"
    (opt / "src").mkdir(parents=True)
    (opt / "src" / "new.py").write_text("new")
    (opt / "build.json").write_text('{"version": "0.12.0", "commit": "bbbbbbb"}\n')
    site = opt / "venv" / "lib" / "python3.13" / "site-packages"
    (site / "polymath").mkdir(parents=True)
    (site / "polymath" / "code.py").write_text("new")
    (site / "polymath-0.12.0.dist-info").mkdir()
    if previous:
        (opt / "previous" / "src").mkdir(parents=True)
        (opt / "previous" / "src" / "old.py").write_text("old")
        (opt / "previous" / "build.json").write_text('{"version": "0.11.4", "commit": "aaaaaaa"}\n')
        (opt / "previous" / "site" / "polymath").mkdir(parents=True)
        (opt / "previous" / "site" / "polymath" / "code.py").write_text("old")
        (opt / "previous" / "site" / "polymath-0.11.4.dist-info").mkdir()
    bin_.mkdir()
    calls = tmp / "calls"
    (bin_ / "curl").write_text(f"#!/bin/sh\nexit {0 if healthy else 7}\n")
    (bin_ / "systemctl").write_text(
        '#!/bin/sh\necho "$*" >>' + str(calls) + "\n"
        f'case "$*" in *NRestarts*) echo {restarts} ;; *ActiveState*) echo {state} ;; esac\n'
    )
    for f in bin_.iterdir():
        f.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "POLYMATH_OPT": str(opt), "POLYMATH_STATE": str(st)}
    return {"opt": opt, "state": st, "site": site, "calls": calls, "env": env}


def run(s: dict[str, Any], base: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(SCRIPT), str(base)], env=s["env"], capture_output=True, text=True, timeout=30)


def test_a_healthy_update_is_left_alone(tmp_path):
    s = stage(tmp_path, healthy=True, state="active", restarts=9)
    r = run(s)
    assert r.returncode == 0 and "healthy" in r.stdout and (s["opt"] / "src" / "new.py").exists()


def test_an_agent_that_waits_without_crashing_is_left_alone(tmp_path):
    s = stage(tmp_path, healthy=False, state="active", restarts=4)
    r = run(s, base=3)  # one restart since the install: waiting for its drive, say
    assert r.returncode == 0 and "not crashing" in r.stdout and not (s["state"] / ROLLBACK_FILE).exists()


def test_a_crash_looping_update_goes_back_to_the_previous_version(tmp_path):
    s = stage(tmp_path, healthy=False, state="activating", restarts=7)
    r = run(s, base=2)
    assert r.returncode == 0 and "going back" in r.stdout
    opt, site = s["opt"], s["site"]
    assert (opt / "src" / "old.py").exists() and (opt / "src.failed" / "new.py").exists()
    assert (site / "polymath" / "code.py").read_text() == "old" and (site / "polymath-0.11.4.dist-info").is_dir()
    assert not (site / "polymath-0.12.0.dist-info").exists()
    assert json.loads((opt / "build.json").read_text())["version"] == "0.11.4"
    marker = json.loads((s["state"] / ROLLBACK_FILE).read_text())
    assert marker["from"]["commit"] == "bbbbbbb" and marker["to"]["version"] == "0.11.4"
    assert marker["why"] == "activating, 5 restarts since the install"
    assert "restart --no-block polymath.service polymath-dashboard.service" in s["calls"].read_text()


def test_a_failed_service_without_a_previous_version_says_so(tmp_path):
    s = stage(tmp_path, healthy=False, state="failed", restarts=0, previous=False)
    r = run(s)
    assert r.returncode == 1 and "no previous version" in r.stdout


def test_the_agent_reports_the_rollback_once(config):
    marker = Path(config.storage.standby_dir).parent / ROLLBACK_FILE
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"at": time.time(), "from": {"version": "0.12.0", "commit": "bbbbbbb"},
                                  "to": {"version": "0.11.4", "commit": "aaaaaaa"}, "why": "failed, 3 restarts"}))  # fmt: skip
    agent = build_agent(config, planners=False)
    agent.start()
    text = agent.db.scalar("SELECT text FROM events WHERE kind = 'issue' AND text LIKE '%kept failing%'")
    assert "the update to v0.12.0 (bbbbbbb) kept failing (failed, 3 restarts), so v0.11.4 (aaaaaaa)" in text
    assert not marker.exists() and list(marker.parent.glob(f"{ROLLBACK_FILE}.reported-*"))
    agent.shutdown()
    agent.db.close()
