"""Own HTML → main-text extractor (built on the stdlib ``html.parser`` tokenizer).

Pipeline:

1. Tokenise into *blocks* (paragraph-level elements), dropping non-content
   subtrees (script/style/nav/footer/forms/...) and elements whose class/id
   names mark boilerplate (menu, cookie, share, comment, ...).
2. Classify each block jusText-style from its length, link density and
   stop-word density: good / near-good / short / bad.
3. Context pass: short and near-good blocks inherit the class of their nearest
   good/bad neighbours, so headings and short paragraphs inside the article
   survive while short menu items do not.

Also returns title, meta description, language, canonical URL, license link,
robots directives and every outgoing link (absolute URL + anchor text).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin

SKIP_TAGS = {
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "math",
    "iframe",
    "object",
    "embed",
    "canvas",
    "form",
    "button",
    "select",
    "textarea",
    "nav",
    "footer",
    "aside",
    "head",
    "video",
    "audio",
    "map",
    "dialog",
}
BLOCK_TAGS = {
    "p",
    "div",
    "section",
    "article",
    "main",
    "li",
    "ul",
    "ol",
    "dl",
    "dt",
    "dd",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "pre",
    "blockquote",
    "td",
    "th",
    "tr",
    "table",
    "caption",
    "figcaption",
    "header",
    "address",
    "br",
    "hr",
    "summary",
    "details",
    "body",
    "html",
    "center",
}
HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
BOILERPLATE_HINT = re.compile(
    r"(?:^|[\s_-])(?:nav|navbar|menu|footer|sidebar|side-bar|breadcrumbs?|cookie|consent|banner|advert|ads?|"
    r"share|sharing|social|comments?|related|promo|newsletter|subscribe|popup|modal|toolbar|skip|pagination|"
    r"masthead|site-header|login|signup|widget)(?:$|[\s_-])",
    re.I,
)
STOPWORDS = frozenset(
    [
        "a",
        "about",
        "above",
        "after",
        "again",
        "against",
        "all",
        "am",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "because",
        "been",
        "before",
        "being",
        "below",
        "between",
        "both",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "doing",
        "down",
        "during",
        "each",
        "few",
        "for",
        "from",
        "further",
        "had",
        "has",
        "have",
        "having",
        "he",
        "her",
        "here",
        "hers",
        "him",
        "his",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "itself",
        "just",
        "me",
        "more",
        "most",
        "my",
        "no",
        "nor",
        "not",
        "now",
        "of",
        "off",
        "on",
        "once",
        "only",
        "or",
        "other",
        "our",
        "out",
        "over",
        "own",
        "same",
        "she",
        "should",
        "so",
        "some",
        "such",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "to",
        "too",
        "under",
        "until",
        "up",
        "very",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "whom",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
    ]
)
_WS = re.compile(r"\s+")
_WORD = re.compile(r"[^\W\d_]+", re.U)


@dataclass
class Block:
    text: str
    tag: str
    link_chars: int = 0
    cls: str = ""

    @property
    def link_density(self) -> float:
        return self.link_chars / max(1, len(self.text))

    def stopword_density(self) -> float:
        words = _WORD.findall(self.text.lower())
        return sum(w in STOPWORDS for w in words) / max(1, len(words))


@dataclass
class Link:
    url: str
    text: str
    nofollow: bool = False


@dataclass
class Extracted:
    title: str = ""
    text: str = ""
    description: str = ""
    lang: str | None = None
    canonical: str | None = None
    license_url: str | None = None
    noindex: bool = False
    nofollow: bool = False
    links: list[Link] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)


class _Tokenizer(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base_url
        self.out = Extracted()
        self.skip_depth = 0
        self.stack: list[tuple[str, bool]] = []  # (tag, opened a skip region)
        self.cur: list[str] = []
        self.cur_links = 0
        self.cur_tag = "p"
        self.in_title = False
        self.title_parts: list[str] = []
        self.link_href: str | None = None
        self.link_nofollow = False
        self.link_text: list[str] = []

    # helpers -----------------------------------------------------------------
    def _flush(self) -> None:
        text = _WS.sub(" ", "".join(self.cur)).strip()
        if text:
            self.out.blocks.append(Block(text, self.cur_tag, self.cur_links))
        self.cur = []
        self.cur_links = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "html" and a.get("lang"):
            self.out.lang = a["lang"].split("-")[0].lower()
        if tag == "base" and a.get("href"):
            self.base = urljoin(self.base, a["href"])
        if tag == "meta":
            self._meta(a)
        if tag == "link":
            rel = a.get("rel", "").lower().split()
            if "canonical" in rel and a.get("href"):
                self.out.canonical = urljoin(self.base, a["href"])
            if "license" in rel and a.get("href"):
                self.out.license_url = urljoin(self.base, a["href"])
        if tag == "title":
            self.in_title = True
        if tag in VOID_TAGS:
            if tag in {"br", "hr"} and not self.skip_depth:
                self._flush()
            return
        hint = f"{a.get('class', '')} {a.get('id', '')} {a.get('role', '')}"
        skipping = (
            tag in SKIP_TAGS
            or bool(BOILERPLATE_HINT.search(hint))
            or "hidden" in a
            or ("display:none" in a.get("style", "").replace(" ", ""))
            or a.get("aria-hidden") == "true"
        )
        if tag == "header" and not self.skip_depth:
            skipping = skipping or not self.out.blocks  # page header (before any content)
        self.stack.append((tag, skipping))
        if skipping:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag in BLOCK_TAGS:
            self._flush()
            self.cur_tag = tag
        if tag == "a":
            href = a.get("href", "")
            if href and not href.startswith(("javascript:", "mailto:", "tel:", "#")):
                self.link_href = urldefrag(urljoin(self.base, href))[0]
                self.link_nofollow = "nofollow" in a.get("rel", "").lower()
                self.link_text = []
        if tag == "img" and a.get("alt"):
            pass  # alt text is not article prose

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        if tag in VOID_TAGS:
            return
        # pop to the matching tag (tolerates unclosed children)
        for idx in range(len(self.stack) - 1, -1, -1):
            if self.stack[idx][0] == tag:
                for _t, skipping in self.stack[idx:]:
                    if skipping:
                        self.skip_depth -= 1
                del self.stack[idx:]
                break
        else:
            return
        if self.skip_depth:
            return
        if tag == "a" and self.link_href:
            text = _WS.sub(" ", "".join(self.link_text)).strip()
            self.out.links.append(Link(self.link_href, text, self.link_nofollow))
            self.link_href = None
        if tag in BLOCK_TAGS:
            self._flush()
            self.cur_tag = "p"

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)
            return
        if self.skip_depth:
            return
        self.cur.append(data)
        if self.link_href is not None:
            self.link_text.append(data)
            self.cur_links += len(data.strip())

    def _meta(self, a: dict[str, str]) -> None:
        name = (a.get("name") or a.get("property") or "").lower()
        content = a.get("content", "")
        if name in {"description", "og:description"} and not self.out.description:
            self.out.description = _WS.sub(" ", content).strip()
        if name in {"robots", "polymathbot"}:
            directives = {d.strip() for d in content.lower().split(",")}
            self.out.noindex |= bool(directives & {"noindex", "none"})
            self.out.nofollow |= bool(directives & {"nofollow", "none"})
        if name in {"dc.rights", "dcterms.license", "license"} and content.startswith("http"):
            self.out.license_url = content
        if name == "og:title" and not self.title_parts:
            self.title_parts.append(content)

    def close(self) -> None:
        super().close()
        self._flush()


def classify(blocks: list[Block]) -> list[str]:
    """jusText-style classification of blocks into good/bad (after the context pass)."""
    first: list[str] = []
    for b in blocks:
        n = len(b.text)
        sw = b.stopword_density()
        if b.link_density > 0.4:
            c = "bad"
        elif b.tag in HEADINGS:
            c = "heading"
        elif n < 50:
            c = "short"
        elif (n >= 150 and sw >= 0.25) or (n >= 400 and sw >= 0.15):
            c = "good"
        elif sw >= 0.20 or n >= 250:
            c = "near"
        else:
            c = "bad"
        first.append(c)

    def neighbour(i: int, step: int) -> str:
        j = i + step
        while 0 <= j < len(first):
            if first[j] in {"good", "bad"}:
                return first[j]
            if first[j] == "near":
                return "near"
            j += step
        return "bad"

    final = list(first)
    for i, c in enumerate(first):
        if c in {"short", "heading"}:
            prev, nxt = neighbour(i, -1), neighbour(i, 1)
            if c == "heading":
                final[i] = "good" if "good" in {prev, nxt} or nxt == "near" else "bad"
            else:
                final[i] = "good" if prev == "good" and nxt == "good" else "bad"
        elif c == "near":
            prev, nxt = neighbour(i, -1), neighbour(i, 1)
            final[i] = "good" if "good" in {prev, nxt} else "bad"
    # A page with no "good" block at all keeps its best near/short paragraphs (short docs, abstracts).
    if "good" not in final and blocks:
        best = max(range(len(blocks)), key=lambda k: len(blocks[k].text) * (1 - blocks[k].link_density))
        if len(blocks[best].text) >= 80:
            final[best] = "good"
    return final


def extract(html_text: str, base_url: str = "") -> Extracted:
    tok = _Tokenizer(base_url)
    try:
        tok.feed(html_text)
        tok.close()
    except (AssertionError, ValueError):  # malformed markup: keep what was parsed
        tok._flush()
    out = tok.out
    out.title = _WS.sub(" ", "".join(tok.title_parts)).strip()
    labels = classify(out.blocks)
    kept = [b for b, lab in zip(out.blocks, labels) if lab == "good"]
    paragraphs: list[str] = []
    for b in kept:
        if b.tag in HEADINGS or (paragraphs and b.tag == "li"):
            paragraphs.append(b.text)
        else:
            paragraphs.append(b.text)
    out.text = "\n\n".join(paragraphs)
    return out


def detect_license(ex: Extracted, html_text: str) -> tuple[str, str | None]:
    """A license string for crawled pages: CC licenses when declared, else 'unknown'."""
    url = ex.license_url or ""
    m = re.search(r"creativecommons\.org/(licenses|publicdomain)/([a-z\-]+)/?([\d.]+)?", url + " " + html_text[-20000:])
    if m:
        kind, code, version = m.groups()
        if kind == "publicdomain":
            return ("CC0 1.0" if code == "zero" else "Public Domain Mark"), m.group(0)
        return f"CC {code.upper()} {version or ''}".strip(), "https://" + m.group(0)
    return "unknown (all rights reserved assumed; robots.txt permitted; stored for private learning)", ex.canonical
