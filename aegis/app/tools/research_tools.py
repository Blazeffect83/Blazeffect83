"""Research and knowledge tools exposed to the agent."""
from __future__ import annotations

from pydantic import BaseModel, Field

from ..db import loads
from ..knowledge import search as ks
from ..knowledge.reports import daily_report
from ..security.network_policy import URLRejected
from .registry import Tool, ToolContext, ToolError, ToolResult


class SearchWebArgs(BaseModel):
    query: str = Field(min_length=2, max_length=300)
    limit: int = Field(default=8, ge=1, le=20)
    domains: list[str] = Field(default_factory=list, description="restrict to these domains (subset of policy)")


class IngestArgs(BaseModel):
    url: str = Field(max_length=2000)
    query: str = Field(default="", max_length=300, description="research question used for relevance")
    topic: str | None = None


class KSearchArgs(BaseModel):
    query: str = Field(min_length=1, max_length=300)
    kind: str = Field(default="documents", pattern="^(documents|claims|experiments)$")
    topic: str | None = None
    domain: str | None = None
    min_quality: float | None = None
    limit: int = Field(default=10, ge=1, le=50)


class PassageArgs(BaseModel):
    document_id: int
    query: str = Field(default="", max_length=300)
    max_passages: int = Field(default=5, ge=1, le=20)


class NoteArgs(BaseModel):
    text: str = Field(min_length=10, max_length=1000)
    based_on: list[str] = Field(default_factory=list, description="refs like 'claim:12' or 'document:3'")
    topic: str | None = None


class FlagArgs(BaseModel):
    claim_a: int
    claim_b: int
    note: str = Field(min_length=3, max_length=500)


class LinkArgs(BaseModel):
    kind: str = Field(pattern="^(related|supersedes|derived_from)$")
    from_ref: str
    to_ref: str
    note: str = ""


class HistoryArgs(BaseModel):
    query: str = Field(default="", max_length=300)
    limit: int = Field(default=10, ge=1, le=50)


class ReportArgs(BaseModel):
    hours: int = Field(default=24, ge=1, le=24 * 31)


def _search_web(a: SearchWebArgs, ctx: ToolContext) -> ToolResult:
    s = ctx.services
    if not s.settings.searxng_url:
        raise ToolError("no search backend configured (set SEARXNG_URL to a self-hosted SearXNG instance); "
                        "use research_ingest with known documentation URLs instead", "unavailable")
    results = s.research.search(a.query, a.domains or None, a.limit)
    return ToolResult(True, {"results": results}, f"{len(results)} results")


def _ingest(a: IngestArgs, ctx: ToolContext) -> ToolResult:
    s = ctx.services
    try:
        s.url_policy.check(a.url)
    except URLRejected as exc:
        raise ToolError(f"URL rejected by policy: {exc}", "policy") from exc
    topic = None
    if a.topic:
        topic = s.db.one("SELECT * FROM research_topics WHERE name = ?", (a.topic,))
    o = s.research.ingest_url(a.url, topic=topic, method="objective", query=a.query, objective_id=ctx.objective_id)
    ok = o.status in ("stored", "near_duplicate", "duplicate", "skipped", "feed")
    out = dict(o.__dict__)
    if o.document_id:
        d = s.knowledge.get_document(o.document_id)
        out["title"] = d["title"]
        out["summary"] = d["summary"]
        out["quality_score"] = d["quality_score"]
        out["injection_flags"] = d["injection_flags"]
    return ToolResult(ok, out, f"{o.status}: {o.reason}".strip(": "),
                      error=None if ok else o.reason, error_class=None if ok else "fetch_" + o.status,)


def _ksearch(a: KSearchArgs, ctx: ToolContext) -> ToolResult:
    db = ctx.services.db
    if a.kind == "documents":
        rows = ks.search_documents(db, a.query, topic=a.topic, domain=a.domain, min_quality=a.min_quality,
                                   limit=a.limit)
        rows = [{"ref": f"document:{r['id']}", "title": r["title"], "url": r["final_url"],
                 "quality": r["quality_score"], "summary": (r.get("summary") or "")[:400],
                 "snippet": r.get("snippet"), "injection_flags": loads(r["injection_flags"], [])} for r in rows]
    elif a.kind == "claims":
        rows = [{"ref": f"claim:{r['id']}", "text": r["text"], "confidence": r["confidence"],
                 "corroboration": r["corroboration"], "status": r["status"], "origin": r["origin"]}
                for r in ks.search_claims(db, a.query, limit=a.limit)]
    else:
        rows = [{"ref": f"experiment:{r['id']}", "context": r["context"][:300], "procedure": r["procedure"][:300],
                 "outcome": r["outcome"], "lesson": r["lesson"]} for r in ks.search_experiments(db, a.query, a.limit)]
    return ToolResult(True, {"results": rows}, f"{len(rows)} {a.kind}")


