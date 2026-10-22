"""Deterministic builders for dump-format fixtures (no binary files in the repo)."""

from __future__ import annotations

import bz2
import csv
import gzip
import io
import json
import lzma
import struct
import zlib
from typing import Any
from xml.sax.saxutils import escape

# ----------------------------------------------------------------- Wikipedia


def wiki_page_xml(page_id: int, title: str, text: str, *, ns: int = 0, redirect: str | None = None) -> str:
    red = f'<redirect title="{escape(redirect)}" />' if redirect else ""
    return (
        f"  <page>\n    <title>{escape(title)}</title>\n    <ns>{ns}</ns>\n    <id>{page_id}</id>\n    {red}\n"
        f"    <revision><id>{page_id * 10}</id><timestamp>2026-09-01T00:00:00Z</timestamp>"
        f'<text bytes="{len(text)}" xml:space="preserve">{escape(text)}</text></revision>\n  </page>\n'
    )


def wiki_multistream(pages: list[tuple[Any, ...]], per_stream: int = 2) -> tuple[bytes, bytes]:
    """Build (dump.xml.bz2, index.txt.bz2) exactly like Wikimedia's multistream format.

    Pages are ``(page_id, title, text, redirect)`` with an optional 5th element: the namespace.
    """
    header = (
        '<mediawiki xmlns="http://www.mediawiki.org/xml/export-0.11/" version="0.11" xml:lang="en">\n'
        "  <siteinfo><sitename>Wikipedia</sitename></siteinfo>\n"
    )
    out = bytearray(bz2.compress(header.encode()))
    index_lines = []
    for i in range(0, len(pages), per_stream):
        chunk = pages[i : i + per_stream]
        offset = len(out)
        xml = "".join(
            wiki_page_xml(pg[0], pg[1], pg[2], redirect=pg[3], ns=pg[4] if len(pg) > 4 else 0) for pg in chunk
        )
        out += bz2.compress(xml.encode())
        index_lines += [f"{offset}:{pg[0]}:{pg[1]}" for pg in chunk]
    out += bz2.compress(b"</mediawiki>\n")
    return bytes(out), bz2.compress(("\n".join(index_lines) + "\n").encode())


def dumpstatus(files: dict[str, tuple[int, str]]) -> bytes:
    return json.dumps(
        {
            "jobs": {
                "articlesmultistreamdump": {
                    "status": "done",
                    "files": {name: {"size": size, "url": url} for name, (size, url) in files.items()},
                }
            }
        }
    ).encode()


# ------------------------------------------------------------------ Wikidata


def wikidata_entity(
    qid: str,
    label: str,
    *,
    desc: str = "",
    aliases: list[str] | None = None,
    claims: dict[str, list[dict]] | None = None,
    enwiki: str | None = None,
    kind: str = "item",
) -> dict:
    ent: dict = {
        "type": kind,
        "id": qid,
        "labels": {"en": {"language": "en", "value": label}, "de": {"language": "de", "value": label + "_de"}},
        "descriptions": {"en": {"language": "en", "value": desc}} if desc else {},
        "aliases": {"en": [{"language": "en", "value": a} for a in (aliases or [])]},
        "claims": claims or {},
        "sitelinks": {"enwiki": {"site": "enwiki", "title": enwiki}} if enwiki else {},
    }
    if kind == "property":
        ent["datatype"] = "wikibase-item"
    return ent


def snak_item(pid: str, qid: str, rank: str = "normal") -> dict:
    return {
        "mainsnak": {
            "snaktype": "value",
            "property": pid,
            "datatype": "wikibase-item",
            "datavalue": {"type": "wikibase-entityid", "value": {"entity-type": "item", "id": qid}},
        },
        "type": "statement",
        "rank": rank,
    }


def snak_time(pid: str, time: str, precision: int = 11) -> dict:
    return {
        "mainsnak": {
            "snaktype": "value",
            "property": pid,
            "datatype": "time",
            "datavalue": {"type": "time", "value": {"time": time, "precision": precision}},
        },
        "type": "statement",
        "rank": "normal",
    }


