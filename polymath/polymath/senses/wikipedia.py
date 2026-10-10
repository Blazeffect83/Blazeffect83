"""Wikipedia from the official multistream XML dumps (dumps.wikimedia.org).

The dump is pinned to a dated, finished run (``dumpstatus.json``) so offsets
never shift under a running job. Each bz2 stream (100 pages) is fetched by
HTTP range request, so no raw dump is stored on disk; the checkpoint is the
index of the next stream.
"""

from __future__ import annotations

import bz2
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote
from xml.etree import ElementTree as ET

from polymath.core.jobs import JobContext, JobOutcome, PermanentError
from polymath.memory.documents import Document, DocumentStore
from polymath.senses.net import HttpClient
from polymath.senses.openweb import record_citations
from polymath.senses.streams import local_name, multistream_ranges
from polymath.senses.wikitext import clean_wikitext, normalize_title, redirect_target

LICENSE = "CC BY-SA 4.0 (and GFDL)"
LICENSE_URL = "https://creativecommons.org/licenses/by-sa/4.0/"
DUMPS = "https://dumps.wikimedia.org"
_PAGE = re.compile(rb"<page>.*?</page>", re.S)
_DUMP_RE = re.compile(r"multistream(\d*)\.xml(-p\d+p\d+)?\.bz2$")
_INDEX_RE = re.compile(r"multistream-index(\d*)\.txt(-p\d+p\d+)?\.bz2$")
DISAMBIG_TEMPLATES = {
    "disambiguation",
    "disambig",
    "dab",
    "disamb",
    "hndis",
    "geodis",
    "set index article",
    "sia",
    "surname",
    "given name",
    "human name disambiguation",
    "place name disambiguation",
    "letter disambiguation",
    "number disambiguation",
    "school disambiguation",
    "species latin name disambiguation",
    "mathematical disambiguation",
    "call sign disambiguation",
    "molecular formula disambiguation",
    "road disambiguation",
    "ship index",
}
INFOBOX_PREFIX = ("infobox", "taxobox", "speciesbox", "automatic taxobox", "chembox", "drugbox")


@dataclass
class DumpPart:
    lang: str
    date: str
    dump_url: str
    index_url: str
    size: int
    name: str


def resolve_multistream(client: HttpClient, lang: str, *, base: str | None = None, tries: int = 4) -> list[DumpPart]:
    """Parts of the newest *finished* multistream articles dump for ``lang``."""
    base = base or DUMPS
    listing = client.get(f"{base}/{lang}wiki/").text()
    dates = sorted(set(re.findall(r'href="(\d{8})/"', listing)), reverse=True)
    for date in dates[:tries]:
        try:
            status = json.loads(client.get(f"{base}/{lang}wiki/{date}/dumpstatus.json").body)
        except Exception:
            continue
        job = status.get("jobs", {}).get("articlesmultistreamdump")
        if not job or job.get("status") != "done":
            continue
        dumps: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
        indexes: dict[tuple[str, str], str] = {}
        for name, info in job.get("files", {}).items():
            if m := _DUMP_RE.search(name):
                dumps[(m.group(1), m.group(2) or "")] = (name, info)
            elif m := _INDEX_RE.search(name):
                indexes[(m.group(1), m.group(2) or "")] = str(info["url"])
        parts = []
        for key in sorted(dumps, key=lambda k: (int(k[0] or 0), k[1])):
            if key not in indexes:
                continue
            name, info = dumps[key]
            parts.append(DumpPart(lang, date, base + str(info["url"]), base + indexes[key], int(info["size"]), name))
        if parts:
            return parts
    raise PermanentError(f"no finished multistream dump found for {lang}wiki")


def article_url(lang: str, title: str) -> str:
    return f"https://{lang}.wikipedia.org/wiki/{quote(title.replace(' ', '_'), safe='/:(),')}"


@dataclass
class Page:
    page_id: int
    ns: int
    title: str
    timestamp: str | None
    text: str
    redirect: str | None


def iter_pages(xml_fragment: bytes) -> Iterator[Page]:
    """Pages inside one decompressed multistream stream (header/footer tolerated)."""
    for m in _PAGE.finditer(xml_fragment):
        try:
            el = ET.fromstring(m.group(0))
        except ET.ParseError:
            continue
        fields: dict[str, str] = {}
        redirect = None
        for child in el.iter():
            name = local_name(child.tag)
            if name == "redirect":
                redirect = child.get("title")
            elif name in {"title", "ns", "id", "timestamp", "text"} and name not in fields:
                fields[name] = child.text or ""
        try:
            yield Page(
                page_id=int(fields.get("id", "0")),
                ns=int(fields.get("ns", "0")),
                title=fields.get("title", ""),
                timestamp=fields.get("timestamp"),
                text=fields.get("text", ""),
                redirect=redirect,
            )
        except ValueError:
            continue


