"""Dashboard: stdlib ThreadingHTTPServer on port 8765, read-only, canvas charts, no external resources.

* Opens the database **read-only**; the only input is the ask box, which runs the
  offline answering engine (still read-only).
* ``/health`` answers 200 only when the database is open and the agent's
  heartbeat is younger than ``loop.heartbeat_stale`` (60 s) — so systemd, the
  kiosk and monitoring see a stalled agent as unhealthy.
* Strict CSP, small request bodies, per-client rate limit for ``/api/ask``.
"""

from __future__ import annotations

import json
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import parse_qs, urlsplit

from polymath import __version__
from polymath.core.config import Config
from polymath.core.db import Database
from polymath.core.logging import get_logger
from polymath.core.loop import heartbeat_path

log = get_logger("dashboard")
MAX_BODY = 4096
ASK_PER_MINUTE = 20
ANSWERER_TTL = 600.0
STATIC = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
    "icon.svg": "image/svg+xml",
}
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}


class DashboardData:
    """All queries the dashboard needs (read-only)."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._db: Database | None = None
        self._answerer: Any = None
        self._answerer_at = 0.0
        self._lock = threading.Lock()

    def db(self) -> Database:
        with self._lock:
            if self._db is None or not self._db.is_open:
                self._db = Database(self.config.paths.db_path, readonly=True)
            return self._db

    def health(self) -> tuple[bool, dict[str, Any]]:
        try:
            hb = self.db().kv_get("heartbeat") or {}
        except Exception as exc:
            return False, {"ok": False, "error": f"database unavailable: {type(exc).__name__}"}
        age = time.time() - float(hb.get("ts", 0)) if hb else None
        if hb and hb.get("state") not in {"stopped", None}:
            try:  # a long job slice cannot commit its heartbeat; the agent's pulse file shows it is alive
                age = min(age or 1e18, time.time() - heartbeat_path(self.config).stat().st_mtime)
            except OSError:
                pass
        ok = age is not None and age < self.config.loop.heartbeat_stale and hb.get("state") != "stopped"
        return ok, {
            "ok": ok,
            "heartbeat_age_s": None if age is None else round(age, 1),
            "state": hb.get("state"),
            "cycle": hb.get("cycle"),
            "version": __version__,
        }

    def overview(self) -> dict[str, Any]:
        db = self.db()
        _ok, health = self.health()
        docs = {
            r["source"]: int(r["n"])
            for r in db.query(
                "SELECT source, COUNT(*) AS n FROM documents WHERE state!='duplicate' GROUP BY source ORDER BY n DESC"
            )
        }
        triples = {
            r["status"]: int(r["n"]) for r in db.query("SELECT status, COUNT(*) AS n FROM triples GROUP BY status")
        }
        quiz = db.one("SELECT n, correct, accuracy, chance, created FROM quizzes ORDER BY id DESC LIMIT 1")
        vit = db.one("SELECT * FROM vitals ORDER BY at DESC LIMIT 1")
        recent = [
            dict(r)
            for r in db.query(
                "SELECT id, action, status, ROUND(cpu_seconds, 2) AS cpu, ROUND(value, 2) AS value, ended FROM cycles "
                "ORDER BY id DESC LIMIT 15"
            )
        ]
        queue = {r["state"]: int(r["n"]) for r in db.query("SELECT state, COUNT(*) AS n FROM jobs GROUP BY state")}
        return {
            "health": health,
            "documents": docs,
            "documents_total": sum(docs.values()),
            "entities": int(db.scalar("SELECT COUNT(*) FROM entities", default=0)),
            "triples": triples,
            "quiz": dict(quiz) if quiz else None,
            "vitals": dict(vit) if vit else None,
            "recent_cycles": recent,
            "queue": queue,
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
        db = self.db()  # outside the lock: db() takes it too
        with self._lock:
            stale = time.monotonic() - self._answerer_at > ANSWERER_TTL  # pick up the agent's rebuilt alias index
            if self._answerer is None or self._answerer.db is not db or stale:
                from polymath.interface.answer import Answerer
                from polymath.perception.entities import AliasIndex, EntityLinker

                auto = AliasIndex(db, self.config.paths.index_dir).load()
                self._answerer = Answerer(db, EntityLinker(db, auto) if auto else None)
                self._answerer_at = time.monotonic()
            answerer = self._answerer
        ans = answerer.ask(question)
        return {**ans.to_dict(), "rendered": ans.render()}


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
            return True


def make_handler(data: DashboardData, limiter: RateLimiter) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "Polymath"
        sys_version = ""

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug("http", extra={"client": self.client_address[0], "line": fmt % args})

        def _send(self, status: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, obj: Any) -> None:
            self._send(status, json.dumps(obj, default=str).encode())

        def do_HEAD(self) -> None:
            self.do_GET()

        def do_GET(self) -> None:
            url = urlsplit(self.path)
            path = url.path
            try:
                if path in {"/", "/index.html"}:
                    return self._static("index.html")
                if path.startswith("/static/"):
                    return self._static(path.removeprefix("/static/"))
                if path == "/health":
                    ok, body = data.health()
                    return self._json(200 if ok else 503, body)
                if path == "/api/overview":
                    return self._json(200, data.overview())
                if path == "/api/timeseries":
                    return self._json(200, data.timeseries())
                if path == "/api/topics":
                    return self._json(200, data.topics())
                if path == "/api/agents":
                    return self._json(200, data.agents())
                if path == "/api/knowledge":
                    q = (parse_qs(url.query).get("q") or [""])[0][:200]
                    return self._json(200, data.knowledge(q) if q.strip() else {"query": "", "entities": []})
                return self._json(404, {"error": "not found"})
            except FileNotFoundError:
                return self._json(503, {"error": "database not initialised yet"})
            except Exception:
                log.exception("dashboard request failed", extra={"path": path})
                return self._json(500, {"error": "internal error"})

        def do_POST(self) -> None:
            if urlsplit(self.path).path != "/api/ask":
                return self._json(405, {"error": "the dashboard is read-only"})
            length = int(self.headers.get("Content-Length") or 0)
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
            except Exception:
                log.exception("ask failed")
                return self._json(500, {"error": "internal error"})

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


def make_server(config: Config, host: str | None = None, port: int | None = None) -> ThreadingHTTPServer:
    data = DashboardData(config)
    server = ThreadingHTTPServer(
        (host or config.dashboard.host, config.dashboard.port if port is None else port),
        make_handler(data, RateLimiter(ASK_PER_MINUTE)),
    )
    server.daemon_threads = True
    return server


def serve(config: Config) -> int:
    import signal

    server = make_server(config)
    log.info("dashboard listening", extra={"host": config.dashboard.host, "port": server.server_address[1]})

    def stop(*_: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
    return 0
