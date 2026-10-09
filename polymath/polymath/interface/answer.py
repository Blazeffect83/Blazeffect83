"""Answering engine — entirely offline, with citations.

1. **Understand**: question patterns ("what is the X of Y", "who/what is Y",
   "when was Y born", "where is Y", "Y's X") give a subject phrase and a relation
   phrase; otherwise the whole question is used for retrieval.
2. **Ground**: the subject is linked to an entity (the agent's own entity
   linker, falling back to alias lookup of the longest n-gram); the relation
   phrase is matched to predicates by their labels and aliases learned from
   Wikidata property records, infobox field names and relation patterns.
3. **Answer**: graph facts first (sourced/inferred, never holdout; disputes are
   shown as such), then the best supporting passages from the full-text index.
4. **Cite**: every statement carries its provenance — document title, URL and
   license, or the dump it came from — and a confidence. If nothing grounded is
   found the engine says so instead of guessing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from polymath.core.db import Database
from polymath.memory.documents import DocumentStore
from polymath.memory.graph import Entity, KnowledgeGraph, norm_alias
from polymath.memory.text_index import TextIndex
from polymath.perception.entities import EntityLinker
from polymath.perception.stem import stem

PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"^(?:what|which|who)\s+(?:is|was|are|were)\s+the\s+(?P<rel>.+?)\s+(?:of|for|in)\s+(?P<subj>.+)$"),
        "rel",
    ),
    (
        re.compile(
            r"^when\s+(?:was|were|is|did)\s+(?P<subj>.+?)\s+(?P<rel>born|founded|established|died|created|"
            r"released|published|discovered|built|formed|invented|dissolved|opened)(?:\s.*)?$"
        ),
        "when",
    ),
    (
        re.compile(r"^where\s+(?:is|was|are|were)\s+(?P<subj>.+?)(?:\s+(?P<rel>located|born|based|founded|buried))?$"),
        "where",
    ),
    (re.compile(r"^(?:who|what)\s+(?:is|was|are|were)\s+(?P<subj>.+)$"), "describe"),
    (re.compile(r"^(?:tell me about|describe|explain)\s+(?P<subj>.+)$"), "describe"),
    (re.compile(r"^(?P<subj>.+?)'s\s+(?P<rel>.+)$"), "rel"),
    (
        re.compile(
            r"^how\s+(?:many|much)\s+(?P<rel>.+?)\s+(?:does|do|did|has|have|is|are)\s+(?P<subj>.+?)"
            r"(?:\s+have|\s+has)?$"
        ),
        "rel",
    ),
]
WHEN_REL = {
    "born": ["date of birth", "birth date", "born"],
    "died": ["date of death", "death date", "died"],
    "founded": ["inception", "founded", "established", "formation"],
    "established": ["inception", "established"],
    "created": ["inception", "created"],
    "released": ["publication date", "release date", "released"],
    "published": ["publication date", "published"],
    "discovered": ["time of discovery", "discovered"],
    "built": ["inception", "built"],
    "formed": ["inception", "formed"],
    "invented": ["time of discovery"],
    "dissolved": ["dissolved, abolished or demolished date", "dissolved"],
    "opened": ["inception", "opened"],
}
WHERE_REL = {
    None: ["location", "country", "located in the administrative territorial entity", "continent"],
    "located": ["location", "located in the administrative territorial entity", "country"],
    "born": ["place of birth", "birth place"],
    "based": ["headquarters location", "location"],
    "founded": ["location of formation", "headquarters location"],
    "buried": ["place of burial"],
}


@dataclass
class Citation:
    title: str
    url: str | None
    license: str
    source: str


@dataclass
class Statement:
    text: str
    confidence: float
    kind: str  # fact | inferred | disputed | passage | description
    citations: list[Citation] = field(default_factory=list)


@dataclass
class Answer:
    question: str
    subject: str | None
    relation: str | None
    statements: list[Statement]
    confidence: float
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "subject": self.subject,
            "relation": self.relation,
            "confidence": round(self.confidence, 3),
            "note": self.note,
            "statements": [
                {
                    "text": s.text,
                    "confidence": round(s.confidence, 3),
                    "kind": s.kind,
                    "citations": [c.__dict__ for c in s.citations],
                }
                for s in self.statements
            ],
        }

    def render(self) -> str:
        if not self.statements:
            return f"I don't know yet. {self.note}".strip()
        lines = []
        refs: list[Citation] = []
        for s in self.statements:
            marks = []
            for c in s.citations:
                if c not in refs:
                    refs.append(c)
                marks.append(str(refs.index(c) + 1))
            tag = {"disputed": " (disputed)", "inferred": " (inferred)"}.get(s.kind, "")
            lines.append(f"- {s.text}{tag} [{', '.join(marks)}] (confidence {s.confidence:.2f})")
        lines.append("")
        lines += [f"[{i + 1}] {c.title} — {c.url or c.source} — {c.license}" for i, c in enumerate(refs)]
        return "\n".join(lines)


def render_value(db: Database, o: int, value: str) -> str:
    if o:
        return str(db.scalar("SELECT label FROM entities WHERE id=?", (o,), default=f"#{o}"))
    v = json.loads(value)
    if isinstance(v, dict):
        if "time" in v:
            return str(v["time"])
        if "amount" in v:
            amt = v["amount"]
            num = f"{amt:,.0f}" if float(amt).is_integer() else f"{amt:,}"
            unit = v.get("unit")
            if unit and str(unit).startswith("Q"):
                unit = db.scalar("SELECT label FROM entities WHERE key=?", (unit,), default=unit)
            return f"{num} {unit}".strip() if unit else num
        if "text" in v:
            return str(v["text"])
        if "lat" in v:
            return f"{v['lat']:.4f}, {v['lon']:.4f}"
    return str(v)


class Answerer:
    def __init__(self, db: Database, linker: EntityLinker | None = None) -> None:
        self.db = db
        self.graph = KnowledgeGraph(db)
        self.store = DocumentStore(db)
        self.text = TextIndex(db, self.store)
        self.linker = linker

    # --------------------------------------------------------------- parse
    @staticmethod
    def parse(question: str) -> tuple[str | None, str | None, str]:
        q = question.strip().rstrip("?.! ").strip()
        ql = q.lower()
        for pat, kind in PATTERNS:
            m = pat.match(ql)
            if m:
                subj = q[m.start("subj") : m.end("subj")]
                rel = m.groupdict().get("rel")
                return subj.strip(" '\""), rel, kind
        return None, None, "search"

    # -------------------------------------------------------------- ground
    def find_entity(self, phrase: str) -> Entity | None:
        cands = self.graph.candidates(phrase, limit=5)
        if not cands and phrase.lower().startswith(("the ", "a ", "an ")):
            cands = self.graph.candidates(phrase.split(" ", 1)[1], limit=5)
        if cands:
            # prefer entities the agent actually knows something about
            def score(c: tuple[int, str, int]) -> tuple[float, int]:
                e = self.graph.entity(c[0])
                facts = int(self.db.scalar("SELECT COUNT(*) FROM triples WHERE s=? AND holdout=0", (c[0],), 0))
                return (c[2] + 5.0 * (e is not None and e.doc_id is not None) + min(facts, 50) / 10, -c[0])

            best = max(cands, key=score)
            return self.graph.entity(best[0])
        if self.linker is not None:
            links = self.linker.link(phrase)
            if links:
                return self.graph.entity(max(links, key=lambda lk: lk.end - lk.start).entity_id)
        words = phrase.split()
        for n in range(min(len(words), 6), 0, -1):  # longest n-gram that names something
            for i in range(len(words) - n + 1):
                c = self.graph.candidates(" ".join(words[i : i + n]), limit=1)
                if c:
                    return self.graph.entity(c[0][0])
        return None

    def predicates_for(self, relation: str, labels: list[str] | None = None) -> list[int]:
        """Predicates whose label (or learned alias) matches the relation phrase, best first."""
        wanted = [norm_alias(x) for x in (labels or [relation])]
        out: list[int] = []
        for w in wanted:
            for r in self.db.query(
                "SELECT id FROM predicates WHERE lower(label)=? OR key=?", (w, f"infobox:{w.replace(' ', '_')}")
            ):
                if int(r["id"]) not in out:
                    out.append(int(r["id"]))
            for r in self.db.query(  # Wikidata property aliases ("capital city" → capital)
                "SELECT p.id FROM aliases a JOIN entities e ON e.id=a.entity_id JOIN predicates p ON p.key=e.key "
                "WHERE a.alias=? AND e.kind='property'",
                (w,),
            ):
                if int(r["id"]) not in out:
                    out.append(int(r["id"]))
        if not out and labels is None:
            stem = norm_alias(relation)
            for r in self.db.query("SELECT id FROM predicates WHERE lower(label) LIKE ? LIMIT 5", (f"%{stem}%",)):
                out.append(int(r["id"]))
        return out

    # ------------------------------------------------------------- answer
    def _citations(self, triple_id: int) -> list[Citation]:
        cits: list[Citation] = []
        for p in self.graph.provenance(triple_id)[:3]:
            if p.get("doc_title"):
                c = Citation(p["doc_title"], p["doc_url"], p["doc_license"], p["source"])
            elif p["source"] == "wikidata":
                c = Citation("Wikidata", "https://www.wikidata.org/", "CC0 1.0", "wikidata dump")
            elif p["source"] == "rule":
                c = Citation(f"Inference: {p['detail'][:80]}", None, "derived", "rule")
            else:
                c = Citation(p["source"], None, "see source", p["source"])
            if c not in cits:
                cits.append(c)
        return cits

    def facts(self, entity: Entity, predicates: list[int], limit: int = 5) -> list[Statement]:
        out: list[Statement] = []
        for p in predicates:
            plabel = str(self.db.scalar("SELECT label FROM predicates WHERE id=?", (p,), default="?"))
            for t in self.db.query(
                "SELECT id, o, value, status, confidence FROM triples WHERE s=? AND p=? AND "
                "holdout=0 ORDER BY status='disputed', confidence DESC LIMIT ?",
                (entity.id, p, limit),
            ):
                value = render_value(self.db, int(t["o"]), str(t["value"]))
                kind = {"sourced": "fact", "inferred": "inferred", "disputed": "disputed"}[t["status"]]
                out.append(
                    Statement(
                        f"The {plabel} of {entity.label} is {value}.",
                        float(t["confidence"]),
                        kind,
                        self._citations(int(t["id"])),
                    )
                )
            if out:
                break
        return out

    def describe(self, entity: Entity) -> list[Statement]:
        out = []
        doc = self.store.get(entity.doc_id) if entity.doc_id else None
        if entity.description:
            out.append(
                Statement(
                    f"{entity.label}: {entity.description}.",
                    0.8,
                    "description",
                    [Citation("Wikidata", "https://www.wikidata.org/", "CC0 1.0", "wikidata dump")],
                )
            )
        if doc is not None and doc.text:
            first = doc.meta.get("summary") or [doc.text[:400].rsplit(". ", 1)[0].rstrip(".") + "."]
            out.append(
                Statement(
                    " ".join(first[:2])[:600], 0.75, "passage", [Citation(doc.title, doc.url, doc.license, doc.source)]
                )
            )
        if not out:
            for e in self.graph.neighbors(entity.id, limit=4):
                if e.direction == "out":
                    value = (
                        e.other_label or render_value(self.db, 0, json.dumps(e.value))
                        if e.value is not None
                        else e.other_label
                    )
                    out.append(
                        Statement(
                            f"{entity.label} — {e.predicate}: {value}.",
                            e.confidence,
                            "fact",
                            self._citations(e.triple_id),
                        )
                    )
        return out

    def passages(
        self, query: str, entity: Entity | None, k: int = 3, *, must: set[str] | None = None
    ) -> list[Statement]:
        """Best supporting sentences. A sentence must contain a *specific* query word (and one of ``must``)."""
        q = f"{entity.label} {query}" if entity else query
        content = self.text.content_terms(q)
        if not content or (must is not None and not must):
            return []
        out = []
        for h in self.text.search(q, limit=k * 3):
            sent = self._best_sentence(h.text, content, must)
            if not sent:
                continue
            out.append(
                Statement(
                    sent, min(0.7, 0.3 + h.score / 30), "passage", [Citation(h.title, h.url, h.license, h.source)]
                )
            )
            if len(out) >= k:
                break
        return out

    @staticmethod
    def _best_sentence(text: str, content: set[str], must: set[str] | None = None) -> str:
        best, best_score = "", 0
        for s in re.split(r"(?<=[.!?])\s+", text):
            words = {stem(w) for w in norm_alias(s).split()}
            if must and not words & must:
                continue
            score = len(content & words)
            if score > best_score and 20 <= len(s) <= 600:
                best, best_score = s.strip(), score
        return best

    def ask(self, question: str) -> Answer:
        subj, rel, kind = self.parse(question)
        entity = self.find_entity(subj) if subj else None
        statements: list[Statement] = []
        note = ""
        if entity is not None:
            if kind == "describe":
                statements = self.describe(entity)
            else:
                labels = None
                if kind == "when":
                    labels = WHEN_REL.get(rel or "", [rel or ""])
                elif kind == "where":
                    labels = WHERE_REL.get(rel, WHERE_REL[None])
                preds = self.predicates_for(rel or "", labels) if (rel or labels) else []
                statements = self.facts(entity, preds)
            if not statements and kind == "describe":
                statements = self.passages(question, entity)
            elif not statements:
                # a passage must speak to the relation asked about, not merely mention the subject
                rel_terms = self.text.content_terms(rel or "")
                statements = self.passages(question, entity, must=rel_terms) if rel_terms else []
                if not statements:
                    note = f"I know about {entity.label} but have not learned its {rel or 'answer to that'} yet."
        else:
            # an unknown subject must itself appear in a passage, or the passage is about something else
            must = self.text.content_terms(subj) if subj else None
            statements = self.passages(question, None, must=must)
            if subj and not statements:
                note = f"I could not identify “{subj}” among the entities I have learned."
        if not statements and not note:
            note = "Nothing in my memory supports an answer; I may not have read about this yet."
        conf = max((s.confidence for s in statements), default=0.0)
        return Answer(question, entity.label if entity else subj, rel, statements, conf, note)

    # --------------------------------------------------------- quiz support
    def score_options(self, subject: int, predicate: int, options: list[int]) -> tuple[int, float, str]:
        """Pick the best-supported option for (subject, predicate, ?) with the learned link predictor."""
        from polymath.reasoning.link_prediction import LinkPredictor

        pred = LinkPredictor(self.db, text=self.text).predict(subject, predicate, options)
        return pred.best, pred.confidence, pred.method
