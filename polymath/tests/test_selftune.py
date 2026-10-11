"""Self-tuning with a safety net, and the changelog of what it changed about itself."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from polymath.drive import changelog, selftune
from polymath.drive.selftune import KNOBS, Trial
from polymath.interface import cli
from polymath.interface.face import Face
from polymath.interface.feed import render_event
from polymath.reasoning.link_prediction import LinkPredictor
from tests.fixtures.kb import build


def text_of(segs) -> str:
    return "".join(t for t, _ in segs)


def ctx(db, checkpoint=None, stop=lambda: False) -> Any:
    job = type("Job", (), {"checkpoint": checkpoint})()
    return type("Ctx", (), {"db": db, "job": job, "tick": staticmethod(lambda: None),
                            "should_stop": staticmethod(stop)})()  # fmt: skip


def quiz(db, at: float, acc: float) -> None:
    db.execute("INSERT INTO quizzes(created, n, correct, accuracy, chance, details) VALUES(?, 20, ?, ?, 0.25, '{}')",
               (at, int(acc * 20), acc))  # fmt: skip


# ------------------------------------------------------------------ changelog
def test_changelog_records_feeds_and_summarises(db):
    cid = changelog.record(db, "tuning", "link.subjects", "adopted", "Changed X from 1 to 2", old=1, new=2,
                           before=0.5, after=0.6)  # fmt: skip
    changelog.record(db, "tuning", "link.features", "rejected", "Tried Y", feed=False)
    changelog.record(db, "rules", "r", "rolled_back", "Undid Z")
    assert cid > 0
    assert [c["action"] for c in changelog.recent(db)] == ["rolled_back", "rejected", "adopted"]
    assert [c["action"] for c in changelog.recent(db, include_rejected=False)] == ["rolled_back", "adopted"]
    notes = db.query("SELECT text, detail FROM events WHERE kind = 'selfchange' ORDER BY id")
    assert [n["text"] for n in notes] == ["Changed X from 1 to 2", "Undid Z"]  # trials that changed nothing stay quiet
    c = changelog.counts(db, 0)
    assert changelog.describe_counts(c) == "Improved itself 1 time (1 adopted); undid 1 change that made it worse."
    assert changelog.describe_counts({}) == "" and changelog.describe_counts({"rejected": 3}) == ""
    assert changelog.describe_counts({"adopted": 2, "demoted": 1}) == "Improved itself 3 times (2 adopted, 1 demoted)."
    assert changelog.describe_counts({"reset": 1}) == ""  # your resets are not its improvements
    ev = {"kind": "note", "at": time.time(), "what": "selfchange", "status": "adopted", "text": "Changed X"}
    assert "improved" in text_of(render_event(ev))
    assert any(s == "yellow" for _t, s in render_event(ev | {"status": "rolled_back"}))
    f = Face()
    f.update({"online": True, "state": "running", "mode": "normal", "action": "self.tune"})
    assert f.mood(1.0) == "improving"
    f.see(ev, 1.0)
    assert f.mood(1.0) == "proud"
    f.see(ev | {"status": "rolled_back"}, 2.0)
    assert f.mood(2.0) == "oops"
    assert any(s == "yellow" for _t, s in render_event(ev | {"status": "reset"}))
    g = Face()
    g.update({"online": True, "state": "running", "mode": "normal", "action": "wikipedia.part"})
    g.see(ev | {"status": "reset"}, 1.0)
    assert g.mood(1.0) != "proud"


# ------------------------------------------------------------------ knobs and tests
def test_tuned_values_cache_and_grid(db):
    assert selftune.tuned(db, "link.subjects", 400) == 400
    selftune.set_value(db, "link.subjects", 800)
    assert selftune.tuned(db, "link.subjects", 400) == 800
    assert LinkPredictor(db).subjects == 800 and LinkPredictor(db, subjects=200).subjects == 200
    assert KNOBS["predict.min_confidence"].grid()[:3] == [0.3, 0.35, 0.4] and KNOBS["link.subjects"].grid()[:2] == [
        100.0, 200.0]  # fmt: skip
    assert selftune.neighbours("link.features", 40) == [20, 80] and selftune.neighbours("link.features", 5) == [10]
    assert selftune.neighbours("link.subjects", 800) == [400] and selftune.neighbours("link.subjects", 300) == [
        100,
        200,
    ]
    assert selftune.paired([1.0, 1.0, 1.0], [2.0, 2.0, 2.0]) == (1.0, 0.0)
    assert selftune.paired([1.0], [2.0])[1] == float("inf")
    assert selftune.fmt(0.45) == "0.45" and selftune.fmt(400) == "400"
    t = Trial("k", 1, 2, 0.5, 0.6, 50, 0.01, "u")
    assert t.significant(0.05) and not Trial("k", 1, 1, 0.5, 0.9, 50, 0.0, "u").significant(0.0)
    assert not Trial("k", 1, 2, 0.5, 0.52, 50, 0.02, "u").significant(0.0)  # within two standard errors


def test_trials_on_a_real_knowledge_base(db):
    build(db, n=30)
    for knob in ("link.subjects", "link.features", "predict.min_confidence"):
        t = selftune.run_trial(db, knob, lambda: None)
        assert t is not None and t.n >= 20 and t.knob == knob and t.unit
        assert t.best in {*KNOBS[knob].grid(), *selftune.LINK_CHOICES.get(knob, ())} and t.best != t.current
    assert selftune.run_trial(db, "infer.min_confidence", lambda: None) is None  # no audit data yet


def test_too_few_questions(db):
    assert selftune.run_trial(db, "link.subjects", lambda: None) is None
    assert selftune.run_trial(db, "predict.min_confidence", lambda: None) is None


def test_a_trial_pauses_between_slices_and_resumes(db):
    """A trial is worked through in many short slices, through JSON checkpoints, to the same result."""
    build(db, n=30)
    whole = selftune.run_trial(db, "link.features", lambda: None)
    db.kv_set("tune_cursor", 1)  # link.features is next
    out = selftune.tune_job(ctx(db, stop=lambda: True))
    slices = 1
    while not out.done:
        assert out.checkpoint and out.result["answered"] == slices  # one question per slice, at least
        out = selftune.tune_job(ctx(db, json.loads(json.dumps(out.checkpoint)), stop=lambda: True))
        slices += 1
    assert whole is not None and slices == 3 * whole.n  # the current value, half and double: every question once each
    assert out.result["knob"] == "link.features" and out.result["n"] == whole.n
    assert out.result["best"] == whole.best and out.result["gain"] == pytest.approx(round(whole.gain, 4))
    assert db.scalar("SELECT COUNT(*) FROM self_changes WHERE subject = 'link.features'") == 1


def test_tune_job_skips_when_every_knob_is_watched(db):
    for knob in KNOBS:
        changelog.record(db, "tuning", knob, "adopted", f"Changed {knob}", state="watching", feed=False)
    assert selftune.tune_job(ctx(db)).result["skipped"] == "every setting is under watch or pinned"


def test_inference_threshold_from_the_rule_audit(db):
    db.kv_set("rule_audit_buckets", {"0.30": [5, 15], "0.40": [8, 8], "0.50": [30, 3], "0.60": [40, 2]})
    t = selftune.infer_trial(db)
    assert t is not None and t.best == 0.35 and t.after > 0.8 > t.before  # low-confidence conclusions were mostly wrong
    db.kv_set("rule_audit_buckets", {"0.30": [40, 2]})
    assert selftune.infer_trial(db).best == 0.3  # type: ignore[union-attr]


def test_tune_job_adopts_rejects_and_the_guard_rolls_back(db, monkeypatch):
    now = time.time()
    for i in range(3):
        quiz(db, now - 3000 + i, 0.8)
    trials = iter([Trial("link.subjects", 400, 800, -1.0, -0.8, 60, 0.02, "log-likelihood"),
                   Trial("link.features", 40, 80, -1.0, -0.99, 60, 0.02, "log-likelihood")])  # fmt: skip
    monkeypatch.setattr(selftune, "start_trial", lambda db, knob, seed: {"knob": knob})
    monkeypatch.setattr(selftune, "finish", lambda db, state: next(trials))
    out = selftune.tune_job(ctx(db))
    assert out.result["adopted"] and selftune.tuned(db, "link.subjects", 0) == 800
    ch = db.one("SELECT * FROM self_changes WHERE action = 'adopted'")
    assert ch["state"] == "watching" and "from 400 to 800" in ch["summary"]
    assert json.loads(ch["detail"])["quiz_before"] == pytest.approx(0.8)
    out = selftune.tune_job(ctx(db))  # the next knob: no clear gain
    assert not out.result["adopted"] and db.scalar("SELECT COUNT(*) FROM self_changes WHERE action = 'rejected'") == 1
    # two self-tests after the change came out much worse: the old value comes back
    quiz(db, time.time() + 10, 0.55)
    quiz(db, time.time() + 20, 0.6)
    assert selftune.guard(db) == {"kept": 0, "rolled_back": 1}
    assert selftune.tuned(db, "link.subjects", 0) == 400
    rb = db.one("SELECT * FROM self_changes WHERE action = 'rolled_back'")
    assert "fell from 80% to 57%" in rb["summary"]
    assert db.scalar("SELECT state FROM self_changes WHERE id = ?", (ch["id"],)) == "final"


def test_guard_keeps_a_change_that_held_up_and_waits_for_quizzes(db, monkeypatch):
    now = time.time()
    quiz(db, now - 100, 0.7)
    monkeypatch.setattr(selftune, "start_trial", lambda db, knob, seed: {"knob": knob})
    monkeypatch.setattr(selftune, "finish",
                        lambda db, state: Trial(state["knob"], 0.4, 0.5, 0.1, 0.3, 60, 0.02, "reward"))  # fmt: skip
    selftune.tune_job(ctx(db))
    assert selftune.guard(db) == {"kept": 0, "rolled_back": 0}  # no self-test since: still watching
    quiz(db, time.time() + 5, 0.72)
    quiz(db, time.time() + 6, 0.69)
    assert selftune.guard(db) == {"kept": 1, "rolled_back": 0}
    # a knob under watch is not tested again; with nothing to test the job says so
    monkeypatch.setattr(selftune, "start_trial", lambda db, knob, seed: None)
    assert selftune.tune_job(ctx(db)).result["skipped"] == "not enough data yet"
    monkeypatch.setattr(selftune, "start_trial", lambda db, knob, seed: {"knob": knob})
    monkeypatch.setattr(selftune, "finish", lambda db, state: None)
    assert selftune.tune_job(ctx(db)).result["skipped"] == "not enough data yet"


def test_planner_and_cli(db, tmp_path, capsys):
    from polymath.core.scheduler import Scheduler

    agent = type("A", (), {"db": db, "scheduler": Scheduler(db)})()
    selftune.planner(agent)
    assert db.scalar("SELECT COUNT(*) FROM jobs WHERE kind = 'self.tune'") == 0  # no self-test yet
    quiz(db, time.time(), 0.5)
    selftune.planner(agent)
    assert db.scalar("SELECT COUNT(*) FROM jobs WHERE kind = 'self.tune'") == 1
    db.conn.commit()
    path = str(tmp_path / "polymath.toml")
    assert cli.main(["--config", path, "changes"]) == 0
    assert "no changes yet" in capsys.readouterr().out
    changelog.record(db, "tuning", "k", "adopted", "Changed k", state="watching")
    changelog.record(db, "tuning", "k2", "rejected", "Tried k2", feed=False)
    selftune.set_value(db, "k", 1)
    db.conn.commit()
    assert cli.main(["--config", path, "changes"]) == 0
    out = capsys.readouterr().out
    assert "Changed k" in out and "watching" in out and "Tried k2" not in out and "k = 1" in out
    assert cli.main(["--config", path, "changes", "--all", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data["changes"]) == 2 and data["tuned"] == {"k": 1}


def test_you_can_reset_and_pin_a_setting(db, tmp_path, capsys):
    changelog.record(db, "tuning", "link.features", "adopted", "Changed clues", state="watching", old=40, new=20)
    selftune.set_value(db, "link.features", 20)
    assert selftune.reset(db, ["link.features"]) == ["link.features"]
    assert selftune.tuned(db, "link.features", 40) == 40 and selftune.pinned(db) == {"link.features"}
    assert db.scalar("SELECT COUNT(*) FROM self_changes WHERE state = 'watching'") == 0
    assert "You reset how many of a subject's facts" in db.scalar("SELECT summary FROM self_changes "
                                                                  "WHERE action = 'reset'")  # fmt: skip
    # a pinned setting is skipped by the tuner
    db.kv_set("tune_cursor", 1)
    assert selftune.pick_knob(db) == "predict.min_confidence"
    with pytest.raises(ValueError, match="unknown setting nope"):
        selftune.reset(db, ["nope"])
    assert selftune.unpin(db, ["link.features"]) == ["link.features"] and selftune.pinned(db) == set()
    assert selftune.reset(db, ["all"]) == list(KNOBS) and selftune.unpin(db, ["all"]) == sorted(KNOBS)
    assert db.scalar("SELECT COUNT(*) FROM self_changes WHERE action = 'reset'") == 1  # nothing else had changed
    # from the command line
    db.conn.commit()
    path = str(tmp_path / "polymath.toml")
    assert cli.main(["--config", path, "changes", "--reset", "predict.min_confidence"]) == 0
    assert "reset to defaults and pinned: predict.min_confidence" in capsys.readouterr().out
    assert cli.main(["--config", path, "changes"]) == 0
    assert "pinned by you (not tuned): predict.min_confidence" in capsys.readouterr().out
    assert cli.main(["--config", path, "changes", "--allow", "all"]) == 0
    assert "may tune again: predict.min_confidence" in capsys.readouterr().out
    assert cli.main(["--config", path, "changes", "--allow", "all"]) == 0
    assert "nothing was pinned" in capsys.readouterr().out
    assert cli.main(["--config", path, "changes", "--reset", "bogus"]) == 2
    assert "unknown setting bogus" in capsys.readouterr().err
