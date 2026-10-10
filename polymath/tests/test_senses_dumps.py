"""Phase 2 (dump side): wikitext, Wikipedia, Wikidata, OpenAlex, PubMed, Gutenberg, Stack Exchange, planning.

Ends with the offline integration test: every source served from fixtures by a
local HTTP server and collected through the real agent loop.
"""

from __future__ import annotations

import bz2
import gzip
import itertools
import json
import os
import threading
import time
from pathlib import Path

import pytest

from polymath.body.systemd_notify import Notifier
from polymath.core.app import build_components
from polymath.core.jobs import JobContext, PermanentError
from polymath.core.loop import Agent
from polymath.core.scheduler import Job, Scheduler
from polymath.memory.documents import DocumentStore
from polymath.senses import books_qa, scholarly, sources, wikidata, wikipedia
from polymath.senses.dumpfiles import download_job, raw_path, resume_records
from polymath.senses.net import FetchError
from polymath.senses.sevenzip import SevenZipError, SevenZipFile
from polymath.senses.streams import (
    Bz2BlockReader,
    LineCursor,
    decode_block,
    file_fetcher,
    iter_block_lines,
    iter_gzip_chunks,
    iter_lines,
    iter_xml_elements,
    multistream_ranges,
)
from polymath.senses.wikitext import clean_wikitext, normalize_title, parse_template, redirect_target
from tests.fixtures import builders as B
from tests.fixtures.typing import some
from tests.webserver import FakeWeb, client


@pytest.fixture()
def web():
    w = FakeWeb()
    yield w
    w.close()


def ctx_for(config, db, kind, payload, services, *, checkpoint=None, budget=20.0, key=None):
    s = Scheduler(db)
    key = key or f"{kind}:{time.time_ns()}"
    jid, _ = s.enqueue(kind, payload, key=key)
    job = Job(
        id=jid,
        kind=kind,
        key=key,
        payload=payload,
        checkpoint=checkpoint,
        priority=0,
        attempts=0,
        crashes=0,
        max_attempts=5,
        slices=0,
    )
    return JobContext(
        config=config,
        db=db,
        scheduler=s,
        job=job,
        deadline=time.monotonic() + budget,
        stop_event=threading.Event(),
        services=services,
    )


# ------------------------------------------------------------------ wikitext

WIKI = """{{Short description|Planet}}{{Infobox planet
| name = Mars
| orbit = [[Sun]]
| moons = {{plainlist|* [[Phobos (moon)|Phobos]] * [[Deimos (moon)|Deimos]]}}
}}
'''Mars''' is the fourth [[planet]] from the [[Sun]]&nbsp;in the [[Solar System|solar system]].<ref>{{cite web|url=x}}</ref>
It has a radius of {{convert|3389.5|km}} and two [[natural satellite|moon]]s.<ref name="a"/> <!-- hidden -->
[[File:Mars.jpg|thumb|A [[photograph]] of Mars]]
{| class="wikitable"
| cell || cell
|}
== Name ==
The name comes from the [[Roman mythology|Roman]] god of war, born {{birth date|1950|5|3}}.
* A list item about [[Olympus Mons]].
[[Category:Planets]][[Category:Mars| ]][[de:Mars (Planet)]]
"""


def test_clean_wikitext_links_templates_sections():
    page = clean_wikitext(WIKI)
    t = page.text
    assert t.startswith("Mars is the fourth planet from the Sun in the solar system.")
    assert "3389.5 km" in t and "two moons" in t and "3 May 1950" in t
    for junk in ("cite web", "hidden", "photograph", "wikitable", "cell", "Category", "Mars (Planet)", "{{", "[["):
        assert junk not in t, junk
    for a, b, _target in page.links:
        assert t[a:b].strip() == t[a:b] and t[a:b]
    spans = {t[a:b]: target for a, b, target in page.links}
    assert spans["planet"] == "Planet" and spans["solar system"] == "Solar System"
    assert spans["moons"] == "Natural satellite"  # link trail extends the anchor
    assert spans["Roman"] == "Roman mythology" and "photograph" not in spans
    assert page.categories == ["Planets", "Mars"]
    assert [h for _o, h in page.sections] == ["Name"]
    info = next(x for x in page.templates if x.name.lower().startswith("infobox"))
    assert info.params["name"] == "Mars" and "[[Sun]]" in info.params["orbit"]
    assert redirect_target("#REDIRECT [[Red planet#x]]") == "Red planet" and redirect_target("text") is None
    assert normalize_title("solar_system") == "Solar system"


def test_wikitext_robust_to_garbage():
    assert clean_wikitext("{{unclosed [[link text\n'''bold").text
    assert clean_wikitext("{" * 200 + "x" + "}" * 10).text is not None
    assert clean_wikitext("").text == ""
    deep = "{{a|" * 60 + "x" + "}}" * 60
    assert "{{" not in clean_wikitext(deep + " after").text
    tpl = parse_template("Template:Lang|fr|bonjour|italic=no")
    assert tpl.name == "Lang" and tpl.positional == ["fr", "bonjour"] and tpl.params == {"italic": "no"}
    s = clean_wikitext(
        "{{lang|fr|bonjour}} {{nowrap|x y}} {{cvt|5|-|10|km}} {{frac|1|2}} {{circa|1900}} {{as of|2020}}"
    )
    assert s.text == "bonjour x y 5–10 km 1/2 c. 1900 As of 2020"


