"""Project Gutenberg books and Stack Exchange Q&A dumps."""

from __future__ import annotations

import csv
import gzip
import io
import re
import time
from collections.abc import Iterator
from xml.etree import ElementTree as ET

from polymath.core.jobs import JobContext, JobOutcome, PermanentError
from polymath.memory.documents import Document, DocumentStore
from polymath.senses.crawler import Frontier, host_of
from polymath.senses.dumpfiles import raw_path, run_records
from polymath.senses.html_text import extract
from polymath.senses.net import FetchError, HttpClient
from polymath.senses.sevenzip import SevenZipFile
from polymath.senses.streams import iter_xml_elements

# ------------------------------------------------------------------ Gutenberg

GUTENBERG = "https://www.gutenberg.org"  # catalog and canonical book pages
# Project Gutenberg asks automated clients not to bulk-download from www.gutenberg.org; texts come
# from its official high-speed mirror instead (same cache/epub layout).
GUTENBERG_MIRROR = "https://gutenberg.pglaf.org"
GUTENBERG_LICENSE = "Public domain in the USA (Project Gutenberg License for the e-text)"
GUTENBERG_LICENSE_URL = "https://www.gutenberg.org/policy/license.html"
GUTENBERG_DELAY = 2.0  # Project Gutenberg asks robots to wait at least 2 s between requests
_START = re.compile(r"\*\*\*\s*START OF (?:THE|THIS) PROJECT GUTENBERG EBOOK[^*]*\*\*\*", re.I)
_END = re.compile(r"\*\*\*\s*END OF (?:THE|THIS) PROJECT GUTENBERG EBOOK", re.I)


def strip_gutenberg(text: str) -> str:
    text = text.replace("\r\n", "\n")
    m = _START.search(text)
    if m:
        text = text[m.end() :]
    m = _END.search(text)
    if m:
        text = text[: m.start()]
    # Re-flow hard-wrapped paragraphs.
    paras = [" ".join(p.split()) for p in re.split(r"\n\s*\n", text)]
    return "\n\n".join(p for p in paras if p)


def catalog_rows(data: bytes) -> list[dict[str, str]]:
    raw = gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8", errors="replace")))
    return [r for r in reader if r.get("Type") == "Text" and r.get("Language", "").split(";")[0].strip() == "en"]


def gutenberg_job(ctx: JobContext) -> JobOutcome:
    """Job ``gutenberg.books``: payload {catalog: rel path, limit}; fetches book texts politely."""
    p = ctx.job.payload
    client: HttpClient = ctx.services["http"]
    store: DocumentStore = ctx.services["docs"]
    frontier: Frontier = ctx.services["crawler"].frontier
    cache: dict[str, list[dict[str, str]]] = ctx.services.setdefault("gutenberg_catalog", {})
    rel = p["catalog"]
    if rel not in cache:
        cache[rel] = catalog_rows(raw_path(ctx, rel).read_bytes())
    rows = cache[rel]
    cp = ctx.job.checkpoint or {"position": 0, "stored": 0, "failed": 0}
    limit = int(p.get("limit", len(rows)))
    base = str(p.get("base", GUTENBERG_MIRROR))
    delay = float(p.get("delay", GUTENBERG_DELAY))
    host = host_of(base)
    ctx.db.execute("INSERT OR IGNORE INTO hosts(host) VALUES(?)", (host,))
    value = 0.0
    first = True
    retry_in = 0.0
    while cp["position"] < len(rows) and cp["stored"] < limit and (first or not ctx.should_stop()):
        if ctx.db.scalar("SELECT 1 FROM documents WHERE source='gutenberg' AND external_id=?",
                         (rows[cp["position"]]["Text#"],)):
            cp["position"] += 1  # already in memory: no request needed, and it counts towards the target
            cp["stored"] += 1
            continue
        if not frontier.take_token(host, delay):
            wait = float(ctx.db.scalar("SELECT next_fetch_at FROM hosts WHERE host=?", (host,))) - time.time()
            if wait > ctx.time_left():
                retry_in = wait
                break
            ctx.stop_event.wait(max(0.0, wait))
            continue
        first = False
        row = rows[cp["position"]]
        book = row["Text#"]
        url = f"{base}/cache/epub/{book}/pg{book}.txt"
        try:
            resp = client.get(url, max_bytes=30_000_000)
        except FetchError as exc:
            if exc.status in (404, 403, 410):
                cp["failed"] += 1
                cp["position"] += 1
                continue
            # Transient trouble (reset, 5xx, 429): keep this slice's progress, rest the host, retry later.
            frontier.defer_host(host, 300.0)
            retry_in = 300.0
            cp["transient_errors"] = int(cp.get("transient_errors", 0)) + 1
            break
        text = strip_gutenberg(resp.text())
        cp["position"] += 1
        if len(text) < 2000:
            continue
        _, status = store.add(
            Document(
                source="gutenberg",
                external_id=book,
                title=row.get("Title", "")[:500],
                text=text,
                license=GUTENBERG_LICENSE,
                license_url=GUTENBERG_LICENSE_URL,
                url=f"{GUTENBERG}/ebooks/{book}",
                lang="en",
                published=row.get("Issued"),
                meta={
                    "kind": "book",
                    "authors": [a.strip() for a in row.get("Authors", "").split(";") if a.strip()],
                    "subjects": [s.strip() for s in row.get("Subjects", "").split(";") if s.strip()][:10],
                    "bookshelves": [s.strip() for s in row.get("Bookshelves", "").split(";") if s.strip()][:10],
                },
            )
        )
        if status in {"new", "updated"}:
            cp["stored"] += 1
            value += 3.0
        ctx.tick()
    done = cp["position"] >= len(rows) or cp["stored"] >= limit
    return JobOutcome(done=done, checkpoint=cp, value=value, delay=retry_in, result=dict(cp))


