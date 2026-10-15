"""Wikidata from the JSON entity dump (``wikidata-YYYYMMDD-all.json.bz2``).

The 100+ GB dump is a single bz2 stream. :class:`~polymath.senses.streams.Bz2BlockReader`
reads it block by block over HTTP range requests; the checkpoint is a
(bit offset, byte offset) pair, so the agent can sip the dump for months and
resume instantly after any interruption.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from polymath.core.jobs import JobContext, JobOutcome, PermanentError
from polymath.senses.net import HttpClient
from polymath.senses.streams import Bz2BlockReader, LineCursor, iter_block_lines

LICENSE = "CC0 1.0"
LICENSE_URL = "https://creativecommons.org/publicdomain/zero/1.0/"
ENTITIES = "https://dumps.wikimedia.org/wikidatawiki/entities"
SKIP_DATATYPES = {
    "external-id",
    "commonsMedia",
    "url",
    "geo-shape",
    "tabular-data",
    "math",
    "musical-notation",
    "wikibase-lexeme",
    "wikibase-form",
    "wikibase-sense",
    "entity-schema",
}
MAX_PROPS = 80
MAX_VALUES = 25
TIME_QUALIFIERS = {"P580": "from", "P582": "to", "P585": "at"}  # start time, end time, point in time
SITELINK_LANGS = ("es", "de", "fr", "it", "pt")  # titles kept for reading a second Wikipedia language


def resolve_dump(client: HttpClient, *, base: str | None = None, tries: int = 8) -> tuple[str, int]:
    """URL and size of the newest dated ``-all.json.bz2`` dump."""
    base = base or ENTITIES
    listing = client.get(base + "/").text()
    for date in sorted(set(re.findall(r'href="(\d{8})/"', listing)), reverse=True)[:tries]:
        page = client.get(f"{base}/{date}/").text()
        name = f"wikidata-{date}-all.json.bz2"
        if name in page:
            url = f"{base}/{date}/{name}"
            with client.stream(url, method="HEAD", decode=False) as resp:
                size = int(resp.headers.get("content-length", "0"))
            if size:
                return url, size
    raise PermanentError("no dated Wikidata JSON dump found")


def _time_value(v: dict[str, Any]) -> str | None:
    t = str(v.get("time", ""))
    m = re.match(r"([+-])0*(\d+)-(\d\d)-(\d\d)", t)
    if not m:
        return None
    sign, year, month, day = m.groups()
    year_s = ("-" if sign == "-" else "") + year
    precision = int(v.get("precision", 11))
    if precision >= 11:
        return f"{year_s}-{month}-{day}"
    if precision == 10:
        return f"{year_s}-{month}"
    return year_s


def convert_value(snak: dict[str, Any]) -> Any:
    if snak.get("snaktype") != "value" or snak.get("datatype") in SKIP_DATATYPES:
        return None
    dv = snak.get("datavalue") or {}
    v = dv.get("value")
    kind = dv.get("type")
    if kind == "wikibase-entityid" and isinstance(v, dict):
        return v.get("id")
    if kind == "time" and isinstance(v, dict):
        t = _time_value(v)
        return {"time": t} if t else None
    if kind == "quantity" and isinstance(v, dict):
        try:
            amount = float(v["amount"])
        except (KeyError, ValueError):
            return None
        unit = str(v.get("unit", "1")).rsplit("/", 1)[-1]
        return {"amount": amount, "unit": unit if unit != "1" else None}
    if kind == "monolingualtext" and isinstance(v, dict):
        return {"text": v.get("text")} if v.get("language") == "en" else None
    if kind == "globecoordinate" and isinstance(v, dict):
        return {"lat": v.get("latitude"), "lon": v.get("longitude")}
    if kind == "string" and isinstance(v, str):
        return {"text": v}
    return None


def _qualifier_time(statement: dict[str, Any], pid: str) -> str | None:
    """A statement's time qualifier (start time, end time, point in time) as YYYY[-MM[-DD]]."""
    for snak in (statement.get("qualifiers") or {}).get(pid) or []:
        v = convert_value(snak)
        if isinstance(v, dict) and v.get("time"):
            return str(v["time"])
    return None


