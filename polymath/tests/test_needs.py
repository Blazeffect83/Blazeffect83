"""What only Christian can do (core.needs): one list, from the drives, Orin and the inbox."""

from __future__ import annotations

import argparse
import json
import time

from polymath.core import issues
from polymath.core.needs import lines, needs
from polymath.interface import cli, doctor, feed


def roles(run_dir, **rec):
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "drive-roles.json").write_text(json.dumps(rec))


def test_the_list_gathers_the_drives_orin_and_the_inbox_without_repeats(config, db):
    run = config.paths.run_dir
    assert needs(db, run) == [] and lines([])[0].startswith("nothing needs you")
    roles(run, brain="sd", drives=[{"id": "EA06", "name": "EA06-6985", "role": "pending",
                                    "why_not": "on USB 2 (move it to a blue USB 3 port)"}],
          warnings=["T7 is failing (SMART: …): copy anything of yours off it and replace it",
                    "the drives ask for 1800 mA, more than the 1200 mA a Pi 4 gives its USB ports: …"])  # fmt: skip
    db.execute("INSERT INTO incidents(key, kind, area, title, opened, updated, state, advice) "
               "VALUES('k', 'm', 'body', 'too hot', 1, 1, 'escalated', ?)", ("add a fan or a heatsink\nmore",))  # fmt: skip
    issues.report(db, "update", "rolled_back", "the update … kept failing", severity="bad")
    issues.report(db, "body", "backup_broken", "the backup … failed", severity="bad")
    issues.report(db, "body", "something_else", "not for you")
    got = needs(db, run)
    what = [g["what"] for g in got]
    assert what[0].startswith("move EA06-6985 to a blue USB 3 port")
    assert any("T7 is failing" in w for w in what) and any("1800 mA" in w for w in what)
    assert "add a fan or a heatsink" in what and any("previous version was put back" in w for w in what)
    assert any("failed its test restore" in w for w in what) and len(what) == 6
    assert lines(got)[0].startswith("1. move EA06-6985") and "(drive roles)" in lines(got)[0]
    old = time.time() + 3 * 86400  # two days later the inbox items are gone
    assert len(needs(db, run, now=old)) == 4
    assert needs(None, None) == []


def test_the_feed_doctor_and_cli_show_it(config, db, capsys, monkeypatch):
    roles(config.paths.run_dir, brain="sd", drives=[{"id": "X", "name": "X", "role": "extra",
                                                    "why_not": "on USB 2 (move it to a blue USB 3 port)"}])  # fmt: skip
    f = feed.Feed(db, run_dir=config.paths.run_dir)
    st = f.status()
    assert len(st["needs"]) == 1
    now = "".join(t for t, _ in feed.now_line({"online": True, "state": "running", "activity": "reading",
                                               "needs": st["needs"]}))  # fmt: skip
    assert "(1 thing needs you: polymath needs)" in now
    paused = "".join(t for t, _ in feed.now_line({"online": True, "state": "paused", "paused_reason": "disk",
                                                  "needs": st["needs"] * 2}))  # fmt: skip
    assert "(2 things need you: polymath needs)" in paused
    monkeypatch.setattr("polymath.interface.snapshot.read_status", lambda _p: ({"needs": st["needs"]}, 1.0))
    checks = doctor.run_checks(config, "http://127.0.0.1:1", health=lambda _u: ("down", 0.0, None))
    assert any(c.name == "needs you" and "blue USB 3 port" in c.detail for c in checks)
    assert cli.cmd_needs(config, argparse.Namespace(json=False)) == 1
    assert "1. move X to a blue USB 3 port" in capsys.readouterr().out
    monkeypatch.setattr("polymath.interface.snapshot.read_status", lambda _p: (None, None))
    assert cli.cmd_needs(config, argparse.Namespace(json=True)) == 1  # from the brain itself
    assert json.loads(capsys.readouterr().out)[0]["source"] == "drive roles"
    monkeypatch.setattr(feed, "Path", None)  # anything going wrong inside: the feed still works
    assert feed.Feed(db, run_dir=config.paths.run_dir).needs() == []
