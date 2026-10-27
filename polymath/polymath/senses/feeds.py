"""RSS 2.0 / RSS 1.0 (RDF) / Atom feed reader with conditional GET."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree as ET

from polymath.core.jobs import JobContext, JobOutcome
from polymath.memory.documents import Document, DocumentStore
from polymath.senses.crawler import Frontier, domain_allowed, host_of, normalize_url, site_of
from polymath.senses.html_text import extract
from polymath.senses.net import FetchError, HttpClient
from polymath.senses.safexml import UnsafeXML, parse_untrusted_xml
from polymath.senses.streams import local_name

POLL_INTERVAL = 3600.0


@dataclass
class FeedItem:
    title: str
    link: str | None
    summary: str
    published: str | None
    guid: str


@dataclass
class Feed:
    title: str
    rights: str | None
    items: list[FeedItem]


def _child_text(el: ET.Element, *names: str) -> str:
    for child in el:
        if local_name(child.tag) in names:
            return "".join(child.itertext()).strip()
    return ""


def _atom_link(el: ET.Element) -> str | None:
    fallback = None
    for child in el:
        if local_name(child.tag) == "link":
            href = child.get("href")
            if href and child.get("rel", "alternate") == "alternate":
                return href
            fallback = fallback or href
    return fallback


def parse_feed(data: bytes) -> Feed:
    root = parse_untrusted_xml(data)
    kind = local_name(root.tag).lower()
    items: list[FeedItem] = []
    if kind == "feed":  # Atom
        title = _child_text(root, "title")
        rights = _child_text(root, "rights") or None
        for entry in (e for e in root if local_name(e.tag) == "entry"):
            link = _atom_link(entry)
            summary = _child_text(entry, "content") or _child_text(entry, "summary")
            items.append(
                FeedItem(
                    _child_text(entry, "title"),
                    link,
                    summary,
                    _child_text(entry, "published", "updated") or None,
                    _child_text(entry, "id") or link or "",
                )
            )
        return Feed(title, rights, items)
    channel = next((c for c in root if local_name(c.tag) == "channel"), root)
    title = _child_text(channel, "title")
    rights = _child_text(channel, "copyright", "rights") or None
    candidates = [e for e in channel if local_name(e.tag) == "item"] or [e for e in root if local_name(e.tag) == "item"]
    for it in candidates:
        link = _child_text(it, "link") or it.get("{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about")
        summary = _child_text(it, "encoded") or _child_text(it, "description")
        items.append(
            FeedItem(
                _child_text(it, "title"),
                link or None,
                summary,
                _child_text(it, "pubDate", "date") or None,
                _child_text(it, "guid") or link or _child_text(it, "title"),
            )
        )
    return Feed(title, rights, items)


def poll_feed(
    db: Any, client: HttpClient, store: DocumentStore, frontier: Frontier, url: str, allow_domains: list[str]
) -> dict[str, Any]:
    row = db.one("SELECT etag, last_modified FROM feeds WHERE url=?", (url,))
    headers = {}
    if row is not None and row["etag"]:
        headers["If-None-Match"] = row["etag"]
    if row is not None and row["last_modified"]:
        headers["If-Modified-Since"] = row["last_modified"]
    resp = client.get(url, headers, max_bytes=10_000_000, ok=(200, 304))
    now = time.time()
    if resp.status == 304:
        db.execute("UPDATE feeds SET last_checked=? WHERE url=?", (now, url))
        return {"url": url, "status": 304, "new": 0}
    feed = parse_feed(resp.body)
    license_ = (
        f"publisher terms ({feed.rights})" if feed.rights else "unknown (publisher feed; stored for private learning)"
    )
    new = 0
    feed_site = site_of(host_of(url))
    for item in feed.items:
        text = extract(f"<p>{item.summary}</p>").text if "<" in item.summary else item.summary.strip()
        body = f"{item.title}\n\n{text}".strip()
        if len(body) < 40:
            continue
        _, status = store.add(
            Document(
                source="feed",
                external_id=f"{url}#{item.guid}"[:1000],
                title=item.title[:500],
                text=body,
                license=license_,
                url=item.link,
                published=item.published,
                meta={"kind": "feed_item", "feed": url, "feed_title": feed.title},
            )
        )
        new += status in {"new", "updated"}
        if item.link and (norm := normalize_url(item.link)):
            if domain_allowed(host_of(norm), allow_domains):
                frontier.add(norm, priority=1.0, depth=0)
            elif site_of(host_of(norm)) == feed_site:
                frontier.add(norm, priority=1.0, depth=99)  # the feed's own article page; links not followed
            # a link to any other site is never fetched: the feed's summary is all that is kept
    db.execute(
        "INSERT INTO feeds(url, title, etag, last_modified, last_checked, items_seen) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(url) DO UPDATE SET title=excluded.title, etag=excluded.etag, last_modified=excluded.last_modified,"
        " last_checked=excluded.last_checked, items_seen=feeds.items_seen+excluded.items_seen, errors=0",
        (url, feed.title, resp.headers.get("etag"), resp.headers.get("last-modified"), now, len(feed.items)),
    )
    return {"url": url, "status": resp.status, "new": new, "items": len(feed.items)}


def feeds_job(ctx: JobContext) -> JobOutcome:
    """Job ``feeds.poll``: polls every feed whose interval elapsed."""
    client: HttpClient = ctx.services["http"]
    store: DocumentStore = ctx.services["docs"]
    crawler = ctx.services["crawler"]
    cutoff = time.time() - float(ctx.job.payload.get("interval", POLL_INTERVAL))
    for url in ctx.config.senses.feeds:
        ctx.db.execute("INSERT OR IGNORE INTO feeds(url) VALUES(?)", (url,))
    due = [
        r["url"]
        for r in ctx.db.query(
            "SELECT url FROM feeds WHERE (last_checked IS NULL OR last_checked < ?) AND errors < 10 "
            "ORDER BY last_checked IS NOT NULL, last_checked LIMIT 50",
            (cutoff,),
        )
    ]
    value = 0.0
    results = []
    for url in due:
        if ctx.should_stop():
            break
        host = host_of(url)
        ctx.db.execute("INSERT OR IGNORE INTO hosts(host) VALUES(?)", (host,))
        robots = crawler.robots(host)
        if robots is None:
            robots = crawler.fetch_robots(host, url.split(":", 1)[0])
        if not robots.allowed(url, crawler.agent):
            ctx.db.execute("UPDATE feeds SET errors=10, last_checked=? WHERE url=?", (time.time(), url))
            results.append({"url": url, "error": "robots.txt disallows this feed"})
            continue
        try:
            res = poll_feed(ctx.db, client, store, crawler.frontier, url, crawler.allow)
            value += float(res["new"])
        except (FetchError, UnsafeXML, ValueError) as exc:
            ctx.db.execute("UPDATE feeds SET errors=errors+1, last_checked=? WHERE url=?", (time.time(), url))
            res = {"url": url, "error": str(exc)}
        results.append(res)
        ctx.tick()
    return JobOutcome(done=True, value=value, result={"polled": len(results), "results": results[:20]})
