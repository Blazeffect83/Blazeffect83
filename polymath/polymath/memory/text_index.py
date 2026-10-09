"""Passage index: documents are cut into paragraph-sized chunks indexed by FTS5.

The FTS table is contentless; chunk text is sliced from the (compressed) document
body on demand, so the text is stored exactly once.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from polymath.core.db import Database
from polymath.memory.documents import DocumentStore
from polymath.perception.stem import stem

CHUNK_TARGET = 900
CHUNK_MAX = 1600
VAGUE_DF_SHARE = 0.25  # a query whose rarest word is in a quarter of all passages says nothing specific
MAX_DF_SHARE = 0.03  # terms in more than 3% of passages carry little evidence and are slow to rank
_TERM = re.compile(r"[^\W_]+(?:['’][^\W_]+)?", re.U)
_PARA = re.compile(r"\n\s*\n")


def chunk_spans(text: str, target: int = CHUNK_TARGET, hard_max: int = CHUNK_MAX) -> list[tuple[int, int]]:
    """Paragraph-aligned spans of roughly ``target`` characters (long paragraphs split at sentences)."""
    spans: list[tuple[int, int]] = []
    paras: list[tuple[int, int]] = []
    pos = 0
    for m in _PARA.finditer(text):
        if m.start() > pos:
            paras.append((pos, m.start()))
        pos = m.end()
    if pos < len(text):
        paras.append((pos, len(text)))
    for a, b in paras:  # split oversized paragraphs at sentence ends
        while b - a > hard_max:
            cut = text.rfind(". ", a, a + hard_max)
            if cut > a + target // 2:
                cut += 1
            else:
                space = text.rfind(" ", a + target // 2, a + hard_max)
                cut = space if space > a else a + hard_max
            spans.append((a, cut))
            a = cut
            while a < b and text[a] == " ":
                a += 1
        if b > a:
            spans.append((a, b))
    merged: list[tuple[int, int]] = []
    for a, b in spans:
        if merged and (b - merged[-1][0]) <= target and text[merged[-1][1] : a].strip() == "":
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    return merged


def fts_query(text: str, *, mode: str = "or", max_terms: int = 24) -> str:
    """Safe FTS5 MATCH expression from free text (every term quoted; no operator injection)."""
    terms = []
    seen = set()
    for t in _TERM.findall(text.lower()):
        t = t.replace("'", "").replace("’", "")
        if t and t not in seen:
            seen.add(t)
            terms.append(f'"{t}"')
        if len(terms) >= max_terms:
            break
    joiner = " AND " if mode == "and" else " OR "
    return joiner.join(terms)


@dataclass
class Hit:
    chunk_id: int
    doc_id: int
    score: float  # bm25: lower is better; negated so that higher is better
    title: str
    text: str
    url: str | None
    source: str
    license: str


class TextIndex:
    def __init__(self, db: Database, store: DocumentStore | None = None) -> None:
        self.db = db
        self.store = store or DocumentStore(db)
        self._total = (0.0, 0)

    def total_chunks(self) -> int:
        at, n = self._total
        if time.monotonic() - at > 60 or n == 0:
            n = int(self.db.scalar("SELECT MAX(id) FROM chunks", default=0))
            self._total = (time.monotonic(), n)
        return n

    def plan_terms(self, text: str, *, max_terms: int = 24) -> list[str]:
        return self.plan(text, max_terms=max_terms)[0]

    def plan(self, text: str, *, max_terms: int = 24) -> tuple[list[str], bool]:
        """Query words worth matching: unknown words dropped, near-stopwords pruned (rarest kept)."""
        words: list[str] = []
        for t in _TERM.findall(text.lower()):
            t = t.replace("'", "").replace("’", "")
            if t and t not in words:
                words.append(t)
            if len(words) >= max_terms:
                break
        if not words:
            return [], False
        stems = {w: stem(w) for w in words}
        uniq = sorted(set(stems.values()))
        df = {
            r["term"]: int(r["doc"])
            for r in self.db.query(
                f"SELECT term, doc FROM chunk_vocab WHERE term IN ({','.join('?' * len(uniq))})", uniq
            )
        }
        known = [w for w in words if df.get(stems[w], 0) > 0]
        if not known:
            return [], False
        cutoff = max(50, MAX_DF_SHARE * self.total_chunks())
        kept = [w for w in known if df[stems[w]] <= cutoff]
        total = self.total_chunks()
        if total >= 200 and min(df[stems[w]] for w in known) > VAGUE_DF_SHARE * total:
            return [], True  # too vague to search meaningfully
        if not kept:
            # every word is common: intersect the rarest ones instead of ranking their union
            return sorted(known, key=lambda w: df[stems[w]])[:3], True
        return kept, False

    def content_terms(self, text: str) -> set[str]:
        """Stems of the words in ``text`` that are specific: known to the index and in at most a quarter of passages.

        Unlike :meth:`plan` there is no absolute floor, so even a small corpus treats "the" as uninformative.
        """
        stems = {stem(t.replace("'", "").replace("’", "")) for t in _TERM.findall(text.lower())}
        stems.discard("")
        if not stems:
            return set()
        total = max(1, self.total_chunks())
        rows = self.db.query(
            f"SELECT term, doc FROM chunk_vocab WHERE term IN ({','.join('?' * len(stems))})", sorted(stems)
        )
        return {str(r["term"]) for r in rows if int(r["doc"]) <= VAGUE_DF_SHARE * total or total < 8}

    def index(self, doc_id: int, title: str, text: str) -> int:
        spans = chunk_spans(text)
        for ord_, (a, b) in enumerate(spans):
            cur = self.db.execute("INSERT INTO chunks(doc_id, ord, start, end) VALUES(?,?,?,?)", (doc_id, ord_, a, b))
            self.db.execute(
                "INSERT INTO chunk_fts(rowid, title, body) VALUES(?,?,?)", (cur.lastrowid, title, text[a:b])
            )
        return len(spans)

    def unindex(self, doc_id: int, title: str, text: str) -> None:
        for row in self.db.query("SELECT id, start, end FROM chunks WHERE doc_id=?", (doc_id,)):
            self.db.execute(
                "INSERT INTO chunk_fts(chunk_fts, rowid, title, body) VALUES('delete', ?, ?, ?)",
                (row["id"], title, text[row["start"] : row["end"]]),
            )
        self.db.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))

    def search(self, query: str, *, limit: int = 10, mode: str = "or", sources: list[str] | None = None) -> list[Hit]:
        terms, common = self.plan(query)
        if common and mode == "or":
            hits = self.search(" ".join(terms), limit=limit, mode="and", sources=sources)
            if len(hits) >= limit or len(terms) < 2:
                return hits
            terms = terms[:2]
        expr = fts_query(" ".join(terms), mode=mode)
        if not expr:
            return []
        sql = (
            "SELECT c.id, c.doc_id, c.start, c.end, bm25(chunk_fts, 2.0, 1.0) AS score, d.title, d.url, d.source, "
            "d.license FROM chunk_fts JOIN chunks c ON c.id = chunk_fts.rowid JOIN documents d ON d.id = c.doc_id "
            "WHERE chunk_fts MATCH ?"
        )
        params: list[object] = [expr]
        if sources:
            sql += f" AND d.source IN ({','.join('?' * len(sources))})"
            params += sources
        sql += " ORDER BY score LIMIT ?"
        params.append(limit)
        rows = self.db.query(sql, params)
        texts: dict[int, str] = {}
        hits = []
        for r in rows:
            doc_id = int(r["doc_id"])
            if doc_id not in texts:
                doc = self.store.get(doc_id)
                texts[doc_id] = doc.text if doc else ""
            hits.append(
                Hit(
                    int(r["id"]),
                    doc_id,
                    -float(r["score"]),
                    r["title"],
                    texts[doc_id][r["start"] : r["end"]],
                    r["url"],
                    r["source"],
                    r["license"],
                )
            )
        return hits

    def search_docs(self, query: str, *, limit: int = 10) -> list[tuple[int, float]]:
        """Document-level ranking: best chunk score per document."""
        expr = fts_query(" ".join(self.plan_terms(query)))
        if not expr:
            return []
        rows = self.db.query(
            "SELECT doc_id, MIN(score) AS best FROM (SELECT c.doc_id, bm25(chunk_fts, 2.0, 1.0) AS score "
            "FROM chunk_fts JOIN chunks c ON c.id = chunk_fts.rowid WHERE chunk_fts MATCH ? ORDER BY score "
            "LIMIT ?) GROUP BY doc_id ORDER BY best LIMIT ?",
            (expr, limit * 20, limit),
        )
        return [(int(r["doc_id"]), -float(r["best"])) for r in rows]
