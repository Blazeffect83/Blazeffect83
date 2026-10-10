"""Polite crawler: SQLite frontier, per-host token buckets, own robots parser.

Politeness guarantees (enforced in code, covered by tests):

* at most ``crawl_rate`` (≤ 1) requests per second per host — a token bucket
  of capacity 1 persisted in ``hosts`` so restarts cannot burst;
* ``Crawl-delay`` honoured when larger; ``Retry-After`` and 429/5xx back off;
* robots.txt fetched (and cached 24 h) before any page of a host;
* ``noindex``/``nofollow`` meta directives and ``rel=nofollow`` obeyed;
* only http(s), public addresses, size-capped bodies, allow-listed domains.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from polymath.core.db import Database
from polymath.core.jobs import JobContext, JobOutcome
from polymath.memory.documents import Document, DocumentStore
from polymath.senses.html_text import detect_license, extract
from polymath.senses.net import BlockedURL, FetchError, HttpClient, Response
from polymath.senses.robots import MAX_ROBOTS_BYTES, Robots, robots_for_status
from polymath.senses.safexml import UnsafeXML, parse_untrusted_xml
from polymath.senses.streams import local_name

ROBOTS_TTL = 24 * 3600.0
TRACKING = re.compile(r"^(utm_|fbclid$|gclid$|mc_cid$|mc_eid$|ref$|ref_src$)")
HTML_TYPES = {"text/html", "application/xhtml+xml"}
TEXT_TYPES = {"text/plain"}
FEED_TYPES = {"application/rss+xml", "application/atom+xml", "application/rdf+xml"}


def normalize_url(url: str) -> str | None:
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return None
    scheme = p.scheme.lower()
    if scheme not in {"http", "https"} or not p.hostname:
        return None
    host = p.hostname.lower().rstrip(".")
    try:
        port = p.port
    except ValueError:
        return None
    netloc = host if port in (None, 80 if scheme == "http" else 443) else f"{host}:{port}"
    segs: list[str] = []
    for seg in (p.path or "/").split("/"):
        if seg == "..":
            if len(segs) > 1:
                segs.pop()
        elif seg != ".":
            segs.append(seg)
    path = "/".join(segs) or "/"
    if not path.startswith("/"):
        path = "/" + path
    query = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not TRACKING.match(k)])
    return urlunsplit((scheme, netloc, path, query, ""))


def host_of(url: str) -> str:
    """Origin key for politeness/robots: lower-case host plus any non-default port."""
    try:
        p = urlsplit(url)
        host = (p.hostname or "").lower()
        port = p.port
    except ValueError:
        return ""
    default = 443 if p.scheme == "https" else 80
    return host if port in (None, default) else f"{host}:{port}"


def domain_allowed(host: str, allow: list[str]) -> bool:
    name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return any(name == d or name.endswith("." + d) for d in allow)


_SECOND_LEVEL = {"co", "com", "org", "net", "ac", "gov", "edu", "or", "ne", "go"}


def site_of(host: str) -> str:
    """The site a host belongs to, roughly its registrable domain.

    ``feeds.npr.org`` → ``npr.org``; ``rss.nytimes.com`` → ``nytimes.com``; ``feeds.bbci.co.uk`` → ``bbci.co.uk``.
    """
    name = (host.rsplit(":", 1)[0] if host.count(":") == 1 else host).lower().strip(".")
    if ":" in name or name.replace(".", "").isdigit():
        return name  # an IP address is its own site
    parts = name.split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in _SECOND_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


class TokenBucket:
    """Classic token bucket; with capacity 1 it enforces a strict minimum interval."""

    def __init__(self, rate: float, capacity: float = 1.0, tokens: float = 1.0, ts: float = 0.0) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = rate
        self.capacity = capacity
        self.tokens = tokens
        self.ts = ts

    def refill(self, now: float) -> None:
        if now > self.ts:
            self.tokens = min(self.capacity, self.tokens + (now - self.ts) * self.rate)
            self.ts = now

    def try_take(self, now: float) -> bool:
        self.refill(now)
        if self.tokens >= 1.0 - 1e-9:
            self.tokens -= 1.0
            return True
        return False

    def available_at(self, now: float) -> float:
        self.refill(now)
        return now if self.tokens >= 1.0 else now + (1.0 - self.tokens) / self.rate


@dataclass
class FrontierItem:
    url: str
    host: str
    depth: int
    priority: float
    attempts: int
    etag: str | None
    last_modified: str | None


class Frontier:
    def __init__(self, db: Database, *, rate: float = 1.0, clock: Any = time.time) -> None:
        self.db = db
        self.rate = min(1.0, rate)
        self.clock = clock

    def add(self, url: str, *, priority: float = 0.0, depth: int = 0, referrer: str | None = None) -> bool:
        norm = normalize_url(url)
        if norm is None:
            return False
        host = host_of(norm)
        self.db.execute("INSERT OR IGNORE INTO hosts(host) VALUES(?)", (host,))
        cur = self.db.execute(
            "INSERT OR IGNORE INTO frontier(url, host, priority, depth, referrer, added) VALUES(?,?,?,?,?,?)",
            (norm, host, priority, depth, referrer, self.clock()),
        )
        return bool(cur.rowcount)

    def next_ready(self) -> FrontierItem | None:
        now = self.clock()
        row = self.db.one(
            "SELECT f.* FROM frontier f JOIN hosts h ON h.host=f.host WHERE f.state='new' AND f.next_attempt<=? "
            "AND h.next_fetch_at<=? ORDER BY f.priority DESC, f.depth ASC, f.added ASC LIMIT 1",
            (now, now),
        )
        if row is None:
            return None
        return FrontierItem(
            row["url"],
            row["host"],
            int(row["depth"]),
            float(row["priority"]),
            int(row["attempts"]),
            row["etag"],
            row["last_modified"],
        )

    def next_time(self) -> float | None:
        v = self.db.scalar(
            "SELECT MIN(MAX(f.next_attempt, h.next_fetch_at)) FROM frontier f JOIN hosts h ON h.host=f.host "
            "WHERE f.state='new'"
        )
        return None if v is None else float(v)

    def pending(self) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM frontier WHERE state='new'", default=0))

    def take_token(self, host: str, crawl_delay: float | None = None) -> bool:
        """Consume the host's token; schedules the next permitted request time."""
        row = self.db.one("SELECT tokens, bucket_ts, crawl_delay FROM hosts WHERE host=?", (host,))
        delay = max(crawl_delay or 0.0, float(row["crawl_delay"] or 0.0) if row else 0.0)
        rate = min(self.rate, 1.0 / delay) if delay > 0 else self.rate
        now = self.clock()
        bucket = TokenBucket(rate, 1.0, float(row["tokens"]) if row else 1.0, float(row["bucket_ts"]) if row else 0.0)
        ok = bucket.try_take(now)
        self.db.execute(
            "UPDATE hosts SET tokens=?, bucket_ts=?, next_fetch_at=? WHERE host=?",
            (bucket.tokens, bucket.ts, bucket.available_at(now), host),
        )
        return ok

    def defer_host(self, host: str, seconds: float) -> None:
        self.db.execute(
            "UPDATE hosts SET next_fetch_at=MAX(next_fetch_at, ?), errors=errors+1 WHERE host=?",
            (self.clock() + seconds, host),
        )

    def mark(
        self,
        url: str,
        state: str,
        *,
        status: int | None = None,
        reason: str = "",
        retry_in: float | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> None:
        now = self.clock()
        if retry_in is not None:
            self.db.execute(
                "UPDATE frontier SET attempts=attempts+1, next_attempt=?, http_status=?, reason=?, "
                "state=CASE WHEN attempts+1>=4 THEN 'failed' ELSE 'new' END WHERE url=?",
                (now + retry_in, status, reason[:300], url),
            )
            return
        self.db.execute(
            "UPDATE frontier SET state=?, http_status=?, reason=?, fetched_at=?, etag=COALESCE(?, etag), "
            "last_modified=COALESCE(?, last_modified) WHERE url=?",
            (state, status, reason[:300], now, etag, last_modified, url),
        )


def _retry_after(resp_headers: dict[str, str], default: float) -> float:
    value = resp_headers.get("retry-after", "")
    if value.isdigit():
        return min(86400.0, float(value))
    if value:
        try:
            return max(default, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError):
            pass
    return default


class Crawler:
    def __init__(
        self,
        db: Database,
        client: HttpClient,
        store: DocumentStore,
        *,
        user_agent: str,
        allow_domains: list[str],
        feed_sites: list[str] | None = None,
        max_bytes: int = 5_000_000,
        max_depth: int = 3,
        rate: float = 1.0,
        clock: Any = time.time,
    ) -> None:
        self.db = db
        self.client = client
        self.store = store
        self.agent = user_agent
        self.allow = [d.lower().lstrip(".") for d in allow_domains]
        # sites of the approved news/science feeds: their own article pages may be fetched (never followed further)
        self.feed_sites = {site_of(s) for s in (feed_sites or [])}
        self.max_bytes = max_bytes
        self.max_depth = max_depth
        self.frontier = Frontier(db, rate=rate, clock=clock)
        self.clock = clock
        self._robots: dict[str, Robots] = {}

    # ------------------------------------------------------------------ robots
    def robots(self, host: str, scheme: str = "https") -> Robots | None:
        """Cached robots for ``host``; ``None`` means it must be fetched first."""
        row = self.db.one("SELECT robots, robots_status, robots_fetched FROM hosts WHERE host=?", (host,))
        if row is None or row["robots_fetched"] is None or self.clock() - row["robots_fetched"] > ROBOTS_TTL:
            return None
        if host not in self._robots:
            self._robots[host] = robots_for_status(int(row["robots_status"]), row["robots"] or "")
        return self._robots[host]

    def fetch_robots(self, host: str, scheme: str) -> Robots:
        status, text = 0, ""
        try:
            resp = self.client.get(
                f"{scheme}://{host}/robots.txt", max_bytes=MAX_ROBOTS_BYTES * 2, ok=tuple(range(200, 600))
            )
            status, text = resp.status, resp.text()[:MAX_ROBOTS_BYTES]
        except BlockedURL:
            status = 403  # we will never fetch from this host anyway
            text = ""
        except FetchError:
            status = 503
        robots = robots_for_status(status, text)
        delay = robots.crawl_delay(self.agent)
        retry = self.clock() - ROBOTS_TTL + 3600 if status >= 500 or status == 0 else self.clock()
        self.db.execute(
            "UPDATE hosts SET robots=?, robots_status=?, robots_fetched=?, crawl_delay=? WHERE host=?",
            (text, status, retry, min(delay, 60.0) if delay else None, host),
        )
        self._robots[host] = robots
        return robots

    def permitted(self, host: str) -> bool:
        """Only approved sites are ever fetched: the crawl allow-list (reference sites, and sites you asked it to
        learn from) and the sites of the approved feeds. Nothing else, whatever links point to."""
        return domain_allowed(host, self.allow) or site_of(host) in self.feed_sites

    # ------------------------------------------------------------------- steps
    def step(self) -> dict[str, Any]:
        """Perform at most one HTTP request. Returns what happened."""
        item = self.frontier.next_ready()
        if item is None:
            return {"action": "none"}
        if not self.permitted(item.host):
            self.frontier.mark(item.url, "skipped", reason="not an approved site")
            return {"action": "skipped", "url": item.url, "reason": "not an approved site"}
        scheme = urlsplit(item.url).scheme
        robots = self.robots(item.host)
        if robots is None:
            if not self.frontier.take_token(item.host):
                return {"action": "wait"}
            robots = self.fetch_robots(item.host, scheme)
            return {"action": "robots", "host": item.host}
        if not robots.allowed(item.url, self.agent):
            self.frontier.mark(item.url, "skipped", reason="robots.txt disallow")
            return {"action": "skipped", "url": item.url}
        if not self.frontier.take_token(item.host, robots.crawl_delay(self.agent)):
            return {"action": "wait"}
        headers: dict[str, str] = {"Accept": "text/html,application/xhtml+xml,text/plain;q=0.8,*/*;q=0.1"}
        if item.etag:
            headers["If-None-Match"] = item.etag
        if item.last_modified:
            headers["If-Modified-Since"] = item.last_modified
        try:
            resp = self.client.get(item.url, headers, max_bytes=self.max_bytes, ok=tuple(range(200, 600)))
        except BlockedURL as exc:
            self.frontier.mark(item.url, "skipped", reason=str(exc))
            return {"action": "blocked", "url": item.url}
        except FetchError as exc:
            self.frontier.defer_host(item.host, 60.0)
            self.frontier.mark(item.url, "new", reason=str(exc), retry_in=300.0)
            return {"action": "error", "url": item.url, "error": str(exc)}
        return self._handle(item, resp)

    def _handle(self, item: FrontierItem, resp: Response) -> dict[str, Any]:
        if resp.status == 304:
            self.frontier.mark(item.url, "done", status=304, reason="not modified")
            return {"action": "unchanged", "url": item.url}
        if resp.status == 429 or resp.status >= 500:
            wait = _retry_after(resp.headers, 120.0)
            self.frontier.defer_host(item.host, wait)
            self.frontier.mark(item.url, "new", status=resp.status, reason="server busy", retry_in=wait)
            return {"action": "backoff", "url": item.url, "status": resp.status}
        if resp.status >= 400:
            self.frontier.mark(item.url, "failed", status=resp.status, reason=f"HTTP {resp.status}")
            return {"action": "failed", "url": item.url, "status": resp.status}
        final = normalize_url(resp.url) or item.url
        if not self.permitted(host_of(final)):
            self.frontier.mark(item.url, "skipped", status=resp.status, reason="redirected off the approved sites")
            return {"action": "skipped", "url": item.url, "reason": "redirected off the approved sites"}
        ctype = resp.content_type
        etag, lm = resp.headers.get("etag"), resp.headers.get("last-modified")
        if ctype in FEED_TYPES or (ctype in {"application/xml", "text/xml"} and b"<rss" in resp.body[:2000]):
            self.db.execute("INSERT OR IGNORE INTO feeds(url) VALUES(?)", (final,))
            self.frontier.mark(item.url, "done", status=resp.status, reason="feed registered")
            return {"action": "feed", "url": final}
        head = resp.body[:4000]
        if ctype in {"application/xml", "text/xml"} and (b"<urlset" in head or b"<sitemapindex" in head):
            added = self._sitemap(resp.body, item.depth)
            self.frontier.mark(item.url, "done", status=resp.status, reason=f"sitemap +{added}")
            return {"action": "sitemap", "url": final, "added": added}
        if ctype not in HTML_TYPES | TEXT_TYPES:
            self.frontier.mark(item.url, "skipped", status=resp.status, reason=f"content-type {ctype}")
            return {"action": "skipped", "url": item.url}
        html = resp.text()
        stored = False
        if ctype in TEXT_TYPES:
            title, text, lang, links, noindex, nofollow = final.rsplit("/", 1)[-1], html, None, [], False, True
            license_, license_url = "unknown (robots.txt permitted; stored for private learning)", None
        else:
            ex = extract(html, final)
            title, text, lang, links, noindex, nofollow = ex.title, ex.text, ex.lang, ex.links, ex.noindex, ex.nofollow
            license_, license_url = detect_license(ex, html)
        if not noindex and len(text) >= 200:
            _, status = self.store.add(
                Document(
                    source="web",
                    external_id=final,
                    title=title[:500],
                    text=text,
                    license=license_,
                    license_url=license_url,
                    url=final,
                    lang=lang or "en",
                    meta={"kind": "webpage", "host": host_of(final), "depth": item.depth},
                )
            )
            stored = status in {"new", "updated"}
        added = 0
        if not nofollow and item.depth < self.max_depth:
            for link in links:
                if link.nofollow:
                    continue
                norm = normalize_url(link.url)
                if norm and domain_allowed(host_of(norm), self.allow):
                    added += self.frontier.add(norm, priority=item.priority - 0.1, depth=item.depth + 1, referrer=final)
        self.frontier.mark(
            item.url,
            "done",
            status=resp.status,
            reason="stored" if stored else "not stored",
            etag=etag,
            last_modified=lm,
        )
        return {"action": "fetched", "url": final, "stored": stored, "links_added": added}

    def _sitemap(self, body: bytes, depth: int) -> int:
        try:
            root = parse_untrusted_xml(body)
        except (UnsafeXML, ValueError):
            return 0
        added = 0
        for loc in root.iter():
            if local_name(loc.tag) == "loc" and loc.text:
                norm = normalize_url(loc.text.strip())
                if norm and domain_allowed(host_of(norm), self.allow):
                    added += self.frontier.add(norm, priority=0.5, depth=depth + 1)
            if added >= 5000:
                break
        return added


def crawl_job(ctx: JobContext) -> JobOutcome:
    """Job ``crawl.step``: performs polite requests until the slice budget is used."""
    crawler: Crawler = ctx.services["crawler"]
    stats = {"requests": 0, "stored": 0, "links_added": 0}
    value = 0.0
    while not ctx.should_stop():
        res = crawler.step()
        action = res["action"]
        if action == "none":
            break
        if action == "wait":
            nxt = crawler.frontier.next_time()
            if nxt is None:
                break
            pause = nxt - crawler.clock()
            if pause > min(2.0, ctx.time_left()):
                break  # every host is resting; free the loop for other work
            if pause > 0:
                ctx.stop_event.wait(pause)
            ctx.tick()
            continue
        if action not in {"skipped"}:
            stats["requests"] += 1
        if res.get("stored"):
            stats["stored"] += 1
            value += 1.0
        stats["links_added"] += int(res.get("links_added", 0))
        ctx.tick()
    return JobOutcome(done=True, value=value, result=stats)
