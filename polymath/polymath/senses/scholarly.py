"""Scholarly sources: the OpenAlex snapshot (S3 JSON-lines) and PubMed XML files."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any
from xml.etree import ElementTree as ET

from polymath.core.jobs import JobContext, JobOutcome
from polymath.memory.documents import Document, DocumentStore
from polymath.senses.dumpfiles import raw_path, run_records
from polymath.senses.net import HttpClient
from polymath.senses.streams import iter_gzip_chunks, iter_lines, iter_xml_elements, local_name

OPENALEX = "https://openalex.s3.amazonaws.com"
OPENALEX_LICENSE = "OpenAlex metadata CC0 1.0; abstract text © its publisher"
OPENALEX_LICENSE_URL = "https://creativecommons.org/publicdomain/zero/1.0/"
PUBMED = "https://ftp.ncbi.nlm.nih.gov/pubmed"
PUBMED_LICENSE = "NLM PubMed data terms; abstracts may be © publishers"
PUBMED_LICENSE_URL = "https://www.nlm.nih.gov/databases/download/terms_and_conditions.html"


# ------------------------------------------------------------------ OpenAlex


def openalex_files(client: HttpClient, count: int, *, newest_first: bool = True) -> list[dict[str, Any]]:
    manifest = json.loads(client.get(f"{OPENALEX}/data/jsonl/works/manifest.json", max_bytes=50_000_000).body)
    files: list[dict[str, Any]] = []
    for entry in manifest.get("files", manifest.get("entries", [])):
        url = str(entry["url"]).replace("s3://openalex", OPENALEX)
        meta = entry.get("meta", {})
        files.append(
            {"url": url, "bytes": int(meta.get("content_length", 0)), "records": int(meta.get("record_count", 0))}
        )
    files.sort(key=lambda f: str(f["url"]), reverse=newest_first)
    return files[:count]


def rebuild_abstract(inverted: dict[str, list[int]] | None) -> str:
    if not inverted:
        return ""
    positions: dict[int, str] = {}
    for word, idxs in inverted.items():
        for i in idxs:
            if isinstance(i, int) and 0 <= i < 20000:
                positions[i] = word
    return " ".join(positions[i] for i in sorted(positions))


def openalex_document(work: dict[str, Any]) -> Document | None:
    title = (work.get("title") or work.get("display_name") or "").strip()
    abstract = rebuild_abstract(work.get("abstract_inverted_index"))
    if not title or len(abstract) < 150:
        return None
    lang = work.get("language")
    if lang and lang != "en":
        return None
    oa = work.get("best_oa_location") or {}
    oa_license = oa.get("license")
    license_ = OPENALEX_LICENSE + (f"; open-access license {oa_license}" if oa_license else "")
    topics = [t.get("display_name") for t in (work.get("topics") or [])[:5] if t.get("display_name")]
    concepts = [c.get("display_name") for c in (work.get("concepts") or [])[:8] if c.get("display_name")]
    authors = [((a.get("author") or {}).get("display_name")) for a in (work.get("authorships") or [])[:10]]
    source = (((work.get("primary_location") or {}).get("source")) or {}).get("display_name")
    wid = str(work.get("id", "")).rsplit("/", 1)[-1]
    return Document(
        source="openalex",
        external_id=wid,
        title=title[:1000],
        text=f"{title}\n\n{abstract}",
        license=license_,
        license_url=OPENALEX_LICENSE_URL,
        url=work.get("doi") or work.get("id"),
        lang="en",
        published=work.get("publication_date"),
        meta={
            "kind": "paper",
            "year": work.get("publication_year"),
            "doi": work.get("doi"),
            "venue": source,
            "authors": [a for a in authors if a],
            "topics": topics,
            "concepts": concepts,
            "cited_by": work.get("cited_by_count"),
            "type": work.get("type"),
        },
    )


def ingest_openalex(ctx: JobContext) -> JobOutcome:
    """Job ``openalex.ingest``: payload {dest, limit?} (file already downloaded)."""
    rel = ctx.job.payload["dest"]
    path = raw_path(ctx, rel)
    store: DocumentStore = ctx.services["docs"]
    limit_bytes = ctx.job.payload.get("bytes_limit")

    def make() -> Iterator[bytes]:
        return iter_lines(iter_gzip_chunks(path, limit=limit_bytes))

    def handle(line: bytes) -> float:
        try:
            work = json.loads(line)
        except ValueError:
            return 0.0
        doc = openalex_document(work)
        if doc is None:
            return 0.0
        _, status = store.add(doc)
        return 1.0 if status in {"new", "updated"} else 0.0

    return run_records(ctx, make, handle, rel=rel)


# -------------------------------------------------------------------- PubMed


def pubmed_files(client: HttpClient, count: int, *, folder: str = "updatefiles") -> list[str]:
    listing = client.get(f"{PUBMED}/{folder}/").text()
    names = sorted(set(re.findall(r'href="(pubmed\d+n\d+\.xml\.gz)"', listing)), reverse=True)
    return [f"{PUBMED}/{folder}/{n}" for n in names[:count]]


def _text(el: ET.Element | None) -> str:
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


def pubmed_document(article: ET.Element) -> Document | None:
    def find(path: str) -> ET.Element | None:
        node: ET.Element | None = article
        for part in path.split("/"):
            if node is None:
                return None
            node = next((c for c in node if local_name(c.tag) == part), None)
        return node

    pmid = _text(find("MedlineCitation/PMID"))
    art = find("MedlineCitation/Article")
    if not pmid or art is None:
        return None
    title = _text(next((c for c in art if local_name(c.tag) == "ArticleTitle"), None))
    abstract_el = next((c for c in art if local_name(c.tag) == "Abstract"), None)
    parts = []
    if abstract_el is not None:
        for at in abstract_el:
            if local_name(at.tag) == "AbstractText":
                label = at.get("Label")
                txt = _text(at)
                parts.append(f"{label.capitalize()}: {txt}" if label else txt)
    abstract = "\n".join(p for p in parts if p)
    if not title or len(abstract) < 150:
        return None
    lang = _text(next((c for c in art if local_name(c.tag) == "Language"), None))
    if lang and lang != "eng":
        return None
    journal = _text(find("MedlineCitation/Article/Journal/Title"))
    year = _text(find("MedlineCitation/Article/Journal/JournalIssue/PubDate/Year"))
    mesh = []
    mesh_list = find("MedlineCitation/MeshHeadingList")
    if mesh_list is not None:
        for mh in mesh_list:
            d = next((c for c in mh if local_name(c.tag) == "DescriptorName"), None)
            if d is not None and d.text:
                mesh.append(d.text)
    keywords = [k.text for k in article.iter() if local_name(k.tag) == "Keyword" and k.text][:20]
    return Document(
        source="pubmed",
        external_id=pmid,
        title=title[:1000],
        text=f"{title}\n\n{abstract}",
        license=PUBMED_LICENSE,
        license_url=PUBMED_LICENSE_URL,
        url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        lang="en",
        published=year or None,
        meta={
            "kind": "paper",
            "year": int(year) if year.isdigit() else None,
            "journal": journal,
            "mesh": mesh[:30],
            "keywords": keywords,
        },
    )


def ingest_pubmed(ctx: JobContext) -> JobOutcome:
    """Job ``pubmed.ingest``: payload {dest, limit?}."""
    rel = ctx.job.payload["dest"]
    path = raw_path(ctx, rel)
    store: DocumentStore = ctx.services["docs"]

    def make() -> Iterator[ET.Element]:
        return iter_xml_elements(iter_gzip_chunks(path), {"PubmedArticle"})

    def handle(article: ET.Element) -> float:
        doc = pubmed_document(article)
        if doc is None:
            return 0.0
        _, status = store.add(doc)
        return 1.0 if status in {"new", "updated"} else 0.0

    return run_records(ctx, make, handle, rel=rel)
