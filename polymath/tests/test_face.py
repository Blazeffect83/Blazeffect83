"""The feed's face and version badge: polymath.interface.face, polymath.version, and how the feed shows them."""

from __future__ import annotations

import io
import json
import os
import time
import unicodedata
from pathlib import Path
from typing import Any

import pytest

from polymath import __version__
from polymath.core.loop import Agent
from polymath.interface import cli, feed
from polymath.interface.face import FRAME_S, MOODS, SPINNER, WIDTH, Face, mood_for_action
from polymath.interface.feed import Feed, Screen, badge, milestone_after, transitions
from polymath.version import Build, build_file, git_commit, read_build
from tests.conftest import ROOT

DEJAVU = Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf")


def text_of(segs) -> str:
    return "".join(t for t, _ in segs)


def online(**kw):
    return {"online": True, "state": "running", "mode": "normal", "activity": "x", "action": "wikipedia.part"} | kw


# ------------------------------------------------------------------ version
def test_read_build_prefers_the_installer_stamp(tmp_path, monkeypatch):
    stamp = tmp_path / "build.json"
    monkeypatch.setenv("POLYMATH_BUILD_FILE", str(stamp))
    assert build_file() == stamp
    dev = read_build()  # no stamp: this checkout's version and commit
    assert dev.version == __version__ and not dev.installed
    stamp.write_text(json.dumps({"version": "9.1.0", "commit": "abcdef1234567890", "date": "2026-10-10"}))
    b = read_build()
    assert b == Build("9.1.0", "abcdef123456", "2026-10-10", True)
    assert b.key == "9.1.0+abcdef123456" and b.label() == "v9.1.0 · abcdef123456"
    assert Build("1.0").key == "1.0" and Build("1.0").label() == "v1.0"
    for bad in ("not json", "[]", '{"commit": "x"}'):
        stamp.write_text(bad)
        assert not read_build().installed  # a broken stamp falls back to the checkout
    monkeypatch.delenv("POLYMATH_BUILD_FILE")
    assert build_file().name == "build.json"  # next to the virtual environment


def test_the_repository_reports_its_commit():
    head = git_commit(ROOT)
    if (ROOT.parent / ".git").exists() or (ROOT / ".git").exists():
        assert len(head) == 7 and all(c in "0123456789abcdef" for c in head)


def test_git_commit_reads_loose_packed_detached_and_linked_checkouts(tmp_path):
    sha = "0123456789abcdef0123456789abcdef01234567"
    repo = tmp_path / "repo"
    (repo / ".git/refs/heads").mkdir(parents=True)
    (repo / "pkg").mkdir()
    (repo / ".git/HEAD").write_text("ref: refs/heads/main\n")
    assert git_commit(repo / "pkg") == ""  # branch with no commit yet
    (repo / ".git/packed-refs").write_text(f"# pack-refs\n{sha} refs/heads/main\n")
    assert git_commit(repo / "pkg") == "0123456"
    (repo / ".git/refs/heads/main").write_text("fedcba9876543210\n")
    assert git_commit(repo / "pkg") == "fedcba9"  # a loose ref wins
    (repo / ".git/HEAD").write_text("abcdef0123456789\n")
    assert git_commit(repo) == "abcdef0"  # detached HEAD
    linked = tmp_path / "linked"
    (linked / "sub").mkdir(parents=True)
    wt = repo / ".git/worktrees/linked"
    wt.mkdir(parents=True)
    (wt / "HEAD").write_text("ref: refs/heads/main\n")
    (wt / "commondir").write_text("../..\n")
    (linked / ".git").write_text(f"gitdir: {wt}\n")
    assert git_commit(linked / "sub") == "fedcba9"  # worktree: the ref lives in the common directory
    (tmp_path / "plain").mkdir()
    (tmp_path / "plain/.git").write_text("garbage\n")
    assert git_commit(tmp_path / "plain") == git_commit(tmp_path)  # not a gitdir file: keep looking upwards