def page_to_document(page: Page, lang: str) -> Document | None:
    if page.ns != 0 or page.redirect or not page.text:
        return None
    clean = clean_wikitext(page.text)
    if len(clean.text) < 200:
        return None
    names = {t.name.lower() for t in clean.templates}
    disambig = bool(names & DISAMBIG_TEMPLATES) or any(n.endswith(" disambiguation") for n in names)
    infoboxes = [
        {"name": t.name, "params": t.params} for t in clean.templates if t.name.lower().startswith(INFOBOX_PREFIX)
    ]
    short_desc = next(
        (t.positional[0] for t in clean.templates if t.name.lower() == "short description" and t.positional), None
    )
    return Document(
        source="wikipedia",
        external_id=f"{lang}:{page.page_id}",
        title=page.title,
        text=clean.text,
        license=LICENSE,
        license_url=LICENSE_URL,
        url=article_url(lang, page.title),
        lang=lang,
        published=page.timestamp,
        links=clean.links,
        meta={
            "kind": "article",
            "categories": clean.categories[:50],
            "disambiguation": disambig,
            "short_description": short_desc,
        },
        extra={"infoboxes": infoboxes[:3], "sections": clean.sections},
    )


_CATEGORY_LINK = re.compile(r"\[\[\s*Category\s*:\s*([^\]|]+)", re.I)


def store_category_edges(ctx: JobContext, lang: str, page: Page) -> int:
    """Category pages (namespace 14) carry the category hierarchy: child → parent edges."""
    child = normalize_title(page.title.split(":", 1)[-1])
    parents = {normalize_title(m.group(1)) for m in _CATEGORY_LINK.finditer(page.text)}
    ctx.db.executemany(
        "INSERT OR IGNORE INTO wiki_category_edges(lang, child, parent) VALUES(?,?,?)",
        [(lang, child, parent) for parent in parents if parent and parent != child],
    )
    return len(parents)


def store_redirect(ctx: JobContext, lang: str, page: Page) -> None:
    if page.ns == 0 and page.redirect:
        ctx.db.execute(
            "INSERT OR REPLACE INTO wiki_redirects(lang, title, target) VALUES(?,?,?)",
            (lang, normalize_title(page.title), normalize_title(page.redirect)),
        )
    elif page.ns == 0 and (target := redirect_target(page.text)):
        ctx.db.execute(
            "INSERT OR REPLACE INTO wiki_redirects(lang, title, target) VALUES(?,?,?)",
            (lang, normalize_title(page.title), target),
        )


def _ranges(
    ctx: JobContext, client: HttpClient, index_url: str, size: int, *, dump_url: str = "", lang: str = "en"
) -> list[tuple[int, int]]:
    cache: dict[str, list[tuple[int, int]]] = ctx.services.setdefault("wikipedia_ranges", {})
    if index_url not in cache:
        raw = client.get(index_url, max_bytes=2_000_000_000).body
        lines = bz2.decompress(raw).decode("utf-8", errors="replace").splitlines()
        cache.clear()  # keep only the active part's index in memory
        cache[index_url] = multistream_ranges(lines, size)
        if dump_url and not ctx.db.kv_get(f"wiki_index_loaded:{index_url}"):
            store_title_index(ctx, lang, dump_url, lines, cache[index_url])
            ctx.db.kv_set(f"wiki_index_loaded:{index_url}", True)
    return cache[index_url]


def store_title_index(
    ctx: JobContext, lang: str, dump_url: str, lines: list[str], ranges: list[tuple[int, int]]
) -> int:
    """Title → (stream start, stream end) so single articles can be fetched later (targeted reading)."""
    ends = dict(ranges)
    rows = []
    for line in lines:
        off, _, rest = line.partition(":")
        _pid, _, title = rest.partition(":")
        if off.isdigit() and title and int(off) in ends:
            rows.append((lang, normalize_title(title), dump_url, int(off), ends[int(off)]))
        if len(rows) >= 20000:
            ctx.db.executemany(
                "INSERT OR REPLACE INTO wiki_index(lang, title, dump_url, stream, next) VALUES(?,?,?,?,?)", rows
            )
            rows = []
            ctx.tick()
    ctx.db.executemany("INSERT OR REPLACE INTO wiki_index(lang, title, dump_url, stream, next) VALUES(?,?,?,?,?)", rows)
    return len(lines)


