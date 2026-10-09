"""In-process HTTP server for offline tests: static routes, Range/If-Range, ETag, scripted responses."""

from __future__ import annotations

import gzip
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from polymath.senses.net import HttpClient


@dataclass
class Route:
    body: bytes = b""
    status: int = 200
    ctype: str = "text/html; charset=utf-8"
    headers: dict[str, str] = field(default_factory=dict)
    etag: str | None = None
    gzip: bool = False
    ranges: bool = True
    handler: Callable[[BaseHTTPRequestHandler], tuple[int, dict[str, str], bytes]] | None = None


class FakeWeb:
    def __init__(self) -> None:
        self.routes: dict[str, Route] = {}
        self.log: list[tuple[float, str, str, dict[str, str]]] = []
        web = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                return

            def _serve(self, head: bool) -> None:
                web.log.append(
                    (time.monotonic(), self.command, self.path, {k.lower(): v for k, v in self.headers.items()})
                )
                route = web.routes.get(self.path) or web.routes.get(self.path.split("?", 1)[0])
                if route is None:
                    self._send(404, {"Content-Type": "text/plain"}, b"not found", head)
                    return
                if route.handler is not None:
                    status, headers, body = route.handler(self)
                    self._send(status, headers, body, head)
                    return
                headers = {"Content-Type": route.ctype, **route.headers}
                if route.etag:
                    headers["ETag"] = route.etag
                    if self.headers.get("If-None-Match") == route.etag:
                        self._send(304, headers, b"", head)
                        return
                body = route.body
                rng = self.headers.get("Range")
                if_range = self.headers.get("If-Range")
                if rng and route.ranges and (if_range is None or if_range == route.etag):
                    spec = rng.split("=", 1)[1]
                    start_s, end_s = spec.split("-", 1)
                    start = int(start_s)
                    end = int(end_s) if end_s else len(body) - 1
                    if start >= len(body):
                        self._send(416, {"Content-Range": f"bytes */{len(body)}"}, b"", head)
                        return
                    end = min(end, len(body) - 1)
                    headers["Content-Range"] = f"bytes {start}-{end}/{len(body)}"
                    headers["Accept-Ranges"] = "bytes"
                    self._send(206, headers, body[start : end + 1], head)
                    return
                if route.gzip and "gzip" in self.headers.get("Accept-Encoding", ""):
                    body = gzip.compress(body)
                    headers["Content-Encoding"] = "gzip"
                self._send(route.status, headers, body, head)

            def _send(self, status: int, headers: dict[str, str], body: bytes, head: bool) -> None:
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if not head and body:
                    self.wfile.write(body)

            def do_GET(self) -> None:
                self._serve(False)

            def do_HEAD(self) -> None:
                self._serve(True)

        class QuietServer(ThreadingHTTPServer):
            def handle_error(self, request: Any, client_address: Any) -> None:
                return  # clients that hang up early (byte limits, time budgets) are expected in tests

        self.server = QuietServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def add(self, path: str, body: bytes | str = b"", **kw: Any) -> str:
        self.routes[path] = Route(body.encode() if isinstance(body, str) else body, **kw)
        return self.base + path

    def hits(self, path: str) -> list[float]:
        return [t for t, _m, p, _h in self.log if p == path]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def client(**kw: Any) -> HttpClient:
    return HttpClient(user_agent="PolymathBot/0.1 (test)", allow_private=True, env={}, timeout=5, **kw)
