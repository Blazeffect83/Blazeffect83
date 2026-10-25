"""Dashboard: stdlib ThreadingHTTPServer on port 8765, read-only, canvas charts, no external resources.

* Opens the database **read-only**; the only input is the ask box, which runs the
  offline answering engine (still read-only).
* ``/health`` answers 200 only when the database is open and the agent's
  heartbeat is younger than ``loop.heartbeat_stale`` (60 s) — so systemd, the
  live feed and monitoring see a stalled agent as unhealthy.
* Strict CSP, small request bodies, per-client rate limit for ``/api/ask``.
* Two listeners (``serve``): the Pi's own ``http://127.0.0.1:8765`` (loopback, no sign-in), and, for your other
  devices, ``https://<pi>:8443``: home network only, a paired device required (``interface.access``). Both check the
  ``Host`` header (DNS rebinding), and every POST must be JSON from the page's own origin (cross-site requests).
* ``/api/feed`` serves the live learning feed to ``polymath feed`` (the desktop terminal), so the
  desktop user never needs access to the database.
* It never hangs: requests run on a fixed pool of threads, each with its own read-only connection, and every
  query has a deadline (``REQUEST_LIMIT_S``), so one slow page cannot hold up the feed. The expensive counts
  are made by a background thread. Under systemd a watchdog thread asks ``/health`` of this very server every
  few seconds and stops the keep-alive pings if it gets no answer, so systemd restarts a stuck dashboard.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import sqlite3
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from polymath import __version__
from polymath.core.config import Config
from polymath.core.db import Database
from polymath.core.logging import get_logger
from polymath.core.loop import heartbeat_path
from polymath.interface import access as acc
from polymath.version import read_build

log = get_logger("dashboard")
MAX_BODY = 4096
ASK_PER_MINUTE = 20
ANSWERER_TTL = 600.0
MAP_TTL = 120.0  # the knowledge map is recomputed at most this often
WORKERS = 8  # request threads (each keeps one read-only connection)
WORKER_CACHE_KIB = 8000  # page cache per request connection (8 of them: 64 MB at most, inside MemoryHigh)
COUNTS_EVERY = 60.0  # the background thread recounts documents, things and facts this often (at least)
FIRST_COUNT_WAIT_S = 2.0  # the overview waits this long for the first count after a start, then shows "counting"
COUNTS_DUTY = 0.1  # …and never spends more than a tenth of its time counting (slow counts on a big brain)
# per-route query deadlines (seconds); the feed and health must answer fast, pages may take a little longer
REQUEST_LIMIT_S = {"/health": 3.0, "/api/feed": 4.0}
DEFAULT_LIMIT_S = 20.0
ASK_LIMIT_S = 30.0
STATIC = {
    "index.html": "text/html; charset=utf-8",
    "live.html": "text/html; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "live.js": "text/javascript; charset=utf-8",
    "face.js": "text/javascript; charset=utf-8",
    "focus.js": "text/javascript; charset=utf-8",
    "access.js": "text/javascript; charset=utf-8",
    "pair.html": "text/html; charset=utf-8",
    "pair.js": "text/javascript; charset=utf-8",
    "manifest.webmanifest": "application/manifest+json",
    "sw.js": "text/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
    "icon.svg": "image/svg+xml",
}
# what a browser that is not paired yet may load from the remote listener: the pairing page and its parts, the
# app manifest and icon (Chrome fetches those without cookies), and the Pi's public CA certificate
PUBLIC_REMOTE = frozenset({"/pair", "/static/pair.js", "/static/style.css", "/static/icon.svg", "/ca.crt",
                           "/manifest.webmanifest", "/sw.js", "/api/access/pair", "/api/access/me"})  # fmt: skip
HANDSHAKE_S = 10.0  # a TLS client gets this long to say hello
REQUEST_TIMEOUT_S = 30.0  # an idle or trickling connection is closed after this long (both listeners)
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=(), serial=(), bluetooth=()",
}


@dataclass
class Gate:
    """Who may use a listener (``interface.access``): ``remote`` needs the home network and a paired device."""

    access: acc.Access
    remote: bool = False
    nets: list[Any] = field(default_factory=list)
    names: set[str] = field(default_factory=set)
    tls_dir: Path = Path("/etc/polymath/tls")
    port: int = 8443  # the remote listener's port (shown when a pairing code is made)


def gate_for(config: Config, *, remote: bool) -> Gate:
    a = config.access
    return Gate(acc.Access(a.state_dir), remote=remote, nets=acc.networks(a.networks),
                names=acc.host_names(a.names), tls_dir=Path(a.tls_dir), port=a.port)  # fmt: skip


class DashboardData:
    """All queries the dashboard needs (read-only)."""

    def __init__(self, config: Config) -> None:
        from polymath.memory import pool as storage_pool

        self.config = config
        storage_pool.activate(config)  # documents on plugged-in drives read transparently
        self._local = threading.local()  # one read-only connection per request thread
        self._answerer: Any = None
        self._answerer_db: Database | None = None
        self._answerer_at = 0.0
        self._lock = threading.Lock()  # short critical sections only (never held around a query)
        self._ask_lock = threading.Lock()  # one question at a time; loading the answerer never blocks the rest
        from polymath.interface.feed import _Seen

        self._seen = _Seen()
        self._map: tuple[float, dict[str, Any]] | None = None
        self._counts: tuple[float, dict[str, Any]] | None = None
        self._stop = threading.Event()
        self._counted = threading.Event()  # set once the first background count is done
        self._counter: threading.Thread | None = None

    def db(self) -> Database:
        db: Database | None = getattr(self._local, "db", None)
        if db is None or not db.is_open:
            from polymath.core import fsprobe

            if fsprobe.unhealthy(fsprobe.probe(self.config.paths.db_path, timeout=3.0)):
                raise FileNotFoundError(self.config.paths.db_path)  # a frozen drive: answer "not there", never hang
            db = Database(self.config.paths.db_path, readonly=True)
            db.execute(f"PRAGMA cache_size=-{WORKER_CACHE_KIB}")  # several of these: keep each one's cache small
            self._local.db = db
        return db

    # ---------------------------------------------------------------- background counts
    def start_counter(self) -> None:
        if self._counter is None:
            self._counter = threading.Thread(target=self._count_loop, name="dashboard-counts", daemon=True)
            self._counter.start()

    def stop(self) -> None:
        self._stop.set()

    def counts(self) -> dict[str, Any] | None:
        """The last full-table counts for the feed (None until the first is done); never computed on a request."""
        with self._lock:
            return dict(self._counts[1]["feed"]) if self._counts else None

    def table_counts(self) -> dict[str, Any] | None:
        """Everything the background thread counted (feed counts plus the overview's breakdowns)."""
        with self._lock:
            return dict(self._counts[1]) if self._counts else None

    def count_now(self, db: Database | None = None) -> dict[str, Any]:
        """Count everything that needs a full scan, once, on its own connection."""
        from polymath.interface.feed import heavy_counts

        own = db is None
        db = db or Database(self.config.paths.db_path, readonly=True)
        try:
            counts = {
                "feed": heavy_counts(db),
                "documents": {
                    r["source"]: int(r["n"])
                    for r in db.query(
                        "SELECT source, COUNT(*) AS n FROM documents WHERE state!='duplicate' GROUP BY source "
                        "ORDER BY n DESC"
                    )
                },
                "triples": {
                    r["status"]: int(r["n"])
                    for r in db.query("SELECT status, COUNT(*) AS n FROM triples GROUP BY status")
                },
                "entities": int(db.scalar("SELECT COUNT(*) FROM entities", default=0)),
                "queue": {
                    r["state"]: int(r["n"]) for r in db.query("SELECT state, COUNT(*) AS n FROM jobs GROUP BY state")
                },
                "at": time.time(),
            }
        finally:
            if own:
                db.close()
        with self._lock:
            self._counts = (time.monotonic(), counts)
        self._counted.set()
        return counts

    def _count_loop(self) -> None:
        db: Database | None = None
        while not self._stop.is_set():
            wait = COUNTS_EVERY
            try:
                if db is None:
                    db = Database(self.config.paths.db_path, readonly=True)
                t0 = time.monotonic()
                self.count_now(db)
                wait = max(COUNTS_EVERY, (time.monotonic() - t0) / COUNTS_DUTY)
            except FileNotFoundError:
                wait = 10.0  # not initialised yet
            except Exception:
                log.exception("background counts failed")
                if db is not None:
                    db.close()
                db, wait = None, 30.0
            self._stop.wait(wait)
        if db is not None:
            db.close()

    def missing_reason(self) -> str:
        """Why there is no database to read: the brain's drive is not mounted, or it was never initialised."""
        from polymath.core import fsprobe
        from polymath.core.config import HOME_FILE, MOVED_FILE

        data = self.config.paths.data_dir
        home = fsprobe.probe(data / HOME_FILE, timeout=3.0)
        if fsprobe.unhealthy(home):
            return "the brain's drive is not answering (it froze or dropped out): Polymath brings it back by itself"
        moved = home == fsprobe.MISSING and fsprobe.probe(data / MOVED_FILE, timeout=3.0) == fsprobe.OK
        if moved:
            return "the brain's drive is not mounted: plug it in (Polymath starts by itself)"
        return "database not initialised yet"

    def health(self) -> tuple[bool, dict[str, Any]]:
        try:
            hb = self.db().kv_get("heartbeat") or {}
        except FileNotFoundError:
            reason = self.missing_reason()
            if reason.startswith("the brain's drive"):
                return False, {"ok": False, "error": f"database unavailable: {reason}"}
            return False, {"ok": False, "error": "database unavailable: FileNotFoundError"}
        except Exception as exc:
            return False, {"ok": False, "error": f"database unavailable: {type(exc).__name__}"}
        age = time.time() - float(hb.get("ts", 0)) if hb else None
        if hb and hb.get("state") not in {"stopped", None}:
            try:  # a long job slice cannot commit its heartbeat; the agent's pulse file shows it is alive
                age = min(age or 1e18, time.time() - heartbeat_path(self.config).stat().st_mtime)
            except OSError:
                pass
            from polymath.core import vigil

            busy = vigil.read(self.config.paths.run_dir, stale=self.config.loop.heartbeat_stale)
            if busy is not None:  # one long step: the vigil vouches for it (core.vigil)
                age = min(age or 1e18, time.time() - float(busy["at"]))
        ok = age is not None and age < self.config.loop.heartbeat_stale and hb.get("state") != "stopped"
        return ok, {
            "ok": ok,
            "heartbeat_age_s": None if age is None else round(age, 1),
            "state": hb.get("state"),
            "cycle": hb.get("cycle"),
            "version": __version__,
            "build": read_build().label(),
            "agent_build": hb.get("version"),
        }

    def overview(self) -> dict[str, Any]:
        db = self.db()
        _ok, health = self.health()
        self._counted.wait(FIRST_COUNT_WAIT_S)  # right after a start: a small brain is counted in a moment
        counted = self.table_counts() or {}  # the full-table counts come from the background thread
        docs: dict[str, int] = counted.get("documents") or {}
        triples: dict[str, int] = counted.get("triples") or {}
        quiz = db.one("SELECT n, correct, accuracy, chance, created FROM quizzes ORDER BY id DESC LIMIT 1")
        vit = db.one("SELECT * FROM vitals ORDER BY at DESC LIMIT 1")
        recent = [
            dict(r)
            for r in db.query(
                "SELECT id, action, status, ROUND(cpu_seconds, 2) AS cpu, ROUND(value, 2) AS value, ended FROM cycles "
                "ORDER BY id DESC LIMIT 15"
            )
        ]
        return {
            "health": health,
            "counting": not counted,  # the first count after a start is still running
            "counted_at": counted.get("at"),
            "documents": docs,
            "documents_total": sum(docs.values()),
            "entities": int(counted.get("entities") or 0),
            "triples": triples,
            "quiz": dict(quiz) if quiz else None,
            "vitals": dict(vit) if vit else None,
            "recent_cycles": recent,
            "queue": counted.get("queue") or {},
            "paused_reason": db.kv_get("paused_reason") if health.get("state") == "paused" else None,
            "linker": (db.kv_get("linker_model") or {}).get("validation"),
            "embeddings": db.kv_get("embedding_quality"),
        }

    def timeseries(self) -> dict[str, Any]:
        db = self.db()
        now = time.time()
        day = 86400
        docs: dict[str, list[list[float]]] = defaultdict(list)
        for r in db.query(
            "SELECT source, CAST(fetched / ? AS INTEGER) AS d, COUNT(*) AS n FROM documents "
            "WHERE fetched > ? GROUP BY source, d ORDER BY d",
            (day, now - 30 * day),
        ):
            docs[str(r["source"])].append([int(r["d"]) * day, int(r["n"])])
        quiz = [
            [float(r["created"]), float(r["accuracy"]), float(r["chance"])]
            for r in db.query("SELECT created, accuracy, chance FROM quizzes ORDER BY id DESC LIMIT 60")
        ][::-1]
        effort: dict[str, float] = {}
        for r in db.query(
            "SELECT action, SUM(cpu_seconds) AS c FROM cycles WHERE ended > ? GROUP BY action", (now - day,)
        ):
            effort[str(r["action"])] = round(float(r["c"]), 1)
        vitals = [
            [float(r["at"]), r["temp_c"], r["load1"]]
            for r in db.query("SELECT at, temp_c, load1 FROM vitals WHERE at > ? ORDER BY at", (now - day,))
        ]
        return {"documents_per_day": docs, "quiz": quiz, "effort_24h": effort, "vitals_24h": vitals}

    def topics(self) -> dict[str, Any]:
        db = self.db()
        top = [
            dict(r)
            for r in db.query(
                "SELECT t.name, ROUND(p.priority, 4) AS priority, ROUND(p.gap, 3) AS gap, ROUND(p.importance, 3) AS "
                "importance, ROUND(p.novelty, 3) AS novelty FROM topic_priority p JOIN topics t ON t.id = p.topic_id "
                "ORDER BY p.priority DESC LIMIT 15"
            )
        ]
        weakest = [
            dict(r)
            for r in db.query(
                "SELECT t.name, ROUND(p.gap, 3) AS gap, ROUND(p.importance, 3) AS importance FROM topic_priority p "
                "JOIN topics t ON t.id = p.topic_id WHERE p.importance >= 0.01 ORDER BY p.gap DESC LIMIT 15"
            )
        ]
        decisions = [
            dict(r)
            for r in db.query("SELECT at, context, chosen, reason, reward FROM decisions ORDER BY id DESC LIMIT 20")
        ]
        return {"top": top, "weakest": weakest, "decisions": decisions}

    def agents(self) -> dict[str, Any]:
        from polymath.interface.cli import agents_list

        db = self.db()
        if not db.scalar("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agents'"):
            return {"agents": [], "rewards": []}
        rewards = [
            dict(r)
            for r in db.query(
                "SELECT r.at, a.name, r.amount, r.reason FROM agent_rewards r JOIN agents a ON a.id=r.agent_id "
                "ORDER BY r.id DESC LIMIT 25"
            )
        ]
        return {"agents": agents_list(db), "rewards": rewards}

    def digest(self) -> dict[str, Any]:
        from polymath.evaluation import digest

        db = self.db()
        last = digest.latest(db) if db.scalar("SELECT 1 FROM sqlite_master WHERE name='digests'") else None
        sites = [
            dict(r)
            for r in db.query("SELECT site, status, citations, reason FROM sites ORDER BY vetted_at DESC LIMIT 12")
        ] if db.scalar("SELECT 1 FROM sqlite_master WHERE name='sites'") else []  # fmt: skip
        if last is None:
            data = digest.collect(db, time.time() - 86400, cfg=self.config)
            return {"day": "last 24 hours (no digest written yet)", "lines": digest.lines(data), "sites": sites}
        return {"day": last["day"], "lines": last["lines"], "sites": sites}

    def feed(self, cursor: str | None, *, render: bool = False) -> dict[str, Any]:
        from polymath.interface.feed import Feed

        try:
            db = self.db()
        except FileNotFoundError:  # no brain to read (its drive is not mounted): show what the agent says
            result = self._snapshot_feed(cursor)
        else:
            feed = Feed(db, pulse=heartbeat_path(self.config), stale=self.config.loop.heartbeat_stale,
                        counts_source=self.counts, run_dir=self.config.paths.run_dir)  # fmt: skip
            result = feed.poll(cursor)
        from polymath.interface.face import state
        from polymath.interface.feed import badge, transitions

        st = result.get("status") or {}
        result["face"] = state(st, transitions(self._seen, st) + list(result.get("events") or []))
        installed = read_build()
        if render:  # the live page shows the terminal's own wording and colours
            from polymath.interface.feed import render_event

            result["lines"] = [render_event(e) for e in result.get("events") or []]
        result["badge"] = {
            "text": "".join(t for t, _ in badge(installed, st)),
            "current": st.get("build") == installed.key if st.get("online") else None,
        }
        return result

    def _snapshot_feed(self, cursor: str | None) -> dict[str, Any]:
        from polymath.interface import snapshot
        from polymath.interface.feed import FeedUnavailable

        try:
            return snapshot.source(snapshot.path_for(self.config))(
                cursor if snapshot.parse_cursor(cursor) is not None else None
            )
        except FeedUnavailable:
            raise FileNotFoundError(self.config.paths.db_path) from None  # nothing at all to show: 503

    def storage(self, db: Database) -> dict[str, Any]:
        """Where the room goes and when it would run out (``body.usage``; cheap: file sizes and vitals)."""
        from polymath.body import usage

        f = usage.files(self.config)
        fc = usage.forecast(db, keep_free_gb=self.config.body.disk_min_free_gb)
        where = "SD card" if f["sd_card"] else "brain's disk"
        lines = [f"{usage.gb(f['free'])} free on the {where} (it keeps {usage.gb(f['keep_free'])} free): database "
                 f"{usage.gb(f['database'])}, backups {usage.gb(f['backups'])} ({f['backup_count']}), indexes "
                 f"{usage.gb(f['indexes'])}."]  # fmt: skip
        if fc is not None and fc.get("days_left") is not None:
            lines.append(f"Free space falls {fc['decline_gb_per_day']:.2f} GB a day: about {fc['days_left']:.0f} days "
                         "before it starts dropping old document texts (facts stay).")  # fmt: skip
        elif fc is not None:
            lines.append("Free space is steady.")
        from polymath.body.roles import read_public as read_roles

        roles = {} if f["sd_card"] else (read_roles(self.config) or {})
        for d in roles.get("drives") or []:
            what = {"brain": "holds the brain", "backup": "keeps the backups", "extra": "extra room"}.get(
                str(d.get("role")), str(d.get("role"))
            )
            why = f" (not the brain: {d['why_not']})" if d.get("why_not") and d.get("role") != "brain" else ""
            lines.append(f"{d.get('name')}: {d.get('kind') or 'drive'}, {what}{why}.")
        if roles.get("decision"):
            lines.append(str(roles["decision"])[:1].upper() + str(roles["decision"])[1:] + ".")
        lines += [f"Warning: {w}." for w in roles.get("warnings") or []]
        from polymath.memory.inventory import relearn_status

        with contextlib.suppress(Exception):
            rel = relearn_status(db)
            if rel["waiting"] or rel["queued"]:
                lines.append(f"A drive was lost: {rel['done']:,} of its documents read again, "
                             f"{rel['waiting'] + rel['queued']:,} to go, the most important first.")  # fmt: skip
        watch: dict[str, Any] = {}
        if f["sd_card"]:  # what the SSD watch sees: it moves the brain back onto the SSD by itself
            from polymath.body.ssdwatch import read_public

            watch = read_public(self.config) or {}
            if watch.get("detail"):
                fix = f" To do: {watch['fix']}." if watch.get("fix") else ""
                lines.append(f"SSD: {watch['detail']}.{fix}")
        return {"files": f, "forecast": fc, "lines": lines, "ssd": watch.get("state")}

    def insights(self) -> dict[str, Any]:
        """The weekly recap, predictions, surprises, and the disks (one call for the dashboard's new panels)."""
        from polymath.body import wear
        from polymath.drive import changelog, selftune
        from polymath.evaluation import recap, surprise
        from polymath.reasoning import predictions

        db = self.db()
        last = recap.latest(db)
        if last is None:
            data = recap.collect(db, cfg=self.config)
            week = {"week": data["week"] + " (so far)", "lines": recap.lines(data)}
        else:
            week = {"week": last["week"], "lines": last["lines"]}
        sb = predictions.scoreboard(db)
        home = self.config.paths.data_dir / "home-drive.json"
        try:
            home_info = json.loads(home.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            home_info = None
        return {
            "recap": week,
            "predictions": sb | {"line": predictions.describe(sb)},
            "didyouknow": surprise.recent(db, time.time() - 7 * 86400, 6),
            "wear": [r | {"line": wear.describe(r)} for r in wear.report(db, self.config)],
            "storage": self.storage(db),
            "home": home_info,
            "changes": [
                {
                    "at": c["at"],
                    "area": c["area"],
                    "action": c["action"],
                    "summary": c["summary"],
                    "watching": c["state"] == "watching",
                }
                for c in changelog.recent(db, 0.0, 12, include_rejected=False)
            ],
            "tuned": [
                {
                    "name": k,
                    "label": selftune.KNOBS[k].label if k in selftune.KNOBS else k,
                    "value": v,
                    "default": selftune.KNOBS[k].default if k in selftune.KNOBS else None,
                }
                for k, v in sorted((db.kv_get("tuned") or {}).items())
            ],
            "reading_weights": {  # only the sources it reads clearly more or less of
                k: round(float(v), 1)
                for k, v in (db.kv_get("reading_weights") or {}).items()
                if abs(float(v) - 1) >= 0.1
            },
        }

    def tell(self, q: str) -> dict[str, Any]:
        from polymath.interface.tell import Teller

        story = Teller(self.db()).tell(q)
        return story.to_dict() | {"text": story.render()}

    def knowledge_map(self) -> dict[str, Any]:
        from polymath.interface.knowledge_map import build_map

        with self._lock:
            hit = self._map
        if hit is not None and time.monotonic() - hit[0] < MAP_TTL:
            return hit[1]
        data = build_map(self.db(), store=False)
        with self._lock:
            self._map = (time.monotonic(), data)
        return data

    def knowledge(self, q: str) -> dict[str, Any]:
        from polymath.interface.answer import render_value
        from polymath.memory.graph import KnowledgeGraph

        db = self.db()
        g = KnowledgeGraph(db)
        out: dict[str, Any] = {"query": q, "entities": []}
        for eid, label, n in g.candidates(q, limit=5):
            e = g.entity(eid)
            out["entities"].append(
                {
                    "id": eid,
                    "label": label,
                    "aliases_count": n,
                    "description": e.description if e else None,
                    "facts": [
                        {
                            "predicate": x.predicate,
                            "direction": x.direction,
                            "other": x.other_label or render_value(db, 0, json.dumps(x.value)),
                            "status": x.status,
                            "confidence": round(x.confidence, 3),
                        }
                        for x in g.neighbors(eid, limit=25)
                    ],
                }
            )
        return out

    def ask(self, question: str) -> dict[str, Any]:
        """Answer one question on the answerer's own connection. Loading it (the alias index) can take a while on
        a large brain, so it happens under its own lock: the feed and every other page keep answering."""
        with self._ask_lock:
            stale = time.monotonic() - self._answerer_at > ANSWERER_TTL  # pick up the agent's rebuilt alias index
            if self._answerer is None or self._answerer_db is None or not self._answerer_db.is_open or stale:
                from polymath.interface.answer import Answerer
                from polymath.perception.entities import AliasIndex, EntityLinker

                if self._answerer_db is not None:
                    self._answerer_db.close()
                db = self._answerer_db = Database(self.config.paths.db_path, readonly=True)
                auto = AliasIndex(db, self.config.paths.index_dir).load()
                self._answerer = Answerer(db, EntityLinker(db, auto) if auto else None)
                self._answerer_at = time.monotonic()
            self._answerer_db.limit_queries(ASK_LIMIT_S)
            try:
                ans = self._answerer.ask(question)
            finally:
                self._answerer_db.limit_queries(None)
        return {**ans.to_dict(), "rendered": ans.render()}


RATE_KEYS_IDLE = 1024  # more clients than this: drop the idle ones
RATE_KEYS_MAX = 4096  # never more than this, whatever happens


class RateLimiter:
    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self.hits: dict[str, deque[float]] = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self.lock:
            q = self.hits[key]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= self.per_minute:
                return False
            q.append(now)
            if len(self.hits) > RATE_KEYS_IDLE:  # one entry per client ever seen: drop the idle ones
                for k in [k for k, v in self.hits.items() if not v or now - v[-1] > 60]:
                    del self.hits[k]
                if len(self.hits) > RATE_KEYS_MAX:  # a flood of busy addresses: forget the least recent half
                    keep = sorted(self.hits.items(), key=lambda kv: kv[1][-1])[-RATE_KEYS_MAX // 2 :]
                    self.hits = defaultdict(deque, keep)
            return True


def make_handler(data: DashboardData, limiter: RateLimiter, gate: Gate | None = None) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "Polymath"
        sys_version = ""
        timeout = REQUEST_TIMEOUT_S
        device: acc.Device | None = None

        def _gate(self) -> bool:
            """Let this request in? Sends the refusal itself when not (``interface.access``)."""
            self.device = None
            if gate is None:
                return True
            ip = str(self.client_address[0])
            if gate.remote and not acc.client_allowed(ip, gate.nets):
                gate.access.log("refused", ip=ip, why="not on the home network")
                self._send(403, b"home network only\n", "text/plain; charset=utf-8")
                return False
            host = self.headers.get("Host")
            if not acc.host_allowed(host, gate.names):
                self._json(421, {"error": "this server does not answer to that host name"})
                return False
            path = urlsplit(self.path).path
            if self.command == "POST":
                ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                origin = self.headers.get("Origin")
                if ctype != "application/json":
                    self._json(415, {"error": "expected application/json"})
                    return False
                scheme = "https" if gate.remote else "http"
                if (gate.remote or origin) and not acc.same_origin(origin, host, scheme):  # the CLI sends no Origin
                    gate.access.log("refused", ip=ip, why="cross-site request", path=path)
                    self._json(403, {"error": "cross-site requests are refused"})
                    return False
            if not gate.remote:
                return True
            self.device = gate.access.check(acc.cookie_token(self.headers.get("Cookie")), ip=ip)
            if self.device is not None or path in PUBLIC_REMOTE:
                return True
            if self.command in {"GET", "HEAD"} and not path.startswith("/api/") and path != "/health":
                self.send_response(303)
                self.send_header("Location", "/pair")
                self.send_header("Content-Length", "0")
                for k, v in SECURITY_HEADERS.items():
                    self.send_header(k, v)
                self.end_headers()
                return False
            self._json(401, {"error": "this browser is not paired with the Pi yet", "pair": "/pair"})
            return False

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug("http", extra={"client": self.client_address[0], "line": fmt % args})

        def _send(self, status: int, body: bytes, ctype: str = "application/json",
                  extra: dict[str, str] | None = None) -> None:  # fmt: skip
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _length(self) -> int:
            """The request body's length, or -1 for a missing, negative or non-numeric Content-Length."""
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return -1
            return n if n >= 0 else -1

        def _json(self, status: int, obj: Any, extra: dict[str, str] | None = None) -> None:
            self._send(status, json.dumps(obj, default=str).encode(), extra=extra)

        def do_HEAD(self) -> None:
            self.do_GET()

        def do_GET(self) -> None:
            if not self._gate():
                return
            url = urlsplit(self.path)
            path = url.path
            limited: Database | None = None
            try:
                if gate is not None and (path in {"/pair", "/ca.crt", "/manifest.webmanifest", "/sw.js"}
                                         or path.startswith("/api/access/")):  # fmt: skip
                    return self._access_get(path)
                if path == "/health" or path.startswith("/api/"):
                    try:
                        limited = data.db()
                    except (FileNotFoundError, sqlite3.Error):
                        limited = None  # each route reports a missing database its own way
                    else:
                        limited.limit_queries(REQUEST_LIMIT_S.get(path, DEFAULT_LIMIT_S))
                if path in {"/", "/index.html"}:
                    return self._static("index.html")
                if path.startswith("/static/"):
                    return self._static(path.removeprefix("/static/"))
                if path == "/health":
                    ok, body = data.health()
                    if gate is not None and gate.remote:  # other devices learn only whether it is up
                        body = {k: v for k, v in body.items() if k in {"ok", "heartbeat_age_s", "state"}}
                    return self._json(200 if ok else 503, body)
                if path == "/api/overview":
                    return self._json(200, data.overview())
                if path == "/api/timeseries":
                    return self._json(200, data.timeseries())
                if path == "/api/topics":
                    return self._json(200, data.topics())
                if path == "/api/agents":
                    return self._json(200, data.agents())
                if path == "/api/digest":
                    return self._json(200, data.digest())
                if path == "/api/insights":
                    return self._json(200, data.insights())
                if path == "/api/map":
                    return self._json(200, data.knowledge_map())
                if path == "/api/face":
                    from polymath.interface.face import definitions

                    return self._json(200, definitions())
                if path == "/api/tell":
                    q = (parse_qs(url.query).get("q") or [""])[0][:200]
                    return self._json(200, data.tell(q) if q.strip() else {"subject": "", "paragraph": ""})
                if path in {"/live", "/live.html"}:
                    return self._static("live.html")
                if path == "/api/feed":
                    qs = parse_qs(url.query)
                    cursor = (qs.get("cursor") or [""])[0][:400]
                    return self._json(200, data.feed(cursor or None, render=(qs.get("render") or [""])[0] == "1"))
                if path == "/api/knowledge":
                    q = (parse_qs(url.query).get("q") or [""])[0][:200]
                    return self._json(200, data.knowledge(q) if q.strip() else {"query": "", "entities": []})
                return self._json(404, {"error": "not found"})
            except FileNotFoundError:
                return self._json(503, {"error": data.missing_reason()})
            except sqlite3.OperationalError as exc:
                if "interrupted" not in str(exc):
                    log.exception("dashboard request failed", extra={"path": path})
                    return self._json(500, {"error": "internal error"})
                log.warning("request took too long: stopped", extra={"path": path})
                return self._json(503, {"error": "busy: that took too long, try again"})
            except Exception:
                log.exception("dashboard request failed", extra={"path": path})
                return self._json(500, {"error": "internal error"})
            finally:
                if limited is not None and limited.is_open:
                    limited.limit_queries(None)

        def do_POST(self) -> None:
            if not self._gate():
                return
            path = urlsplit(self.path).path
            if gate is not None and path.startswith("/api/access/"):
                return self._access_post(path)
            if path != "/api/ask":
                return self._json(405, {"error": "the dashboard is read-only"})
            length = self._length()
            if length <= 0 or length > MAX_BODY:
                return self._json(413, {"error": f"body must be 1..{MAX_BODY} bytes"})
            if not limiter.allow(self.client_address[0]):
                return self._json(429, {"error": "too many questions; try again in a minute"})
            try:
                payload = json.loads(self.rfile.read(length))
                question = str(payload.get("question", "")).strip()[:500]
            except (ValueError, AttributeError):
                return self._json(400, {"error": 'expected JSON {"question": "..."}'})
            if not question:
                return self._json(400, {"error": "empty question"})
            try:
                return self._json(200, data.ask(question))
            except sqlite3.OperationalError as exc:
                if "interrupted" in str(exc):
                    return self._json(503, {"error": "busy: that question took too long, try a simpler one"})
                log.exception("ask failed")
                return self._json(500, {"error": "internal error"})
            except Exception:
                log.exception("ask failed")
                return self._json(500, {"error": "internal error"})

        # ------------------------------------------------------------------ private access (interface.access)
        def _body(self) -> dict[str, Any] | None:
            length = self._length()
            if length < 0 or length > MAX_BODY:
                self._json(413, {"error": f"body must be at most {MAX_BODY} bytes"})
                return None
            try:
                obj = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self._json(400, {"error": "expected JSON"})
                return None
            if not isinstance(obj, dict):
                self._json(400, {"error": "expected a JSON object"})
                return None
            return obj

        def _owner(self) -> bool:
            """The Pi itself (loopback listener) or a paired device: may manage access."""
            assert gate is not None
            return not gate.remote or self.device is not None

        def _access_get(self, path: str) -> None:
            assert gate is not None
            if path == "/pair":
                return self._static("pair.html")
            if path == "/manifest.webmanifest":
                return self._static("manifest.webmanifest")
            if path == "/sw.js":
                return self._static("sw.js")
            if path == "/ca.crt":
                try:
                    pem = (gate.tls_dir / "ca.crt").read_bytes()
                except OSError:
                    return self._json(404, {"error": "no certificate yet (scripts/access-cert.sh)"})
                disp = {"Content-Disposition": 'attachment; filename="polymath-ca.crt"'}
                return self._send(200, pem, "application/x-x509-ca-cert", extra=disp)
            if path == "/api/access/me":
                dev = self.device.public() if self.device else None
                known = {"devices": len(gate.access.devices())} if self._owner() else {}  # not told to strangers
                return self._json(200, {"remote": gate.remote, "device": dev, **known,
                                        "fingerprint": acc.fingerprints(gate.tls_dir / "ca.crt")})  # fmt: skip
            if not self._owner():  # pragma: no cover - the gate already asked for a paired device
                return self._json(401, {"error": "not paired"})
            if path == "/api/access/devices":
                mine = self.device.id if self.device else None
                return self._json(
                    200, {"devices": [d.public() | {"this": d.id == mine} for d in gate.access.devices()]}
                )
            if path == "/api/access/log":
                return self._json(200, {"log": gate.access.history(100)})
            return self._json(404, {"error": "not found"})

        def _access_post(self, path: str) -> None:
            assert gate is not None
            body = self._body()
            if body is None:
                return None
            ip = str(self.client_address[0])
            if path == "/api/access/pair":
                if not gate.remote:
                    return self._json(400, {"error": f"pair from your other device, at https://<this Pi>:{gate.port}"})
                name = acc.device_name(self.headers.get("User-Agent"), str(body.get("name") or ""))
                try:
                    token, dev = gate.access.pair(str(body.get("code") or ""), name=name, ip=ip)
                except acc.PairError as exc:
                    return self._json(403, {"error": str(exc)})
                return self._json(
                    200, {"ok": True, "device": dev.public()}, extra={"Set-Cookie": acc.set_cookie(token)}
                )
            if not self._owner():  # pragma: no cover - the gate already asked for a paired device
                return self._json(401, {"error": "not paired"})
            if path == "/api/access/code":
                code = gate.access.new_code()
                return self._json(200, {"code": code, "expires_in": int(acc.CODE_TTL), "urls": acc.lan_urls(gate.port),
                                        "fingerprint": acc.fingerprints(gate.tls_dir / "ca.crt")})  # fmt: skip
            if path == "/api/access/revoke":
                which = str(body.get("id") or "")
                if not which:
                    return self._json(400, {"error": 'expected {"id": "<device id>"} or {"id": "all"}'})
                n = gate.access.revoke(which, by=self.device.name if self.device else "the Pi")
                own = self.device is not None and which in {"all", self.device.id}
                return self._json(200, {"revoked": n}, extra={"Set-Cookie": acc.clear_cookie()} if own else None)
            if path == "/api/access/logout":
                if self.device is None:
                    return self._json(400, {"error": "this browser is not signed in"})
                gate.access.revoke(self.device.id, by=self.device.name)
                return self._json(200, {"ok": True}, extra={"Set-Cookie": acc.clear_cookie()})
            return self._json(404, {"error": "not found"})

        def do_PUT(self) -> None:
            self._json(405, {"error": "the dashboard is read-only"})

        do_DELETE = do_PUT
        do_PATCH = do_PUT

        def _static(self, name: str) -> None:
            if name not in STATIC:
                return self._json(404, {"error": "not found"})
            body = resources.files("polymath.interface").joinpath("static", name).read_bytes()
            return self._send(200, body, STATIC[name])

    return Handler


class PooledHTTPServer(ThreadingHTTPServer):
    """Requests run on a fixed pool of threads (not one new thread each): a flood of slow requests queues up
    instead of piling up threads and connections, and each worker keeps its read-only connection."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler], data: DashboardData,
                 *, workers: int = WORKERS) -> None:  # fmt: skip
        super().__init__(address, handler)
        self.data = data
        self.owns_data = True
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dashboard")

    def process_request(self, request: Any, client_address: Any) -> None:
        self.pool.submit(self.process_request_thread, request, client_address)

    def server_close(self) -> None:
        super().server_close()
        if self.owns_data:
            self.data.stop()
        self.pool.shutdown(wait=False, cancel_futures=True)


class TLSServer(PooledHTTPServer):
    """The remote listener: each connection's TLS handshake runs on a worker thread with a time limit, so a slow or
    silent client never holds up accepting the next one."""

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler], data: DashboardData,
                 ctx: ssl.SSLContext) -> None:  # fmt: skip
        super().__init__(address, handler, data)
        self.ctx = ctx
        self.owns_data = False  # the loopback listener's data: stopped once, by it

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            request.settimeout(HANDSHAKE_S)
            tls = self.ctx.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError):  # not TLS, wrong version, or gave up: nothing to answer
            self.shutdown_request(request)
            return
        super().process_request_thread(tls, client_address)


def tls_context(tls_dir: Path) -> ssl.SSLContext | None:
    """TLS 1.2+ with the Pi's certificate (``scripts/access-cert.sh``), or None when there is none yet."""
    crt, key = Path(tls_dir) / "server.crt", Path(tls_dir) / "server.key"
    if not (crt.is_file() and key.is_file()):
        return None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.options |= ssl.OP_NO_COMPRESSION
    try:
        ctx.load_cert_chain(str(crt), str(key))
    except (OSError, ssl.SSLError) as exc:
        log.error("cannot load the dashboard's TLS certificate", extra={"error": str(exc)})
        return None
    return ctx


def loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def make_server(
    config: Config, host: str | None = None, port: int | None = None, *, gate: Gate | None = None
) -> PooledHTTPServer:
    data = DashboardData(config)
    server = PooledHTTPServer(
        (host or config.dashboard.host, config.dashboard.port if port is None else port),
        make_handler(data, RateLimiter(ASK_PER_MINUTE), gate),
        data,
    )
    data.start_counter()
    return server


def make_remote_server(
    config: Config, data: DashboardData, gate: Gate, ctx: ssl.SSLContext, *, host: str = "0.0.0.0",
    port: int | None = None,
) -> TLSServer:  # fmt: skip
    """The listener for your other devices: HTTPS, home network, paired devices (``interface.access``)."""
    return TLSServer((host, config.access.port if port is None else port),
                     make_handler(data, RateLimiter(ASK_PER_MINUTE), gate), data, ctx)  # fmt: skip


def self_check_url(server: PooledHTTPServer) -> str:
    raw, port = server.server_address[:2]
    host = raw.decode() if isinstance(raw, bytes) else str(raw)
    if host in {"", "0.0.0.0", "::"}:
        host = "127.0.0.1"
    return f"http://{f'[{host}]' if ':' in host else host}:{port}/health"


def answers(url: str, timeout: float = 10.0) -> bool:
    """Does the server at ``url`` answer at all? A 503 counts: it means the agent is down, not the server."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as resp:
            resp.read(1)
        return True
    except urllib.error.HTTPError:
        return True
    except (TimeoutError, urllib.error.URLError, OSError):
        return False


def watchdog_loop(server: PooledHTTPServer, notifier: Any, stop: threading.Event) -> None:
    """Keep systemd's watchdog fed only while this server really answers: if a request ever hangs the server
    (or it stops accepting), the pings stop and systemd restarts the dashboard (``WatchdogSec``)."""
    url = self_check_url(server)
    every = max(0.05, float(notifier.ping_every))
    while not stop.wait(every):
        if answers(url, timeout=min(10.0, every * 2)):
            notifier.watchdog(force=True)
        else:
            log.warning("the dashboard did not answer its own health check; systemd will restart it")


def serve(config: Config) -> int:
    import signal

    from polymath.body.systemd_notify import Notifier

    host = config.dashboard.host
    if not loopback(host):  # the sign-in-free listener never leaves the Pi; other devices use the remote one
        log.warning("dashboard.host is not loopback: the Pi's own listener stays on 127.0.0.1 (other devices: "
                    "https on access.port, after pairing)", extra={"host": host})  # fmt: skip
        host = "127.0.0.1"
    local = gate_for(config, remote=False)
    server = make_server(config, host=host, gate=local)
    notifier = Notifier()
    log.info("dashboard listening", extra={"host": host, "port": server.server_address[1]})
    remote: TLSServer | None = None
    remote_gate: Gate | None = None
    if config.access.remote:
        ctx = tls_context(config.access.tls_dir)
        if ctx is None:
            log.warning("no TLS certificate: other devices cannot reach the dashboard (sudo ./install.sh makes one)",
                        extra={"tls_dir": str(config.access.tls_dir)})  # fmt: skip
        else:
            try:
                remote_gate = gate_for(config, remote=True)
                remote = make_remote_server(config, server.data, remote_gate, ctx)
            except OSError as exc:
                log.error("cannot listen for other devices", extra={"port": config.access.port, "error": str(exc)})
            else:
                threading.Thread(target=remote.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True,
                                 name="dashboard-remote").start()  # fmt: skip
                log.info("listening for paired devices", extra={"port": config.access.port})
    stop_watch = threading.Event()

    def stop(*_: Any) -> None:
        stop_watch.set()
        notifier.stopping()
        threading.Thread(target=server.shutdown, daemon=True).start()
        if remote is not None:
            threading.Thread(target=remote.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    notifier.ready(f"listening on port {server.server_address[1]}")
    if notifier.watchdog_interval:
        threading.Thread(target=watchdog_loop, args=(server, notifier, stop_watch), daemon=True,
                         name="dashboard-watchdog").start()  # fmt: skip
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        stop_watch.set()
        if remote is not None:
            remote.server_close()
        server.server_close()
        for g in (local, remote_gate):
            if g is not None:
                g.access.flush()  # the devices' last-seen times
    return 0