# ----------------------------------------------------------------- Wikipedia

ARTICLE_TEXT = (
    "{{Infobox country|capital=[[Paris]]}}'''France''' is a country in [[Western Europe]]. Its capital is "
    "[[Paris]], which is also its largest city. France has a long history and a rich culture that has "
    "influenced the arts and sciences for centuries, and its language is spoken on several continents. "
    "[[Category:Countries]]"
)


def wiki_pages(n: int):
    pages: list[tuple[int, str, str, str | None]] = []
    for i in range(1, n + 1):
        text = ARTICLE_TEXT.replace("France", f"Country{i}") + f" [https://www.stats{i % 3}.example/c{i} Statistics]"
        pages.append((i, f"Country {i}", text, None))
    pages.append((n + 1, "Old name", "#REDIRECT [[Country 1]]", "Country 1"))
    pages.append(
        (
            n + 2,
            "Disambig",
            "'''X''' may refer to many things in the world of geography and more. " * 5 + "{{disambiguation}}",
            None,
        )
    )
    pages.append((n + 3, "Stub", "Too short.", None))
    return pages


def serve_wikipedia(web: FakeWeb, n: int = 9) -> None:
    dump, index = B.wiki_multistream(wiki_pages(n), per_stream=2)
    web.add("/enwiki/", '<a href="20260901/">20260901/</a><a href="20261001/">20261001/</a>')
    web.add(
        "/enwiki/20261001/dumpstatus.json", json.dumps({"jobs": {"articlesmultistreamdump": {"status": "in-progress"}}})
    )
    status = B.dumpstatus(
        {
            "enwiki-20260901-pages-articles-multistream1.xml-p1p99.bz2": (len(dump), "/enwiki/20260901/d1.bz2"),
            "enwiki-20260901-pages-articles-multistream-index1.txt-p1p99.bz2": (len(index), "/enwiki/20260901/i1.bz2"),
        }
    )
    web.add("/enwiki/20260901/dumpstatus.json", status, ctype="application/json")
    web.add("/enwiki/20260901/d1.bz2", dump, ctype="application/octet-stream")
    web.add("/enwiki/20260901/i1.bz2", index, ctype="application/octet-stream")


def test_multistream_ranges_and_pages():
    dump, index = B.wiki_multistream(wiki_pages(3), per_stream=2)
    lines = bz2.decompress(index).decode().splitlines()
    ranges = multistream_ranges(lines, len(dump))
    assert ranges[0][0] > 0 and ranges[-1][1] == len(dump)
    pages = [p for a, b in ranges for p in wikipedia.iter_pages(bz2.decompress(dump[a:b]))]
    assert [p.title for p in pages][:3] == ["Country 1", "Country 2", "Country 3"]
    assert pages[3].redirect == "Country 1"
    doc = wikipedia.page_to_document(pages[0], "en")
    assert doc and doc.license.startswith("CC BY-SA") and doc.url == "https://en.wikipedia.org/wiki/Country_1"
    assert doc.extra["infoboxes"][0]["params"]["capital"] == "[[Paris]]"
    assert doc.meta["categories"] == ["Countries"]
    dis = wikipedia.page_to_document(pages[4], "en")
    assert dis and dis.meta["disambiguation"] is True
    assert wikipedia.page_to_document(pages[5], "en") is None
    assert list(wikipedia.iter_pages(b"<page><title>broken")) == []


def test_wikipedia_resolve_and_sliced_ingest(config, db, web, monkeypatch):
    serve_wikipedia(web, 9)
    monkeypatch.setattr(wikipedia, "DUMPS", web.base)
    c = client()
    parts = wikipedia.resolve_multistream(c, "en")
    assert len(parts) == 1 and parts[0].date == "20260901"  # unfinished newer run skipped
    store = DocumentStore(db)
    services = {"http": c, "docs": store}
    payload = {
        "dump_url": parts[0].dump_url,
        "index_url": parts[0].index_url,
        "size": parts[0].size,
        "lang": "en",
        "batch_bytes": 1,
    }
    cp = None
    slices = 0
    while True:
        ctx = ctx_for(config, db, "wikipedia.part", payload, services, checkpoint=cp, budget=0.0)
        out = wikipedia.ingest_part(ctx)
        slices += 1
        cp = out.checkpoint
        if out.done:
            break
    assert slices > 3  # really resumed from checkpoints
    assert store.count("wikipedia") == 10  # 9 countries + disambiguation page
    cites = {r["site"]: r["citations"] for r in db.query("SELECT site, citations FROM site_citations")}
    assert cites == {"stats0.example": 3, "stats1.example": 3, "stats2.example": 3}  # sites the articles cite
    assert db.scalar("SELECT target FROM wiki_redirects WHERE title='Old name'") == "Country 1"
    with pytest.raises((PermanentError, FetchError)):
        wikipedia.resolve_multistream(c, "xx")


def test_wikipedia_limit(config, db, web, monkeypatch):
    serve_wikipedia(web, 9)
    monkeypatch.setattr(wikipedia, "DUMPS", web.base)
    part = wikipedia.resolve_multistream(client(), "en")[0]
    payload = {"dump_url": part.dump_url, "index_url": part.index_url, "size": part.size, "lang": "en", "limit": 3}
    out = wikipedia.ingest_part(
        ctx_for(config, db, "wikipedia.part", payload, {"http": client(), "docs": DocumentStore(db)})
    )
    assert out.done and out.checkpoint and out.checkpoint["articles"] == 3


