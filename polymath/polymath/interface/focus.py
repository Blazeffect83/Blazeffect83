"""What it is learning right now, and how far along it is: the ``LEARNING`` lines of the feed and the dashboards.

The ``NOW`` line says what the current job slice does ("indexing what it read"). Jobs run in short slices that take
turns, so ``NOW`` changes every few seconds. The *focus* is the learning thread underneath: the most recent reading
job, kept while it is still running even when other work takes a turn in between.

Read-only, from the agent's own tables (the job's payload and checkpoint, the newest document, the topic's
numbers). Nothing is written and nothing new is stored. A focus says:

* **what** — the subject: the topic curiosity pursues, what you asked about, the wrong answer it reads up on, an
  agent's subject, or the dump part, book list or file it works through;
* **why** — who chose it: ``you asked``, ``curious``, ``fixing a wrong answer``, ``agent <name>``, or its reading
  plan (empty);
* **item** — what it read last for it (the newest document or fact);
* **step** — this round's progress: articles of the batch read, the share of a dump, books of a list;
* **topic** — how well it knows the topic, the same measure curiosity ranks topics by (``drive/priority.py``):
  test questions on the topic answered right once it has been quizzed on it, otherwise the share of the things its
  articles mention that it has read about too. Live (every ``TOPIC_TTL`` seconds) for topics of up to
  ``LIVE_MAX_DOCS`` articles, otherwise the hourly value. ``start`` is the value when this process first saw the
  round, so ``known − start`` is what the round has taught it so far.
"""

from __future__ import annotations

import json
import re
import time
from collections import OrderedDict
from typing import Any

from polymath.core.db import Database
from polymath.senses.wikitext import normalize_title

LIVE_MAX_DOCS = 400  # a topic with more articles uses the hourly value (the live count would be slow on a Pi)
TOPIC_TTL = 30.0  # seconds a topic's numbers are reused
RECENT_S = 900.0  # a learning job that ran this recently is still the focus, even when finished
ITEM_S = 900.0  # the newest document is "what it read last" for a job only if it is this fresh
REMEMBER = 64  # jobs and topics remembered per process
MAX_TITLES = 200  # wikipedia.titles reads at most this many

SOURCES = {
    "wikipedia": "Wikipedia",
    "wikidata": "Wikidata",
    "openalex": "OpenAlex",
    "pubmed": "PubMed",
    "gutenberg": "Project Gutenberg",
    "stackexchange": "Stack Exchange",
    "feed": "news feeds",
    "web": "the web",
}
# job kind → (document source, what it works through when nothing more specific is known)
LEARNING: dict[str, tuple[str, str]] = {
    "wikipedia.titles": ("wikipedia", "Wikipedia articles"),
    "wikipedia.part": ("wikipedia", "Wikipedia"),
    "wikidata.dump": ("wikidata", "Wikidata facts"),
    "wikidata.properties": ("wikidata", "what Wikidata properties mean"),
    "openalex.ingest": ("openalex", "scholarly works (OpenAlex)"),
    "pubmed.ingest": ("pubmed", "medical abstracts (PubMed)"),
    "gutenberg.books": ("gutenberg", "books (Project Gutenberg)"),
    "stackexchange.ingest": ("stackexchange", "Stack Exchange questions and answers"),
    "feeds.poll": ("feed", "news feeds"),
    "crawl.step": ("web", "vetted web pages"),
    "dump.download": ("", "a dump file"),
    "drive.learn": ("", "what you asked"),
}
_PART = re.compile(r"multistream(\d+)")
_QID = re.compile(r"[QP]\d+")


class _Memory:
    """Per-process memory: where each round started, topic numbers (briefly) and the focus kept between slices."""

    def __init__(self) -> None:
        self.start: OrderedDict[tuple[str, int], float] = OrderedDict()
        self.topics: OrderedDict[tuple[str, int], tuple[float, dict[str, Any] | None]] = OrderedDict()
        self.focus: dict[str, int] = {}  # database → the job in focus
        self.last: dict[str, dict[str, Any]] = {}  # database → the last focus that had a subject

    @staticmethod
    def put(d: OrderedDict[Any, Any], key: Any, value: Any) -> None:
        d[key] = value
        d.move_to_end(key)
        while len(d) > REMEMBER:
            d.popitem(last=False)


MEMORY = _Memory()


def _db_key(db: Database) -> str:
    return str(getattr(db, "path", id(db)))