# -------------------------------------------------------------- Stack Exchange

SE_ARCHIVE = "https://archive.org/download/stackexchange"
SE_LICENSES = {
    "CC BY-SA 2.5": "https://creativecommons.org/licenses/by-sa/2.5/",
    "CC BY-SA 3.0": "https://creativecommons.org/licenses/by-sa/3.0/",
    "CC BY-SA 4.0": "https://creativecommons.org/licenses/by-sa/4.0/",
}


def se_archive_url(site: str) -> tuple[str, str]:
    """Archive URL and the canonical site host (``ai`` → ai.stackexchange.com)."""
    host = site if "." in site else f"{site}.stackexchange.com"
    return f"{SE_ARCHIVE}/{host}.7z", host


def se_document(row: ET.Element, host: str, titles: dict[str, str]) -> Document | None:
    a = row.attrib
    ptype = a.get("PostTypeId")
    if ptype not in {"1", "2"}:
        return None
    try:
        score = int(a.get("Score", "0"))
    except ValueError:
        score = 0
    if score < 0:
        return None
    body = extract(a.get("Body", ""), f"https://{host}/").text
    pid = a.get("Id", "")
    if ptype == "1":
        title = a.get("Title", "")
        titles[pid] = title
        text = f"{title}\n\n{body}"
        url = f"https://{host}/q/{pid}"
        kind = "question"
    else:
        parent = a.get("ParentId", "")
        title = titles.get(parent, "")
        text = body
        url = f"https://{host}/a/{pid}"
        kind = "answer"
    if len(text) < 120:
        return None
    lic = a.get("ContentLicense", "CC BY-SA 4.0")
    tags = [t for t in re.split(r"[<>|]", a.get("Tags", "")) if t]
    return Document(
        source="stackexchange",
        external_id=f"{host}:{pid}",
        title=title[:500],
        text=text,
        license=lic,
        license_url=SE_LICENSES.get(lic),
        url=url,
        lang="en",
        published=a.get("CreationDate"),
        meta={
            "kind": kind,
            "site": host,
            "score": score,
            "tags": tags,
            "parent": a.get("ParentId"),
            "accepted": a.get("AcceptedAnswerId"),
        },
    )


def ingest_stackexchange(ctx: JobContext) -> JobOutcome:
    """Job ``stackexchange.ingest``: payload {dest, host, limit?}; streams Posts.xml from the 7z."""
    rel = ctx.job.payload["dest"]
    host = ctx.job.payload["host"]
    path = raw_path(ctx, rel)
    if not path.exists():
        raise PermanentError(f"archive missing: {rel}")
    store: DocumentStore = ctx.services["docs"]
    titles: dict[str, str] = ctx.services.setdefault("se_titles", {}).setdefault(host, {})

    def make() -> Iterator[ET.Element]:
        return iter_xml_elements(SevenZipFile(path).open("Posts.xml"), {"row"})

    def handle(row: ET.Element) -> float:
        parent = row.get("ParentId")
        if parent and parent not in titles:  # after a restart: recover the question title from memory
            titles[parent] = str(
                ctx.db.scalar(
                    "SELECT title FROM documents WHERE source='stackexchange' AND external_id=?",
                    (f"{host}:{parent}",),
                    default="",
                )
            )
        doc = se_document(row, host, titles)
        if doc is None:
            return 0.0
        _, status = store.add(doc)
        return 1.0 if status in {"new", "updated"} else 0.0

    out = run_records(ctx, make, handle, rel=rel)
    if out.done:
        ctx.services["se_titles"].pop(host, None)
    return out