def parse_entity(line: bytes) -> dict[str, Any] | None:
    line = line.strip()
    if line.endswith(b","):
        line = line[:-1]
    if not line.startswith(b"{"):
        return None
    try:
        e = json.loads(line)
    except ValueError:
        return None
    if e.get("type") not in {"item", "property"}:
        return None
    label = ((e.get("labels") or {}).get("en") or {}).get("value")
    if not label:
        return None
    desc = ((e.get("descriptions") or {}).get("en") or {}).get("value")
    aliases = [a.get("value") for a in (e.get("aliases") or {}).get("en", []) if a.get("value")][:30]
    claims: dict[str, Any] = {}
    when: dict[str, list[list[Any]]] = {}  # pid → [[value index, from, to, at], …]
    for pid, statements in (e.get("claims") or {}).items():
        if len(claims) >= MAX_PROPS:
            break
        ranked = [s for s in statements if s.get("rank") != "deprecated"]
        preferred = [s for s in ranked if s.get("rank") == "preferred"]
        # the preferred (current) values, plus earlier values that say when they ended: Bonn, capital until 1990
        chosen = [s for s in ranked if s in preferred or _qualifier_time(s, "P582")] if preferred else ranked
        values: list[Any] = []
        for st in chosen:
            val = convert_value(st.get("mainsnak") or {})
            if val is None or val in values:
                continue
            times = [_qualifier_time(st, q) for q in TIME_QUALIFIERS]
            if any(times):
                when.setdefault(pid, []).append([len(values), *times])
            values.append(val)
            if len(values) >= MAX_VALUES:
                break
        if values:
            claims[pid] = values
    if when:
        claims["_when"] = when
    links = e.get("sitelinks") or {}
    sitelinks = {
        lang: links[f"{lang}wiki"]["title"] for lang in SITELINK_LANGS if (links.get(f"{lang}wiki") or {}).get("title")
    }
    if sitelinks:
        claims["_sitelinks"] = sitelinks
    enwiki = ((e.get("sitelinks") or {}).get("enwiki") or {}).get("title")
    return {
        "qid": e["id"],
        "label": label,
        "description": desc,
        "aliases": aliases,
        "enwiki": enwiki,
        "claims": claims,
        "type": e["type"],
        "datatype": e.get("datatype"),
    }


def store_entity(ctx: JobContext, ent: dict[str, Any]) -> bool:
    claims = ent["claims"]
    if ent["type"] == "property":
        claims = {"_datatype": [ent.get("datatype")], **claims}
    cur = ctx.db.execute(
        "INSERT INTO wd_entities(qid, label, description, aliases, enwiki, claims, state, fetched) "
        "VALUES(?,?,?,?,?,?, 'new', ?) ON CONFLICT(qid) DO UPDATE SET label=excluded.label, "
        "description=excluded.description, aliases=excluded.aliases, enwiki=excluded.enwiki, "
        "claims=excluded.claims, state='new', fetched=excluded.fetched "
        "WHERE wd_entities.claims != excluded.claims OR wd_entities.label != excluded.label",
        (
            ent["qid"],
            ent["label"],
            ent["description"],
            json.dumps(ent["aliases"], ensure_ascii=False),
            ent["enwiki"],
            json.dumps(claims, ensure_ascii=False, separators=(",", ":")),
            time.time(),
        ),
    )
    for lang, title in (claims.get("_sitelinks") or {}).items():
        ctx.db.execute(
            "INSERT OR REPLACE INTO wiki_sitelinks(lang, title, qid) VALUES(?,?,?)", (lang, title, ent["qid"])
        )
    return bool(cur.rowcount)


def ingest_dump(ctx: JobContext) -> JobOutcome:
    """Job ``wikidata.dump``: payload {url, size, max_bytes?, limit?}."""
    p = ctx.job.payload
    client: HttpClient = ctx.services["http"]
    size = min(int(p["size"]), int(p.get("max_bytes") or p["size"]))
    url = p["url"]

    def fetch(start: int, end: int) -> bytes:
        return client.get_range(url, start, min(end, size) - 1).body if end > start else b""

    cp = ctx.job.checkpoint or {"block_bit": 32, "offset": 0, "entities": 0, "lines": 0}
    reader = Bz2BlockReader(fetch, size, window=int(p.get("window", 8 << 20)), level=9)
    limit = p.get("limit")
    value = 0.0
    finished = True
    for line, cursor in iter_block_lines(reader, LineCursor(int(cp["block_bit"]), int(cp["offset"]))):
        ent = parse_entity(line)
        cp["lines"] += 1
        if ent is not None and store_entity(ctx, ent):
            cp["entities"] += 1
            value += 0.5
        cp["block_bit"], cp["offset"] = cursor.block_bit, cursor.offset
        if limit is not None and cp["entities"] >= limit:
            break
        if ctx.should_stop():
            finished = False
            break
        if cp["lines"] % 200 == 0:
            ctx.tick()
    done = finished or (limit is not None and cp["entities"] >= limit)
    return JobOutcome(done=done, checkpoint=cp, value=value, result=dict(cp))
