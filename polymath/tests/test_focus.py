"""What it is learning and how far along: interface/focus.py, the feed's LEARNING lines and events, the web card."""

from __future__ import annotations

import io
import json
import sqlite3
import time
from typing import Any

import pytest

from polymath.core.db import Database
from polymath.interface import face as face_mod
from polymath.interface import feed, focus
from polymath.interface.feed import Feed, render_event, status_lines


def text_of(segs) -> str:
    return "".join(t for t, _s in segs)


@pytest.fixture(autouse=True)
def _fresh_memory(monkeypatch):
    monkeypatch.setattr(focus, "MEMORY", focus._Memory())


# ------------------------------------------------------------------ a small brain
class Brain:
    """Rows for focus to read: a topic, its articles and the things they mention, jobs and cycles."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.now = time.time()
        self.n = 0

    def doc(self, title: str, *, source: str = "wikipedia", fetched: float | None = None) -> int:
        self.n += 1
        cur = self.db.execute(
            "INSERT INTO documents(source, external_id, title, license, fetched, content_hash, nchars) "
            "VALUES(?,?,?,?,?,?,?)",
            (source, f"x:{self.n}", title, "CC BY-SA", self.now if fetched is None else fetched, f"h{self.n}", 100),
        )
        return int(cur.lastrowid or 0)

    def entity(self, label: str, doc_id: int | None = None, *, kind: str = "item") -> int:
        cur = self.db.execute(
            "INSERT INTO entities(key, label, kind, wiki_title, doc_id, updated) VALUES(?,?,?,?,?,?)",
            (f"wiki:{label}", label, kind, label, doc_id, self.now),
        )
        return int(cur.lastrowid or 0)

    def topic(self, name: str, *, n_docs: int = 0, gap: float | None = None, evidence: Any = None) -> int:
        cur = self.db.execute("INSERT INTO topics(name, n_docs, n_total) VALUES(?,?,?)", (name, n_docs, n_docs + 5))
        tid = int(cur.lastrowid or 0)
        if gap is not None:
            self.db.execute(
                "INSERT INTO topic_priority(topic_id, gap, importance, novelty, effort, priority, evidence, updated) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (tid, gap, 0.5, 1.0, 0.0, 0.3, json.dumps(evidence or {}), self.now),
            )
        return tid

    def file(self, doc_id: int, topic_id: int, *mentions: int) -> None:
        self.db.execute("INSERT INTO doc_topics(doc_id, topic_id) VALUES(?,?)", (doc_id, topic_id))
        for e in mentions:
            self.db.execute("INSERT INTO doc_entities(doc_id, entity_id, count, score) VALUES(?,?,1,1.0)", (doc_id, e))

    def job(self, kind: str, key: str, payload: dict[str, Any], *, cp: Any = None, state: str = "queued") -> int:
        cur = self.db.execute(
            "INSERT INTO jobs(kind, key, payload, checkpoint, state, created, updated) VALUES(?,?,?,?,?,?,?)",
            (kind, key, json.dumps(payload), json.dumps(cp) if cp is not None else None, state, self.now, self.now),
        )
        return int(cur.lastrowid or 0)

    def ran(self, job_id: int | None, action: str, *, ago: float = 1.0, status: str = "continue") -> None:
        t = self.now - ago
        self.db.execute("INSERT INTO cycles(started, ended, action, job_id, status) VALUES(?,?,?,?,?)",
                        (t - 1, t, action, job_id, status))  # fmt: skip

    def cycles(self) -> list[Any]:
        return self.db.query("SELECT action, status, job_id, ended FROM cycles ORDER BY id DESC LIMIT 30")


def photosynthesis(b: Brain) -> tuple[int, list[int], list[str]]:
    """A topic whose 2 articles mention 6 things; 2 of those it has read about. A pursuit queued 4 titles."""
    tid = b.topic("Photosynthesis", n_docs=2)
    d1, d2 = b.doc("Photosynthesis"), b.doc("Light")
    read = [b.entity("Plant", b.doc("Plant")), b.entity("Sun", b.doc("Sun"))]
    unread = [b.entity(name) for name in ("Chlorophyll", "Stoma", "Glucose", "Carbon fixation")]
    b.file(d1, tid, *read, unread[0], unread[1])
    b.file(d2, tid, unread[2], unread[3])
    return tid, unread, ["Chlorophyll", "Stoma", "Glucose", "Carbon_fixation"]


# ------------------------------------------------------------------ what it learns and why
def test_a_curiosity_round_says_topic_progress_and_what_it_read_last(db):
    b = Brain(db)
    tid, unread, titles = photosynthesis(b)
    jid = b.job("wikipedia.titles", f"pursue:{tid}:123", {"titles": titles, "topic_id": tid, "lang": "en"})
    b.ran(jid, "wikipedia.titles")
    f = focus.current(db, focus.pick(db, b.cycles(), b.now), b.now)
    assert f is not None
    assert (f["what"], f["why"], f["kind"], f["finished"]) == ("Photosynthesis", "curious", "wikipedia.titles", False)
    assert f["step"] == {"done": 0, "total": 4, "unit": "articles"} and f["item"] == ""
    t = f["topic"]
    assert t["based_on"] == "read" and (t["read"], t["linked"]) == (2, 6)
    assert t["known"] == pytest.approx(3 / 8) and t["start"] == pytest.approx(3 / 8)  # (read + 1) / (linked + 2)
    assert focus.basis(t) == "read about 2 of 6 things it mentions"
    # it reads two of the batch (underscores in titles are normalised) and links them: the numbers move
    for e, name in zip(unread[:2], ("Chlorophyll", "Stoma"), strict=True):
        db.execute("UPDATE entities SET doc_id=? WHERE id=?", (b.doc(name), e))
    cached = focus.current(db, jid, b.now + 1)
    assert cached is not None and cached["step"]["done"] == 2 and cached["item"] == "Stoma"
    assert cached["topic"]["known"] == pytest.approx(3 / 8)  # topic numbers are reused for TOPIC_TTL seconds
    later = focus.current(db, jid, b.now + focus.TOPIC_TTL + 1)
    assert later is not None and later["topic"]["known"] == pytest.approx(5 / 8)
    assert later["topic"]["start"] == pytest.approx(3 / 8) and focus.gain(later["topic"]) == pytest.approx(0.25)
    assert focus.last(db) == later


def test_quizzed_topics_use_right_answers_and_big_topics_the_hourly_value(db):
    b = Brain(db)
    tid = b.topic("Astronomy", n_docs=3)
    db.execute(
        "INSERT INTO quizzes(id, created, n, correct, accuracy, chance, details) VALUES(1, 0, 10, 7, .7, .25, '{}')"
    )
    for i in range(10):
        db.execute("INSERT INTO quiz_answers(quiz_id, triple_id, question, options, answer, chosen, correct, method, "
                   "confidence, topic_id) VALUES(1, ?, 'q', '[]', 1, 1, ?, 'graph', 1, ?)", (i, int(i < 7), tid))  # fmt: skip
    t = focus.topic_progress(db, tid, b.now)
    assert t is not None and t["based_on"] == "quiz" and (t["right"], t["asked"]) == (7, 10)
    assert t["known"] == pytest.approx(8 / 12) and focus.basis(t) == "7 of 10 test questions right"
    big = b.topic("History", n_docs=focus.LIVE_MAX_DOCS + 1, gap=0.6,
                  evidence={"gap": {"source": "unread_share", "linked": 900, "unread": 539}})  # fmt: skip
    h = focus.topic_progress(db, big, b.now)
    assert h is not None and h["hourly"] and h["known"] == pytest.approx(0.4) and (h["read"], h["linked"]) == (361, 900)
    assert focus.topic_progress(db, 999_999, b.now) is None
    empty = focus.topic_progress(db, b.topic("Nothing yet"), b.now)
    assert empty is not None and empty["known"] is None and focus.basis(empty) == ""


def test_who_chose_it_you_a_wrong_answer_or_an_agent(db):
    b = Brain(db)
    tid, _unread, titles = photosynthesis(b)
    asked = b.job("wikipedia.titles", "learn:photosynthesis:1",
                  {"titles": titles, "about": "photosynthesis", "topic_id": tid})  # fmt: skip
    f = focus.current(db, asked, b.now)
    assert f is not None and (f["what"], f["why"]) == ("photosynthesis", "you asked")
    assert f["topic"]["name"] == "Photosynthesis"  # your words stay; the topic's numbers come along
    old = b.job("wikipedia.titles", "learn:photosynthesis:2", {"titles": titles})  # before payloads named it
    g = focus.current(db, old, b.now)
    assert g is not None and g["what"] == "photosynthesis" and g["topic"]["name"] == "Photosynthesis"
    cur = db.execute("INSERT INTO remediation(origin, triple_id, subject, predicate, answer, wrong, options, question, "
                     "state, created, next_at, updated) VALUES('quiz', 1, 1, 1, 1, 2, '[]', "
                     "'What is the capital of Peru?', 'reading', 0, 0, 0)")  # fmt: skip
    fix = focus.current(db, b.job("wikipedia.titles", f"relearn:{cur.lastrowid}:0", {"titles": ["Lima"]}), b.now)
    assert fix is not None and (fix["what"], fix["why"]) == ("What is the capital of Peru?", "fixing a wrong answer")
    db.execute("INSERT INTO agents(id, name, kind, directive, subject, scope, params, created, updated) "
               "VALUES(7, 'Vega', 'research', 'research black holes', 'black holes', '{}', '{}', 0, 0)")  # fmt: skip
    ag = focus.current(db, b.job("wikipedia.titles", "agent:7:read:5", {"titles": ["Black hole"]}), b.now)
    assert ag is not None and (ag["what"], ag["why"]) == ("black holes", "agent Vega") and ag["topic"] is None
    req = focus.current(db, b.job("drive.learn", "drive.learn:x", {"query": "Photosynthesis"}), b.now)
    assert req is not None and (req["what"], req["why"]) == ("Photosynthesis", "you asked")
    assert req["topic"]["id"] == tid  # found by name
    assert focus.current(db, b.job("reason.rules", "reason.rules:1", {}), b.now) is None  # not a learning job
    assert focus.current(db, None, b.now) is None and focus.current(db, 123_456, b.now) is None


def test_each_kind_reports_its_own_progress(db):
    b = Brain(db)
    part = b.job("wikipedia.part", "wp:3", {"dump_url": "https://x/enwiki-pages-articles-multistream3.xml-p1p2.bz2",
                                             "limit": 5000}, cp={"stream": 9, "articles": 1200})  # fmt: skip
    b.doc("Andromeda Galaxy")
    p = focus.current(db, part, b.now)
    assert p is not None and p["what"] == "Wikipedia, dump part 3" and p["why"] == ""
    assert p["step"] == {"done": 1200, "total": 5000, "unit": "articles"} and p["item"] == "Andromeda Galaxy"
    assert p["source"] == "Wikipedia" and focus.step_text(p["step"]) == "1,200 of 5,000 articles"
    wd = b.job("wikidata.dump", "wd", {"url": "u", "size": 1000}, cp={"block_bit": 4000, "offset": 0})
    plant = b.entity("Fern")
    b.entity("Q42")  # an unnamed stub is never shown
    db.execute("INSERT INTO predicates(key, label) VALUES('P1', 'p')")
    db.execute("INSERT INTO triples(s, p, o, value, status, confidence, created, updated, holdout) "
               "VALUES(?, 1, 0, '1', 'sourced', 1, 0, 0, 0)", (plant,))  # fmt: skip
    w = focus.current(db, wd, b.now)
    assert w is not None and w["step"] == {"done": 500, "total": 1000, "unit": "bytes"} and w["item"] == "Fern"
    assert focus.step_text(w["step"]) == "50% of the file" and focus.step_fraction(w["step"]) == 0.5
    dl = focus.current(db, b.job("dump.download", "dl", {"url": "https://x/a.gz", "dest": "pubmed/b.xml.gz"},
                                 cp={"bytes": 25, "total": 100}), b.now)  # fmt: skip
    assert dl is not None and dl["what"] == "b.xml.gz" and focus.step_fraction(dl["step"]) == 0.25
    b.doc("Moby Dick", source="gutenberg")
    bk = focus.current(db, b.job("gutenberg.books", "gb", {"catalog": "c", "limit": 50}, cp={"stored": 3}), b.now)
    assert bk is not None and focus.step_text(bk["step"]) == "3 of 50 books" and bk["item"] == "Moby Dick"
    oa = focus.current(db, b.job("openalex.ingest", "oa", {}, cp={"position": 9, "stored": 7}), b.now)
    assert (
        oa is not None and focus.step_text(oa["step"]) == "7 records so far" and focus.step_fraction(oa["step"]) is None
    )
    old_doc = b.doc("Yesterday's news", source="feed", fetched=b.now - 7200)
    assert old_doc and (fp := focus.current(db, b.job("feeds.poll", "fp", {}), b.now)) is not None
    assert fp["item"] == "" and fp["step"] is None  # a stale document is not "what it read last"
    props = focus.current(db, b.job("wikidata.properties", "pi", {}, cp={"i": 1, "parts": [1, 2, 3, 4]}), b.now)
    assert props is not None and focus.step_text(props["step"]) == "1 of 4 dump parts"
    bad = focus.current(db, b.job("wikipedia.part", "bad", {"limit": "many"}, cp={"articles": "x"}), b.now)
    assert bad is not None and bad["step"] == {"done": 0, "total": None, "unit": "articles"}  # never breaks


def test_the_focus_stays_on_a_round_while_other_work_takes_turns(db):
    b = Brain(db)
    first = b.job("wikipedia.titles", "pursue:1:1", {"titles": ["A"]})
    other = b.job("feeds.poll", "fp", {})
    b.ran(first, "wikipedia.titles", ago=30)
    assert focus.pick(db, b.cycles(), b.now) == first
    b.ran(None, "memory.index", ago=20)  # not learning: the focus stays
    b.ran(other, "feeds.poll", ago=10)  # another learning job takes a turn: the round still runs
    assert focus.pick(db, b.cycles(), b.now) == first
    db.execute("UPDATE jobs SET state='done' WHERE id=?", (first,))
    assert focus.pick(db, b.cycles(), b.now) == other  # the round is over: the newest learning job
    assert focus.pick(db, b.cycles(), b.now + focus.RECENT_S + 60) is None  # nothing learned for a while


# ------------------------------------------------------------------ the terminal
def st_with(f: dict[str, Any] | None, **extra: Any) -> dict[str, Any]:
    st = {"online": True, "state": "running", "activity": "reading articles it is curious about", "mode": "normal",
          "counts": {"documents": 10, "entities": 5, "facts": 3, "rules": 1}}  # fmt: skip
    return st | ({"focus": f} if f is not None else {}) | extra


ROUND: dict[str, Any] = {"job": 5, "kind": "wikipedia.titles", "what": "Photosynthesis", "why": "curious", "item": "Stoma",
         "source": "Wikipedia", "finished": False, "step": {"done": 2, "total": 4, "unit": "articles"},
         "topic": {"id": 1, "name": "Photosynthesis", "known": 0.625, "start": 0.375, "based_on": "read", "read": 4,
                   "linked": 6}}  # fmt: skip


def test_header_shows_the_subject_and_progress_bars():
    lines = status_lines(st_with(ROUND))
    assert len(lines) == 5
    learning, progress = text_of(lines[2]), text_of(lines[3])
    assert learning == " LEARNING   Photosynthesis  · curious  · just read “Stoma”"
    assert progress == ("  knows █████████░░░░░ 62% (+25%) — read about 4 of 6 things it mentions"
                        "   this round ███████░░░░░░░ 2 of 4 articles")  # fmt: skip
    assert lines[2][0] == (" LEARNING ", "learn") and ("█████████░░░░░", "green") in lines[3]
    small = text_of(status_lines(st_with(ROUND), width=72)[3])
    assert "█████░░░ 62%" in small and "read about" not in small  # a small screen: short bars, no detail
    done = text_of(status_lines(st_with(ROUND | {"finished": True}))[2])
    assert done.startswith(" LEARNED ") and "2 of 4 articles ✓" in text_of(
        status_lines(st_with(ROUND | {"finished": True}))[3]
    )
    other = ROUND | {"what": "photosynthesis", "why": "you asked", "item": "", "step": None}
    o = status_lines(st_with(other))
    assert text_of(o[2]) == " LEARNING   photosynthesis  · you asked  · from Wikipedia"
    assert text_of(o[3]).startswith("  knows Photosynthesis ██")  # your words differ from the topic's name
    plain = ROUND | {"topic": None, "item": "", "source": "Wikipedia", "what": "Wikipedia, dump part 3",
                     "step": {"done": 1200, "total": None, "unit": "articles"}}  # fmt: skip
    p = status_lines(st_with(plain))
    assert (
        text_of(p[2]) == " LEARNING   Wikipedia, dump part 3  · curious"
        and text_of(p[3]) == "  this round 1,200 articles so far"
    )
    assert feed.bar(0.5, 4) == "██░░" and feed.bar(2, 3) == "███" and feed.bar(-1, 3) == "░░░"


def test_between_topics_it_shows_the_last_subject_and_what_is_next():
    st = st_with(None, last_focus=ROUND, curious=["Optics", "Algae", "Leaves", "Roots"])
    lines = status_lines(st)
    assert text_of(lines[2]) == " LEARNING   between topics — last: Photosynthesis (knows 62%)"
    assert text_of(lines[3]) == "  next: Optics · Algae · Leaves" and lines[2][0][1] == "learnoff"
    bare = status_lines(st_with(None))
    assert text_of(bare[2]) == " LEARNING   between topics" and text_of(bare[3]) == ""


def test_screen_keeps_six_header_rows_and_ascii_bars():
    class Ascii(io.StringIO):
        encoding = "ascii"

    buf = Ascii()
    s = feed.Screen(buf, color=False, fancy=True)
    s.cols, s.rows = 100, 30
    assert "##--" in s.paint([(feed.bar(0.5, 4), "green")])
    s.header([[("only one line", "")]])
    out = buf.getvalue()
    for row in range(1, 7):
        assert f"\033[{row};1H\033[2K" in out  # stale rows are cleared, the rule is on row 6
    assert "\033[6;1H\033[2K" + "-" * 100 in out
    pbuf = io.StringIO()
    plain = feed.Screen(pbuf, color=False, fancy=False, clock=lambda: 1000.0)
    plain.header(status_lines(st_with(None)), force_plain=True)
    assert " ·  ·" not in pbuf.getvalue() and "between topics" in pbuf.getvalue()  # empty lines skipped


# ------------------------------------------------------------------ feed lines and the face
def test_a_new_round_and_a_finished_one_get_a_feed_line():
    seen = feed._Seen()
    start = feed.transitions(seen, st_with(ROUND | {"topic": ROUND["topic"] | {"known": 0.375}}))
    ev = [e for e in start if e["kind"] == "focus"]
    assert len(ev) == 1 and not ev[0].get("done") and ev[0]["what"] == "Photosynthesis"
    assert text_of(render_event(ev[0])).endswith("learning started on Photosynthesis (curious) — knows 38% so far")
    assert not [e for e in feed.transitions(seen, st_with(ROUND)) if e["kind"] == "focus"]  # same round: quiet
    end = [e for e in feed.transitions(seen, st_with(ROUND | {"finished": True})) if e["kind"] == "focus"]
    assert len(end) == 1 and end[0]["done"]
    line = text_of(render_event(end[0]))
    assert "learned  finished a round on Photosynthesis (curious) · 2 of 4 articles · knows 38% → 62%" in line
    assert not [e for e in feed.transitions(seen, st_with(ROUND | {"finished": True})) if e["kind"] == "focus"]
    # a round already over when first seen, or one nobody chose with no size: no line
    assert not feed.focus_events(feed._Seen(), st_with(ROUND | {"job": 9, "finished": True}), 0)
    quiet = {"job": 11, "what": "news feeds", "why": "", "topic": None, "step": None, "finished": False}
    assert not feed.focus_events(seen, st_with(quiet), 0)
    flat = {"kind": "focus", "at": 0, "done": True, "what": "X", "known": 0.5, "start": 0.5, "step": None}
    assert text_of(render_event(flat)).endswith("finished a round on X · knows 50%")
    sized = {
        "kind": "focus",
        "at": 0,
        "what": "Wikipedia, dump part 3",
        "step": {"done": 1, "total": 9, "unit": "articles"},
    }
    assert text_of(render_event(sized)).endswith("started on Wikipedia, dump part 3 — 1 of 9 articles")


def test_the_face_is_proud_when_a_round_taught_it_something():
    f = face_mod.Face()
    f.see({"kind": "focus", "done": True, "known": 0.62, "start": 0.38}, now=100.0)
    assert f.reaction is not None and f.reaction.mood == "proud"
    g = face_mod.Face()
    g.see({"kind": "focus", "what": "X"}, now=100.0)
    assert g.reaction is not None and g.reaction.mood == "aha"
    h = face_mod.Face()
    h.see({"kind": "focus", "done": True, "known": 0.5, "start": 0.5}, now=100.0)
    assert h.reaction is None


# ------------------------------------------------------------------ the feed's status
def test_feed_status_carries_the_focus_and_never_breaks(db):
    b = Brain(db)
    tid, _unread, titles = photosynthesis(b)
    jid = b.job("wikipedia.titles", f"pursue:{tid}:1", {"titles": titles, "topic_id": tid})
    b.ran(jid, "wikipedia.titles")
    st = Feed(db, heavy_counts=False).status()
    assert st["focus"]["what"] == "Photosynthesis" and "last_focus" not in st
    b.ran(None, "noop", status="idle")
    db.execute("UPDATE cycles SET ended = ended - ? WHERE action='wikipedia.titles'", (focus.RECENT_S + 60,))
    st = Feed(db, heavy_counts=False).status()
    assert "focus" not in st and st["last_focus"]["what"] == "Photosynthesis"
    assert "between topics — last: Photosynthesis" in text_of(status_lines(st)[2])

    class Broken(Feed):
        pass

    feed_obj = Broken(db, heavy_counts=False)

    def boom(*_a: Any, **_k: Any) -> Any:
        raise sqlite3.OperationalError("interrupted")

    orig = focus.pick
    focus.pick = boom  # type: ignore[assignment]
    try:
        assert "focus" not in feed_obj.status() and "last_focus" not in feed_obj.focus([], 0.0)
    finally:
        focus.pick = orig  # type: ignore[assignment]


def test_reading_up_on_a_mistake_names_the_question(db, monkeypatch):
    """eval.remedy puts the question into the reading job, so the feed can say what it reads up on."""
    from polymath.evaluation import remedy

    calls: list[tuple[str, dict[str, Any]]] = []

    class Sched:
        def enqueue(self, kind: str, payload: dict[str, Any], **_kw: Any) -> tuple[int, bool]:
            calls.append((kind, payload))
            return 1, True

    class Ctx:
        scheduler = Sched()

    Ctx.db = db  # type: ignore[attr-defined]
    monkeypatch.setattr(remedy, "_titles", lambda _db, _ents: ["Lima"])
    item = {"subject": 1, "answer": 1, "wrong": 2, "attempts": 0, "question": "Where is Lima?", "id": 4}
    assert remedy.read_about(Ctx(), item) == ["Lima"]  # type: ignore[arg-type]
    assert calls == [("wikipedia.titles", {"titles": ["Lima"], "about": "Where is Lima?"})]