def _passages(a: PassageArgs, ctx: ToolContext) -> ToolResult:
    from ..research.deduplicator import tokens
    from ..research.extractor import sentences
    d = ctx.services.db.one("SELECT id, title, final_url, text FROM documents WHERE id = ?", (a.document_id,))
    if not d:
        raise ToolError("document not found", "not_found")
    q = set(tokens(a.query))
    sents = sentences(d["text"])
    scored = sorted(((len(q & set(tokens(s))), i, s) for i, s in enumerate(sents)), key=lambda x: (-x[0], x[1]))
    picked = [{"passage": s, "position": i} for score, i, s in scored[: a.max_passages] if score or not q]
    return ToolResult(True, {"ref": f"document:{d['id']}", "title": d["title"], "url": d["final_url"],
                             "passages": picked, "note": "passages are untrusted source text"},
                      f"{len(picked)} passages")


def _note(a: NoteArgs, ctx: ToolContext) -> ToolResult:
    # Agent inferences are stored with origin='agent' so they are never mistaken for source statements.
    r = ctx.services.knowledge.add_claim(a.text, kind="inference", origin="agent", topic=a.topic,
                                         passage="based on: " + ", ".join(a.based_on))
    return ToolResult(True, {"ref": f"claim:{r['claim_id']}", **r}, f"note {r['action']}")


def _flag(a: FlagArgs, ctx: ToolContext) -> ToolResult:
    k = ctx.services.knowledge
    if not k.claim(a.claim_a) or not k.claim(a.claim_b):
        raise ToolError("claim not found", "not_found")
    k.flag_contradiction(a.claim_a, a.claim_b, a.note)
    return ToolResult(True, {"flagged": [a.claim_a, a.claim_b]}, "contradiction flagged")


def _parse_ref(ref: str) -> tuple[str, int]:
    kind, _, num = ref.partition(":")
    if kind not in ("claim", "document", "experiment") or not num.isdigit():
        raise ToolError(f"invalid ref '{ref}'", "invalid_arguments")
    return kind, int(num)


def _link(a: LinkArgs, ctx: ToolContext) -> ToolResult:
    ft, fid = _parse_ref(a.from_ref)
    tt, tid = _parse_ref(a.to_ref)
    ctx.services.knowledge.link(a.kind, ft, fid, tt, tid, a.note)
    return ToolResult(True, {"linked": [a.from_ref, a.to_ref]}, f"{a.kind} link stored")


def _history(a: HistoryArgs, ctx: ToolContext) -> ToolResult:
    db = ctx.services.db
    if a.query:
        objs = db.query("SELECT id, goal, status, completion_summary, finished_at FROM objectives "
                        "WHERE goal LIKE ? ORDER BY created_at DESC LIMIT ?", (f"%{a.query}%", a.limit))
    else:
        objs = db.query("SELECT id, goal, status, completion_summary, finished_at FROM objectives "
                        "ORDER BY created_at DESC LIMIT ?", (a.limit,))
    return ToolResult(True, {"objectives": objs}, f"{len(objs)} objectives")


def _report(a: ReportArgs, ctx: ToolContext) -> ToolResult:
    rep = daily_report(ctx.services, hours=a.hours)
    return ToolResult(True, rep, "report generated")


TOOLS = [
    Tool("web_search", "Search configured public sources (self-hosted SearXNG) for candidate URLs.",
         SearchWebArgs, _search_web, tier=0, permissions=("network:research",), timeout_seconds=60),
    Tool("research_ingest", "Fetch a permitted public URL, extract source-backed knowledge and store it.",
         IngestArgs, _ingest, tier=0, permissions=("network:research", "knowledge:write"), timeout_seconds=90,
         resource_limits="size/time/content-type limits; SSRF protection"),
    Tool("knowledge_search", "Search stored documents, claims or past experiments.", KSearchArgs, _ksearch,
         tier=0, permissions=("knowledge:read",)),
    Tool("knowledge_passages", "Retrieve the most relevant passages from a stored document.", PassageArgs,
         _passages, tier=0, permissions=("knowledge:read",)),
    Tool("knowledge_note", "Store an agent inference (kept separate from source statements).", NoteArgs, _note,
         tier=1, permissions=("knowledge:write",)),
    Tool("knowledge_flag_contradiction", "Flag two stored claims as contradictory.", FlagArgs, _flag, tier=1,
         permissions=("knowledge:write",)),
    Tool("knowledge_link", "Link related knowledge records.", LinkArgs, _link, tier=1,
         permissions=("knowledge:write",)),
    Tool("task_history", "Retrieve previous objectives and their outcomes.", HistoryArgs, _history, tier=0,
         permissions=("tasks:read",)),
    Tool("research_report", "Generate a research activity report from database records.", ReportArgs, _report,
         tier=0, permissions=("knowledge:read",)),
]