def test_git_commit_unreadable_head(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    assert git_commit(tmp_path) == ""  # no HEAD file


def test_agent_heartbeat_records_the_build_it_runs(config, db, tmp_path, monkeypatch):
    from polymath.body.systemd_notify import Notifier
    from polymath.core.app import build_components

    stamp = tmp_path / "b.json"
    stamp.write_text(json.dumps({"version": "0.2.0", "commit": "ab12cd3", "date": "2026-10-10"}))
    monkeypatch.setenv("POLYMATH_BUILD_FILE", str(stamp))
    agent = Agent(config, db, build_components(config, db).registry, notifier=Notifier({}))
    agent.start()
    hb = db.kv_get("heartbeat")
    assert hb["version"] == "v0.2.0 · ab12cd3" and hb["build"] == "0.2.0+ab12cd3"
    st = Feed(db).status()
    assert st["version"] == "v0.2.0 · ab12cd3" and st["build"] == "0.2.0+ab12cd3"
    assert badge(read_build(), st)[-1] == (" ✓", "bgreen")


def test_cli_version(config, tmp_path, monkeypatch, capsys):
    cfg = str(tmp_path / "polymath.toml")
    assert cli.main(["--config", cfg, "version"]) == 0
    out = capsys.readouterr().out
    assert f"installed: polymath v{__version__}" in out and "not running" in out  # no database yet
    stamp = tmp_path / "b.json"
    stamp.write_text(json.dumps({"version": "0.2.0", "commit": "ab12cd3", "date": "2026-10-10"}))
    monkeypatch.setenv("POLYMATH_BUILD_FILE", str(stamp))
    from polymath.core.db import open_database

    db = open_database(config.paths.db_path)
    db.kv_set("heartbeat", {"ts": time.time(), "state": "running", "version": "v0.2.0 · ab12cd3",
                            "build": "0.2.0+ab12cd3"})  # fmt: skip
    assert cli.main(["--config", cfg, "version"]) == 0
    out = capsys.readouterr().out
    assert "installed: polymath v0.2.0 · ab12cd3 (2026-10-10)" in out and "✓ running the installed build" in out
    db.kv_set("heartbeat", {"ts": time.time(), "state": "running", "version": "v0.1.0 · 9f8e7d6",
                            "build": "0.1.0+9f8e7d6"})  # fmt: skip
    db.close()
    assert cli.main(["--config", cfg, "version", "--json"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info == {"version": "0.2.0", "commit": "ab12cd3", "date": "2026-10-10", "installed": True,
                    "agent": "v0.1.0 · 9f8e7d6", "agent_current": False}  # fmt: skip
    assert cli.main(["--config", cfg, "version"]) == 0
    assert "↻ restart the agent" in capsys.readouterr().out
    assert cli.main(["--config", cfg, "status"]) == 0
    st = json.loads(capsys.readouterr().out)
    assert st["build"] == "v0.2.0 · ab12cd3" and st["agent_build"] == "v0.1.0 · 9f8e7d6"
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert "polymath v0.2.0 · ab12cd3" in capsys.readouterr().out


def test_version_numbers_agree():
    text = (ROOT / "pyproject.toml").read_text()
    assert f'version = "{__version__}"' in text


# ------------------------------------------------------------------ badge
def test_badge_states():
    b = Build("0.2.0", "ab12cd3", "2026-10-10", True)
    assert text_of(badge(b, None)) == "v0.2.0 · ab12cd3 · 2026-10-10"  # connecting: no verdict
    assert text_of(badge(b, online(online=False))) == "v0.2.0 · ab12cd3 · 2026-10-10"
    assert text_of(badge(b, online(build="0.2.0+ab12cd3"))) == "v0.2.0 · ab12cd3 · 2026-10-10 ✓"
    assert text_of(badge(b, online(build="0.1.0+9f8e7d6", version="v0.1.0 · 9f8e7d6"))) == (
        "v0.2.0 · ab12cd3 ↻ agent still on v0.1.0"
    )
    assert text_of(badge(b, online(build="0.2.0+1111111", version="v0.2.0 · 1111111"))) == (
        "v0.2.0 · ab12cd3 ↻ agent still on 1111111"  # same version number, older commit
    )
    assert text_of(badge(b, online())).endswith("agent still on the previous build")  # agent predates build stamps
    assert text_of(badge(Build("0.2.0"), online(build="0.2.0"))) == "v0.2.0 ✓"


def test_header_puts_the_badge_top_right_and_lets_it_win(monkeypatch):
    monkeypatch.setattr("shutil.get_terminal_size", lambda fallback=None: os.terminal_size((60, 20)))
    out = io.StringIO()
    s = Screen(out, color=False, fancy=True)
    s.start()
    right = [("v0.2.0 · ab12cd3", "dim"), (" ✓", "bgreen")]
    s.header([[("x" * 80, "")], [("second", "")]], right=right)
    first = out.getvalue().split("\033[1;1H\033[2K", 1)[1].split("\033[2;1H")[0]
    left, badge_part = first.split("\033[1;43H")
    assert len(left) == 60 - 18 - 2 and left.endswith("…")  # the left side is shortened, the badge is whole
    assert badge_part == "v0.2.0 · ab12cd3 ✓"
    plain = io.StringIO()
    p = Screen(plain, color=False, fancy=False)
    p.header([[("POLYMATH", "")]], right=right, force_plain=True)
    assert plain.getvalue() == "── POLYMATH · v0.2.0 · ab12cd3 ✓\n"


# ------------------------------------------------------------------ milestones
def test_milestones():
    assert milestone_after(990, 1_000) == 1_000
    assert milestone_after(1_900, 2_100) == 2_000
    assert milestone_after(4_000, 60_000) == 50_000  # the biggest one passed
    assert milestone_after(9_870_000, 10_010_000) == 10_000_000
    assert milestone_after(1_000, 1_999) is None and milestone_after(0, 999) is None
    seen = feed._Seen()
    assert transitions(seen, online(counts={"facts": 9_990_000, "documents": 10, "rules": 4})) == []  # baseline
    evs = transitions(seen, online(counts={"facts": 10_020_000, "documents": 10, "rules": 4}))
    assert [e["text"] for e in evs] == ["milestone: 10.00M facts known"]
    assert "milestone" in text_of(feed.render_event(evs[0])) and "★" in text_of(feed.render_event(evs[0]))
    evs = transitions(seen, online(counts={"facts": 10_020_000, "documents": 1_000, "rules": 4}))
    assert [e["text"] for e in evs] == ["milestone: 1,000 documents read"]


# ------------------------------------------------------------------ face
def test_every_face_is_single_width_and_fits():
    glyphs: set[str] = set()
    for name, m in MOODS.items():
        for f in m.frames:
            for t in m.trail:
                assert 1 + len(f"[{f}]{t}") <= WIDTH, name
                glyphs.update(f + t)
    glyphs.update("✦✧○ ")
    for ch in glyphs:
        assert unicodedata.east_asian_width(ch) not in {"W", "F"}, ch  # never a double-width cell
        assert ch.isascii() or ch in feed.ASCII_FALLBACK, ch  # terminals without Unicode get ASCII
    if DEJAVU.exists():  # the Raspberry Pi OS terminal font has every glyph
        assert not [ch for ch in glyphs if not ch.isascii() and ord(ch) not in _cmap(DEJAVU)]


def _cmap(path: Path) -> set[int]:
    import struct

    d = path.read_bytes()
    n = struct.unpack(">H", d[4:6])[0]
    tables = {d[12 + 16 * i : 16 + 16 * i].decode(): struct.unpack(">I", d[20 + 16 * i : 24 + 16 * i])[0]
              for i in range(n)}  # fmt: skip
    off = tables["cmap"]
    cps: set[int] = set()
    for i in range(struct.unpack(">H", d[off + 2 : off + 4])[0]):
        s = off + struct.unpack(">I", d[off + 8 + 8 * i : off + 12 + 8 * i])[0]
        if struct.unpack(">H", d[s : s + 2])[0] == 4:
            seg2 = struct.unpack(">H", d[s + 6 : s + 8])[0]
            ends = struct.unpack(f">{seg2 // 2}H", d[s + 14 : s + 14 + seg2])
            starts = struct.unpack(f">{seg2 // 2}H", d[s + 16 + seg2 : s + 16 + 2 * seg2])
            for a, b in zip(starts, ends, strict=True):
                cps.update(range(a, b + 1))
    return cps


@pytest.mark.parametrize(
    ("action", "mood"),
    [
        ("wikipedia.part", "reading"),
        ("crawl.step", "reading"),
        ("perception.read", "reading"),
        ("dump.download", "downloading"),
        ("web.blocklist", "downloading"),
        ("web.vet", "vetting"),
        ("reason.infer", "thinking"),
        ("embed.train", "training"),
        ("perception.train_linker", "training"),
        ("eval.quiz", "quizzing"),
        ("agents.step", "agents"),
        ("eval.digest", "writing"),
        ("body.backup", "tidying"),
        ("something.new", "thinking"),
        ("noop", "idle"),
        (None, "idle"),
    ],
)
def test_mood_follows_the_job(action, mood):
    assert mood_for_action(action) == mood


def test_mood_from_state_and_night():
    f = Face()
    assert f.mood(0) == "connecting"
    f.update(online(), offline="waiting")
    assert f.mood(0) == "connecting"
    f.update(online(online=False))
    assert f.mood(0) == "offline"
    f.update(online(mode="pause"))
    assert f.mood(0) == "paused"
    f.update(online(state="paused"))
    assert f.mood(0) == "paused"
    f.update(online(mode="throttle"))
    assert f.mood(0) == "hot"
    f.update(online(mode="yield"))
    assert f.mood(0) == "hot"
    f.update(online(activity="waiting for work", action="reason.infer"))
    noon = time.mktime((2026, 10, 10, 12, 0, 0, 0, 0, -1))
    night = time.mktime((2026, 10, 10, 3, 0, 0, 0, 0, -1))
    assert f.mood(noon) == "idle" and f.mood(night) == "sleepy"
    f.update(online(action="reason.infer"))
    assert f.mood(noon) == "thinking"


def test_reactions_last_a_while_and_respect_priority():
    f = Face()
    f.update(online())
    t = 1000.0
    f.see({"kind": "quiz", "correct": True}, t)
    assert f.mood(t + 1) == "happy" and f.mood(t + 3.1) == "reading"
    f.see({"kind": "quiz", "correct": False}, t)
    assert f.mood(t) == "oops"
    f.see({"kind": "note", "what": "fixed"}, t)
    assert f.mood(t) == "proud"
    f.see({"kind": "inferred"}, t + 1)  # lower priority: does not interrupt
    assert f.mood(t + 1) == "proud"
    f.see({"kind": "milestone"}, t + 2)  # higher: does
    assert f.mood(t + 2) == "celebrate"
    f.see({"kind": "inferred"}, t + 20)  # after it expired: anything goes
    assert f.mood(t + 20) == "aha"
    cases: list[tuple[dict[str, Any], str]] = [
        ({"kind": "quizscore", "accuracy": 0.8, "chance": 0.25}, "happy"),
        ({"kind": "quizscore", "accuracy": 0.2, "chance": 0.25}, "oops"),
        ({"kind": "disputed"}, "doubt"),
        ({"kind": "error"}, "error"),
        ({"kind": "note", "what": "site", "status": "approved"}, "happy"),
        ({"kind": "note", "what": "site", "status": "refused"}, "doubt"),
        ({"kind": "note", "what": "relearn"}, "reading"),  # no reaction
        ({"kind": "report"}, "happy"),
        ({"kind": "newagent"}, "happy"),
        ({"kind": "digest", "head": True}, "happy"),
        ({"kind": "digest"}, "reading"),
        ({"kind": "storage", "event": "added"}, "celebrate"),
        ({"kind": "read"}, "reading"),
    ]
    for ev, mood in cases:
        g = Face()
        g.update(online())
        g.see(ev, t)
        assert g.mood(t) == mood, ev
    g.see({"kind": "quiz", "correct": True})  # default clock
    assert g.mood() == "happy"


def test_segments_animate_blink_and_keep_their_width():
    f = Face()
    f.update(online())
    frames = [text_of(f.segments(n * FRAME_S)) for n in range(1, 13)]
    assert all(len(x) == WIDTH for x in frames)
    assert {x[1:6] for x in frames} == {"[◐‿◐]", "[◑‿◑]"}  # eyes run along the line
    assert {x[0] for x in frames} == {"✦", "✧"}  # the spark pulses
    assert text_of(f.segments(13 * FRAME_S))[1:6] == "[-‿-]"  # a blink
    f.update(online(action="embed.train"))
    assert {text_of(f.segments(n * FRAME_S))[6] for n in range(4)} == set(SPINNER)
    f.update(online(action="reason.infer"))
    assert [text_of(f.segments(n * FRAME_S))[6:9].strip() for n in range(1, 5)] == [".", "..", "...", ""]
    f.update(online(online=False))
    seg = f.segments(1.0)
    assert seg[0] == ("○", "red") and seg[1][0].startswith("[×_×]") and seg[1][1] == "red"
    f.update(None)
    assert {f.segments(n * FRAME_S)[0][0] for n in range(2)} == {" ", "✧"}  # connecting: the spark flickers
    f.update(online())
    f.see({"kind": "note", "what": "fixed"}, 50.0)
    assert text_of(f.segments(52.0)).startswith("✦[⌐■_■]") or text_of(f.segments(52.0)).startswith("✧[⌐■_■]")
    assert len(text_of(f.segments())) == WIDTH  # default clock


# ------------------------------------------------------------------ the feed loop
def fake_source(script):
    calls = []

    def source(cursor):
        calls.append(cursor)
        return script[min(len(calls) - 1, len(script) - 1)]

    return source, calls


def test_run_animates_between_polls_and_shows_face_and_badge(monkeypatch):
    monkeypatch.setattr("shutil.get_terminal_size", lambda fallback=None: os.terminal_size((120, 24)))
    st = online(cycle=5, counts={}, build="0.2.0+ab12cd3", version="v0.2.0 · ab12cd3")
    ev = {"kind": "quiz", "at": time.time(), "question": "q?", "chosen": "a", "answer": "a", "correct": True,
          "method": "rules", "conf": 0.9}  # fmt: skip
    source, _ = fake_source([{"cursor": "d1", "events": [ev], "status": st}])
    out = io.StringIO()
    slept: list[float] = []
    b = Build("0.2.0", "ab12cd3", "2026-10-10", True)
    rc = feed.run(source, out=out, color=False, fancy=True, max_polls=2, sleep=slept.append, clock=lambda: 0.0,
                  build=lambda: b)  # fmt: skip
    text = out.getvalue()
    assert rc == 0
    assert slept == [0.5, 0.5, 0.5]  # one animation frame at a time between the two polls
    assert text.count("\0337") >= 6  # the header is redrawn every frame
    assert "[^‿^]" in text  # a right answer makes it happy
    assert "v0.2.0 · ab12cd3 · 2026-10-10 ✓" in text


def sequence(*items):
    """A callable returning the items in turn, then the last one forever."""
    it = iter(items)
    last = [items[0]]

    def call():
        last[0] = next(it, last[0])
        return last[0]

    return call


def test_run_reloads_into_a_newly_installed_build(monkeypatch):
    builds = sequence(Build("0.2.0", "aaaaaaa", "", True), Build("0.2.0", "aaaaaaa", "", True),
                      Build("0.3.0", "bbbbbbb", "", True))  # fmt: skip
    ticks = iter(range(0, 10_000, 40))
    source, _ = fake_source([{"cursor": "d1", "events": [], "status": online(counts={})}])
    updated: list[Build] = []
    out = io.StringIO()
    feed.run(source, out=out, color=False, fancy=False, max_polls=3, sleep=lambda s: None,
             clock=lambda: float(next(ticks)), build=builds, on_update=updated.append)  # fmt: skip
    assert updated == [Build("0.3.0", "bbbbbbb", "", True)]
    assert "updated to v0.3.0 · bbbbbbb: reloading the feed" in out.getvalue()


def test_run_does_not_reload_a_development_checkout():
    ticks = iter(range(0, 10_000, 40))
    source, _ = fake_source([{"cursor": "d1", "events": [], "status": online(counts={})}])
    updated: list[Build] = []
    seq = sequence(Build("0.2.0", "a"), Build("0.2.0", "b"), Build("0.2.0", "c"))
    feed.run(source, out=io.StringIO(), color=False, fancy=False, max_polls=3, sleep=lambda s: None,
             clock=lambda: float(next(ticks)), build=seq, on_update=updated.append)  # fmt: skip
    assert updated == []  # no installer stamp: nothing to reload into


def test_install_writes_the_build_stamp(tmp_path):
    import subprocess

    stage = tmp_path / "stage"
    args = [str(ROOT / "install.sh"), "--root", str(stage), "--user", "tester", "--skip-venv"]
    first = subprocess.run(args, capture_output=True, text=True, check=True, timeout=120).stdout
    stamp = json.loads((stage / "opt/polymath/build.json").read_text())
    assert stamp["version"] == __version__ and stamp["commit"] == git_commit(ROOT)
    assert "build: polymath v" in first
    again = subprocess.run(args, capture_output=True, text=True, check=True, timeout=120).stdout
    assert "build: polymath" not in again  # unchanged: not rewritten
    assert read_build(stage / "opt/polymath/build.json").installed
