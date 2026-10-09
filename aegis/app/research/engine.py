"""Research pipeline: discovery → safe fetch → parse → dedupe → screen →
extract → score → store source-backed knowledge."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import urlencode, urlsplit

from ..db import loads, now_iso
from ..models.base import ModelError
from ..security.network_policy import URLRejected
from . import deduplicator as dd
from . import injection
from .extractor import extract_deterministic, extract_with_model
from .fetcher import FetchError
from .parser import ParsedDocument, parse
from .source_quality import score_source

log = logging.getLogger(__name__)

MIN_WORDS = 40


@dataclass
class IngestOutcome:
    url: str
    status: str  # stored | duplicate | near_duplicate | rejected | failed | feed | skipped
    reason: str = ""
    document_id: int | None = None
    claims_created: int = 0
    claims_reinforced: int = 0
    contradictions: int = 0
    new_sources: int = 0
    links: list[tuple[str, str]] = field(default_factory=list)  # (url, anchor text) for link expansion
    quality: float | None = None


@dataclass
class RunStats:
    topic: str
    examined: int = 0
    stored: int = 0
    duplicates: int = 0
    rejected: int = 0
    failed: int = 0
    claims_created: int = 0
    claims_reinforced: int = 0
    contradictions: int = 0
    outcomes: list[IngestOutcome] = field(default_factory=list)

    def add(self, o: IngestOutcome) -> None:
        self.outcomes.append(o)
        if o.status in ("feed", "skipped"):
            return
        self.examined += 1
        if o.status in ("stored", "near_duplicate"):
            self.stored += 1
        if o.status in ("duplicate", "near_duplicate"):
            self.duplicates += 1
        if o.status == "rejected":
            self.rejected += 1
        if o.status == "failed":
            self.failed += 1
        self.claims_created += o.claims_created
        self.claims_reinforced += o.claims_reinforced
        self.contradictions += o.contradictions

    def as_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "outcomes"}
        d["outcomes"] = [o.__dict__ for o in self.outcomes[:50]]
        return d


class ResearchEngine:
    def __init__(self, services):
        self.s = services
        self.settings = services.settings
        self.db = services.db
        self.store = services.knowledge

    # -- discovery ----------------------------------------------------------
    def search(self, query: str, allowed: list[str] | None = None, limit: int = 10) -> list[dict]:
        """Search configured public sources (self-hosted SearXNG JSON API)."""
        if not self.settings.searxng_url:
            return []
        url = self.settings.searxng_url.rstrip("/") + "/search?" + urlencode({"q": query, "format": "json"})
        try:
            res = self.s.searx_fetcher.fetch(url)
        except FetchError as exc:
            log.warning("search failed: %s", exc)
            return []
        data = loads(res.text, {}) or {}
        out = []
        for r in data.get("results", [])[: limit * 3]:
            u = r.get("url", "")
            try:
                self.s.url_policy.check(u, allowed)
            except URLRejected:
                continue
            out.append({"url": u, "title": r.get("title", ""), "snippet": r.get("content", "")})
            if len(out) >= limit:
                break
        return out

    def discover(self, topic: dict) -> int:
        """Queue candidate URLs for a topic. Returns the number of new sources."""
        allowed = loads(topic["allowed_domains"], []) or None
        new = 0
        for url in loads(topic["seed_urls"], []):
            new += self._queue(url, "seed", topic["id"], None, allowed)
        for feed in loads(topic["feeds"], []):
            o = self.ingest_url(feed, topic=topic, method="feed")
            new += o.new_sources
        if topic.get("query"):
            for r in self.search(topic["query"], allowed, limit=topic["max_docs_per_run"]):
                new += self._queue(r["url"], "search", topic["id"], self.settings.searxng_url, allowed)
        return new

    def _queue(self, url: str, method: str, topic_id: int | None, origin: str | None,
               allowed: list[str] | None) -> int:
        try:
            host = self.s.url_policy.check(url, allowed)
        except URLRejected:
            return 0
        before = self.db.scalar("SELECT id FROM research_sources WHERE url = ?", (url,))
        self.store.upsert_source(url, host, method, topic_id, origin)
        return 0 if before else 1

    # -- ingestion ----------------------------------------------------------
    def ingest_url(self, url: str, *, topic: dict | None = None, method: str = "manual",
                   query: str | None = None, objective_id: str | None = None, refresh: bool = False,
                   topic_name: str | None = None, allowed_domains: list[str] | None = None) -> IngestOutcome:
        allowed = allowed_domains or (loads(topic["allowed_domains"], []) if topic else []) or None
        topic_id = topic["id"] if topic else None
        query = query if query is not None else (topic["query"] or topic["name"]) if topic else ""
        host = urlsplit(url).hostname or ""
        src = self.store.upsert_source(url, host, method, topic_id)
        if src["status"] in ("fetched", "duplicate") and not refresh and method != "feed":
            return IngestOutcome(url, "skipped", "already processed")
        try:
            res = self.s.fetcher.fetch(url, allowed)
        except FetchError as exc:
            status = "failed" if exc.transient else "rejected"
            self.store.set_source_status(src["id"], status, str(exc)[:500], exc.status)
            return IngestOutcome(url, status, str(exc))
        try:
            doc = parse(res.text, res.content_type, res.final_url)
        except Exception as exc:  # malformed XML/JSON etc.
            self.store.set_source_status(src["id"], "rejected", f"parse error: {exc}"[:500], res.status)
            return IngestOutcome(url, "rejected", f"parse error: {exc}")

        if doc.kind == "feed":
            new = 0
            for e in doc.feed_entries[: (topic or {}).get("max_docs_per_run", 25) * 2]:
                new += self._queue(e["link"], "feed", topic_id, url, allowed)
            self.store.set_source_status(src["id"], "fetched", f"feed with {len(doc.feed_entries)} entries",
                                         res.status)
            return IngestOutcome(url, "feed", new_sources=new)
        out = self.store_content(
            src, url=url, final_url=res.final_url, content_type=res.content_type, http_status=res.status, doc=doc,
            query=query, topic_name=topic_name or (topic["name"] if topic else None),
            freshness_days=(topic or {}).get("freshness_days", 365), objective_id=objective_id)
        if out.status in ("stored", "near_duplicate"):
            out.links = [(u, doc.anchors.get(u, "")) for u in doc.links[:300]]
        return out

    def ingest_content(self, *, url: str, title: str, text: str, method: str, domain: str | None = None,
                       published_at: str | None = None, author: str | None = None, query: str = "",
                       topic_name: str | None = None, objective_id: str | None = None,
                       content_type: str = "text/plain") -> IngestOutcome:
        """Store content obtained from an official API (e.g. a paper abstract) with the
        same dedupe, injection screening, extraction and scoring as fetched pages."""
        src = self.store.upsert_source(url, domain or urlsplit(url).hostname or "", method, None)
        if src["status"] in ("fetched", "duplicate"):
            return IngestOutcome(url, "skipped", "already processed")
        doc = ParsedDocument(title=title[:500], text=text, author=author, published_at=published_at, kind="api")
        return self.store_content(src, url=url, final_url=url, content_type=content_type, http_status=200, doc=doc,
                                  query=query, topic_name=topic_name, freshness_days=3650,
                                  objective_id=objective_id, domain=domain)

    def store_content(self, src: dict, *, url: str, final_url: str, content_type: str, http_status: int | None,
                      doc: ParsedDocument, query: str, topic_name: str | None, freshness_days: int,
                      objective_id: str | None, domain: str | None = None) -> IngestOutcome:
        host = urlsplit(url).hostname or ""
        words = len(doc.text.split())
        if words < MIN_WORDS:
            self.store.set_source_status(src["id"], "rejected", f"too little readable text ({words} words)",
                                         http_status)
            return IngestOutcome(url, "rejected", "too little readable text")

        chash = dd.content_hash(doc.text)
        dup = self.store.document_by_hash(chash)
        if dup:
            self.store.set_source_status(src["id"], "duplicate", f"exact duplicate of document {dup['id']}",
                                         http_status)
            return IngestOutcome(url, "duplicate", f"duplicate of {dup['url']}", document_id=dup["id"])

        sh = dd.simhash(doc.text)
        near = next((i for i, h, _dom in self.store.recent_simhashes() if dd.is_near_duplicate(sh, h)), None)
        flags = injection.detect(doc.text, doc.hidden_text)
        domain = domain or urlsplit(final_url).hostname or host

        ext = extract_deterministic(doc.title, doc.text, doc.list_items, query)
        if self.settings.research_use_model_extraction and self.s.router.available:
            try:
                ext = extract_with_model(self.s.router, doc.title, doc.text, final_url, query, ext)
            except ModelError as exc:
                log.warning("model extraction unavailable (%s); using deterministic extraction", exc.error_class)

        q = score_source(url=final_url, domain=domain, text=doc.text, published_at=doc.published_at,
                         author=doc.author, primary_domains=self.settings.primary_source_domains,
                         relevance=ext.relevance, injection_flags=flags, freshness_days=freshness_days)
        doc_id = self.store.add_document(
            source_id=src["id"], url=url, final_url=final_url, domain=domain, title=doc.title or ext.subject,
            author=doc.author, published_at=doc.published_at, content_type=content_type, content_hash=chash,
            simhash=f"{sh:016x}", text=doc.text[:400_000], summary=ext.summary, subject=ext.subject,
            quality_score=q.score, quality_breakdown=q.breakdown, injection_flags=flags,
            near_duplicate_of=near, extraction=ext.as_dict())
        self.store.tag_document(doc_id, ([topic_name] if topic_name else []) + ext.keywords[:5])
        self.store.set_source_status(src["id"], "fetched", None, http_status)

        out = IngestOutcome(url, "near_duplicate" if near else "stored", document_id=doc_id, quality=q.score)
        # Suspected injection: keep the document for inspection but do not promote its claims.
        if not flags:
            for c in ext.claims:
                r = self.store.add_claim(c.text, kind=c.kind, origin="source", document_id=doc_id,
                                         passage=c.passage, topic=topic_name,
                                         independent=near is None, primary=q.is_primary)
                if r["action"] == "created":
                    out.claims_created += 1
                elif r["action"] == "evidence_added":
                    out.claims_reinforced += 1
                out.contradictions += len(r["contradicts"])
        else:
            out.reason = "prompt-injection markers found; claims not promoted: " + ", ".join(flags)
        if objective_id:
            self.s.audit.record("research.ingest", objective_id=objective_id, result=f"document {doc_id}")
        return out

    def run_topic(self, topic: dict) -> RunStats:
        stats = RunStats(topic=topic["name"])
        self.discover(topic)
        pending = self.db.query(
            "SELECT url FROM research_sources WHERE topic_id = ? AND (status = 'pending' OR "
            "(status = 'failed' AND attempts < 3)) ORDER BY discovered_at LIMIT ?",
            (topic["id"], topic["max_docs_per_run"]))
        for row in pending:
            stats.add(self.ingest_url(row["url"], topic=topic, method="queued"))
        self.mark_stale(topic)
        return stats

    def mark_stale(self, topic: dict) -> int:
        """Flag documents older than the topic's freshness window; claims whose every
        supporting document is stale become 'outdated' (history retained)."""
        cutoff = now_iso(timedelta(days=-int(topic.get("freshness_days") or 365)))
        cur = self.db.execute(
            "UPDATE documents SET stale = 1 WHERE stale = 0 AND COALESCE(published_at, retrieved_at) < ? AND id IN "
            "(SELECT dt.document_id FROM document_topics dt JOIN topics t ON t.id = dt.topic_id WHERE t.name = ?)",
            (cutoff, topic["name"].lower()))
        rows = self.db.query(
            "SELECT c.id FROM claims c WHERE c.status = 'active' AND c.origin = 'source' AND c.topic = ? AND NOT EXISTS "
            "(SELECT 1 FROM claim_evidence e JOIN documents d ON d.id = e.document_id "
            " WHERE e.claim_id = c.id AND d.stale = 0)", (topic["name"],))
        for r in rows:
            self.store.mark_outdated(r["id"], "all supporting sources are older than the freshness window")
        return cur.rowcount