# ------------------------------------------------------------------ Wikidata


def wd_entities(n: int) -> list[dict]:
    ents = [B.wikidata_entity("P36", "capital", kind="property", desc="seat of government")]
    for i in range(n):
        ents.append(
            B.wikidata_entity(
                f"Q{i + 100}",
                f"Thing {i}",
                desc="a test item",
                aliases=[f"T{i}"],
                enwiki=f"Thing {i}",
                claims={
                    "P31": [B.snak_item("P31", "Q5"), B.snak_item("P31", "Q6", rank="deprecated")],
                    "P569": [
                        B.snak_time("P569", "+1950-05-03T00:00:00Z"),
                        B.snak_time("P569", "-0300-00-00T00:00:00Z", 9),
                    ],
                    "P2048": [B.snak_quantity("P2048", "+1.83", "http://www.wikidata.org/entity/Q11573")],
                    "P214": [B.snak_other("P214", "external-id", "string", "123")],
                    "P625": [
                        B.snak_other("P625", "globe-coordinate", "globecoordinate", {"latitude": 1.5, "longitude": 2})
                    ],
                    "P1476": [
                        B.snak_other("P1476", "monolingualtext", "monolingualtext", {"text": "T", "language": "en"})
                    ],
                    "P1449": [B.snak_other("P1449", "string", "string", "nick" + "x" * (i % 3))],
                },
            )
        )
    ents.append({"type": "lexeme", "id": "L1"})
    ents.append(B.wikidata_entity("Q9999", "", desc="no english label"))
    return ents


def test_parse_entity_values():
    line = json.dumps(wd_entities(1)[1]).encode() + b","
    e = wikidata.parse_entity(line)
    assert e and e["qid"] == "Q100" and e["label"] == "Thing 0" and e["enwiki"] == "Thing 0" and e["aliases"] == ["T0"]
    c = e["claims"]
    assert c["P31"] == ["Q5"]  # deprecated statement dropped
    assert c["P569"] == [{"time": "1950-05-03"}, {"time": "-300"}]
    assert c["P2048"] == [{"amount": 1.83, "unit": "Q11573"}]
    assert "P214" not in c and c["P625"] == [{"lat": 1.5, "lon": 2}] and c["P1476"] == [{"text": "T"}]
    assert wikidata.parse_entity(b"[") is None and wikidata.parse_entity(b"{bad json") is None
    assert wikidata.convert_value({"snaktype": "novalue"}) is None


