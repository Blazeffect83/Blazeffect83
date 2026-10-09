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

import logging

from defusedxml import ElementTree as SafeET

log = logging.getLogger(__name__)

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
    anchors: dict[str, str] = field(default_factory=dict)  # url -> anchor text (HTML only)
    list_items: list[str] = field(default_factory=list)
    headings: list[str] = field(default_factory=list)
    hidden_text: str = ""  # text the page tried to hide from humans
    feed_entries: list[dict] = field(default_factory=list)
    kind: str = "html"


# Boilerplate containers (comment threads, sidebars, share widgets, ads, cookie banners). A class/id
# token must *start* with the keyword ("comments-area" yes, "has-sidebar" no), so main content survives.
_BOILERPLATE = re.compile(
    r"^(comments?|comment-list|replies|respond|disqus|sidebar|related|share|sharing|social|newsletter|"
    r"subscribe|advert|ads|sponsor|cookie|consent|breadcrumbs?|promo|popup|modal|footer|navbar|menu|toc|"
    r"mw-navigation|navbox|reflist|references?|mw-references|mw-editsection|noprint|mw-jump-link|"
    r"hatnote|metadata|ambox|infobox-caption)([_-].*)?$", re.I)


def _is_boilerplate(attrs: dict) -> bool:
    if attrs.get("role") in ("navigation", "complementary", "banner", "contentinfo"):
        return True
    toks = " ".join(filter(None, (attrs.get("class"), attrs.get("id")))).split()
    return any(_BOILERPLATE.match(t) for t in toks)


_REFLIST = re.compile(r"^(references?|reflist|mw-references|citations?|bibliography)([_-].*)?$", re.I)


def _is_reference_list(attrs: dict) -> bool:
    toks = " ".join(filter(None, (attrs.get("class"), attrs.get("id")))).split()
    return any(_REFLIST.match(t) for t in toks)


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
        self.stack: list[tuple[str, bool, bool, bool]] = []  # (tag, suppress, hidden, reference-list)
        self.parts: list[str] = []
        self.hidden: list[str] = []
        self.title = ""
        self._in_title = False
        self.meta: dict[str, str] = {}
        self.links: list[str] = []
        self.anchors: dict[str, str] = {}
        self._cur_a: tuple[str, list[str]] | None = None
        self.list_items: list[str] = []
        self.headings: list[str] = []
        self._cur_li: list[str] | None = None
        self._cur_h: list[str] | None = None

    @property
    def suppressed(self) -> bool:
        return any(s for _, s, _h, _r in self.stack)

    @property
    def links_allowed(self) -> bool:
        """Links count unless suppressed by something other than a reference list
        (reference lists are noise as text but are where the primary sources are)."""
        return all(r or not s for _, s, _h, r in self.stack)

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key and a.get("content"):
                self.meta[key] = a["content"]
            return
        if tag == "a" and a.get("href") and self.links_allowed:
            href = urljoin(self.base, a["href"]).split("#", 1)[0]
            self.links.append(href)
            self._cur_a = (href, [])
        if tag == "title":
            self._in_title = True
        if tag in _VOID:
            if tag == "br":
                self.parts.append("\n")
            return
        hidden = _is_hidden(a)
        boiler = tag in ("div", "section", "aside", "ol", "ul", "table", "span", "p", "sup") and _is_boilerplate(a)
        refs = boiler and _is_reference_list(a)
        self.stack.append((tag, tag in _SKIP_TAGS or hidden or boiler, hidden, refs))
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "li" and not self.suppressed:
            self._cur_li = []
        if tag in ("h1", "h2", "h3") and not self.suppressed:
            self._cur_h = []

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag == "a" and self._cur_a is not None:
            href, parts = self._cur_a
            text = " ".join("".join(parts).split())[:200]
            if text and len(text) > len(self.anchors.get(href, "")):
                self.anchors[href] = text
            self._cur_a = None
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
        if self._cur_a is not None:
            self._cur_a[1].append(data)  # anchor text, even inside reference lists
        if self.suppressed:
            # Record text hidden via CSS/attributes (not scripts/styles) for injection screening.
            if any(h for _t, _s, h, _r in self.stack):
                self.hidden.append(data)
            return
        if not any(t == "pre" for t, _s, _h, _r in self.stack):
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
    except AssertionError:  # pragma: no cover - malformed markup HTMLParser gives up on; keep what was parsed
        log.warning("HTML parser stopped early on malformed markup at %s", base_url)
    meta = p.meta
    return ParsedDocument(
        title=_clean(unescape(meta.get("og:title") or p.title))[:500],
        text=_clean("".join(p.parts)),
        author=meta.get("author") or meta.get("article:author"),
        published_at=meta.get("article:published_time") or meta.get("date") or meta.get("dc.date")
        or meta.get("pubdate"),
        links=list(dict.fromkeys(p.links))[:500],
        anchors=p.anchors,
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
