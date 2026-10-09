"""Source discovery providers for deep research.

Only official, documented public APIs whose robots.txt permits automated
access are used (checked live): the Wikimedia search API and OpenAlex.
Wikipedia's /w/api.php and arXiv's export API disallow crawlers in robots.txt
and are therefore *not* used. A self-hosted SearXNG instance is used when
configured. Every request goes through the SSRF-safe fetcher (domain policy,
robots.txt, size/time limits, per-domain pacing).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import quote, urlencode

from .fetcher import FetchError

log = logging.getLogger(__name__)

BRAVE_SEARCH = "https://api.search.brave.com/res/v1/web/search"
WIKIMEDIA_SEARCH = "https://api.wikimedia.org/core/v1/wikipedia/{lang}/search/page"
OPENALEX_WORKS = "https://api.openalex.org/works"
_TAG = re.compile(r"<[^>]+>")


@dataclass
class Candidate:
    """A URL worth fetching later."""
    url: str
    title: str = ""
    snippet: str = ""
    origin: str = ""  # provider or parent URL


@dataclass
class ContentItem:
    """Content delivered directly by an API (no page fetch needed)."""
    url: str
    title: str
    text: str
    domain: str
    published_at: str | None = None
    author: str | None = None
    origin: str = ""
    meta: dict = field(default_factory=dict)


@dataclass
class DiscoveryResult:
    candidates: list[Candidate] = field(default_factory=list)
    content: list[ContentItem] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _json(fetcher, url: str):
    res = fetcher.fetch(url)
    return json.loads(res.text)


def wikipedia_search(fetcher, query: str, limit: int = 8, lang: str = "en") -> DiscoveryResult:
    out = DiscoveryResult()
    url = WIKIMEDIA_SEARCH.format(lang=lang) + "?" + urlencode({"q": query, "limit": limit})
    try:
        data = _json(fetcher, url)
    except (FetchError, ValueError) as exc:
        out.errors.append(f"wikipedia: {exc}")
        return out
    for page in data.get("pages", [])[:limit]:
        key = page.get("key")
        if not key:
            continue
        out.candidates.append(Candidate(
            url=f"https://{lang}.wikipedia.org/wiki/{quote(key)}", title=page.get("title", ""),
            snippet=_TAG.sub("", page.get("excerpt") or page.get("description") or ""), origin="wikipedia"))
    return out


def _abstract(inverted: dict | None) -> str:
    if not inverted:
        return ""
    positions = []
    for word, idxs in inverted.items():
        for i in idxs:
            positions.append((i, word))
    return " ".join(w for _, w in sorted(positions))


def openalex_search(fetcher, query: str, limit: int = 8, contact: str = "", cites: str | None = None) -> DiscoveryResult:
    """Scholarly works. Abstracts become content items; the work's DOI is the citation.
    Independence for corroboration is tracked per publisher (DOI prefix), so many
    papers from one publisher do not count as many independent sources."""
    out = DiscoveryResult()
    params = {"per-page": limit,
              "select": "id,doi,title,publication_year,publication_date,authorships,abstract_inverted_index,"
                        "primary_location,cited_by_count,type"}
    if cites:  # snowballing: works that cite the given OpenAlex work, most cited first
        params["filter"] = f"cites:{cites}"
        params["sort"] = "cited_by_count:desc"
    else:
        params["search"] = query
    if contact:
        params["mailto"] = contact
    try:
        data = _json(fetcher, OPENALEX_WORKS + "?" + urlencode(params))
    except (FetchError, ValueError) as exc:
        out.errors.append(f"openalex: {exc}")
        return out
    for w in data.get("results", [])[:limit]:
        abstract = _abstract(w.get("abstract_inverted_index"))
        title = (w.get("title") or "").strip()
        doi = (w.get("doi") or "").strip()
        if not title or len(abstract.split()) < 40:
            continue
        doi_path = doi.replace("https://doi.org/", "")
        prefix = doi_path.split("/", 1)[0] if doi_path else ""
        url = doi or w.get("id") or ""
        if not url.startswith("https://"):
            continue
        authors = [a.get("author", {}).get("display_name") for a in (w.get("authorships") or [])][:5]
        venue = ((w.get("primary_location") or {}).get("source") or {}).get("display_name") or ""
        # The title is stored as the document title, not repeated in the body (a title is not a claim).
        text = (f"{abstract}\n\nPublished {w.get('publication_year') or 'n.d.'}"
                + (f" in {venue}" if venue else "") + f". Cited by {w.get('cited_by_count', 0)} works.")
        out.content.append(ContentItem(
            url=url, title=title, text=text, domain=f"doi.org/{prefix}" if prefix else "openalex.org",
            published_at=w.get("publication_date"), author=", ".join(a for a in authors if a) or None,
            origin="openalex", meta={"cited_by": w.get("cited_by_count", 0), "type": w.get("type"),
                                     "work": (w.get("id") or "").rsplit("/", 1)[-1]}))
    return out


def searxng_search(engine, query: str, limit: int = 10) -> DiscoveryResult:
    out = DiscoveryResult()
    for r in engine.search(query, None, limit):
        out.candidates.append(Candidate(url=r["url"], title=r.get("title", ""), snippet=r.get("snippet", ""),
                                        origin="searxng"))
    return out


def brave_search(fetcher, query: str, api_key: str, limit: int = 10) -> DiscoveryResult:
    """Brave Search API (web-scale discovery; requires BRAVE_SEARCH_API_KEY)."""
    out = DiscoveryResult()
    url = BRAVE_SEARCH + "?" + urlencode({"q": query, "count": min(limit, 20)})
    try:
        res = fetcher.fetch(url, headers={"X-Subscription-Token": api_key, "Accept": "application/json"})
        data = json.loads(res.text)
    except (FetchError, ValueError) as exc:
        out.errors.append(f"brave: {exc}")
        return out
    for r in (data.get("web") or {}).get("results", [])[:limit]:
        if r.get("url", "").startswith(("https://", "http://")):
            out.candidates.append(Candidate(url=r["url"], title=_TAG.sub("", r.get("title", "")),
                                            snippet=_TAG.sub("", r.get("description", "")), origin="brave"))
    return out
