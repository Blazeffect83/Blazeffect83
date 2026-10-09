"""Wikidata property bootstrap: labels, aliases, datatypes and rule metadata for every property.

The entity JSON dump is read in page order, so property records (P31 "instance of",
P1696 "inverse property", …) arrive late and scattered over a 100+ GB file. Until
then predicates are bare ids: answers cannot name relations, questions such as
"capital city of X" cannot be grounded, and reasoning lacks the declared
inverse / transitive / symmetric metadata it trusts most.

Wikidata's *XML* multistream dump is split into ~100 parts, each with a small
index (``offset:page_id:title`` per page, ≈5 MB compressed). Property pages are
titled ``Property:P…`` and their page text *is* the entity JSON. So:

1. ``wikidata.propindex`` scans every index part once per dump (≈0.5 GB in total,
   one part per slice, resumable) and records the bz2 stream holding each property
   page in ``wd_property_pages``;
2. ``wikidata.properties`` range-reads only those streams — properties already used
   by known facts first — and stores the property entities exactly like dump
   records, so ``memory.graph`` turns them into predicate labels, aliases and
   metadata triples.
"""

from __future__ import annotations

import bz2
import time
from typing import Any

from polymath.core.jobs import JobContext, JobOutcome
from polymath.senses.net import FetchError, HttpClient
from polymath.senses.wikidata import parse_entity, store_entity
from polymath.senses.wikipedia import iter_pages, resolve_multistream

PROPERTY_NS = 120
PREFIX = "Property:"
REFRESH_AFTER = 90 * 86400.0  # property records change rarely; re-read them quarterly
STREAMS_PER_SLICE = 200
RETRY_DELAY = 300.0


def property_streams(index_bz2: bytes, file_size: int) -> list[tuple[str, int, int]]:
    """``(pid, stream_start, stream_end)`` for every property page in one multistream index part.

    Index lines are sorted by stream offset; a stream ends where the next stream starts
    (the last one at ``file_size``). Decompression is incremental, so a part never sits
    fully expanded in memory.
    """
    out: list[tuple[str, int, int]] = []
    pending: list[tuple[str, int]] = []  # properties whose stream end is not known yet
    prefix = PREFIX.encode()

    def line(raw: bytes) -> None:
        nonlocal pending
        off_s, _, rest = raw.partition(b":")
        if not off_s.isdigit():
            return
        off = int(off_s)
        if pending and off > pending[0][1]:
            out.extend((pid, start, off) for pid, start in pending)
            pending = []
        title = rest.partition(b":")[2]
        if title.startswith(prefix):
            pid = title[len(prefix) :].decode("ascii", "replace")
            if pid[:1] == "P" and pid[1:].isdigit():
                pending.append((pid, off))

    dec = bz2.BZ2Decompressor()
    data, carry = index_bz2, b""
    while True:
        lines = (carry + dec.decompress(data, max_length=4 << 20)).split(b"\n")
        data, carry = b"", lines.pop()
        for raw in lines:
            line(raw)
        if dec.eof:
            if not dec.unused_data.startswith(b"BZh"):
                break
            data, dec = dec.unused_data, bz2.BZ2Decompressor()  # concatenated bz2 streams
        elif dec.needs_input:
            break
    line(carry)
    out.extend((pid, start, file_size) for pid, start in pending)
    return out


