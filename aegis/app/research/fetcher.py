"""Safe document fetching: URL policy, hop-by-hop redirect validation, size,
time and content-type limits, polite per-domain pacing, robots.txt."""
from __future__ import annotations

import logging
import threading
import time
import urllib.robotparser
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import httpx

from ..security.network_policy import URLPolicy, URLRejected, safe_client

log = logging.getLogger(__name__)

PERMITTED_TYPES = (
    "text/html", "application/xhtml+xml", "text/plain", "text/markdown",
    "application/rss+xml", "application/atom+xml", "application/xml", "text/xml",
    "application/json", "application/feed+json",
)


class FetchError(Exception):
    def __init__(self, message: str, error_class: str, transient: bool = False, status: int | None = None):
        super().__init__(message)
        self.error_class = error_class
        self.transient = transient
        self.status = status


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    content_type: str
    body: bytes
    redirects: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        charset = "utf-8"
        if "charset=" in self.content_type:
            charset = self.content_type.split("charset=")[-1].split(";")[0].strip() or "utf-8"
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")


class Fetcher:
    def __init__(self, policy: URLPolicy, *, max_bytes: int, connect_timeout: float, read_timeout: float,
                 max_redirects: int, user_agent: str, per_domain_delay: float = 2.0,
                 transport: httpx.BaseTransport | None = None, respect_robots: bool = True,
                 sleep=time.sleep):
        self.policy = policy
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.user_agent = user_agent
        self.per_domain_delay = per_domain_delay
        self.respect_robots = respect_robots
        self._client = safe_client(connect_timeout, read_timeout, user_agent, transport)
        self._last_hit: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._lock = threading.Lock()
        self._sleep = sleep

    def close(self):
        self._client.close()

    def _pace(self, host: str) -> None:
        with self._lock:
            last = self._last_hit.get(host, 0.0)
            wait = self.per_domain_delay - (time.monotonic() - last)
            self._last_hit[host] = time.monotonic() + max(0.0, wait)
        if wait > 0:
            self._sleep(wait)

    def _robots_allows(self, url: str, extra_allowed: list[str] | None) -> bool:
        if not self.respect_robots:
            return True
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            rp = None
            try:
                body = self._get(origin + "/robots.txt", extra_allowed, check_robots=False,
                                 accept_any_type=True, max_bytes=200_000)
                if body.status == 200:
                    rp = urllib.robotparser.RobotFileParser()
                    rp.parse(body.text.splitlines())
            except FetchError:
                rp = None  # unreachable robots.txt: treat as no restrictions
            self._robots[origin] = rp
        rp = self._robots[origin]
        return True if rp is None else rp.can_fetch(self.user_agent, url)

    def fetch(self, url: str, extra_allowed: list[str] | None = None) -> FetchResult:
        return self._get(url, extra_allowed, check_robots=True)

    def _get(self, url: str, extra_allowed, *, check_robots: bool, accept_any_type: bool = False,
             max_bytes: int | None = None) -> FetchResult:
        max_bytes = max_bytes or self.max_bytes
        redirects: list[str] = []
        current = url
        for _ in range(self.max_redirects + 1):
            try:
                host = self.policy.check(current, extra_allowed)
            except URLRejected as exc:
                raise FetchError(f"rejected {current}: {exc}", "policy") from exc
            if check_robots and not self._robots_allows(current, extra_allowed):
                raise FetchError(f"robots.txt disallows {current}", "robots")
            self._pace(host)
            try:
                with self._client.stream("GET", current) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        loc = resp.headers.get("location")
                        if not loc:
                            raise FetchError("redirect without location", "http", status=resp.status_code)
                        redirects.append(current)
                        current = urljoin(current, loc)
                        continue
                    if resp.status_code == 429 or resp.status_code >= 500:
                        raise FetchError(f"HTTP {resp.status_code}", "http", transient=True, status=resp.status_code)
                    if resp.status_code >= 400:
                        raise FetchError(f"HTTP {resp.status_code}", "http", status=resp.status_code)
                    ctype = resp.headers.get("content-type", "").lower()
                    base_type = ctype.split(";")[0].strip()
                    if not accept_any_type and base_type not in PERMITTED_TYPES:
                        raise FetchError(f"content type '{base_type or 'unknown'}' not permitted", "content_type")
                    declared = resp.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > max_bytes:
                        raise FetchError(f"response too large ({declared} bytes)", "too_large")
                    chunks, total = [], 0
                    for chunk in resp.iter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            raise FetchError(f"response exceeded {max_bytes} bytes", "too_large")
                        chunks.append(chunk)
                    return FetchResult(url, current, resp.status_code, ctype, b"".join(chunks), redirects)
            except URLRejected as exc:  # raised from the validating network backend
                raise FetchError(f"rejected {current}: {exc}", "policy") from exc
            except httpx.TimeoutException as exc:
                raise FetchError(f"timeout fetching {current}", "timeout", transient=True) from exc
            except httpx.TransportError as exc:
                cause = exc.__cause__ or exc.__context__
                if isinstance(cause, URLRejected) or "resolves to non-public" in str(exc) or "not public" in str(exc):
                    raise FetchError(f"rejected {current}: {exc}", "policy") from exc
                raise FetchError(f"network error fetching {current}: {exc}", "network", transient=True) from exc
        raise FetchError(f"too many redirects (>{self.max_redirects})", "redirects")
