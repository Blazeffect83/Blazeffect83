"""v0.6.1: the caretaker's knowledge (polymath/agents/faults.toml) is complete, valid and current.

These tests are the rule "every update keeps the caretaker informed": bumping the version without updating
faults.toml, or adding a new problem report without a known fault for it, fails here.
"""

from __future__ import annotations

import fnmatch
import json
import re
import time
from pathlib import Path

import pytest

from polymath import __version__
from polymath.agents import faults
from polymath.core import issues
from polymath.core.db import open_database
from polymath.interface import cli, doctor, snapshot
from tests.conftest import make_config

ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE = ROOT / "polymath" / "agents" / "faults.toml"
HOW = ("update polymath/agents/faults.toml in the same change: [meta] updated_for, an [[update]] entry for this "
       "version, [situation], and a [[fault]] for every new way things can fail (see polymath/CLAUDE.md)")  # fmt: skip


def _version(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


# ------------------------------------------------------------------------------------------- kept current
def test_the_knowledge_is_for_this_version():
    k = faults.knowledge()
    assert k.updated_for == __version__, HOW
    assert any(u["version"] == __version__ for u in k.updates), HOW
    assert k.situation.get("summary") and k.situation.get("as_of"), HOW


def test_every_problem_polymath_reports_is_a_known_fault():
    """Every issue code in the code (``issues.report(db, area, code, …)``) is matched by a fault's codes."""
    globs = [g for f in faults.knowledge().faults for g in f.codes]
    found: set[str] = set()
    call = re.compile(r"issues\.report\(\s*[\w.]+,\s*\"(\w+)\",\s*(f?)\"([^\"]+)\"", re.S)
    for path in (ROOT / "polymath").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for area, is_f, code in call.findall(text):
            found.add(f"{area}/{re.sub(r'{[^}]*}', '*', code) if is_f else code}")
    assert {"storage/brain_missing", "storage/standby", "feed/snapshot", "agent/restarts"} <= found  # scan works
    missing = sorted(c for c in found
                     if not any(fnmatch.fnmatchcase(c, g) or fnmatch.fnmatchcase(g, c) for g in globs))  # fmt: skip
    assert not missing, f"no known fault for issue code(s) {missing}: {HOW}"


def test_history_and_updates_are_consistent():
    k = faults.knowledge()
    now = _version(__version__)
    for h in k.history:
        assert _version(h.fixed_in) <= now, f"history {h.date}: fixed_in {h.fixed_in} is after {__version__}"
    for f in k.faults:
        assert _version(f.since) <= _version(f.updated) <= now, f"fault {f.id}: since/updated out of order"
    versions = [u["version"] for u in k.updates]
    assert len(versions) == len(set(versions)), "one [[update]] per version"


# ------------------------------------------------------------------------------------------- safe by construction
def test_steps_that_change_the_system_always_need_approval():
    risky = re.compile(r"/boot/|apt-get|dpkg|mkfs|chmod|chown|cmdline\.txt|uhubctl -a")
    for f in faults.knowledge().faults:
        for s in f.steps:
            if risky.search(s.command):
                assert s.needs == "approval", f"{f.id}: '{s.command}' changes the system: needs = 'approval'"
        assert any(s.needs != "auto" for s in f.steps), f"{f.id}: says nothing to do for a person or the caretaker"


def test_malformed_knowledge_is_refused():
    good = KNOWLEDGE.read_text(encoding="utf-8")
    assert faults.parse(good).faults
    bad_needs = good.replace('needs = "you"', 'needs = "whenever"', 1)
    with pytest.raises(faults.FaultsError, match="unknown step needs"):
        faults.parse(bad_needs)
    dup = (
        good + '\n[[fault]]\nid = "offline"\ntitle = "x"\narea = "x"\nsymptoms = ["x"]\n'
        'steps = [{ needs = "you", do = "x" }]\nsince = "0.1.0"\nupdated = "0.1.0"\n'
    )
    with pytest.raises(faults.FaultsError, match="appears twice"):
        faults.parse(dup)
    priors = good.replace("prior = 0.35 }", "prior = 0.95 }", 1)
    with pytest.raises(faults.FaultsError, match="priors"):
        faults.parse(priors)
    ghost = good.replace('fault = "install-interrupted"', 'fault = "no-such-fault"', 1)
    with pytest.raises(faults.FaultsError, match="unknown faults"):
        faults.parse(ghost)


def test_it_ships_inside_the_package():
    assert "agents/faults.toml" in (ROOT / "pyproject.toml").read_text()
    assert "caretaker" in (ROOT / "deploy/polymath-cli").read_text().split("esac")[0]  # runs as you, no sudo


# ------------------------------------------------------------------------------------------- what it recognises
@pytest.mark.parametrize(("code", "text", "first"), [
    ("storage/heal:dead_mount:1760000000.0", "", "brain-mount-dead"),
    ("storage/heal:usb_power:1760000000.0", "", "brain-drive-power"),
    ("storage/brain_missing", "", "brain-drive-missing"),
    ("storage/standby", "", "standby-active"),
    ("jobs/dead:storage.fold_standby", "", "standby-fold-failed"),
    ("jobs/dead:crawl.step", "", "job-dead"),
    ("body/thermal:throttle", "", "body-thermal"),
    ("body/read_only:pause", "", "body-read-only"),
    ("agent/restarts", "", "agent-restarts"),
    ("whatever/new", "Job for polymath.service failed because a timeout was exceeded", "brain-mount-frozen"),
])  # fmt: skip
def test_problems_are_recognised(code, text, first):
    hits = faults.for_issue(code, text)
    assert hits and hits[0].id == first


def test_words_from_the_pi_are_recognised():
    assert "uhubctl-missing" in {f.id for f in faults.for_text("could not install uhubctl: E: dpkg was interrupted")}
    assert {f.id for f in faults.for_text("sda 0B usb")} == {"brain-drive-zero-bytes"}
    assert faults.for_text("everything is fine") == []


def test_every_problem_report_says_which_known_fault_it_is(tmp_path):
    db = open_database(tmp_path / "x.sqlite3")
    issues.report(db, "storage", "brain_missing", "the brain's storage was missing for 3 min")
    issues.report(db, "brand", "new", "something nobody has seen before")
    got = {i["code"]: i for i in issues.recent(db, 0)}
    assert got["brain_missing"]["known"] == ["brain-drive-missing"]
    assert "known" not in got["new"]  # new to the caretaker: no guess
    db.close()


# ------------------------------------------------------------------------------------------- what people see
def test_doctor_names_the_known_fix(tmp_path):
    cfg = make_config(tmp_path)
    cfg.paths.run_dir = tmp_path / "run"
    cfg.paths.run_dir.mkdir()
    status = {"online": True, "state": "running", "activity": "reading", "net": {"online": True},
              "issues": [{"at": time.time(), "text": "the brain's storage was missing for 3 min",
                          "status": "warn", "area": "storage", "code": "brain_missing",
                          "known": ["brain-drive-missing"]}]}  # fmt: skip
    snapshot.write(snapshot.path_for(cfg), {"version": 1, "written": time.time(), "seq": 0, "status": status,
                                            "events": []})  # fmt: skip
    checks = {c.name: c for c in doctor.run_checks(cfg, "http://x", health=lambda u: ("answered", 0.1, {}))}
    assert (
        "known problem:" in checks["problems"].fix
        and "polymath caretaker show brain-drive-missing" in checks["problems"].fix
    )


def test_polymath_caretaker(tmp_path, capsys):
    make_config(tmp_path)
    conf = str(tmp_path / "polymath.toml")
    assert cli.main(["--config", conf, "caretaker"]) == 0
    out = capsys.readouterr().out
    assert f"caretaker knowledge for v{__version__}" in out and "situation (" in out and "recommended:" in out
    assert "! this build" not in out  # current
    assert cli.main(["--config", conf, "caretaker", "known"]) == 0
    assert "brain-mount-frozen" in capsys.readouterr().out
    assert cli.main(["--config", conf, "caretaker", "show", "brain-drive-uas"]) == 0
    shown = capsys.readouterr().out
    assert "usb-storage.quirks=31db:9210:u" in shown and "[needs your approval]" in shown
    assert cli.main(["--config", conf, "caretaker", "show", "nope"]) == 2
    capsys.readouterr()
    assert cli.main(["--config", conf, "caretaker", "history"]) == 0
    assert "still open" in capsys.readouterr().out
    assert cli.main(["--config", conf, "caretaker", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["updated_for"] == __version__


def test_the_briefing_pairs_each_recent_problem_with_what_it_is():
    lines = faults.briefing([{"area": "storage", "code": "standby", "text": "learning on the standby brain"},
                             {"area": "x", "code": "y", "text": "a brand new problem"}], version=__version__)  # fmt: skip
    text = "\n".join(lines)
    assert "→ known: standby-active" in text and "→ not a known problem yet" in text
    assert "! this build" in "\n".join(faults.briefing(version="0.0.1"))  # an old knowledge base says so
