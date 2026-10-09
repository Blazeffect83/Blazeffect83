"""Knowledge store: documents, source-backed claims, evidence, contradictions,
claim history, topics and experiments.

Rules enforced here:
* duplicate pages never count as independent corroboration
* conflicting evidence is flagged (``contradicts`` relationship, ``contested``
  status) — never silently overwritten
* every status/confidence change is written to ``claim_history``
* agent inferences are stored with ``origin='agent'`` and kept distinct from
  source statements
"""
from __future__ import annotations

import re

from ..db import Database, dumps, loads, now_iso
from ..research.deduplicator import claim_key, jaccard, tokens
from .search import fts_query

_NEGATIONS = {"not", "no", "never", "cannot", "can't", "doesn't", "don't", "isn't", "aren't", "won't",
              "unsupported", "without", "neither", "nor", "lacks", "fails"}


def _negated(text: str) -> bool:
    words = set(re.findall(r"[a-z']+", text.lower()))
    return bool(words & _NEGATIONS)


_QUANT = re.compile(r"\b(\d+(?:\.\d+)?)\s*(%|°\s?c|[a-z]+(?:/[a-z]+)?)?", re.I)
_YEAR = re.compile(r"^(1[89]|20)\d\d$")


def _quantities(text: str) -> dict[str, set[str]]:
    """unit -> numbers. Years and bare numbers (no unit) are ignored: they usually
    identify things (model numbers, dates) rather than measure them."""
    out: dict[str, set[str]] = {}
    for num, unit in _QUANT.findall(text):
        if not unit or _YEAR.match(num):
            continue
        unit = unit.lower().replace(" ", "")
        if unit in _NEGATIONS or len(unit) > 12:
            continue
        out.setdefault(unit, set()).add(num)
    return out


def conflict_signal(a: str, b: str) -> str | None:
    """Heuristic contradiction detector for two similar claims."""
    if _negated(a) != _negated(b):
        return "negation differs"
    qa, qb = _quantities(a), _quantities(b)
    shared = [u for u in qa if u in qb and qa[u] != qb[u]]
    if shared:
        ta = {t for t in tokens(a) if not t.isdigit()}
        tb = {t for t in tokens(b) if not t.isdigit()}
        if ta and tb and len(ta & tb) / len(ta | tb) >= 0.6:
            u = shared[0]
            return f"values differ ({', '.join(sorted(qa[u]))} vs {', '.join(sorted(qb[u]))} {u})"
    return None


