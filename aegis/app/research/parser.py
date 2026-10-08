"""Readable-text extraction for HTML, feeds, JSON and plain text.

Uses only the standard library plus defusedxml (protects against XML entity
expansion / XXE). Scripts, styles, navigation, forms and hidden elements are
dropped; hidden text is a common prompt-injection vector.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin

from defusedxml import ElementTree as SafeET

_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "nav", "footer", "header", "form",
              "button", "iframe", "object", "embed", "aside", "select", "canvas"}
_BLOCK_TAGS = {"p", "div", "section", "article", "main", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4",
               "h5", "h6", "pre", "blockquote", "tr", "table", "dd", "dt", "figcaption"}
_VOID = {"br", "hr", "img", "input", "meta", "link", "area", "base", "col", "source", "wbr", "track", "param"}


@dataclass
class ParsedDocument:
    title: str = ""
    text: str = ""
    author: str | None = None
    published_at: str | None = None
    links: list[str] = field(default_factory=list)
    list_items: list[str] = field(default_factory=list)
    headings: list[str] = field(default_factory=list)
    hidden_text: str = ""  # text the page tried to hide from humans
    feed_entries: list[dict] = field(default_factory=list)
    kind: str = "html"


def _is_hidden(attrs: dict) -> bool:
    style = (attrs.get("style") or "").replace(" ", "").lower()
    if "display:none" in style or "visibility:hidden" in style or "font-size:0" in style:
        return True
    if "hidden" in attrs or attrs.get("aria-hidden") == "true":
        return True
    return False


class _TextExtractor(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base = base_url
        self.stack: list[tuple[str, bool, bool]] = []  # (tag, suppress, css/attr-hidden)
        self.parts: list[str] = []
        self.hidden: list[str] = []
        self.title = ""
        self._in_title = False
        self.meta: dict[str, str] = {}
        self.links: list[str] = []
        self.list_items: list[str] = []
        self.headings: list[str] = []
        self._cur_li: list[str] | None = None
        self._cur_h: list[str] | None = None

    @property
    def suppressed(self) -> bool:
        return any(s for _, s, _h in self.stack)

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key and a.get("content"):
                self.meta[key] = a["content"]
            return
        if tag == "a" and a.get("href") and not self.suppressed:
            self.links.append(urljoin(self.base, a["href"]))
        if tag == "title":
            self._in_title = True
        if tag in _VOID:
            if tag == "br":
                self.parts.append("\n")
            return
        hidden = _is_hidden(a)
        self.stack.append((tag, tag in _SKIP_TAGS or hidden, hidden))
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "li" and not self.suppressed:
            self._cur_li = []
        if tag in ("h1", "h2", "h3") and not self.suppressed:
            self._cur_h = []

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag == "li" and self._cur_li is not None:
            item = " ".join("".join(self._cur_li).split())
            if item:
                self.list_items.append(item)
            self._cur_li = None
        if tag in ("h1", "h2", "h3") and self._cur_h is not None:
            h = " ".join("".join(self._cur_h).split())
            if h:
                self.headings.append(h)
            self._cur_h = None
        # pop to the matching tag (tolerate malformed HTML)
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        if self.suppressed:
            # Record text hidden via CSS/attributes (not scripts/styles) for injection screening.
            if any(h for _t, _s, h in self.stack):
                self.hidden.append(data)
            return
        if not any(t == "pre" for t, _s, _h in self.stack):
            # Source line breaks inside a paragraph are not sentence boundaries.
            data = re.sub(r"\s+", " ", data)
        self.parts.append(data)
        if self._cur_li is not None:
            self._cur_li.append(data)
        if self._cur_h is not None:
            self._cur_h.append(data)


def _clean(text: str) -> str:
    lines = [" ".join(line.split()) for line in text.splitlines()]
    out, blank = [], 0
    for line in lines:
        if not line:
            blank += 1
            if blank <= 1:
                out.append("")
            continue
        blank = 0
        out.append(line)
    return "\n".join(out).strip()


def parse_html(html: str, base_url: str) -> ParsedDocument:
    p = _TextExtractor(base_url)
    try:
        p.feed(html)
        p.close()
    except Exception:  # pragma: no cover - HTMLParser is very tolerant
        pass
    meta = p.meta
    return ParsedDocument(
        title=_clean(unescape(meta.get("og:title") or p.title))[:500],
        text=_clean("".join(p.parts)),
        author=meta.get("author") or meta.get("article:author"),
        published_at=meta.get("article:published_time") or meta.get("date") or meta.get("dc.date")
        or meta.get("pubdate"),
        links=list(dict.fromkeys(p.links))[:500],
        list_items=p.list_items[:200],
        headings=p.headings[:100],
        hidden_text=_clean(" ".join(p.hidden))[:5000],
        kind="html",
    )


def _strip_tags(s: str) -> str:
    return _clean(parse_html(s, "").text) if "<" in s else _clean(unescape(s))


def parse_feed(xml_text: str) -> ParsedDocument:
    root = SafeET.fromstring(xml_text.encode() if isinstance(xml_text, str) else xml_text)
    entries = []

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1].lower()

    title = ""
    for el in root.iter():
        name = local(el.tag)
        if name in ("item", "entry"):
            e: dict = {}
            for child in el:
                cn = local(child.tag)
                if cn == "title":
                    e["title"] = (child.text or "").strip()
                elif cn == "link":
                    e["link"] = (child.get("href") or child.text or "").strip()
                elif cn in ("pubdate", "published", "updated", "date"):
                    e.setdefault("published", (child.text or "").strip())
                elif cn in ("description", "summary", "content"):
                    e.setdefault("summary", _strip_tags(child.text or "")[:1000])
            if e.get("link"):
                entries.append(e)
        elif name == "title" and not title:
            title = (el.text or "").strip()
    text = "\n".join(f"{e.get('title', '')}: {e.get('summary', '')}" for e in entries)
    return ParsedDocument(title=title, text=text, feed_entries=entries, links=[e["link"] for e in entries],
                          kind="feed")


def parse_json(text: str) -> ParsedDocument:
    data = json.loads(text)
    if isinstance(data, dict) and isinstance(data.get("items"), list) and "version" in data:  # JSON Feed
        entries = [{"title": i.get("title", ""), "link": i.get("url", ""),
                    "published": i.get("date_published"), "summary": (i.get("summary") or "")[:1000]}
                   for i in data["items"] if i.get("url")]
        return ParsedDocument(title=data.get("title", ""), text="\n".join(e["title"] for e in entries),
                              feed_entries=entries, links=[e["link"] for e in entries], kind="feed")
    return ParsedDocument(title="", text=json.dumps(data, indent=1)[:200_000], kind="json")


_FEED_SNIFF = re.compile(r"<(rss|feed|rdf:RDF)\b", re.I)


def parse(body: str, content_type: str, url: str) -> ParsedDocument:
    ct = content_type.split(";")[0].strip().lower()
    head = body[:2000]
    if ct in ("application/rss+xml", "application/atom+xml") or (
            ct in ("application/xml", "text/xml") and _FEED_SNIFF.search(head)):
        return parse_feed(body)
    if ct in ("application/json", "application/feed+json"):
        return parse_json(body)
    if ct in ("text/plain", "text/markdown"):
        lines = body.splitlines()
        return ParsedDocument(title=(lines[0].lstrip("# ").strip() if lines else "")[:300],
                              text=_clean(body), kind="text",
                              list_items=[ln.lstrip("-*0123456789. ").strip() for ln in lines
                                          if re.match(r"^\s*(?:[-*]|\d+[.)])\s+", ln)])
    if ct in ("application/xml", "text/xml"):
        root = SafeET.fromstring(body.encode())
        return ParsedDocument(title="", text=_clean(" ".join(t for t in root.itertext())), kind="xml")
    return parse_html(body, url)
