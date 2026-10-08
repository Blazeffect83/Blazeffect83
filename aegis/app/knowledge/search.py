"""FTS5 keyword search over documents, claims and experiments."""
from __future__ import annotations

import re

from ..db import Database
from ..research.deduplicator import tokens

_TERM = re.compile(r"[A-Za-z0-9]+")


def fts_query(text: str, mode: str = "and", max_terms: int = 16) -> str:
    """Build a safe FTS5 MATCH expression (every term quoted: no operator injection)."""
    terms = [t for t in tokens(text) if len(t) > 1][:max_terms]
    if not terms:
        terms = [t.lower() for t in _TERM.findall(text)][:max_terms]
    if not terms:
        return ""
    quoted = [f'"{t}"' for t in dict.fromkeys(terms)]
    return (" OR " if mode == "or" else " ").join(quoted)


def search_documents(db: Database, query: str, *, topic: str | None = None, domain: str | None = None,
                     since: str | None = None, min_quality: float | None = None, limit: int = 20) -> list[dict]:
    where, params = [], []
    if topic:
        where.append("d.id IN (SELECT dt.document_id FROM document_topics dt JOIN topics t ON t.id = dt.topic_id "
                     "WHERE t.name = ?)")
        params.append(topic.strip().lower())
    if domain:
        where.append("(d.domain = ? OR d.domain LIKE ?)")
        params += [domain, f"%.{domain}"]
    if since:
        where.append("d.retrieved_at >= ?")
        params.append(since)
    if min_quality is not None:
        where.append("d.quality_score >= ?")
        params.append(min_quality)
    cols = ("d.id, d.url, d.final_url, d.domain, d.title, d.summary, d.quality_score, d.published_at, "
            "d.retrieved_at, d.injection_flags, d.stale")
    extra = (" AND " + " AND ".join(where)) if where else ""
    if not query.strip():
        return db.query(f"SELECT {cols} FROM documents d WHERE 1=1{extra} ORDER BY d.id DESC LIMIT ?",
                        (*params, limit))
    for mode in ("and", "or"):
        q = fts_query(query, mode)
        if not q:
            return []
        rows = db.query(
            f"SELECT {cols}, snippet(documents_fts, 2, '[', ']', ' … ', 24) AS snippet, "
            f"bm25(documents_fts) AS rank FROM documents_fts JOIN documents d ON d.id = documents_fts.rowid "
            f"WHERE documents_fts MATCH ?{extra} ORDER BY rank LIMIT ?", (q, *params, limit))
        if rows:
            return rows
    return []


def search_claims(db: Database, query: str, *, status: str | None = None, limit: int = 20) -> list[dict]:
    extra, params = "", []
    if status:
        extra, params = " AND c.status = ?", [status]
    for mode in ("and", "or"):
        q = fts_query(query, mode)
        if not q:
            return []
        rows = db.query(
            "SELECT c.*, bm25(claims_fts) AS rank FROM claims_fts JOIN claims c ON c.id = claims_fts.rowid "
            f"WHERE claims_fts MATCH ?{extra} ORDER BY rank LIMIT ?", (q, *params, limit))
        if rows:
            return rows
    return []


def search_experiments(db: Database, query: str, limit: int = 10) -> list[dict]:
    q = fts_query(query, "or")
    if not q:
        return []
    return db.query(
        "SELECT e.*, bm25(experiments_fts) AS rank FROM experiments_fts JOIN experiments e "
        "ON e.id = experiments_fts.rowid WHERE experiments_fts MATCH ? ORDER BY rank LIMIT ?", (q, limit))


def related_documents(db: Database, document_id: int, limit: int = 8) -> list[dict]:
    doc = db.one("SELECT title, summary FROM documents WHERE id = ?", (document_id,))
    if not doc:
        return []
    q = fts_query(f"{doc['title']} {doc['summary']}", "or", max_terms=12)
    if not q:
        return []
    return db.query(
        "SELECT d.id, d.title, d.url, d.domain, d.quality_score FROM documents_fts JOIN documents d "
        "ON d.id = documents_fts.rowid WHERE documents_fts MATCH ? AND d.id != ? "
        "ORDER BY bm25(documents_fts) LIMIT ?", (q, document_id, limit))