def test_bz2_block_reader_resume_equivalence(tmp_path):
    lines = [json.dumps({"i": i, "pad": "z" * (i * 37 % 3000)}).encode() for i in range(4000)]
    data = bz2.compress(b"\n".join(lines) + b"\n", 1)  # 100 kB blocks → many blocks
    path = tmp_path / "x.bz2"
    path.write_bytes(data)
    reader = Bz2BlockReader(file_fetcher(path), len(data), window=300_000)
    assert b"".join(b.data for b in reader.blocks(32)) == b"\n".join(lines) + b"\n"
    got, cur = [], None
    for line, c in iter_block_lines(reader, LineCursor(32, 0)):
        got.append(line)
        if len(got) in (1500, 2700):
            cur = c
            break
    assert cur is not None
    rest = [line for line, _c in iter_block_lines(Bz2BlockReader(file_fetcher(path), len(data)), cur)]
    assert got + rest == lines
    # truncated (partially downloaded) file: only complete blocks are read
    partial = Bz2BlockReader(lambda a, b: data[a:b], len(data) // 2)
    assert 0 < sum(len(b.data) for b in partial.blocks(32)) < len(b"\n".join(lines))
    with pytest.raises(ValueError):
        Bz2BlockReader(lambda a, b: b"nope", 4)
    two = bz2.compress(b"a\n" * 10) + bz2.compress(b"b\n" * 10)  # concatenated streams
    r2 = Bz2BlockReader(lambda a, b: two[a:b], len(two))
    assert [line for line, _ in iter_block_lines(r2, LineCursor(32, 0))] == [b"a"] * 10 + [b"b"] * 10
    with pytest.raises(OSError):
        decode_block(b"\x00" * 64, 0, 200)


def test_wikidata_resolve_and_ingest_resumable(config, db, web, monkeypatch):
    dump = B.wikidata_dump(wd_entities(400), level=1)
    web.add("/entities/", '<a href="20261005/">x</a><a href="20261007/">y</a>')
    web.add("/entities/20261007/", '<a href="wikidata-20261007-lexemes.json.bz2">l</a>')
    web.add("/entities/20261005/", '<a href="wikidata-20261005-all.json.bz2">a</a>')
    web.add("/entities/20261005/wikidata-20261005-all.json.bz2", dump, ctype="application/octet-stream")
    monkeypatch.setattr(wikidata, "ENTITIES", web.base + "/entities")
    url, size = wikidata.resolve_dump(client())
    assert url.endswith("20261005-all.json.bz2") and size == len(dump)
    services = {"http": client(), "docs": DocumentStore(db)}
    payload = {"url": url, "size": size, "window": 120_000}
    cp, slices = None, 0
    while True:
        out = wikidata.ingest_dump(ctx_for(config, db, "wikidata.dump", payload, services, checkpoint=cp, budget=0.0))
        cp, slices = out.checkpoint, slices + 1
        if out.done:
            break
    assert slices > 50
    assert db.scalar("SELECT COUNT(*) FROM wd_entities") == 401
    prop = db.one("SELECT * FROM wd_entities WHERE qid='P36'")
    assert prop["label"] == "capital" and json.loads(prop["claims"])["_datatype"] == ["wikibase-item"]
    again = wikidata.ingest_dump(ctx_for(config, db, "wikidata.dump", {**payload, "limit": 5}, services))
    assert again.done and again.value == 0  # unchanged entities are not rewritten
    monkeypatch.setattr(wikidata, "ENTITIES", web.base + "/nothing")
    web.add("/nothing/", "")
    with pytest.raises(PermanentError):
        wikidata.resolve_dump(client())


# ------------------------------------------------------- OpenAlex and PubMed

ABSTRACT = (
    "We study how small computers learn from text they collect themselves, measuring accuracy over "
    "many weeks of operation and comparing several statistical methods carefully."
)


def test_openalex_documents_and_ingest(config, db, web, monkeypatch):
    works = [B.openalex_work(i, f"Paper {i}", ABSTRACT + f" Variant {i}.") for i in range(30)]
    works.append(B.openalex_work(99, "No abstract", ""))
    works.append(B.openalex_work(98, "Foreign", ABSTRACT, language="de"))
    gz = B.openalex_gz(works)
    manifest = {
        "files": [
            {
                "url": "s3://openalex/data/jsonl/works/updated_date=2026-09-01/part_0000.gz",
                "meta": {"content_length": len(gz), "record_count": 32},
            },
            {
                "url": "s3://openalex/data/jsonl/works/updated_date=2016-01-01/part_0000.gz",
                "meta": {"content_length": 10, "record_count": 1},
            },
        ]
    }
    web.add("/data/jsonl/works/manifest.json", json.dumps(manifest), ctype="application/json")
    web.add("/data/jsonl/works/updated_date=2026-09-01/part_0000.gz", gz, ctype="application/gzip")
    monkeypatch.setattr(scholarly, "OPENALEX", web.base)
    files = scholarly.openalex_files(client(), 1)
    assert files[0]["url"].endswith("2026-09-01/part_0000.gz") and files[0]["records"] == 32
    assert scholarly.rebuild_abstract({"b": [1], "a": [0], "bad": [99999]}) == "a b"
    doc = scholarly.openalex_document(works[0])
    assert doc and doc.license.endswith("open-access license cc-by") and doc.meta["topics"] == ["Machine learning"]
    services = {"http": client(), "docs": DocumentStore(db)}
    rel = "openalex/p.gz"
    then = {"kind": "openalex.ingest", "payload": {"dest": rel}, "key": "oa-ingest"}
    out = download_job(
        ctx_for(config, db, "dump.download", {"url": files[0]["url"], "dest": rel, "then": then}, services)
    )
    assert out.done and db.scalar("SELECT COUNT(*) FROM jobs WHERE key='oa-ingest'") == 1
    cp = None
    while True:
        o = scholarly.ingest_openalex(
            ctx_for(config, db, "openalex.ingest", {"dest": rel}, services, checkpoint=cp, budget=0.0, key="oa-slices")
        )
        cp = o.checkpoint
        if o.done:
            break
    assert DocumentStore(db).count("openalex") == 30
    assert not raw_path(ctx_for(config, db, "x", {}, services), rel).exists()  # consumed file removed


def test_pubmed_documents_and_ingest(config, db, monkeypatch):
    arts = [
        B.pubmed_article(1000 + i, f"Trial {i}", [("Background", ABSTRACT), ("Results", os.urandom(60).hex())])
        for i in range(20)
    ]
    arts.append(B.pubmed_article(5, "Short", [(None, "tiny")]))
    arts.append(B.pubmed_article(6, "French", [(None, ABSTRACT)], lang="fre"))
    data = B.pubmed_gz(arts)
    rel = "pubmed/f.xml.gz"
    services = {"docs": DocumentStore(db)}
    path = raw_path(ctx_for(config, db, "x", {}, services), rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    out = scholarly.ingest_pubmed(ctx_for(config, db, "pubmed.ingest", {"dest": rel, "keep_raw": True}, services))
    assert out.done and services["docs"].count("pubmed") == 20
    doc = services["docs"].get(int(db.scalar("SELECT id FROM documents WHERE source='pubmed' LIMIT 1")))
    assert doc and "Background: We study" in doc.text and doc.meta["mesh"] == ["Humans"] and doc.meta["year"] == 2025
    # A truncated (sampled) file yields the complete records only.
    path.write_bytes(data[: len(data) // 2])
    elems = list(iter_xml_elements(iter_gzip_chunks(path), {"PubmedArticle"}))
    assert 0 < len(elems) < 22


def test_pubmed_listing(web, monkeypatch):
    web.add("/updatefiles/", '<a href="pubmed26n0002.xml.gz">2</a> <a href="pubmed26n0010.xml.gz">10</a>')
    monkeypatch.setattr(scholarly, "PUBMED", web.base)
    assert scholarly.pubmed_files(client(), 1) == [web.base + "/updatefiles/pubmed26n0010.xml.gz"]


def test_gzip_and_lines_helpers(tmp_path):
    p = tmp_path / "m.gz"
    p.write_bytes(gzip.compress(b"a\nb\n") + gzip.compress(b"c\nd"))
    assert list(iter_lines(iter_gzip_chunks(p), final_partial=True)) == [b"a", b"b", b"c", b"d"]
    assert list(iter_lines(iter_gzip_chunks(p, limit=5))) == []  # not enough bytes for a full line
    p.write_bytes(b"\x1f\x8b garbage")
    assert list(iter_gzip_chunks(p)) == []


# ------------------------------------------------- Gutenberg and Stack Exchange


def test_gutenberg_strip_catalog_and_job(config, db, web):
    body = "Chapter 1\r\n\r\nIt was a bright cold day in April,\r\nand the clocks were striking.\r\n\r\n" * 60
    rows = [
        {
            "Text#": str(i),
            "Type": "Text",
            "Language": "en",
            "Title": f"Book {i}",
            "Authors": "Doe, J.; Roe, R.",
            "Subjects": "Fiction; Tests",
        }
        for i in (11, 12, 13, 14)
    ]
    rows.append({"Text#": "15", "Type": "Sound", "Language": "en", "Title": "Audio"})
    rows.append({"Text#": "16", "Type": "Text", "Language": "fr", "Title": "Livre"})
    web.add("/cat.csv.gz", B.gutenberg_catalog(rows), ctype="application/gzip")
    for i in (11, 13, 14):
        web.add(
            f"/cache/epub/{i}/pg{i}.txt",
            B.gutenberg_text(f"Book {i}", body + f"Unique ending {i}."),
            ctype="text/plain",
        )
    text = books_qa.strip_gutenberg(B.gutenberg_text("X", body).decode())
    assert text.startswith("Chapter 1") and "License text" not in text and "PROJECT GUTENBERG" not in text
    assert "April, and the clocks" in text  # hard-wrapped lines re-flowed
    assert [r["Text#"] for r in books_qa.catalog_rows(B.gutenberg_catalog(rows))] == ["11", "12", "13", "14"]
    store = DocumentStore(db)
    from polymath.senses.crawler import Crawler

    cr = Crawler(db, client(), store, user_agent="PolymathBot", allow_domains=[])
    services = {"http": client(), "docs": store, "crawler": cr}
    rel = "gutenberg/cat.csv.gz"
    download_job(ctx_for(config, db, "dump.download", {"url": web.base + "/cat.csv.gz", "dest": rel}, services))
    payload = {"catalog": rel, "limit": 3, "base": web.base, "delay": 0.2}
    out = books_qa.gutenberg_job(ctx_for(config, db, "gutenberg.books", payload, services, budget=20))
    assert out.done and out.checkpoint and out.checkpoint["stored"] == 3 and out.checkpoint["failed"] == 1
    doc = store.get(int(db.scalar("SELECT id FROM documents WHERE source='gutenberg' LIMIT 1")))
    assert doc and doc.license.startswith("Public domain") and doc.meta["authors"] == ["Doe, J.", "Roe, R."]
    # transient failure keeps progress and asks to be retried later
    web.add("/cache/epub/12/pg12.txt", "busy", status=503)
    rows_after = books_qa.gutenberg_job(
        ctx_for(
            config,
            db,
            "gutenberg.books",
            {**payload, "limit": 10},
            services,
            checkpoint={"position": 1, "stored": 0, "failed": 0},
        )
    )
    assert not rows_after.done and rows_after.delay == 300.0 and some(rows_after.checkpoint)["position"] == 1
    hits = [t for t, _m, p, _h in web.log if p.startswith("/cache/epub/")]
    assert all(b - a >= 0.19 for a, b in itertools.pairwise(hits))  # per-host delay honoured
    n_requests = len(hits)
    again = books_qa.gutenberg_job(ctx_for(config, db, "gutenberg.books", {**payload, "limit": 1}, services))
    assert again.done and some(again.checkpoint)["stored"] == 1  # already-stored books count, without any request
    assert len([1 for _t, _m, p, _h in web.log if p.startswith("/cache/epub/")]) == n_requests


def _posts_xml() -> bytes:
    rows = [
        '<row Id="1" PostTypeId="1" Score="5" Title="What is a perceptron?" Tags="&lt;neural-networks&gt;" '
        'Body="&lt;p&gt;I keep reading about perceptrons in machine learning books and would like a clear '
        'explanation of what they compute.&lt;/p&gt;" ContentLicense="CC BY-SA 4.0" CreationDate="2020-01-01" />',
        '<row Id="2" PostTypeId="2" ParentId="1" Score="3" Body="&lt;p&gt;A perceptron computes a weighted sum '
        'of its inputs and applies a step function to decide which of two classes the input belongs to.&lt;/p&gt;" '
        'ContentLicense="CC BY-SA 3.0" />',
        '<row Id="3" PostTypeId="2" ParentId="1" Score="-2" Body="&lt;p&gt;wrong answer that nobody liked at all '
        'because it was not correct in any way whatsoever.&lt;/p&gt;" />',
        '<row Id="4" PostTypeId="5" Body="tag wiki" />',
    ]
    return ('<?xml version="1.0" encoding="utf-8"?>\n<posts>\n' + "\n".join(rows) + "\n</posts>\n").encode()


def test_stackexchange_ingest_from_7z(config, db, tmp_path):
    archive = B.sevenzip({"Badges.xml": b"<badges/>" * 1000, "Posts.xml": _posts_xml(), "Tags.xml": b"<tags/>"})
    rel = "stackexchange/ai.stackexchange.com.7z"
    store = DocumentStore(db)
    services = {"docs": store}
    path = raw_path(ctx_for(config, db, "x", {}, services), rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(archive)
    assert SevenZipFile(path).names() == ["Badges.xml", "Posts.xml", "Tags.xml"]
    out = books_qa.ingest_stackexchange(
        ctx_for(config, db, "stackexchange.ingest", {"dest": rel, "host": "ai.stackexchange.com"}, services)
    )
    assert out.done and store.count("stackexchange") == 2 and not path.exists()
    rows = {r["external_id"]: r for r in db.query("SELECT * FROM documents WHERE source='stackexchange'")}
    ans = rows["ai.stackexchange.com:2"]
    assert ans["title"] == "What is a perceptron?" and ans["license"] == "CC BY-SA 3.0"
    assert ans["license_url"] == "https://creativecommons.org/licenses/by-sa/3.0/"
    assert json.loads(rows["ai.stackexchange.com:1"]["meta"])["tags"] == ["neural-networks"]
    assert books_qa.se_archive_url("ai") == (
        "https://archive.org/download/stackexchange/ai.stackexchange.com.7z",
        "ai.stackexchange.com",
    )
    with pytest.raises(PermanentError):
        books_qa.ingest_stackexchange(ctx_for(config, db, "stackexchange.ingest", {"dest": rel, "host": "x"}, services))


def test_sevenzip_errors(tmp_path):
    p = tmp_path / "x.7z"
    p.write_bytes(b"not an archive at all, definitely not" * 2)
    with pytest.raises(SevenZipError):
        SevenZipFile(p)
    good = B.sevenzip({"a.txt": b"hello" * 100})
    p.write_bytes(good[: len(good) - 5])
    with pytest.raises(SevenZipError):
        SevenZipFile(p)
    p.write_bytes(good)
    z = SevenZipFile(p)
    with pytest.raises(KeyError):
        list(z.open("missing"))
    assert z.read("a.txt") == b"hello" * 100


def test_resume_records_cache_and_skip(config, db):
    ctx = ctx_for(config, db, "k", {}, {})
    made = []

    def make():
        made.append(1)
        return iter(range(10))

    it = resume_records(ctx, make, 0)
    assert next(it) == 0
    from polymath.senses.dumpfiles import park_records

    park_records(ctx, it, 1)
    assert next(resume_records(ctx, make, 1)) == 1 and len(made) == 1  # live iterator reused
    assert next(resume_records(ctx, make, 7)) == 7 and len(made) == 2  # cold start skips
    with pytest.raises(PermanentError):
        raw_path(ctx, "../../etc/passwd")


def test_download_job_permanent_and_continuation(config, db, web):
    services = {"http": client()}
    with pytest.raises(PermanentError):
        download_job(ctx_for(config, db, "dump.download", {"url": web.base + "/nope", "dest": "a/b"}, services))
    web.add("/big", os.urandom(3_000_000), ctype="application/octet-stream")
    config.senses.download_chunk = 100_000
    out = download_job(
        ctx_for(config, db, "dump.download", {"url": web.base + "/big", "dest": "big"}, services, budget=0.0)
    )
    assert not out.done and out.checkpoint and 0 < out.checkpoint["bytes"] < 3_000_000


# ------------------------------------------------------------------ planning


def test_source_registry_and_planner(config, db):
    comps = build_components(config, db)
    assert {r["name"] for r in db.query("SELECT name FROM sources")} >= {"wikipedia", "wikidata", "web"}
    assert all(r["license"] for r in db.query("SELECT license FROM sources"))
    config.senses.feeds = ["https://example.org/feed"]
    config.senses.seeds = ["https://example.org/"]
    agent = Agent(config, db, comps.registry, services=comps.services, notifier=Notifier({}))
    sources.planner(agent)
    kinds = {r["kind"] for r in db.query("SELECT kind FROM jobs")}
    assert kinds == {"sources.plan", "feeds.poll", "crawl.step"}


def test_plan_sources_against_fixtures(config, db, web, monkeypatch):
    serve_all(web, monkeypatch)
    config.senses.stackexchange_sites = ["ai"]
    comps = build_components(config, db)
    comps.services["http"] = client()
    out = sources.plan_sources(ctx_for(config, db, "sources.plan", {}, comps.services))
    assert out.result["errors"] == {}
    assert out.result["planned"] == {
        "wikipedia": 1,
        "wikidata": 1,
        "openalex": 1,
        "pubmed": 1,
        "gutenberg": 1,
        "stackexchange": 1,
    }
    again = sources.plan_sources(ctx_for(config, db, "sources.plan", {}, comps.services))
    assert sum(again.result["planned"].values()) == 0  # idempotent while work is active


# ------------------------------------------------------- offline integration


def serve_wikidata_properties(web: FakeWeb) -> None:
    """Wikidata's XML multistream (served under the same DUMPS base as Wikipedia): two property pages."""
    pages = [
        (1, "Q1", json.dumps(B.wikidata_entity("Q1", "universe")), None),
        (2, "Property:P31", json.dumps(B.wikidata_entity("P31", "instance of", kind="property")), None, 120),
        (3, "Property:P17", json.dumps(B.wikidata_entity("P17", "country", kind="property")), None, 120),
    ]
    dump, index = B.wiki_multistream(pages, per_stream=2)
    web.add("/wikidatawiki/", '<a href="20261001/">20261001/</a>')
    status = B.dumpstatus(
        {
            "wikidatawiki-20261001-pages-articles-multistream1.xml-p1p9.bz2": (len(dump), "/wikidatawiki/x/d1.bz2"),
            "wikidatawiki-20261001-pages-articles-multistream-index1.txt-p1p9.bz2": (
                len(index),
                "/wikidatawiki/x/i1.bz2",
            ),
        }
    )
    web.add("/wikidatawiki/20261001/dumpstatus.json", status, ctype="application/json")
    web.add("/wikidatawiki/x/d1.bz2", dump, ctype="application/octet-stream")
    web.add("/wikidatawiki/x/i1.bz2", index, ctype="application/octet-stream")


def serve_all(web: FakeWeb, monkeypatch) -> None:
    serve_wikipedia(web, 9)
    monkeypatch.setattr(wikipedia, "DUMPS", web.base)
    dump = B.wikidata_dump(wd_entities(30), level=1)
    web.add("/entities/", '<a href="20261005/">x</a>')
    web.add("/entities/20261005/", '<a href="wikidata-20261005-all.json.bz2">a</a>')
    web.add("/entities/20261005/wikidata-20261005-all.json.bz2", dump, ctype="application/octet-stream")
    monkeypatch.setattr(wikidata, "ENTITIES", web.base + "/entities")
    serve_wikidata_properties(web)
    gz = B.openalex_gz([B.openalex_work(i, f"Paper {i}", ABSTRACT + f" v{i}") for i in range(12)])
    web.add(
        "/data/jsonl/works/manifest.json",
        json.dumps(
            {"files": [{"url": "s3://openalex/data/jsonl/works/updated_date=2026-09-01/part_0000.gz", "meta": {}}]}
        ),
    )
    web.add("/data/jsonl/works/updated_date=2026-09-01/part_0000.gz", gz, ctype="application/gzip")
    monkeypatch.setattr(scholarly, "OPENALEX", web.base)
    web.add("/updatefiles/", '<a href="pubmed26n0001.xml.gz">1</a>')
    web.add(
        "/updatefiles/pubmed26n0001.xml.gz",
        B.pubmed_gz([B.pubmed_article(i, f"T{i}", [(None, ABSTRACT)]) for i in range(12)]),
        ctype="application/gzip",
    )
    monkeypatch.setattr(scholarly, "PUBMED", web.base)
    body = "A paragraph of a public domain book with enough words to be kept as text. " * 50
    rows = [{"Text#": str(i), "Type": "Text", "Language": "en", "Title": f"Book {i}"} for i in range(1, 4)]
    web.add("/cache/epub/feeds/pg_catalog.csv.gz", B.gutenberg_catalog(rows), ctype="application/gzip")
    for i in range(1, 4):
        web.add(f"/cache/epub/{i}/pg{i}.txt", B.gutenberg_text(f"Book {i}", body + f" Ending {i}."), ctype="text/plain")
    monkeypatch.setattr(books_qa, "GUTENBERG", web.base)
    monkeypatch.setattr(books_qa, "GUTENBERG_MIRROR", web.base)
    monkeypatch.setattr(books_qa, "GUTENBERG_DELAY", 0.05)
    web.add(
        "/stackexchange/ai.stackexchange.com.7z",
        B.sevenzip({"Posts.xml": _posts_xml()}),
        ctype="application/x-7z-compressed",
    )
    monkeypatch.setattr(books_qa, "SE_ARCHIVE", web.base + "/stackexchange")
    from tests.test_senses_web import RSS, _site

    web.add("/rss", RSS, ctype="application/rss+xml")
    _site(web)


def test_offline_integration_all_sources_through_agent_loop(config, db, web, monkeypatch):
    """Every source, fixture-served, collected by the real agent loop with licenses on every item."""
    serve_all(web, monkeypatch)
    config.senses.stackexchange_sites = ["ai"]
    config.senses.feeds = [web.base + "/rss"]
    config.senses.seeds = [web.base + "/"]
    config.senses.crawl_allow_domains = ["127.0.0.1"]
    web.add("/blocklist.hosts", "# safety list\n0.0.0.0 bad.example\n0.0.0.0 worse.example\n")
    config.senses.blocklists = [web.base + "/blocklist.hosts"]  # downloaded and loaded offline, like the real ones
    config.loop.planners = True
    comps = build_components(config, db)
    comps.services["http"] = client()
    comps.services["crawler"].client = comps.services["http"]
    agent = Agent(config, db, comps.registry, services=comps.services, planners=comps.planners, notifier=Notifier({}))
    agent.start()
    deadline = time.time() + 120
    idle_rounds = 0
    while time.time() < deadline and idle_rounds < 3:
        rec = agent.cycle()
        if rec.status == "idle":
            idle_rounds += 1
            db.execute("UPDATE jobs SET not_before=0 WHERE state='queued' AND kind NOT IN ('sources.plan')")
            if comps.services["crawler"].frontier.pending():
                agent.scheduler.enqueue("crawl.step", {}, key=f"crawl:{time.time_ns()}")
                idle_rounds = 0
        else:
            idle_rounds = 0
    counts = {
        r["source"]: r["n"]
        for r in db.query("SELECT source, COUNT(*) AS n FROM documents WHERE state!='duplicate' GROUP BY source")
    }
    assert counts.get("wikipedia") == 10
    assert counts.get("openalex") == 12 and counts.get("pubmed") == 12
    assert counts.get("gutenberg") == 3 and counts.get("stackexchange") == 2
    frontier = [(r["url"], r["state"], r["reason"]) for r in db.query("SELECT * FROM frontier")]
    assert counts.get("feed", 0) >= 1 and counts.get("web", 0) >= 3, "\n".join(map(str, frontier))
    assert db.scalar("SELECT COUNT(*) FROM wd_entities WHERE qid GLOB 'Q*'") == 30  # + properties below
    assert {r["qid"] for r in db.query("SELECT qid FROM wd_entities WHERE qid GLOB 'P*'")} >= {"P31", "P17"}
    assert db.scalar("SELECT COUNT(*) FROM wd_property_pages WHERE fetched IS NOT NULL") == 2
    assert db.scalar("SELECT COUNT(*) FROM documents WHERE license IS NULL OR license=''") == 0
    dead = db.query("SELECT kind, last_error FROM jobs WHERE state='dead'")
    assert not dead, [dict(r) for r in dead]
    assert not any(Path(config.paths.raw_dir).rglob("*.part"))
    from polymath.senses import openweb

    assert openweb.blocklists_ready(db, config) and openweb.blocked(db, "www.bad.example")  # safety lists loaded


def test_cli_sample_and_sources_commands(tmp_path, web, monkeypatch, capsys):
    from polymath.interface import cli
    from tests.conftest import make_config

    serve_all(web, monkeypatch)
    make_config(tmp_path)
    cfg = tmp_path / "polymath.toml"
    cfg.write_text(
        cfg.read_text().replace(
            "[senses]\n",
            f'[senses]\nallow_private_networks = true\nfeeds = ["{web.base}/rss"]\n'
            f'seeds = ["{web.base}/"]\ncrawl_allow_domains = ["127.0.0.1"]\nstackexchange_sites = ["ai"]\n',
            1,
        )
    )
    rc = cli.main(
        [
            "--config",
            str(cfg),
            "sample",
            "--n",
            "2",
            "--max-minutes",
            "2",
            "--sources",
            "wikipedia,wikidata,openalex,pubmed,gutenberg,stackexchange,feed,web",
        ]
    )
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    per = report["sources"]
    for name in ("wikipedia", "wikidata", "openalex", "pubmed", "gutenberg", "stackexchange", "web"):
        assert per[name]["target_met"], (name, per[name])
        assert per[name]["with_license"] == per[name]["items"]
    assert per["feed"]["items"] >= 1 and report["dead"] == []
    assert list((tmp_path / "data" / "reports").glob("sample-*.json"))
    assert cli.main(["--config", str(cfg), "sources"]) == 0
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert {x["name"] for x in lines} >= {"wikipedia", "gutenberg"} and all(x["license"] for x in lines)


def test_targeted_reading_by_title(config, db, web, monkeypatch):
    """wikipedia.titles: read just the streams holding specific articles (curiosity / `polymath learn`)."""
    serve_wikipedia(web, 9)
    monkeypatch.setattr(wikipedia, "DUMPS", web.base)
    part = wikipedia.resolve_multistream(client(), "en")[0]
    lines = bz2.decompress(B.wiki_multistream(wiki_pages(9), per_stream=2)[1]).decode().splitlines()
    ctx = ctx_for(config, db, "x", {}, {"http": client(), "docs": DocumentStore(db)})
    wikipedia.store_title_index(ctx, "en", part.dump_url, lines, multistream_ranges(lines, part.size))
    assert db.scalar("SELECT COUNT(*) FROM wiki_index WHERE lang='en'") == 12
    store = DocumentStore(db)
    payload = {"titles": ["Country 3", "Country 4", "Old name", "Nowhere"], "lang": "en"}
    reads_before = len(web.log)
    out = wikipedia.fetch_titles(ctx_for(config, db, "wikipedia.titles", payload, {"http": client(), "docs": store}))
    assert out.done and out.result["not_in_index"] == 1 and out.result["streams"] == 2
    assert out.result["stored"] >= 2 and store.count("wikipedia") == out.result["stored"]
    assert db.scalar("SELECT target FROM wiki_redirects WHERE title='Old name'") == "Country 1"
    ranged = [h for _t, _m, p, h in web.log[reads_before:] if p.endswith("d1.bz2")]
    assert len(ranged) == 2 and all(h.get("range") for h in ranged)  # only the two streams, by range
    again = wikipedia.fetch_titles(
        ctx_for(config, db, "wikipedia.titles", payload, {"http": client(), "docs": store}, checkpoint=out.checkpoint)
    )
    assert again.result["stored"] == 0  # streams already read are skipped on resume