def propindex_job(ctx: JobContext) -> JobOutcome:
    """Job ``wikidata.propindex``: find the stream of every ``Property:`` page (one index part per step)."""
    client: HttpClient = ctx.services["http"]
    cp = dict(ctx.job.checkpoint or {})
    if "parts" not in cp:
        parts = resolve_multistream(client, "wikidata", base=ctx.job.payload.get("base"))
        cp = {
            "date": parts[0].date if parts else "",
            "parts": [[p.dump_url, p.index_url, p.size] for p in parts],
            "i": 0,
            "found": 0,
        }
    first = True
    while cp["i"] < len(cp["parts"]) and (first or not ctx.should_stop()):
        first = False
        dump_url, index_url, size = cp["parts"][cp["i"]]
        raw = client.get(index_url, max_bytes=200_000_000).body
        rows = [(pid, dump_url, a, b) for pid, a, b in property_streams(raw, int(size))]
        ctx.db.executemany(
            "INSERT INTO wd_property_pages(pid, dump_url, stream, next) VALUES(?,?,?,?) ON CONFLICT(pid) DO UPDATE "
            "SET dump_url=excluded.dump_url, stream=excluded.stream, next=excluded.next",
            rows,
        )
        cp["found"] += len(rows)
        cp["i"] += 1
        ctx.tick()
    done = cp["i"] >= len(cp["parts"])
    if done:
        ctx.db.kv_set("wd_propindex", {"date": cp["date"], "found": cp["found"], "at": time.time()})
        ctx.scheduler.ensure_recurring("wikidata.properties", 900, priority=2.1)  # read them right away
    return JobOutcome(
        done=done,
        checkpoint=cp,
        value=0.002 * cp["found"],
        result={"parts_done": cp["i"], "parts": len(cp["parts"]), "properties": cp["found"]},
    )


def due_streams(db: Any, limit: int) -> list[Any]:
    """Streams holding properties not read (recently), those used by known facts first."""
    return list(
        db.query(
            "SELECT dump_url, stream, next, MAX(EXISTS(SELECT 1 FROM predicates p WHERE p.key = w.pid)) AS used "
            "FROM wd_property_pages w WHERE fetched IS NULL OR fetched < ? GROUP BY dump_url, stream, next "
            "ORDER BY used DESC, stream LIMIT ?",
            (time.time() - REFRESH_AFTER, limit),
        )
    )


def properties_job(ctx: JobContext) -> JobOutcome:
    """Job ``wikidata.properties``: read the indexed property pages (bounded slices, resumable)."""
    client: HttpClient = ctx.services["http"]
    stored = streams = 0
    first = True
    for row in due_streams(ctx.db, STREAMS_PER_SLICE):
        if not first and ctx.should_stop():
            break
        first = False
        try:
            body = client.get_range(str(row["dump_url"]), int(row["stream"]), int(row["next"]) - 1).body
        except FetchError as exc:
            if exc.transient:  # keep this slice's progress; try again later
                return JobOutcome(
                    done=False,
                    delay=RETRY_DELAY,
                    value=0.05 * stored,
                    result={"streams": streams, "stored": stored, "more": True, "error": str(exc)[:200]},
                )
            raise
        frag = bz2.decompress(body)
        for page in iter_pages(frag):
            if page.ns != PROPERTY_NS:
                continue
            ent = parse_entity(page.text.encode())
            if ent is not None and ent["type"] == "property" and store_entity(ctx, ent):
                stored += 1
        ctx.db.execute(
            "UPDATE wd_property_pages SET fetched=? WHERE dump_url=? AND stream=?",
            (time.time(), row["dump_url"], row["stream"]),
        )
        streams += 1
        ctx.tick()
    left = len(due_streams(ctx.db, 1))
    return JobOutcome(
        done=left == 0, value=0.05 * stored, result={"streams": streams, "stored": stored, "more": bool(left)}
    )


def planner(agent: Any) -> None:
    """Network-free: index property pages once per month, then read them until none is due."""
    s = agent.scheduler
    state = agent.db.kv_get("wd_propindex") or {}
    if time.time() - float(state.get("at", 0)) > 30 * 86400:
        s.enqueue("wikidata.propindex", {}, key=f"wd-propindex:{int(time.time() // (30 * 86400))}", priority=2.2)
    if agent.db.scalar("SELECT 1 FROM wd_property_pages WHERE fetched IS NULL LIMIT 1"):
        s.ensure_recurring("wikidata.properties", 900, priority=2.1)