def _json(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        out = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return out if isinstance(out, dict) else {}


# ------------------------------------------------------------------------------------------------ the job
def pick(db: Database, cycles: list[Any], now: float | None = None) -> int | None:
    """The job in focus, from the newest cycles (newest first): the job already in focus while it still takes
    turns, else the newest learning job that ran in the last ``RECENT_S`` seconds."""
    now = time.time() if now is None else now
    key = _db_key(db)
    learning = [
        int(r["job_id"])
        for r in cycles
        if r["job_id"] is not None
        and str(r["action"] or "") in LEARNING
        and now - float(r["ended"] or 0) <= RECENT_S
        and r["status"] in {"done", "continue", "interrupted", "failed"}
    ]
    if not learning:
        MEMORY.focus.pop(key, None)
        return None
    kept = MEMORY.focus.get(key)
    if kept in learning and kept != learning[0]:
        state = db.scalar("SELECT state FROM jobs WHERE id=?", (kept,))
        if state in {"queued", "running"}:  # still on it: another learning job only took a turn
            return kept
    MEMORY.focus[key] = learning[0]
    return learning[0]


def current(db: Database, job_id: int | None, now: float | None = None) -> dict[str, Any] | None:
    """The focus for ``job_id`` (see the module docstring), or ``None`` when it is not a learning job."""
    now = time.time() if now is None else now
    if job_id is None:
        return None
    job = db.one("SELECT id, kind, key, payload, checkpoint, state, created FROM jobs WHERE id=?", (job_id,))
    if job is None or str(job["kind"]) not in LEARNING:
        return None
    kind = str(job["kind"])
    source, default_what = LEARNING[kind]
    payload, cp = _json(job["payload"]), _json(job["checkpoint"])
    out: dict[str, Any] = {
        "job": int(job["id"]),
        "kind": kind,
        "what": default_what,
        "why": "",
        "item": "",
        "source": SOURCES.get(source, ""),
        "step": None,
        "topic": None,
        "finished": str(job["state"]) in {"done", "dead"},
        "since": float(job["created"]),
    }
    topic_id = _origin(db, str(job["key"]), kind, payload, out)
    if kind == "wikipedia.titles":
        _titles(db, payload, out)
    else:
        _progress(kind, payload, cp, out)
        out["item"] = out["item"] or _newest_item(db, source, float(job["created"]), now)
    if topic_id is None and out["why"]:
        topic_id = _topic_named(db, str(out["what"]))
    if topic_id is not None:
        out["topic"] = topic_progress(db, topic_id, now)
        if out["topic"] is not None:
            if not out["why"] or out["what"] == default_what:
                out["what"] = out["topic"]["name"]
            _round_start(db, int(job["id"]), out["topic"])
    MEMORY.last[_db_key(db)] = out
    return out


def last(db: Database) -> dict[str, Any] | None:
    """The last focus this process saw (shown dimmed "between topics")."""
    return MEMORY.last.get(_db_key(db))


def _origin(db: Database, key: str, kind: str, payload: dict[str, Any], out: dict[str, Any]) -> int | None:
    """Who chose this round and what it is about (from the job key the producer gave it)."""
    head, _, rest = key.partition(":")
    topic = payload.get("topic_id")
    about = str(payload.get("about") or "").strip()
    if kind == "drive.learn":
        out["why"], out["what"] = "you asked", str(payload.get("query") or "what you asked")[:120]
    elif head == "learn":
        out["why"], out["what"] = "you asked", about or rest.rsplit(":", 1)[0] or out["what"]
    elif head == "pursue":
        out["why"] = "curious"
        if topic is None and rest.split(":", 1)[0].isdigit():
            topic = int(rest.split(":", 1)[0])
    elif head == "relearn":
        out["why"] = "fixing a wrong answer"
        if not about and rest.split(":", 1)[0].isdigit():
            about = str(db.scalar("SELECT question FROM remediation WHERE id=?", (int(rest.split(":", 1)[0]),),
                                  default="") or "")  # fmt: skip
        out["what"] = about[:120] or out["what"]
    elif head == "agent" and rest.split(":", 1)[0].isdigit():
        r = db.one("SELECT name, subject FROM agents WHERE id=?", (int(rest.split(":", 1)[0]),))
        if r is not None:
            out["why"], out["what"] = f"agent {r['name']}", about or str(r["subject"])[:120] or out["what"]
    try:
        return int(topic) if topic is not None else None
    except (TypeError, ValueError):
        return None


def _titles(db: Database, payload: dict[str, Any], out: dict[str, Any]) -> None:
    """``wikipedia.titles``: how many of the batch's articles it has now, and the newest of them."""
    titles = list(dict.fromkeys(normalize_title(str(t)) for t in (payload.get("titles") or [])[:MAX_TITLES]))
    titles = [t for t in titles if t]
    if not titles:
        return
    q = ",".join("?" * len(titles))
    done = int(db.scalar(f"SELECT COUNT(DISTINCT title) FROM documents WHERE source='wikipedia' AND title IN ({q})",
                         titles, default=0))  # fmt: skip
    out["step"] = {"done": done, "total": len(titles), "unit": "articles"}
    newest = db.scalar(f"SELECT title FROM documents WHERE source='wikipedia' AND title IN ({q}) ORDER BY id DESC "
                       "LIMIT 1", titles)  # fmt: skip
    out["item"] = str(newest or "")


def _progress(kind: str, payload: dict[str, Any], cp: dict[str, Any], out: dict[str, Any]) -> None:
    """This round's progress, from the job's own checkpoint (each kind keeps its own)."""

    def num(d: dict[str, Any], k: str) -> int | None:
        try:
            return int(d[k]) if d.get(k) is not None else None
        except (TypeError, ValueError):
            return None

    limit = num(payload, "limit")
    if kind == "wikipedia.part":
        m = _PART.search(str(payload.get("dump_url") or ""))
        out["what"] = f"Wikipedia, dump part {int(m.group(1))}" if m else "Wikipedia"
        out["step"] = {"done": num(cp, "articles") or 0, "total": limit, "unit": "articles"}
    elif kind == "wikidata.dump":
        size = min(num(payload, "size") or 0, num(payload, "max_bytes") or num(payload, "size") or 0)
        bit = num(cp, "block_bit")
        if size > 0 and bit is not None:
            out["step"] = {"done": min(size, bit // 8), "total": size, "unit": "bytes"}
        elif cp.get("entities") is not None:
            out["step"] = {"done": num(cp, "entities") or 0, "total": limit, "unit": "things"}
    elif kind == "dump.download":
        out["what"] = str(payload.get("dest") or payload.get("url") or "a dump file").rsplit("/", 1)[-1][:80]
        if (total := num(cp, "total")) and total > 0:
            out["step"] = {"done": num(cp, "bytes") or 0, "total": total, "unit": "bytes"}
    elif kind == "wikidata.properties" and isinstance(cp.get("parts"), list):
        out["step"] = {"done": num(cp, "i") or 0, "total": len(cp["parts"]), "unit": "dump parts"}
    elif kind in {"gutenberg.books", "openalex.ingest", "pubmed.ingest", "stackexchange.ingest"}:
        if cp.get("stored") is not None or limit:
            unit = {"gutenberg.books": "books", "pubmed.ingest": "abstracts"}.get(kind, "records")
            out["step"] = {"done": num(cp, "stored") or 0, "total": limit, "unit": unit}


def _newest_item(db: Database, source: str, since: float, now: float) -> str:
    """The newest document of ``source`` this round brought in (or the newest fact's subject, for Wikidata)."""
    if not source:
        return ""
    if source == "wikidata":
        for r in db.query("SELECT e.label, e.kind FROM triples t JOIN entities e ON e.id = t.s WHERE t.holdout = 0 "
                          "ORDER BY t.id DESC LIMIT 20"):  # fmt: skip
            if r["kind"] != "stub" and not _QID.fullmatch(str(r["label"])):
                return str(r["label"])
        return ""
    doc = db.one("SELECT title, fetched FROM documents WHERE source=? ORDER BY id DESC LIMIT 1", (source,))
    if doc is None or float(doc["fetched"]) < max(since, now - ITEM_S):
        return ""
    return str(doc["title"] or "")[:160]


def _topic_named(db: Database, name: str) -> int | None:
    name = name.strip()
    if not name:
        return None
    for candidate in dict.fromkeys((name, name[:1].upper() + name[1:], name.title())):
        tid = db.scalar("SELECT id FROM topics WHERE name=?", (candidate,))
        if tid is not None:
            return int(tid)
    return None


def _round_start(db: Database, job_id: int, topic: dict[str, Any]) -> None:
    key = (_db_key(db), job_id)
    if topic.get("known") is None:
        return
    if key not in MEMORY.start:
        MEMORY.put(MEMORY.start, key, float(topic["known"]))
    topic["start"] = MEMORY.start[key]


# ------------------------------------------------------------------------------------------------ the topic
def topic_progress(db: Database, topic_id: int, now: float | None = None) -> dict[str, Any] | None:
    """How well it knows a topic: ``known`` (0–1), what that is ``based_on`` (``quiz`` or ``read``, with the two
    numbers behind it), and ``docs`` (articles filed under it). Cached for ``TOPIC_TTL`` seconds."""
    now = time.time() if now is None else now
    key = (_db_key(db), topic_id)
    hit = MEMORY.topics.get(key)
    if hit is not None and now - hit[0] < TOPIC_TTL:
        return dict(hit[1]) if hit[1] is not None else None
    out = _measure(db, topic_id)
    MEMORY.put(MEMORY.topics, key, (now, out))
    return dict(out) if out is not None else None


def _measure(db: Database, topic_id: int) -> dict[str, Any] | None:
    t = db.one(
        "SELECT t.name, t.n_docs, t.n_total, p.gap, p.evidence FROM topics t "
        "LEFT JOIN topic_priority p ON p.topic_id = t.id WHERE t.id=?",
        (topic_id,),
    )
    if t is None:
        return None
    out: dict[str, Any] = {"id": topic_id, "name": str(t["name"]), "docs": int(t["n_total"] or 0), "known": None}
    quiz = db.one("SELECT COALESCE(SUM(correct), 0) AS c, COUNT(*) AS n FROM quiz_answers WHERE topic_id=?",
                  (topic_id,))  # fmt: skip
    if quiz is not None and int(quiz["n"]) > 0:  # quizzed on it: the share of right answers (as curiosity sees it)
        c, n = int(quiz["c"]), int(quiz["n"])
        return out | {"known": (c + 1) / (n + 2), "based_on": "quiz", "right": c, "asked": n}
    if int(t["n_docs"] or 0) <= LIVE_MAX_DOCS:
        s = db.one(
            "SELECT COUNT(DISTINCT de.entity_id) AS linked, "
            "COUNT(DISTINCT CASE WHEN e.doc_id IS NULL THEN de.entity_id END) AS unread "
            "FROM doc_topics dt JOIN doc_entities de ON de.doc_id = dt.doc_id JOIN entities e ON e.id = de.entity_id "
            "WHERE dt.topic_id = ?",
            (topic_id,),
        )
        if s is not None and int(s["linked"]) > 0:
            linked, unread = int(s["linked"]), int(s["unread"])
            return out | {"known": (linked - unread + 1) / (linked + 2), "based_on": "read",
                          "read": linked - unread, "linked": linked}  # fmt: skip
    if t["gap"] is not None:  # a big topic: the hourly value curiosity computed
        ev = _json(t["evidence"]).get("gap") or {}
        ev = ev if isinstance(ev, dict) else {}
        out |= {"known": 1.0 - float(t["gap"]), "based_on": "read", "hourly": True}
        if ev.get("linked") is not None:
            out |= {"read": int(ev["linked"]) - int(ev.get("unread") or 0), "linked": int(ev["linked"])}
    return out


# ------------------------------------------------------------------------------------------------ wording
def pct(x: float) -> str:
    return f"{100 * x:.0f}%"


def gain(topic: dict[str, Any] | None) -> float:
    """What this round has taught it so far (0 when unknown)."""
    if not topic or topic.get("known") is None or topic.get("start") is None:
        return 0.0
    return float(topic["known"]) - float(topic["start"])


def basis(topic: dict[str, Any]) -> str:
    """Why it thinks it knows that much, in a few words."""
    if topic.get("based_on") == "quiz":
        return f"{topic['right']} of {topic['asked']} test questions right"
    if topic.get("linked"):
        return f"read about {int(topic['read']):,} of {int(topic['linked']):,} things it mentions"
    return ""


def step_text(step: dict[str, Any]) -> str:
    """12 of 25 articles · 41% of the file · 3,204 articles so far."""
    done, total, unit = int(step.get("done") or 0), step.get("total"), str(step.get("unit") or "")
    if unit == "bytes":
        return f"{pct(done / total)} of the file" if total else ""
    if total:
        return f"{done:,} of {int(total):,} {unit}"
    return f"{done:,} {unit} so far"


def step_fraction(step: dict[str, Any] | None) -> float | None:
    if not step or not step.get("total"):
        return None
    return max(0.0, min(1.0, float(step.get("done") or 0) / float(step["total"])))
