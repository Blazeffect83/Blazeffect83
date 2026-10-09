"""Pre-task memory retrieval with references to the underlying records, so the
model can tell established evidence from speculation."""
from __future__ import annotations

from ..db import Database
from .search import search_claims, search_documents, search_experiments


def retrieve_context(db: Database, query: str, *, limit: int = 6) -> dict:
    claims = search_claims(db, query, limit=limit * 2)
    docs = search_documents(db, query, limit=limit)
    exps = search_experiments(db, query, limit=limit)
    prefs = db.kv_get("user_preferences", [])
    return {
        "established_claims": [
            {"ref": f"claim:{c['id']}", "text": c["text"], "confidence": c["confidence"],
             "corroboration": c["corroboration"], "status": c["status"], "origin": c["origin"]}
            for c in claims if c["status"] in ("active", "contested")
        ][:limit],
        "documents": [
            {"ref": f"document:{d['id']}", "title": d["title"], "url": d["final_url"],
             "quality": d["quality_score"], "summary": (d.get("summary") or "")[:400]}
            for d in docs
        ],
        "previous_attempts": [
            {"ref": f"experiment:{e['id']}", "context": e["context"][:300], "procedure": e["procedure"][:300],
             "outcome": e["outcome"], "lesson": e["lesson"][:400]}
            for e in exps
        ],
        "known_failures": [
            {"ref": f"experiment:{e['id']}", "procedure": e["procedure"][:300], "error_class": e["error_class"],
             "lesson": e["lesson"][:400]}
            for e in exps if e["outcome"] == "failure"
        ],
        "user_preferences": prefs,
    }


def format_context(ctx: dict, max_chars: int = 6000) -> str:
    lines = ["Relevant memory (refs point to stored records; 'origin=agent' entries are prior inferences, "
             "not verified facts):"]
    for c in ctx["established_claims"]:
        lines.append(f"- [{c['ref']}] ({c['confidence']} confidence, {c['corroboration']} sources, "
                     f"{c['status']}, origin={c['origin']}) {c['text']}")
    for d in ctx["documents"]:
        lines.append(f"- [{d['ref']}] {d['title']} <{d['url']}> quality={d['quality']}: {d['summary']}")
    for e in ctx["previous_attempts"]:
        lines.append(f"- [{e['ref']}] previous {e['outcome']}: {e['procedure']} — lesson: {e['lesson']}")
    if ctx["user_preferences"]:
        lines.append(f"- user preferences: {ctx['user_preferences']}")
    if len(lines) == 1:
        lines.append("- (no relevant stored knowledge)")
    out = "\n".join(lines)
    return out[:max_chars]