def snak_quantity(pid: str, amount: str, unit: str = "1") -> dict:
    return {
        "mainsnak": {
            "snaktype": "value",
            "property": pid,
            "datatype": "quantity",
            "datavalue": {"type": "quantity", "value": {"amount": amount, "unit": unit}},
        },
        "type": "statement",
        "rank": "normal",
    }


def snak_other(pid: str, datatype: str, vtype: str, value: object) -> dict:
    return {
        "mainsnak": {
            "snaktype": "value",
            "property": pid,
            "datatype": datatype,
            "datavalue": {"type": vtype, "value": value},
        },
        "type": "statement",
        "rank": "normal",
    }


def wikidata_dump(entities: list[dict], level: int = 1) -> bytes:
    lines = ["["] + [json.dumps(e) + "," for e in entities[:-1]] + [json.dumps(entities[-1]), "]"]
    return bz2.compress(("\n".join(lines) + "\n").encode(), level)


# --------------------------------------------------------- OpenAlex / PubMed


def inverted_index(text: str) -> dict[str, list[int]]:
    inv: dict[str, list[int]] = {}
    for i, w in enumerate(text.split()):
        inv.setdefault(w, []).append(i)
    return inv


def openalex_gz(works: list[dict]) -> bytes:
    return gzip.compress(("\n".join(json.dumps(w) for w in works) + "\n").encode())


def openalex_work(n: int, title: str, abstract: str, **extra: object) -> dict:
    w = {
        "id": f"https://openalex.org/W{n}",
        "title": title,
        "abstract_inverted_index": inverted_index(abstract),
        "publication_year": 2024,
        "publication_date": "2024-05-01",
        "doi": f"https://doi.org/10.1/{n}",
        "language": "en",
        "best_oa_location": {"license": "cc-by"},
        "cited_by_count": n,
        "topics": [{"display_name": "Machine learning"}],
        "concepts": [{"display_name": "Computer science"}],
        "authorships": [{"author": {"display_name": "Ada Lovelace"}}],
        "primary_location": {"source": {"display_name": "Journal of Tests"}},
        "type": "article",
    }
    w.update(extra)
    return w


def pubmed_article(
    pmid: int, title: str, abstract: list[tuple[str | None, str]], year: str = "2025", lang: str = "eng"
) -> str:
    parts = "".join(
        f'<AbstractText Label="{label}">{escape(t)}</AbstractText>'
        if label
        else f"<AbstractText>{escape(t)}</AbstractText>"
        for label, t in abstract
    )
    return (
        f'<PubmedArticle><MedlineCitation><PMID Version="1">{pmid}</PMID><Article>'
        f"<Journal><JournalIssue><PubDate><Year>{year}</Year></PubDate></JournalIssue><Title>J Test</Title></Journal>"
        f"<ArticleTitle>{escape(title)}</ArticleTitle><Abstract>{parts}</Abstract><Language>{lang}</Language>"
        f"</Article><MeshHeadingList><MeshHeading><DescriptorName>Humans</DescriptorName></MeshHeading>"
        f"</MeshHeadingList><KeywordList><Keyword>test</Keyword></KeywordList></MedlineCitation></PubmedArticle>"
    )


def pubmed_gz(articles: list[str]) -> bytes:
    doc = (
        '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE PubmedArticleSet PUBLIC "-//NLM//DTD PubMedArticle//EN"'
        ' "https://dtd.nlm.nih.gov/ncbi/pubmed/out/pubmed_250101.dtd">\n<PubmedArticleSet>'
        + "".join(articles)
        + "<DeleteCitation><PMID>1</PMID></DeleteCitation></PubmedArticleSet>\n"
    )
    return gzip.compress(doc.encode())


# ------------------------------------------------------------------ Gutenberg


def gutenberg_catalog(rows: list[dict[str, str]]) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(
        buf, fieldnames=["Text#", "Type", "Issued", "Title", "Language", "Authors", "Subjects", "LoCC", "Bookshelves"]
    )
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, "") for k in w.fieldnames})
    return gzip.compress(buf.getvalue().encode())


