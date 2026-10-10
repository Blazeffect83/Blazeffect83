"""Live learning feed: what the agent is learning, as it happens, in a terminal.

Server side — :class:`Feed` (read-only). Every poll returns the events that happened since the client's
cursor, read straight from the agent's own tables. There is no event log to keep, and nothing is written:

* ``read``      a document arrived (Wikipedia article, paper, book, feed item, crawled page);
* ``fact``      a fact was learned (from Wikidata, an infobox, or read in a sentence), ``disputed`` when
  sources disagree, ``inferred`` when a learned rule derived it;
* ``reason``    a reasoning run finished: rules learned (with examples), facts inferred, contradictions,
  source reliability;
* ``quiz``      a self-test answer on a hidden fact (right or wrong), ``quizscore`` the quiz result;
* ``agent``     an agent was rewarded or penalised, ``newagent`` an agent was spawned or evolved;
* ``request``   you asked it to learn something; ``report`` / ``backup`` nightly work; ``error`` a failed slice;
* ``storage``   a drive was plugged in and added to the brain, went away, came back, or was retired;
* ``site``      a new site passed or failed vetting (open-web learning), ``safety`` a safety list loaded;
* ``relearn`` / ``fixed``  reading up on a wrong self-test answer, and getting it right on the re-test;
* ``digest``    the daily "what I learned today" summary, line by line.

Busy streams are capped per poll: the newest few are shown, the rest are counted in a ``more`` event, so a
Wikidata ingest at thousands of facts per second stays readable. The cursor is opaque to clients.

Client side — :func:`run` polls the dashboard's ``/api/feed`` (the dashboard runs as the ``polymath`` user,
so the desktop user needs no access to the database) or the database directly, and prints the events.
On a terminal it pins a status header (state, current activity, knowledge counts, temperature) above
the scrolling feed. Piped output gets plain lines, plus a status line every minute.

The header also shows Polymath's face (:mod:`polymath.interface.face`), an animated character whose expression
follows what it is doing, and, top right, the installed version and commit with ``✓`` when the agent runs exactly
that build (``↻`` while an update waits for the agent to restart). When an update is installed, the feed reloads
itself into the new code.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from polymath.core.db import Database
from polymath.interface.answer import render_value
from polymath.interface.face import FRAME_S, Face
from polymath.version import Build, read_build

# --------------------------------------------------------------------------------------------- streams
# cursor letter → (table, events shown on first connect)
STREAMS: dict[str, tuple[str, int]] = {
    "d": ("documents", 4),
    "t": ("triples", 6),
    "r": ("reasoning_runs", 2),
    "q": ("quiz_answers", 3),
    "z": ("quizzes", 1),
    "a": ("agent_rewards", 3),
    "g": ("agents", 0),
    "p": ("reports", 1),
    "b": ("backups", 0),
    "j": ("jobs", 50),
    "c": ("cycles", 0),
    "v": ("volume_events", 3),
    "e": ("events", 8),
    "x": ("digests", 1),
}
CAPS = {"read": 4, "fact": 5, "inferred": 3, "disputed": 2, "quiz": 4, "agent": 5, "error": 3}
FACT_SAMPLE = 400  # newest facts examined per poll to find readable ones
EXACT_COUNT_SPAN = 200_000  # id spans larger than this are estimated instead of counted
COUNTS_TTL = 60.0  # full-table counts (documents, entities, facts) are refreshed this often
STUB_LABEL = re.compile(r"[QP]\d+")

# What each job kind means, for the "now:" line. tests/test_feed.py keeps this in step with the registry.
ACTIVITY: dict[str, str] = {
    "noop": "idling",
    "sources.plan": "planning which dumps and feeds to read",
    "dump.download": "downloading a dump file",
    "wikipedia.part": "reading Wikipedia articles",
    "wikidata.dump": "reading Wikidata facts",
    "wikidata.propindex": "locating Wikidata property pages",
    "wikidata.properties": "learning what Wikidata properties mean",
    "openalex.ingest": "reading scholarly works (OpenAlex)",
    "pubmed.ingest": "reading medical abstracts (PubMed)",
    "gutenberg.books": "reading books (Project Gutenberg)",
    "stackexchange.ingest": "reading Stack Exchange questions and answers",
    "feeds.poll": "checking news feeds",
    "crawl.step": "crawling the web politely",
    "memory.index": "indexing what it read",
    "memory.graph": "building the knowledge graph",
    "memory.topics": "organising topics",
    "perception.anchors": "harvesting Wikipedia links and infoboxes",
    "perception.automaton": "compiling its name recogniser",
    "perception.read": "reading documents for names and relations",
    "perception.train_sentences": "learning where sentences end",
    "perception.train_phrases": "learning phrases",
    "perception.train_linker": "training its entity linker",
    "perception.relations": "learning how relations are written",
    "embed.train": "training word embeddings",
    "embed.docs": "embedding documents",
    "embed.rebuild": "rebuilding the vector index",
    "embed.quality": "checking its embeddings",
    "reason.rules": "learning inference rules",
    "reason.infer": "inferring new facts",
    "reason.contradictions": "looking for contradictions",
    "reason.reliability": "learning which sources to trust",
    "reason.predict": "guessing facts it has not read yet",
    "wikipedia.titles": "reading articles it is curious about",
    "drive.pagerank": "ranking what matters",
    "drive.priorities": "deciding what to learn next",
    "drive.learn": "starting on what you asked",
    "agents.step": "agents at work",
    "agents.verify": "checking the agents' work",
    "agents.evolve": "evolving agents",
    "agents.command": "applying your agent commands",
    "body.backup": "backing up",
    "body.housekeeping": "housekeeping",
    "body.evict": "freeing disk space",
    "body.spill": "moving documents to a plugged-in drive",
    "web.blocklists": "refreshing the safety lists",
    "web.blocklist": "loading a safety list",
    "web.vet": "vetting new sites before visiting them",
    "web.trust": "checking the facts of sites on probation",
    "eval.digest": "writing today's digest",
    "eval.remedy": "going back over its mistakes",
    "body.recall": "bringing documents back from a drive",
    "eval.holdout": "hiding facts to test itself on",
    "eval.quiz": "quizzing itself",
    "eval.report": "writing the nightly report",
}


class FeedUnavailable(RuntimeError):
    """The feed cannot be read right now (agent or dashboard not up yet, database not initialised)."""


def parse_cursor(text: str | None) -> dict[str, int] | None:
    """``"d12.t3400.…"`` → ``{"d": 12, "t": 3400, …}``; None for a missing or malformed cursor."""
    if not text:
        return None
    out: dict[str, int] = {}
    for part in text.split("."):
        m = re.fullmatch(r"([a-z])(\d{1,18})", part)
        if m is None:
            return None
        if m.group(1) in STREAMS:
            out[m.group(1)] = int(m.group(2))
    return out


def format_cursor(cursor: dict[str, int]) -> str:
    return ".".join(f"{k}{cursor[k]}" for k in STREAMS if k in cursor)


def _short(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


# ---------------------------------------------------------------------------------------------- server
class Feed:
    """Read-only event source over the agent's tables (one per database connection)."""

    def __init__(self, db: Database, *, pulse: Path | None = None, stale: float = 60.0) -> None:
        self.db = db
        self.pulse = pulse
        self.stale = stale
        self._counts: dict[str, Any] = {}
        self._counts_at = -1e18

    # -------------------------------------------------------------- poll
    def poll(self, cursor: str | None = None) -> dict[str, Any]:
        heads = {
            k: int(self.db.scalar(f"SELECT MAX(id) FROM {table}", default=0) or 0) for k, (table, _) in STREAMS.items()
        }
        given = parse_cursor(cursor)
        if given is None:
            lo = {k: max(0, heads[k] - STREAMS[k][1]) for k in STREAMS}
        else:
            # a stream the client has not seen starts now; a cursor ahead of the data (restored backup) resets
            lo = {k: given[k] if k in given and given[k] <= heads[k] else heads[k] for k in STREAMS}
        events: list[dict[str, Any]] = []
        events += self._documents(lo["d"], heads["d"])
        events += self._facts(lo["t"], heads["t"])
        events += self._reasoning(lo["r"], heads["r"])
        events += self._quiz(lo["q"], heads["q"], lo["z"], heads["z"])
        events += self._agents(lo["a"], heads["a"], lo["g"], heads["g"])
        events += self._requests(lo["j"], heads["j"])
        events += self._reports_backups(lo["p"], heads["p"], lo["b"], heads["b"])
        events += self._failures(lo["c"], heads["c"])
        events += self._storage(lo["v"], heads["v"])
        events += self._events(lo["e"], heads["e"])
        events += self._digests(lo["x"], heads["x"])
        events.sort(key=lambda e: float(e.get("at") or 0))
        return {"cursor": format_cursor(heads), "events": events, "status": self.status()}

    def _count(self, table: str, lo: int, hi: int, where: str = "") -> int:
        if hi - lo > EXACT_COUNT_SPAN:
            return hi - lo
        extra = f" AND {where}" if where else ""
        return int(self.db.scalar(f"SELECT COUNT(*) FROM {table} WHERE id > ? AND id <= ?{extra}", (lo, hi), default=0))

    @staticmethod
    def _more(what: str, n: int, at: float, detail: str = "") -> list[dict[str, Any]]:
        return [{"kind": "more", "what": what, "n": n, "at": at, "detail": detail}] if n > 0 else []

    # -------------------------------------------------------------- streams
    def _documents(self, lo: int, hi: int) -> list[dict[str, Any]]:
        if hi <= lo:
            return []
        rows = self.db.query(
            "SELECT id, source, external_id, title, nchars, fetched FROM documents WHERE id > ? AND id <= ? "
            "ORDER BY id DESC LIMIT ?",
            (lo, hi, CAPS["read"]),
        )
        out = [
            {
                "kind": "read",
                "at": r["fetched"],
                "source": r["source"],
                "title": r["title"] or r["external_id"],
                "chars": int(r["nchars"]),
            }
            for r in reversed(rows)
        ]
        newest = float(rows[0]["fetched"]) if rows else time.time()
        return out + self._more("documents", self._count("documents", lo, hi) - len(out), newest)

    def _label(self, entity: int) -> str | None:
        r = self.db.one("SELECT label, kind FROM entities WHERE id=?", (entity,))
        if r is None or r["kind"] == "stub" or STUB_LABEL.fullmatch(str(r["label"])):
            return None  # a name it has not read yet: not worth showing
        return str(r["label"])

    def _how(self, triple_id: int) -> tuple[str, str]:
        r = self.db.one(
            "SELECT kind, source, doc_id, detail FROM provenance WHERE triple_id=? ORDER BY weight DESC, id LIMIT 1",
            (triple_id,),
        )
        if r is None:
            return "", ""
        kind, detail = str(r["kind"]), str(r["detail"] or "")
        if kind == "wikidata":
            return "Wikidata", ""
        if kind == "infobox":
            title = self.db.scalar("SELECT title FROM documents WHERE id=?", (int(r["doc_id"]),), default="")
            return "infobox", str(title or "")
        if kind == "pattern":
            return "read in text", _short(detail.split("): ", 1)[-1], 160)
        if kind == "rule":
            return f"{detail.split(':', 1)[0].split()[0] if detail.strip() else 'a'} rule", ""
        return str(r["source"]), ""

    def _facts(self, lo: int, hi: int) -> list[dict[str, Any]]:
        if hi <= lo:
            return []
        rows = self.db.query(
            "SELECT t.id, t.s, t.o, t.value, t.status, t.confidence, t.created, pr.label AS pl "
            "FROM triples t JOIN predicates pr ON pr.id = t.p "
            "WHERE t.id > ? AND t.id <= ? AND t.holdout = 0 ORDER BY t.id DESC LIMIT ?",
            (lo, hi, FACT_SAMPLE),
        )
        bucket = {"sourced": "fact", "inferred": "inferred", "disputed": "disputed"}
        shown: dict[str, int] = {"fact": 0, "inferred": 0, "disputed": 0}
        out: list[dict[str, Any]] = []
        for r in rows:
            kind = bucket.get(str(r["status"]), "fact")
            if shown[kind] >= CAPS[kind]:
                continue
            subject = self._label(int(r["s"]))
            if subject is None:
                continue
            if int(r["o"]):
                obj = self._label(int(r["o"]))
                if obj is None:
                    continue
            else:
                obj = _short(render_value(self.db, 0, str(r["value"])), 80)
            how, why = self._how(int(r["id"]))
            out.append(
                {
                    "kind": kind,
                    "at": r["created"],
                    "s": subject,
                    "p": str(r["pl"]),
                    "o": obj,
                    "conf": round(float(r["confidence"]), 2),
                    "how": how,
                    "why": why,
                }
            )
            shown[kind] += 1
        out.reverse()
        if hi - lo > EXACT_COUNT_SPAN:
            total, inferred = hi - lo, 0
        else:
            by = {
                str(r["status"]): int(r["n"])
                for r in self.db.query(
                    "SELECT status, COUNT(*) AS n FROM triples WHERE id > ? AND id <= ? GROUP BY status", (lo, hi)
                )
            }
            total, inferred = sum(by.values()), by.get("inferred", 0)
        rest = total - len(out)
        rest_inferred = max(0, inferred - shown["inferred"])
        newest = float(rows[0]["created"]) if rows else time.time()
        detail = f"{rest_inferred:,} inferred" if rest_inferred else ""
        return out + self._more("facts", rest, newest, detail)

    def _rule_examples(self, n: int = 3) -> list[str]:
        out: list[str] = []
        for r in self.db.query(
            "SELECT r.kind, p.label AS pl, q.label AS ql, e.label AS el FROM rules r JOIN predicates p ON p.id = r.p "
            "LEFT JOIN predicates q ON q.id = r.q AND r.kind = 'inverse' "
            "LEFT JOIN entities e ON e.id = r.q AND r.kind IN ('domain', 'range') "
            "ORDER BY r.support DESC LIMIT ?",
            (n,),
        ):
            kind = str(r["kind"])
            if kind == "inverse" and r["ql"]:
                out.append(f"{r['pl']} ⇄ {r['ql']}")
            elif kind == "transitive":
                out.append(f"{r['pl']} chains (transitive)")
            elif kind == "symmetric":
                out.append(f"{r['pl']} goes both ways")
            elif kind == "domain" and r["el"]:
                out.append(f"whatever has a {r['pl']} is a {r['el']}")
            elif kind == "range" and r["el"]:
                out.append(f"every {r['pl']} is a {r['el']}")
        return out

    def _reasoning(self, lo: int, hi: int) -> list[dict[str, Any]]:
        out = []
        for r in self.db.query(
            "SELECT kind, started, ended, result FROM reasoning_runs WHERE id > ? AND id <= ? ORDER BY id", (lo, hi)
        ):
            try:
                res = json.loads(r["result"])
            except ValueError:
                res = {}
            ev: dict[str, Any] = {
                "kind": "reason",
                "what": str(r["kind"]),
                "at": r["ended"],
                "seconds": round(float(r["ended"]) - float(r["started"]), 1),
                "result": res if isinstance(res, dict) else {},
            }
            if ev["what"] == "rules":
                ev["examples"] = self._rule_examples()
            out.append(ev)
        return out

    def _quiz(self, lo: int, hi: int, zlo: int, zhi: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if hi > lo:
            rows = self.db.query(
                "SELECT a.question, a.options, a.answer, a.chosen, a.correct, a.method, a.confidence, z.created "
                "FROM quiz_answers a JOIN quizzes z ON z.id = a.quiz_id WHERE a.id > ? AND a.id <= ? "
                "ORDER BY a.id DESC LIMIT ?",
                (lo, hi, CAPS["quiz"]),
            )
            for r in reversed(rows):
                try:
                    labels = {int(o["entity"]): str(o["label"]) for o in json.loads(r["options"])}
                except (ValueError, KeyError, TypeError):
                    labels = {}
                out.append(
                    {
                        "kind": "quiz",
                        "at": r["created"],
                        "question": str(r["question"]),
                        "chosen": labels.get(int(r["chosen"]), "?"),
                        "answer": labels.get(int(r["answer"]), "?"),
                        "correct": bool(r["correct"]),
                        "method": str(r["method"]),
                        "conf": round(float(r["confidence"]), 2),
                    }
                )
            newest = float(rows[0]["created"]) if rows else time.time()
            out += self._more("quiz answers", self._count("quiz_answers", lo, hi) - len(out), newest)
        out += [
            {
                "kind": "quizscore",
                "at": r["created"],
                "n": int(r["n"]),
                "correct": int(r["correct"]),
                "accuracy": float(r["accuracy"]),
                "chance": float(r["chance"]),
            }
            for r in self.db.query(
                "SELECT created, n, correct, accuracy, chance FROM quizzes WHERE id > ? AND id <= ? ORDER BY id",
                (zlo, zhi),
            )
        ]
        return out

    def _agents(self, lo: int, hi: int, glo: int, ghi: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = [
            {
                "kind": "newagent",
                "at": r["created"],
                "name": str(r["name"]),
                "directive": str(r["directive"]),
                "origin": str(r["origin"]),
                "parent": r["parent_name"],
            }
            for r in self.db.query(
                "SELECT a.created, a.name, a.directive, a.origin, p.name AS parent_name FROM agents a "
                "LEFT JOIN agents p ON p.id = a.parent WHERE a.id > ? AND a.id <= ? ORDER BY a.id",
                (glo, ghi),
            )
        ]
        if hi > lo:
            rows = self.db.query(
                "SELECT r.amount, r.reason, r.at, a.name, a.level, t.kind AS task_kind, t.payload "
                "FROM agent_rewards r JOIN agents a ON a.id = r.agent_id LEFT JOIN agent_tasks t ON t.id = r.task_id "
                "WHERE r.id > ? AND r.id <= ? ORDER BY r.id DESC LIMIT ?",
                (lo, hi, CAPS["agent"]),
            )
            out += [
                {
                    "kind": "agent",
                    "at": r["at"],
                    "name": str(r["name"]),
                    "amount": float(r["amount"]),
                    "reason": _short(str(r["reason"]), 140),
                    "task": _short(self._task_text(r["task_kind"], r["payload"]), 160),
                    "level": int(r["level"]),
                }
                for r in reversed(rows)
            ]
            newest = float(rows[0]["at"]) if rows else time.time()
            out += self._more(
                "agent rewards", self._count("agent_rewards", lo, hi) - min(len(rows), CAPS["agent"]), newest
            )
        return out

    def _entity_label(self, entity: Any) -> str:
        return str(self.db.scalar("SELECT label FROM entities WHERE id=?", (int(entity or 0),), default="?"))

    def _task_text(self, kind: str | None, payload: str | None) -> str:
        """What the rewarded task was about, from its payload: the question, the document, the prediction."""
        try:
            pl = json.loads(payload or "{}")
        except ValueError:
            return ""
        if not isinstance(pl, dict):
            return ""
        try:
            if kind == "quiz":
                labels = {int(o["entity"]): str(o["label"]) for o in pl.get("options", [])}
                return f"{pl.get('question', '')} → {labels.get(int(pl.get('chosen') or 0), '?')}"
            if kind == "digest":
                return str(pl.get("title") or "")
            if kind == "read":
                return ", ".join(str(t) for t in (pl.get("titles") or [])[:3])
            if kind == "user":
                return str(pl.get("text") or "")
            if kind == "predict":
                pred = self.db.scalar("SELECT label FROM predicates WHERE id=?", (int(pl.get("p") or 0),), default="?")
                return f"{self._entity_label(pl.get('s'))} → {pred} → {self._entity_label(pl.get('choice'))}"
            if kind == "dispute":
                t = self.db.one("SELECT s, p, o, value FROM triples WHERE id=?", (int(pl.get("choice") or 0),))
                if t is None:
                    return ""
                pred = self.db.scalar("SELECT label FROM predicates WHERE id=?", (int(t["p"]),), default="?")
                return f"{self._entity_label(t['s'])} → {pred} → {render_value(self.db, int(t['o']), str(t['value']))}"
        except (KeyError, TypeError, ValueError):
            return ""
        return ""

    def _requests(self, lo: int, hi: int) -> list[dict[str, Any]]:
        out = []
        for r in self.db.query(
            "SELECT payload, created FROM jobs WHERE id > ? AND id <= ? AND kind = 'drive.learn' ORDER BY id", (lo, hi)
        ):
            try:
                query = str(json.loads(r["payload"]).get("query", ""))
            except (ValueError, AttributeError):
                query = ""
            out.append({"kind": "request", "at": r["created"], "query": _short(query, 160)})
        return out

    def _reports_backups(self, lo: int, hi: int, blo: int, bhi: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for r in self.db.query(
            "SELECT created, day, path, summary FROM reports WHERE id > ? AND id <= ? ORDER BY id", (lo, hi)
        ):
            try:
                summary = json.loads(r["summary"])
            except ValueError:
                summary = {}
            out.append(
                {
                    "kind": "report",
                    "at": r["created"],
                    "day": str(r["day"]),
                    "path": str(r["path"]),
                    "summary": summary if isinstance(summary, dict) else {},
                }
            )
        out += [
            {
                "kind": "backup",
                "at": r["created"],
                "ok": bool(r["ok"]),
                "bytes": int(r["bytes"]),
                "detail": _short(str(r["detail"]), 120),
            }
            for r in self.db.query(
                "SELECT created, ok, bytes, detail FROM backups WHERE id > ? AND id <= ? ORDER BY id", (blo, bhi)
            )
        ]
        return out

    def _storage(self, lo: int, hi: int) -> list[dict[str, Any]]:
        out = []
        for r in self.db.query(
            "SELECT e.at, e.volume_id, e.event, e.detail, v.label, v.model FROM volume_events e "
            "LEFT JOIN volumes v ON v.id = e.volume_id WHERE e.id > ? AND e.id <= ? ORDER BY e.id",
            (lo, hi),
        ):
            try:
                detail = json.loads(r["detail"] or "{}")
            except ValueError:
                detail = {}
            detail = detail if isinstance(detail, dict) else {}
            name = str(detail.get("name") or r["label"] or r["model"] or str(r["volume_id"])[:8])
            out.append({"kind": "storage", "at": r["at"], "event": str(r["event"]), "name": name, "detail": detail})
            if detail.get("hint") and r["event"] in {"added", "online"}:  # its own line, so it is never cut off
                out.append({"kind": "storage", "at": r["at"], "event": "hint", "name": name, "detail": detail})
        return out

    def _events(self, lo: int, hi: int) -> list[dict[str, Any]]:
        out = []
        for r in self.db.query(
            "SELECT at, kind, text, detail FROM events WHERE id > ? AND id <= ? ORDER BY id DESC LIMIT 12", (lo, hi)
        ):
            try:
                detail = json.loads(r["detail"] or "{}")
            except ValueError:
                detail = {}
            out.append({"kind": "note", "at": r["at"], "what": str(r["kind"]), "text": _short(str(r["text"]), 240),
                        "status": str(detail.get("status", "")) if isinstance(detail, dict) else ""})  # fmt: skip
        return out[::-1]

    def _digests(self, lo: int, hi: int) -> list[dict[str, Any]]:
        from polymath.evaluation.digest import lines

        out: list[dict[str, Any]] = []
        for r in self.db.query("SELECT day, created, data FROM digests WHERE id > ? AND id <= ? ORDER BY id", (lo, hi)):
            try:
                data = json.loads(r["data"])
            except ValueError:
                continue
            out.append({"kind": "digest", "at": r["created"], "text": f"what I learned — {r['day']}", "head": True})
            out += [{"kind": "digest", "at": float(r["created"]) + 1e-6 * i, "text": line}
                    for i, line in enumerate(lines(data), 1)]  # fmt: skip
        return out

    def _failures(self, lo: int, hi: int) -> list[dict[str, Any]]:
        out = []
        for r in self.db.query(
            "SELECT ended, action, detail FROM cycles WHERE id > ? AND id <= ? AND status = 'failed' "
            "ORDER BY id DESC LIMIT ?",
            (lo, hi, CAPS["error"]),
        ):
            try:
                detail = json.loads(r["detail"] or "{}")
            except ValueError:
                detail = {}
            detail = detail if isinstance(detail, dict) else {}
            out.append(
                {
                    "kind": "error",
                    "at": r["ended"],
                    "job": str(r["action"] or ""),
                    "error": _short(str(detail.get("error", "")), 160),
                    "state": str(detail.get("state", "")),
                }
            )
        return out[::-1]

    # -------------------------------------------------------------- status
    def counts(self, now: float) -> dict[str, Any]:
        """Knowledge counts. Full-table counts are cached (``COUNTS_TTL``); small tables are read every time."""
        if now - self._counts_at >= COUNTS_TTL or not self._counts:
            facts = {
                r["status"]: int(r["n"])
                for r in self.db.query("SELECT status, COUNT(*) AS n FROM triples GROUP BY status")
            }
            self._counts = {
                "documents": int(
                    self.db.scalar("SELECT COUNT(*) FROM documents WHERE state != 'duplicate'", default=0)
                ),
                "entities": int(self.db.scalar("SELECT COUNT(*) FROM entities WHERE kind = 'item'", default=0)),
                "facts": sum(facts.values()),
                "inferred": facts.get("inferred", 0),
                "disputed": facts.get("disputed", 0),
            }
            self._counts_at = now
        quiz = self.db.one("SELECT accuracy, chance FROM quizzes ORDER BY id DESC LIMIT 1")
        return self._counts | {
            "rules": int(self.db.scalar("SELECT COUNT(*) FROM rules", default=0)),
            "agents": int(self.db.scalar("SELECT COUNT(*) FROM agents WHERE status = 'active'", default=0)),
            "quiz": dict(quiz) if quiz else None,
        }

    def brain(self) -> dict[str, Any]:
        """Total brain space: the main disk's budget plus the budgets of the drives plugged in now."""
        primary = self.db.kv_get("disk_budget_bytes")
        row = self.db.one("SELECT COUNT(*) AS n, COALESCE(SUM(budget_bytes), 0) AS b FROM volumes WHERE online = 1")
        drives, extra = (int(row["n"]), int(row["b"])) if row else (0, 0)
        return {"primary_bytes": int(primary or 0), "drives": drives, "drive_bytes": extra}

    def status(self) -> dict[str, Any]:
        now = time.time()
        hb = self.db.kv_get("heartbeat") or {}
        age = now - float(hb.get("ts", 0)) if hb else None
        if hb and self.pulse is not None and hb.get("state") not in {"stopped", None}:
            try:  # a long slice cannot commit its heartbeat; the pulse file shows the agent is alive
                age = min(age if age is not None else 1e18, now - self.pulse.stat().st_mtime)
            except OSError:
                pass
        state = str(hb.get("state") or "not started")
        online = age is not None and age < self.stale and state != "stopped"
        activity = action = ""
        for r in self.db.query("SELECT action, status FROM cycles ORDER BY id DESC LIMIT 30"):
            if r["status"] in {"done", "continue", "interrupted", "failed"} and r["action"]:
                action = str(r["action"])
                activity = ACTIVITY.get(action, action)
                break
            if r["status"] == "idle":
                activity = "waiting for work"
                break
        vit = self.db.one("SELECT temp_c, mode FROM vitals ORDER BY at DESC LIMIT 1")
        curious = [
            str(r["name"])
            for r in self.db.query(
                "SELECT t.name FROM topic_priority tp JOIN topics t ON t.id = tp.topic_id WHERE tp.priority > 0 "
                "ORDER BY tp.priority DESC LIMIT 3"
            )
        ]
        return {
            "online": online,
            "state": state,
            "cycle": hb.get("cycle"),
            "heartbeat_age_s": None if age is None else round(age, 1),
            "activity": activity,
            "action": action,
            "version": hb.get("version"),
            "build": hb.get("build"),
            "temp_c": None if vit is None or vit["temp_c"] is None else round(float(vit["temp_c"]), 1),
            "mode": str(vit["mode"]) if vit else "normal",
            "paused_reason": self.db.kv_get("paused_reason") if state == "paused" else None,
            "curious": curious,
            "counts": self.counts(now),
            "brain": self.brain(),
        }


# ---------------------------------------------------------------------------------------------- client
def http_source(base_url: str, *, timeout: float = 5.0) -> Callable[[str | None], dict[str, Any]]:
    """Poll the dashboard's ``/api/feed``; never through a proxy (it is a local service)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    endpoint = base_url.rstrip("/") + "/api/feed"

    def fetch(cursor: str | None) -> dict[str, Any]:
        url = endpoint + ("?" + urllib.parse.urlencode({"cursor": cursor}) if cursor else "")
        try:
            with opener.open(url, timeout=timeout) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            reason = "the database is not initialised yet" if exc.code == 503 else f"HTTP {exc.code}"
            raise FeedUnavailable(reason) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise FeedUnavailable(f"no answer from {base_url}") from exc
        except ValueError as exc:
            raise FeedUnavailable("unexpected answer from the dashboard") from exc
        if not isinstance(data, dict) or "events" not in data:
            raise FeedUnavailable("unexpected answer from the dashboard")
        return data

    return fetch


def direct_source(db_path: Path, *, pulse: Path | None = None) -> Callable[[str | None], dict[str, Any]]:
    """Read the database directly (read-only); for users who can read the data directory."""
    feed: Feed | None = None

    def fetch(cursor: str | None) -> dict[str, Any]:
        nonlocal feed
        try:
            if feed is None:
                if not db_path.exists():
                    raise FeedUnavailable("the database is not initialised yet")
                feed = Feed(Database(db_path, readonly=True), pulse=pulse)
            return feed.poll(cursor)
        except FeedUnavailable:
            raise
        except Exception as exc:  # locked, permission denied, mid-restore…
            feed = None
            raise FeedUnavailable(f"cannot read the database: {type(exc).__name__}") from exc

    return fetch


Seg = tuple[str, str]  # (text, style)
ANSI = {
    "": "",
    "dim": "2",
    "bold": "1",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "blue": "34",
    "magenta": "35",
    "cyan": "36",
    "bred": "1;31",
    "bgreen": "1;32",
    "byellow": "1;33",
    "bcyan": "1;36",
    "bmagenta": "1;35",
}
ASCII_FALLBACK = {
    "→": "->",
    "⇄": "<->",
    "✓": "ok",
    "✗": "x",
    "●": "*",
    "○": "o",
    "·": "|",
    "…": "...",
    "─": "-",
    "°": "",
    "“": '"',
    "”": '"',
    # the face (polymath.interface.face)
    "✦": "*",
    "✧": "+",
    "◐": "o",
    "◑": "o",
    "◔": "o",
    "◕": "o",
    "‿": "_",
    "≖": "=",
    "¬": "-",
    "•": "o",
    "ω": "w",
    "ᵔ": "^",
    "˘": "-",
    "ᵒ": "o",
    "⌐": "",
    "■": "#",
    "×": "x",
    "★": "*",
    "☆": "+",
    "✎": "/",
    "≈": "~",
    "◜": "-",
    "◝": "\\",
    "◞": "|",
    "◟": "/",
    "↻": "~",
}


def compact(n: int | float) -> str:
    n = float(n)
    if abs(n) >= 1e9:
        return f"{n / 1e9:.2f}B"
    if abs(n) >= 1e6:
        return f"{n / 1e6:.2f}M"
    return f"{int(n):,}"


def human_bytes(n: int) -> str:
    if n >= 1e12:
        return f"{n / 1e12:.2f} TB"
    return f"{n / 1e9:.2f} GB" if n >= 1e9 else f"{n / 1e6:.1f} MB" if n >= 1e6 else f"{n / 1e3:.0f} kB"


def _tag(name: str, style: str) -> Seg:
    return (f"{name:<9}" if len(name) < 9 else name + " ", style)


def render_event(e: dict[str, Any]) -> list[Seg]:
    """One event → coloured segments (time, tag, text)."""
    at = time.strftime("%H:%M:%S", time.localtime(float(e.get("at") or time.time())))
    head: list[Seg] = [(at, "dim"), ("  ", "")]
    k = e.get("kind")
    if k == "read":
        return [
            *head,
            _tag("read", "blue"),
            (f"{e['source']:<14} ", "dim"),
            (str(e["title"]), "bold"),
            (f"  · {compact(e['chars'])} chars", "dim"),
        ]
    if k in {"fact", "inferred", "disputed"}:
        style = {"fact": "green", "inferred": "cyan", "disputed": "yellow"}[k]
        segs = [*head, _tag(k, style), (e["s"], "bold"), (f" → {e['p']} → ", "dim"), (e["o"], "bold")]
        how = f"  [{e['how']} {e['conf']:.2f}]" if e.get("how") else f"  [{e['conf']:.2f}]"
        segs.append((how, "dim"))
        if e.get("why"):
            segs.append((f"  “{e['why']}”", "dim"))
        return segs
    if k == "more":
        extra = f" ({e['detail']})" if e.get("detail") else ""
        return [*head, _tag("", ""), (f"+{e['n']:,} more {e['what']}{extra}", "dim")]
    if k == "reason":
        res, what = e.get("result") or {}, e.get("what")
        if what == "rules":
            counts = ", ".join(f"{v} {kind}" for kind, v in res.items() if v)
            ex = e.get("examples") or []
            text = f"learned rules: {counts or 'none yet'}" + (f"  e.g. {'; '.join(ex)}" if ex else "")
        elif what == "infer":
            text = f"inferred {res.get('new', 0):,} new facts from {res.get('examined', 0):,} examined"
            if res.get("violations"):
                text += f", flagged {res['violations']:,} type violations"
        elif what == "contradictions":
            text = (
                f"checked {res.get('groups_checked', 0):,} claims: {res.get('conflicts', 0):,} conflicts, "
                f"{res.get('disputed', 0):,} disputed, {res.get('settled', 0):,} settled"
            )
        elif what == "reliability":
            src = sorted((res.get("sources") or {}).items(), key=lambda kv: -float(kv[1]))
            text = (
                "source trust: " + ", ".join(f"{s} {float(v):.2f}" for s, v in src[:5])
                if src
                else "source trust: no data yet"
            )
        else:
            text = f"{what}: {json.dumps(res)[:120]}"
        return [*head, _tag("reason", "magenta"), (text, ""), (f"  ({e.get('seconds', 0)} s)", "dim")]
    if k == "quiz":
        mark: Seg = ("✓ ", "bgreen") if e["correct"] else ("✗ ", "bred")
        segs = [
            *head,
            _tag("quiz", "green" if e["correct"] else "red"),
            mark,
            (e["question"], ""),
            (" → ", "dim"),
            (e["chosen"], "bold"),
        ]
        if not e["correct"]:
            segs.append((f"  (truth: {e['answer']})", "yellow"))
        return [*segs, (f"  [{e['method']} {e['conf']:.2f}]", "dim")]
    if k == "quizscore":
        return [
            *head,
            _tag("quiz", "bgreen"),
            (f"self-test: {e['correct']}/{e['n']} = {e['accuracy']:.0%}", "bold"),
            (f"  (guessing would score {e['chance']:.0%})", "dim"),
        ]
    if k == "agent":
        amt = float(e["amount"])
        return [
            *head,
            _tag("agent", "yellow"),
            (str(e["name"]), "bold"),
            (f"  {amt:+.2f}  ", "green" if amt >= 0 else "red"),
            (str(e["reason"]), ""),
            (f"  · level {e['level']}", "dim"),
            (f"  {e['task']}" if e.get("task") else "", "dim"),
        ]
    if k == "newagent":
        origin = f"evolved from {e['parent']}" if e.get("origin") == "evolved" and e.get("parent") else "spawned"
        return [
            *head,
            _tag("agent", "byellow"),
            (f"new agent {e['name']}", "bold"),
            (f" ({origin}): ", "dim"),
            (e["directive"], ""),
        ]
    if k == "request":
        return [*head, _tag("you", "bcyan"), ("asked to learn: ", ""), (e["query"], "bold")]
    if k == "report":
        s = e.get("summary") or {}
        q = s.get("quiz") or {}
        extra = f" · quiz {q.get('accuracy', 0):.0%}" if isinstance(q, dict) and q.get("accuracy") is not None else ""
        docs = f" · {s['documents']:,} documents" if isinstance(s.get("documents"), int) else ""
        return [
            *head,
            _tag("report", "bmagenta"),
            (f"nightly report {e['day']}", "bold"),
            (f"{docs}{extra}  {e['path']}", "dim"),
        ]
    if k == "backup":
        if e["ok"]:
            return [*head, _tag("backup", "dim"), (f"verified backup saved ({human_bytes(e['bytes'])})", "dim")]
        return [*head, _tag("backup", "red"), (f"backup failed: {e['detail']}", "red")]
    if k == "error":
        retry = " (will retry)" if e.get("state") == "queued" else " (gave up)" if e.get("state") == "dead" else ""
        return [*head, _tag("error", "red"), (f"{e['job']}: {e['error']}{retry}", "red")]
    if k == "note":
        what, status = e.get("what"), e.get("status")
        style = {
            "site": {"approved": "bgreen", "probation": "green", "refused": "yellow", "dropped": "red"}.get(
                str(status), "cyan"
            ),
            "safety": "bmagenta",
            "relearn": "magenta",
            "fixed": "bgreen",
            "wear": "red" if status in {"read-only", "critical"} else "yellow",
            "home": "bgreen",
            "didyouknow": "bcyan",
            "prediction": "bgreen" if status == "confirmed" else "yellow",
            "recap": "bmagenta",
        }.get(str(what), "")
        tag = {
            "site": "site",
            "safety": "safety",
            "relearn": "relearn",
            "fixed": "fixed ✓",
            "wear": "SD card",
            "home": "storage",
            "didyouknow": "fun fact",
            "prediction": "predicted",
        }.get(str(what), str(what))
        return [*head, _tag(tag, style), (str(e["text"]), "bold" if what in {"didyouknow", "home"} else "")]
    if k == "digest":
        if e.get("head"):
            return [*head, _tag("digest", "bmagenta"), (str(e["text"]), "bold")]
        return [*head, _tag("", ""), ("· " + str(e["text"]), "")]
    if k == "storage":
        d = e.get("detail") or {}
        ev = e.get("event")
        size = f" ({human_bytes(int(d['size']))} {d.get('fstype', '')})" if d.get("size") else ""
        if ev == "added":
            use = f": {human_bytes(int(d.get('budget', 0)))} of it is now brain space" if d.get("budget") else ""
            if d.get("home"):
                use = ": the whole brain lives on it now"
            return [*head, _tag("storage", "bgreen"), (f"new drive {e['name']}{size}", "bold"), (use, "")]
        if ev == "online":
            return [
                *head,
                _tag("storage", "green"),
                (f"drive {e['name']} is back; its documents are readable again", ""),
            ]
        if ev == "offline":
            return [
                *head,
                _tag("storage", "yellow"),
                (f"drive {e['name']} was unplugged; its documents wait for it", ""),
            ]
        if ev == "hint":
            return [*head, _tag("storage", "yellow"), (str(d.get("hint", "")), "yellow")]
        if ev == "retired":
            text = f"drive {e['name']} retired: {d.get('recalled', 0):,} documents brought back; safe to unplug"
            return [*head, _tag("storage", "bmagenta"), (text, "")]
        return [*head, _tag("storage", ""), (f"drive {e['name']}: {ev}", "")]
    if k == "body":
        return [*head, _tag("body", str(e.get("style", "yellow"))), (str(e["text"]), "")]
    if k == "curious":
        return [*head, _tag("curious", "magenta"), ("most curious about: ", ""), (", ".join(e["topics"]), "bold")]
    if k == "milestone":
        return [*head, _tag("milestone", "bmagenta"), (str(e["text"]), "bold"), (" ★", "byellow")]
    if k == "link":
        return [*head, _tag("feed", str(e.get("style", "dim"))), (str(e["text"]), str(e.get("style", "dim")))]
    return [*head, _tag(str(k), ""), (json.dumps(e, default=str)[:200], "dim")]


def status_lines(st: dict[str, Any] | None, *, offline: str = "") -> list[list[Seg]]:
    """The pinned header: state and activity, then knowledge counts."""
    if st is None or offline:
        return [
            [(" POLYMATH ", "bold"), (" ○ ", "yellow"), (offline or "connecting…", "yellow")],
            [(" waiting for the agent; the feed starts by itself", "dim")],
        ]
    state = st.get("state", "")
    if not st.get("online"):
        dot, label = ("●", "red"), "agent offline" if state != "stopped" else "agent stopped"
    elif state == "paused" or st.get("mode") in {"pause", "throttle", "yield"}:
        dot, label = (
            ("●", "yellow"),
            {"pause": "paused", "throttle": "throttled", "yield": "slowed"}.get(str(st.get("mode")), "paused"),
        )
    else:
        dot, label = ("●", "green"), "learning"
    line1: list[Seg] = [(" POLYMATH ", "bold"), (f" {dot[0]} ", dot[1]), (label, dot[1])]
    if st.get("cycle") is not None:
        line1.append((f"   cycle {int(st['cycle']):,}", "dim"))
    if st.get("activity") and st.get("online"):
        line1 += [("   now: ", "dim"), (str(st["activity"]), "bold")]
    if st.get("paused_reason"):
        line1.append((f"   ({st['paused_reason']})", "yellow"))
    c = st.get("counts") or {}
    line2: list[Seg] = [
        (f" {compact(c.get('documents', 0))} docs · {compact(c.get('entities', 0))} things · ", ""),
        (f"{compact(c.get('facts', 0))} facts", "bold"),
        (f" ({compact(c.get('inferred', 0))} inferred, {compact(c.get('disputed', 0))} disputed)", "dim"),
        (f" · {c.get('rules', 0)} rules", ""),
    ]
    q = c.get("quiz")
    if q:
        line2.append((f" · quiz {float(q['accuracy']):.0%}", ""))
    b = st.get("brain") or {}
    if b.get("drives"):
        total = int(b.get("primary_bytes", 0)) + int(b.get("drive_bytes", 0))
        line2.append((f" · brain {human_bytes(total)} ({b['drives']} drive{'s' if b['drives'] != 1 else ''})", ""))
    if c.get("agents"):
        line2.append((f" · {c['agents']} agents", ""))
    if st.get("temp_c") is not None:
        hot = float(st["temp_c"]) >= 75
        line2.append((f" · {st['temp_c']:.0f} °C", "yellow" if hot else "dim"))
    return [line1, line2]


def badge(installed: Build, st: dict[str, Any] | None) -> list[Seg]:
    """Top right of the header: the installed version and commit, and whether the agent runs exactly that build."""
    online = bool(st and st.get("online"))
    if not online or (st or {}).get("build") == installed.key:
        text = installed.label() + (f" · {installed.date}" if installed.date else "")
        return [(text, "dim"), (" ✓", "bgreen")] if online else [(text, "dim")]
    # an update is installed but the agent has not restarted into it yet: say which build it still runs
    running = str((st or {}).get("version") or "").split(" · ")
    old = running[0] if running[0] and running[0] != f"v{installed.version}" else running[-1] or "the previous build"
    return [(installed.label(), "dim"), (f" ↻ agent still on {old}", "byellow")]


def milestone_after(before: int, after: int) -> int | None:
    """The largest round count (1, 2 or 5 × 10^k, from 1,000) passed when a count went from ``before`` to ``after``."""
    best = None
    k = 1_000
    while k <= after:
        for m in (k, 2 * k, 5 * k):
            if before < m <= after:
                best = m
        k *= 10
    return best


@dataclass
class _Seen:
    """What the client already announced, to turn status changes into feed lines."""

    mode: str | None = None
    online: bool | None = None
    curious: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)


def transitions(seen: _Seen, st: dict[str, Any]) -> list[dict[str, Any]]:
    now = time.time()
    out: list[dict[str, Any]] = []
    online = bool(st.get("online"))
    if seen.online is not None and online != seen.online:
        text = "agent is online" if online else f"agent went quiet (state: {st.get('state')})"
        out.append({"kind": "body", "at": now, "text": text, "style": "green" if online else "red"})
    seen.online = online
    mode = str(st.get("mode") or "normal")
    if seen.mode is not None and mode != seen.mode:
        temp = f" (CPU {st['temp_c']:.0f} °C)" if st.get("temp_c") is not None else ""
        text = {
            "normal": f"back to full speed{temp}",
            "throttle": f"running hot{temp}: light work only",
            "yield": "low on disk space: light work only while it frees room",
            "pause": f"paused: {st.get('paused_reason') or 'cooling down' + temp}",
        }.get(mode, mode)
        out.append({"kind": "body", "at": now, "text": text, "style": "green" if mode == "normal" else "yellow"})
    seen.mode = mode
    curious = list(st.get("curious") or [])
    if curious and curious != seen.curious:
        out.append({"kind": "curious", "at": now, "topics": curious})
    seen.curious = curious
    counts = st.get("counts") or {}
    for key, what in (("facts", "facts known"), ("documents", "documents read"), ("rules", "rules learned")):
        n = int(counts.get(key) or 0)
        if key in seen.counts:
            m = milestone_after(seen.counts[key], n)
            if m is not None:
                out.append({"kind": "milestone", "at": now, "text": f"milestone: {compact(m)} {what}"})
        seen.counts[key] = n
    return out


class Screen:
    """Terminal output: a pinned header above a scrolling feed (tty), or plain lines (pipe / dumb terminal)."""

    HEADER = 3

    def __init__(self, out: TextIO, *, color: bool, fancy: bool, clock: Callable[[], float] = time.time) -> None:
        self.out = out
        self.color = color
        self.fancy = fancy
        self.clock = clock
        enc = (getattr(out, "encoding", None) or "utf-8").lower()  # in-memory streams have no encoding
        try:
            "→⇄✓✗●○·…─°“”".encode(enc)
            self.unicode = True
        except (UnicodeEncodeError, LookupError):
            self.unicode = False
        self.cols, self.rows = 100, 30
        self._resized = False
        self._last_header: list[list[Seg]] = []
        self._plain_status_at = -1e18

    def _size(self) -> None:
        size = shutil.get_terminal_size((100, 30))
        self.cols, self.rows = max(40, size.columns), max(self.HEADER + 4, size.lines)

    def _width(self, segs: list[Seg] | None) -> int:
        return min(sum(len(t) for t, _ in segs or []), self.cols // 2)

    def paint(self, segs: list[Seg], *, width: int | None = None) -> str:
        """Render segments, truncated to ``width`` (default: the terminal width; plain mode: no truncation)."""
        width = (self.cols if width is None else max(0, width)) if self.fancy else 10_000
        out, used = [], 0
        for text, style in segs:
            if not self.unicode:
                for a, b in ASCII_FALLBACK.items():
                    text = text.replace(a, b)
            room = width - used
            if room <= 0:
                break
            if len(text) > room:
                text = text[: max(0, room - 1)] + ("…" if self.unicode else ".")
            used += len(text)
            code = ANSI.get(style, "")
            out.append(f"\033[{code}m{text}\033[0m" if self.color and code else text)
        return "".join(out)

    def start(self) -> None:
        if not self.fancy:
            return
        self._size()
        try:
            signal.signal(signal.SIGWINCH, self._on_resize)
        except (ValueError, AttributeError, OSError):  # not the main thread / not POSIX
            pass
        self.out.write("\033[?25l\033[2J")  # hide cursor, clear
        self._region()

    def _on_resize(self, *_: Any) -> None:
        self._resized = True

    def _region(self) -> None:
        self.out.write(f"\033[{self.HEADER + 1};{self.rows}r\033[{self.rows};1H")
        self.out.flush()

    def header(self, lines: list[list[Seg]], *, right: list[Seg] | None = None, force_plain: bool = False) -> None:
        """Draw the pinned header; ``right`` is right-aligned on its first line (it wins when space is short)."""
        self._last_header = lines
        if self._resized:
            self._resized = False
            self._size()
            self._region()
        if self.fancy:
            rule: list[Seg] = [("─" * self.cols, "dim")]
            buf = ["\0337"]
            rw = self._width(right)
            for i, segs in enumerate([*lines, rule][: self.HEADER]):
                if i == 0 and rw:
                    left, col = self.paint(segs, width=self.cols - rw - 2), self.cols - rw + 1
                    buf.append(f"\033[1;1H\033[2K{left}\033[1;{col}H{self.paint(right or [], width=rw)}")
                else:
                    buf.append(f"\033[{i + 1};1H\033[2K{self.paint(segs)}")
            buf.append("\0338")
            self.out.write("".join(buf))
            self.out.flush()
            return
        now = self.clock()
        if force_plain or now - self._plain_status_at >= 60.0:
            self._plain_status_at = now
            joined: list[Seg] = [("── ", "dim")]
            for i, segs in enumerate(lines):
                joined += [(" ·", "dim")] if i else []
                joined += segs
            if right:
                joined += [(" · ", "dim"), *right]
            self.out.write(self.paint(joined) + "\n")
            self.out.flush()

    def line(self, segs: list[Seg]) -> None:
        self.out.write(self.paint(segs) + "\n")
        self.out.flush()

    def stop(self) -> None:
        if not self.fancy:
            return
        self.out.write(f"\033[r\033[?25h\033[{self.rows};1H\n")
        self.out.flush()


def wants_color(out: TextIO, env: dict[str, str] | None = None) -> bool:
    env = dict(os.environ) if env is None else env
    return bool(getattr(out, "isatty", lambda: False)()) and "NO_COLOR" not in env and env.get("TERM") != "dumb"


BUILD_CHECK_S = 30.0  # how often the feed re-reads the installed build


def run(
    source: Callable[[str | None], dict[str, Any]],
    *,
    out: TextIO | None = None,
    interval: float = 1.5,
    color: bool | None = None,
    fancy: bool | None = None,
    once: bool = False,
    max_polls: int | None = None,
    where: str = "",
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    build: Callable[[], Build] = read_build,
    on_update: Callable[[Build], None] | None = None,
) -> int:
    """Poll ``source`` forever (until Ctrl+C) and print what the agent learns.

    ``build`` reads the installed build (re-read every ``BUILD_CHECK_S``); when an installer-written build differs
    from the one this process started with, ``on_update`` is called (the CLI re-executes the feed in the new code).
    """
    out = out or sys.stdout
    color = wants_color(out) if color is None else color
    fancy = color if fancy is None else fancy
    screen = Screen(out, color=color, fancy=fancy, clock=clock)
    seen = _Seen()
    face = Face()
    mine = installed = build()
    checked_at = clock()
    cursor: str | None = None
    polls, waiting, backoff = 0, "", interval
    last: dict[str, Any] | None = None

    def draw(*, force_plain: bool = False) -> None:
        lines = status_lines(last, offline=f"waiting: {waiting}" if waiting else "")
        lines[0] = [*face.segments(time.time()), *lines[0]]
        screen.header(lines, right=badge(installed, None if waiting else last), force_plain=force_plain)

    def pause(seconds: float) -> None:
        """Sleep, redrawing the header every animation frame so the face keeps moving."""
        if not fancy:
            sleep(max(0.0, seconds))
            return
        steps = max(1, round(seconds / FRAME_S))
        for _ in range(steps):
            sleep(max(0.0, seconds / steps))
            draw()

    screen.start()
    try:
        if fancy:
            draw()
        hello = f"live feed from {where}" if where else "live feed"
        screen.line(render_event({"kind": "link", "text": f"{hello} · Ctrl+C to stop"}))
        while True:
            t0 = clock()
            if t0 - checked_at >= BUILD_CHECK_S:
                checked_at, installed = t0, build()
                if on_update is not None and installed.installed and installed.key != mine.key:
                    screen.line(render_event({"kind": "link", "text": f"updated to {installed.label()}: "
                                              "reloading the feed", "style": "green"}))  # fmt: skip
                    screen.stop()
                    on_update(installed)
                    mine = installed  # on_update returned instead of replacing the process: carry on
                    screen.start()
            try:
                data = source(cursor)
            except FeedUnavailable as exc:
                if str(exc) != waiting:
                    waiting = str(exc)
                    screen.line(render_event({"kind": "link", "text": f"waiting: {waiting}", "style": "yellow"}))
                face.update(None, offline=waiting)
                if fancy:
                    draw()
                polls += 1
                if once or (max_polls is not None and polls >= max_polls):
                    return 1
                pause(backoff)
                backoff = min(backoff * 2, 10.0)
                continue
            if waiting:
                screen.line(render_event({"kind": "link", "text": "connected", "style": "green"}))
                waiting, backoff = "", interval
            cursor = str(data.get("cursor") or "") or None
            last = data.get("status") or {}
            face.update(last)
            events = transitions(seen, last) + list(data.get("events") or [])
            for ev in events:
                face.see(ev)
            draw(force_plain=polls == 0)
            # spread a batch over the interval so it reads like a live feed rather than a burst
            gap = min(0.25, interval / max(1, len(events))) if fancy else 0.0
            for i, ev in enumerate(events):
                screen.line(render_event(ev))
                if gap and i < len(events) - 1:
                    sleep(gap)
            polls += 1
            if once or (max_polls is not None and polls >= max_polls):
                return 0
            pause(interval - (clock() - t0))
    except KeyboardInterrupt:
        return 0
    finally:
        screen.stop()