def fetch_titles(ctx: JobContext) -> JobOutcome:
    """Job ``wikipedia.titles``: read the dump streams that hold specific articles (curiosity-driven)."""
    p = ctx.job.payload
    lang = str(p.get("lang", "en"))
    client: HttpClient = ctx.services["http"]
    store: DocumentStore = ctx.services["docs"]
    streams: dict[tuple[str, int, int], list[str]] = {}
    missing = []
    for title in p.get("titles", [])[:200]:
        r = ctx.db.one(
            "SELECT dump_url, stream, next FROM wiki_index WHERE lang=? AND title=?", (lang, normalize_title(title))
        )
        if r is None:
            missing.append(title)
            continue
        streams.setdefault((str(r["dump_url"]), int(r["stream"]), int(r["next"])), []).append(title)
    stored = redirects = 0
    done_streams = set(ctx.job.checkpoint.get("streams", [])) if ctx.job.checkpoint else set()
    finished = True
    for (url, a, b), _titles in sorted(streams.items()):
        key = f"{a}"
        if key in done_streams:
            continue
        if ctx.should_stop() and done_streams:
            finished = False
            break
        frag = bz2.decompress(client.get_range(url, a, b - 1).body)
        for page in iter_pages(frag):
            if page.ns == 0 and page.redirect:
                store_redirect(ctx, lang, page)
                redirects += 1
                continue
            doc = page_to_document(page, lang)
            if doc is not None and (status := store.add(doc)[1]) in {"new", "updated"}:
                stored += 1
                if status == "new":
                    record_citations(ctx.db, page.text)
        done_streams.add(key)
        ctx.tick()
    result = {"streams": len(streams), "stored": stored, "redirects": redirects, "not_in_index": len(missing)}
    return JobOutcome(done=finished, checkpoint={"streams": sorted(done_streams)}, value=float(stored), result=result)


def ingest_part(ctx: JobContext) -> JobOutcome:
    """Job ``wikipedia.part``: payload {dump_url, index_url, size, lang, limit?}."""
    p = ctx.job.payload
    client: HttpClient = ctx.services["http"]
    store: DocumentStore = ctx.services["docs"]
    ranges = _ranges(ctx, client, p["index_url"], int(p["size"]), dump_url=p["dump_url"], lang=p.get("lang", "en"))
    cp = ctx.job.checkpoint or {"stream": 0, "articles": 0, "redirects": 0}
    i = int(cp["stream"])
    limit = p.get("limit")
    value = 0.0
    batch_bytes = int(p.get("batch_bytes", 1 << 20))
    first = True  # every slice makes progress, however small its budget
    while i < len(ranges) and (first or not ctx.should_stop()):
        first = False
        if limit is not None and cp["articles"] >= limit:
            break
        j = i
        while j < len(ranges) and ranges[j][1] - ranges[i][0] <= batch_bytes:
            j += 1
        j = max(j, i + 1)
        start, end = ranges[i][0], ranges[j - 1][1]
        blob = client.get_range(p["dump_url"], start, end - 1).body
        for k in range(i, j):
            a, b = ranges[k]
            for page in iter_pages(bz2.decompress(blob[a - start : b - start])):
                if page.ns == 14:
                    store_category_edges(ctx, p["lang"], page)
                    continue
                if page.redirect or (page.ns == 0 and page.text.lstrip()[:9].upper() == "#REDIRECT"):
                    store_redirect(ctx, p["lang"], page)
                    cp["redirects"] += 1
                    continue
                if limit is not None and cp["articles"] >= limit:
                    break
                doc = page_to_document(page, p["lang"])
                if doc is None:
                    continue
                _, status = store.add(doc)
                if status in {"new", "updated"}:
                    cp["articles"] += 1
                    value += 1.0
                if status == "new":
                    record_citations(ctx.db, page.text)  # sites it cites: candidates for vetted open-web learning
            cp["stream"] = k + 1
            ctx.tick()
            if ctx.should_stop() or (limit is not None and cp["articles"] >= limit):
                break
        i = int(cp["stream"])
    done = i >= len(ranges) or (limit is not None and cp["articles"] >= limit)
    return JobOutcome(done=done, checkpoint=cp, value=value, result=dict(cp))