def gutenberg_text(title: str, body: str) -> bytes:
    return (
        f"The Project Gutenberg eBook of {title}\r\n\r\nThis eBook is for the use of anyone anywhere.\r\n\r\n"
        f"*** START OF THE PROJECT GUTENBERG EBOOK {title.upper()} ***\r\n\r\n{body}\r\n\r\n"
        f"*** END OF THE PROJECT GUTENBERG EBOOK {title.upper()} ***\r\n\r\nLicense text here.\r\n"
    ).encode()


# ------------------------------------------------------------------------ 7z


def _num(v: int) -> bytes:
    for n in range(8):
        if v < (1 << (7 * (n + 1) + n)) and v < (1 << (8 * n + 7 - n)):
            first = ((0xFF << (8 - n)) & 0xFF) | (v >> (8 * n))
            return bytes([first]) + (v & ((1 << (8 * n)) - 1)).to_bytes(n, "little") if n else bytes([first])
    return b"\xff" + v.to_bytes(8, "little")


def _streams_info(pack_size: int, coder: bytes, unpack_sizes: list[int], crcs: list[int]) -> bytes:
    out = bytearray([0x06]) + _num(0) + _num(1) + b"\x09" + _num(pack_size) + b"\x00"  # PackInfo
    out += b"\x07\x0b" + _num(1) + b"\x00" + coder + b"\x0c" + _num(sum(unpack_sizes)) + b"\x00"  # UnpackInfo
    out += b"\x08\x0d" + _num(len(unpack_sizes))  # SubStreamsInfo
    if len(unpack_sizes) > 1:
        out += b"\x09" + b"".join(_num(s) for s in unpack_sizes[:-1])
    out += b"\x0a\x01" + b"".join(struct.pack("<I", c) for c in crcs) + b"\x00"
    out += b"\x00"
    return bytes(out)


def sevenzip(files: dict[str, bytes], *, encode_header: bool = True, corrupt_crc: bool = False) -> bytes:
    """A real 7z archive (solid LZMA2 folder; optionally LZMA-encoded header)."""
    names = list(files)
    data = b"".join(files[n] for n in names)
    packed = lzma.compress(data, format=lzma.FORMAT_RAW, filters=[{"id": lzma.FILTER_LZMA2, "dict_size": 1 << 20}])
    lzma2_coder = _num(1) + bytes([0x21]) + b"\x21" + _num(1) + bytes([16])
    crcs = [zlib.crc32(files[n]) ^ (1 if corrupt_crc else 0) for n in names]
    header = bytearray([0x01, 0x04]) + _streams_info(len(packed), lzma2_coder, [len(files[n]) for n in names], crcs)
    name_blob = b"\x00" + b"".join(n.encode("utf-16-le") + b"\x00\x00" for n in names)
    header += b"\x05" + _num(len(names)) + b"\x11" + _num(len(name_blob)) + name_blob + b"\x00" + b"\x00"
    body = bytearray(packed)
    next_header = bytes(header)
    if encode_header:
        props = bytes([93]) + (1 << 16).to_bytes(4, "little")
        hpacked = lzma.compress(
            next_header,
            format=lzma.FORMAT_RAW,
            filters=[{"id": lzma.FILTER_LZMA1, "dict_size": 1 << 16, "lc": 3, "lp": 0, "pb": 2}],
        )
        coder = _num(1) + bytes([0x23]) + b"\x03\x01\x01" + _num(5) + props
        enc = bytearray([0x17]) + bytes([0x06]) + _num(len(body)) + _num(1) + b"\x09" + _num(len(hpacked)) + b"\x00"
        enc += b"\x07\x0b" + _num(1) + b"\x00" + coder + b"\x0c" + _num(len(next_header)) + b"\x00" + b"\x00"
        body += hpacked
        next_header = bytes(enc)
    start = struct.pack("<QQI", len(body), len(next_header), zlib.crc32(next_header))
    sig = b"7z\xbc\xaf\x27\x1c\x00\x04" + struct.pack("<I", zlib.crc32(start)) + start
    return sig + bytes(body) + next_header