class KnowledgeStore:
    def __init__(self, db: Database):
        self.db = db

    # -- topics -------------------------------------------------------------
    def topic_id(self, name: str) -> int:
        name = name.strip().lower()[:100]
        row = self.db.one("SELECT id FROM topics WHERE name = ?", (name,))
        if row:
            return row["id"]
        self.db.execute("INSERT OR IGNORE INTO topics(name) VALUES (?)", (name,))
        return self.db.scalar("SELECT id FROM topics WHERE name = ?", (name,))

    def tag_document(self, document_id: int, topics: list[str]) -> None:
        for t in topics:
            if t and t.strip():
                self.db.execute("INSERT OR IGNORE INTO document_topics VALUES (?, ?)",
                                (document_id, self.topic_id(t)))

    # -- sources & documents ------------------------------------------------
    def upsert_source(self, url: str, domain: str, method: str, topic_id: int | None = None,
                      discovered_from: str | None = None) -> dict:
        self.db.execute(
            "INSERT OR IGNORE INTO research_sources(url, domain, topic_id, discovery_method, discovered_from, "
            "status, discovered_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (url, domain, topic_id, method, discovered_from, now_iso()))
        return self.db.one("SELECT * FROM research_sources WHERE url = ?", (url,))

    def set_source_status(self, source_id: int, status: str, reason: str | None = None,
                          http_status: int | None = None) -> None:
        self.db.execute(
            "UPDATE research_sources SET status = ?, status_reason = ?, http_status = COALESCE(?, http_status), "
            "attempts = attempts + 1, fetched_at = ? WHERE id = ?",
            (status, reason, http_status, now_iso(), source_id))

    def document_by_hash(self, h: str) -> dict | None:
        return self.db.one("SELECT id, url FROM documents WHERE content_hash = ?", (h,))

    def recent_simhashes(self, limit: int = 5000) -> list[tuple[int, int, str]]:
        rows = self.db.query("SELECT id, simhash, domain FROM documents ORDER BY id DESC LIMIT ?", (limit,))
        return [(r["id"], int(r["simhash"], 16), r["domain"]) for r in rows]

    def add_document(self, **fields) -> int:
        for k in ("quality_breakdown", "injection_flags", "extraction"):
            if k in fields and not isinstance(fields[k], str):
                fields[k] = dumps(fields[k])
        fields.setdefault("retrieved_at", now_iso())
        return self.db.insert("documents", fields)

    def get_document(self, document_id: int) -> dict | None:
        d = self.db.one("SELECT * FROM documents WHERE id = ?", (document_id,))
        if d:
            for k, default in (("quality_breakdown", {}), ("injection_flags", []), ("extraction", {})):
                d[k] = loads(d[k], default)
            d["topics"] = [r["name"] for r in self.db.query(
                "SELECT t.name FROM topics t JOIN document_topics dt ON dt.topic_id = t.id WHERE dt.document_id = ?",
                (document_id,))]
            d["claims"] = self.db.query(
                "SELECT c.*, e.passage, e.stance FROM claims c JOIN claim_evidence e ON e.claim_id = c.id "
                "WHERE e.document_id = ? ORDER BY c.id", (document_id,))
        return d

    # -- claims -------------------------------------------------------------
    def _history(self, claim_id: int, field: str, old, new, reason: str) -> None:
        if old == new:
            return
        self.db.insert("claim_history", {"claim_id": claim_id, "field": field, "old_value": str(old),
                                         "new_value": str(new), "reason": reason, "created_at": now_iso()})

    def _set_claim(self, claim_id: int, reason: str, **values) -> None:
        cur = self.db.one("SELECT * FROM claims WHERE id = ?", (claim_id,))
        for k, v in values.items():
            self._history(claim_id, k, cur[k], v, reason)
        values["updated_at"] = now_iso()
        self.db.update("claims", "id", claim_id, values)

    def _evidence_domains(self, claim_id: int) -> set[str]:
        rows = self.db.query(
            "SELECT DISTINCT d.domain FROM claim_evidence e JOIN documents d ON d.id = e.document_id "
            "WHERE e.claim_id = ? AND e.stance = 'supports' AND e.independent = 1", (claim_id,))
        return {r["domain"] for r in rows}

    def _recompute(self, claim_id: int, reason: str, primary: bool = False) -> None:
        domains = self._evidence_domains(claim_id)
        exp = int(self.db.scalar(
            "SELECT COUNT(*) FROM claim_evidence WHERE claim_id = ? AND experiment_id IS NOT NULL "
            "AND stance = 'supports'", (claim_id,)) or 0)
        corroboration = len(domains) + exp
        has_primary = primary or bool(self.db.scalar(
            "SELECT 1 FROM claim_evidence e JOIN documents d ON d.id = e.document_id WHERE e.claim_id = ? "
            "AND e.stance = 'supports' AND json_extract(d.quality_breakdown, '$.primary_source') IS NOT NULL",
            (claim_id,)))
        if corroboration >= 3 or (has_primary and corroboration >= 2) or exp >= 1 and corroboration >= 2:
            conf = "high"
        elif corroboration >= 2 or has_primary or exp:
            conf = "medium"
        else:
            conf = "low"
        claim = self.db.one("SELECT status FROM claims WHERE id = ?", (claim_id,))
        values = {"corroboration": corroboration, "confidence": conf}
        if claim["status"] == "outdated":
            values["status"] = "active"
        self._set_claim(claim_id, reason, **values)

    def _add_evidence(self, claim_id: int, *, document_id: int | None = None, experiment_id: int | None = None,
                      stance: str = "supports", passage: str = "", independent: bool = True) -> bool:
        exists = self.db.one(
            "SELECT id FROM claim_evidence WHERE claim_id = ? AND document_id IS ? AND experiment_id IS ?",
            (claim_id, document_id, experiment_id))
        if exists:
            return False
        if document_id is not None and independent:
            dom = self.db.scalar("SELECT domain FROM documents WHERE id = ?", (document_id,))
            # Same publisher (domain) already supports it → not independent.
            if dom in self._evidence_domains(claim_id):
                independent = False
        self.db.insert("claim_evidence", {
            "claim_id": claim_id, "document_id": document_id, "experiment_id": experiment_id, "stance": stance,
            "passage": passage[:2000], "independent": int(independent), "created_at": now_iso()})
        return True

    def similar_claims(self, text: str, limit: int = 10) -> list[dict]:
        q = fts_query(text, mode="or", max_terms=12)
        if not q:
            return []
        return self.db.query(
            "SELECT c.* FROM claims_fts f JOIN claims c ON c.id = f.rowid WHERE claims_fts MATCH ? "
            "ORDER BY bm25(claims_fts) LIMIT ?", (q, limit))

    def add_claim(self, text: str, *, kind: str = "statement", origin: str = "source",
                  document_id: int | None = None, experiment_id: int | None = None, passage: str = "",
                  topic: str | None = None, independent: bool = True, primary: bool = False) -> dict:
        """Add or reinforce a claim. Returns {'claim_id', 'action', 'contradicts'}."""
        text = " ".join(text.split())[:1000]
        key = claim_key(text)
        existing = self.db.one("SELECT * FROM claims WHERE normalized_hash = ?", (key,))
        contradicts: list[int] = []
        if existing is None:
            for cand in self.similar_claims(text):
                sim = jaccard(text, cand["text"])
                signal = conflict_signal(text, cand["text"]) if sim >= 0.45 else None
                if signal and document_id is not None:
                    cand_docs = {r["document_id"] for r in self.db.query(
                        "SELECT document_id FROM claim_evidence WHERE claim_id = ?", (cand["id"],))}
                    if cand_docs == {document_id}:
                        # Two values in the same document (e.g. WHO vs ADA thresholds listed side by
                        # side) are a comparison, not a disagreement between sources.
                        signal = None
                if signal:
                    contradicts.append((cand["id"], signal))
                elif sim >= 0.8 and cand["origin"] == origin:
                    existing = cand
                    break
        if existing is not None:
            added = self._add_evidence(existing["id"], document_id=document_id, experiment_id=experiment_id,
                                       passage=passage, independent=independent)
            if added:
                self._recompute(existing["id"], "additional evidence", primary)
            return {"claim_id": existing["id"], "action": "evidence_added" if added else "unchanged",
                    "contradicts": []}
        ts = now_iso()
        cid = self.db.insert("claims", {
            "text": text, "normalized_hash": key, "kind": kind, "origin": origin, "status": "active",
            "confidence": "low", "corroboration": 0, "topic": topic, "created_at": ts, "updated_at": ts})
        self._add_evidence(cid, document_id=document_id, experiment_id=experiment_id, passage=passage,
                           independent=independent)
        self._recompute(cid, "created", primary)
        for other_id, signal in contradicts:
            self.flag_contradiction(cid, other_id, signal)
        return {"claim_id": cid, "action": "created", "contradicts": [c for c, _ in contradicts]}

    def flag_contradiction(self, claim_a: int, claim_b: int, note: str) -> None:
        a, b = sorted((claim_a, claim_b))
        self.db.execute(
            "INSERT OR IGNORE INTO relationships(kind, from_type, from_id, to_type, to_id, note, created_at) "
            "VALUES ('contradicts', 'claim', ?, 'claim', ?, ?, ?)", (a, b, note, now_iso()))
        for cid in (a, b):
            row = self.db.one("SELECT status FROM claims WHERE id = ?", (cid,))
            if row and row["status"] == "active":
                self._set_claim(cid, f"contradiction flagged: {note}", status="contested")

    def resolve_contradiction(self, relationship_id: int, keep_claim: int | None, reason: str) -> None:
        rel = self.db.one("SELECT * FROM relationships WHERE id = ?", (relationship_id,))
        if not rel:
            raise KeyError("relationship not found")
        self.db.execute("UPDATE relationships SET resolved = 1, note = COALESCE(note,'') || ? WHERE id = ?",
                        (f" | resolved: {reason}", relationship_id))
        for cid in (rel["from_id"], rel["to_id"]):
            if keep_claim is None:
                self._set_claim(cid, f"contradiction resolved: {reason}", status="active")
            elif cid == keep_claim:
                self._set_claim(cid, f"contradiction resolved: {reason}", status="active")
            else:
                self._set_claim(cid, f"superseded: {reason}", status="outdated")

    def mark_outdated(self, claim_id: int, reason: str) -> None:
        self._set_claim(claim_id, reason, status="outdated")

    def link(self, kind: str, from_type: str, from_id: int, to_type: str, to_id: int, note: str = "") -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO relationships(kind, from_type, from_id, to_type, to_id, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", (kind, from_type, from_id, to_type, to_id, note, now_iso()))

    def claim(self, claim_id: int) -> dict | None:
        c = self.db.one("SELECT * FROM claims WHERE id = ?", (claim_id,))
        if not c:
            return None
        c["evidence"] = self.db.query(
            "SELECT e.*, d.url, d.title, d.domain, d.quality_score FROM claim_evidence e "
            "LEFT JOIN documents d ON d.id = e.document_id WHERE e.claim_id = ? ORDER BY e.id", (claim_id,))
        c["history"] = self.db.query("SELECT * FROM claim_history WHERE claim_id = ? ORDER BY id", (claim_id,))
        c["contradictions"] = self.db.query(
            "SELECT * FROM relationships WHERE kind = 'contradicts' AND from_type = 'claim' "
            "AND (from_id = ? OR to_id = ?)", (claim_id, claim_id))
        return c

    def contradictions(self, include_resolved: bool = False) -> list[dict]:
        rows = self.db.query(
            "SELECT r.*, a.text AS a_text, b.text AS b_text FROM relationships r "
            "JOIN claims a ON a.id = r.from_id JOIN claims b ON b.id = r.to_id "
            "WHERE r.kind = 'contradicts' AND r.from_type = 'claim' "
            + ("" if include_resolved else "AND r.resolved = 0 ") + "ORDER BY r.id DESC LIMIT 200")
        return rows

    # -- experiments (learning from action outcomes) ------------------------
    def record_experiment(self, *, objective_id: str | None, task_id: str | None, context: str,
                          procedure: str, outcome: str, lesson: str = "", error_class: str | None = None) -> int:
        return self.db.insert("experiments", {
            "objective_id": objective_id, "task_id": task_id, "context": context[:2000],
            "procedure": procedure[:2000], "outcome": outcome, "lesson": lesson[:2000],
            "error_class": error_class, "created_at": now_iso()})

    # -- export / deletion --------------------------------------------------
    def export(self) -> dict:
        return {
            "exported_at": now_iso(),
            "documents": self.db.query(
                "SELECT id, url, final_url, domain, title, published_at, summary, quality_score, retrieved_at "
                "FROM documents ORDER BY id"),
            "claims": self.db.query("SELECT * FROM claims ORDER BY id"),
            "claim_evidence": self.db.query("SELECT * FROM claim_evidence ORDER BY id"),
            "claim_history": self.db.query("SELECT * FROM claim_history ORDER BY id"),
            "relationships": self.db.query("SELECT * FROM relationships ORDER BY id"),
            "experiments": self.db.query("SELECT * FROM experiments ORDER BY id"),
        }

    def delete_document(self, document_id: int) -> None:
        with self.db.transaction() as c:
            c.execute("DELETE FROM documents WHERE id = ?", (document_id,))
            # Claims left with no evidence at all are removed too.
            c.execute("DELETE FROM claims WHERE id NOT IN (SELECT claim_id FROM claim_evidence)")
