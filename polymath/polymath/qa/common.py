"""Shared helpers for the question skills: looking up values, dates and places of things, and building answers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from polymath.memory.graph import Entity, norm_alias
from polymath.reasoning import temporal

if TYPE_CHECKING:
    from polymath.interface.answer import Answer, Answerer, Citation, Statement

# Wikidata properties the skills reason with
BIRTH, DEATH, INCEPTION, DISSOLVED = "P569", "P570", "P571", "P576"
PUBLISHED, POINT_IN_TIME, START, END, DISCOVERED = "P577", "P585", "P580", "P582", "P575"
COORDINATES, INSTANCE_OF, SUBCLASS_OF, HUMAN = "P625", "P31", "P279", "Q5"
FIRST_DATES = (BIRTH, INCEPTION, PUBLISHED, POINT_IN_TIME, START, DISCOVERED)  # when something began
LEAD = re.compile(r"^(?:the|a|an)\s+", re.I)


@dataclass
class Value:
    triple: int
    value: Any  # decoded literal (dict) or entity id
    plabel: str
    pkey: str


def clean(phrase: str) -> str:
    return LEAD.sub("", phrase.strip(" ?.!,'\"")).strip()


def strict_entity(ans: Answerer, phrase: str, need: tuple[str, ...] | list[str] = ()) -> Entity | None:
    """The thing this exact name (or its singular) means: no guessing from part of the phrase.

    Several things can share a name (Paris the city, Paris in mythology; penguins, Penguin Books). The one chosen has
    the facts the question needs (``need``: property keys), then the most facts, an article, and importance.
    """
    p = clean(phrase)
    ids: dict[int, int] = {}
    for form in dict.fromkeys([p, singular(p)]):
        for eid, _label, count in ans.graph.candidates(form, limit=10):
            ids[eid] = max(ids.get(eid, 0), count)
    best: tuple[float, ...] | None = None
    chosen = None
    for eid, count in ids.items():
        e = ans.graph.entity(eid)
        if e is None or e.kind == "stub":
            continue
        has = 1 if need and _has(ans, eid, need) else 0
        pr = float(ans.db.scalar("SELECT pagerank FROM entities WHERE id = ?", (eid,), default=0) or 0)
        score = (has, min(_facts(ans, eid), 200) / 20 + 3 * (e.doc_id is not None) + min(count, 20) / 4, pr, -eid)
        if best is None or score > best:
            best, chosen = score, e
    return chosen


def _has(ans: Answerer, eid: int, keys: tuple[str, ...] | list[str]) -> bool:
    marks = ",".join("?" * len(keys))
    return bool(
        ans.db.scalar(
            f"SELECT 1 FROM triples t JOIN predicates p ON p.id = t.p WHERE t.s = ? AND p.key IN ({marks}) "
            f"AND t.holdout = 0 LIMIT 1",
            [eid, *keys],
        )
    )


def entity(ans: Answerer, phrase: str, need: tuple[str, ...] | list[str] = ()) -> Entity | None:
    """An exact name first (the right one of several, see :func:`strict_entity`); else the answerer's best guess."""
    return strict_entity(ans, phrase, need) or ans.find_entity(clean(phrase))


def _facts(ans: Answerer, eid: int) -> int:
    return int(ans.db.scalar("SELECT COUNT(*) FROM triples WHERE s=? AND holdout=0", (eid,), default=0))


def singular(word: str) -> str:
    w = word.strip()
    low = w.lower()
    if low.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if low.endswith(("sses", "shes", "ches", "xes")):
        return w[:-2]
    if low.endswith("s") and not low.endswith(("ss", "us", "is")) and len(w) > 3:
        return w[:-1]
    return w


def by_keys(ans: Answerer, e: Entity, keys: tuple[str, ...] | list[str]) -> list[Value]:
    """Visible, undisputed values of these properties (in the order of ``keys``), current before past."""
    out: list[Value] = []
    for key in keys:
        rows = ans.db.query(
            "SELECT t.id, t.o, t.value, p.label, p.key FROM triples t JOIN predicates p ON p.id = t.p "
            "WHERE t.s = ? AND p.key = ? AND t.holdout = 0 AND t.status != 'disputed' ORDER BY t.confidence DESC",
            (e.id, key),
        )
        when = temporal.of(ans.db, [int(r["id"]) for r in rows])
        for r in temporal.current_first(rows, when):
            val: Any = int(r["o"]) if int(r["o"]) else json.loads(str(r["value"]))
            out.append(Value(int(r["id"]), val, str(r["label"]), str(r["key"])))
    return out


def by_labels(ans: Answerer, e: Entity, labels: list[str]) -> list[Value]:
    """Values of the first relation (by label, in order) this entity has."""
    for label in labels:
        for p in ans.predicates_for(label, [label]):
            key = str(ans.db.scalar("SELECT key FROM predicates WHERE id = ?", (p,), default=""))
            vals = by_keys(ans, e, [key])
            if vals:
                return vals
    return []


def first_date(ans: Answerer, e: Entity, keys: tuple[str, ...] = FIRST_DATES) -> Value | None:
    """The earliest date among these properties (when something was born, founded, published …)."""
    best: tuple[tuple[int, int, int], Value] | None = None
    for v in by_keys(ans, e, keys):
        k = temporal.date_key(v.value.get("time")) if isinstance(v.value, dict) else None
        if k is not None and (best is None or k < best[0]):
            best = (k, v)
    return best[1] if best else None


def date_of(ans: Answerer, e: Entity, key: str) -> tuple[tuple[int, int, int], Value] | None:
    for v in by_keys(ans, e, [key]):
        k = temporal.date_key(v.value.get("time")) if isinstance(v.value, dict) else None
        if k is not None:
            return k, v
    return None


def label(ans: Answerer, eid: int) -> str:
    return str(ans.db.scalar("SELECT label FROM entities WHERE id = ?", (eid,), default=f"#{eid}"))


def is_human(ans: Answerer, e: Entity) -> bool:
    return bool(
        ans.db.scalar(
            "SELECT 1 FROM triples t JOIN predicates p ON p.id = t.p JOIN entities c ON c.id = t.o "
            "WHERE t.s = ? AND p.key = ? AND c.key = ?",
            (e.id, INSTANCE_OF, HUMAN),
        )
    )


def cites(ans: Answerer, *triples: int) -> list[Citation]:
    out: list[Citation] = []
    for t in triples:
        for c in ans._citations(t):
            if c not in out:
                out.append(c)
    return out


def statement(text: str, confidence: float, citations: list[Citation], kind: str = "fact") -> Statement:
    from polymath.interface.answer import Statement

    return Statement(text, round(confidence, 3), kind, citations)


def answer(
    question: str, subject: str | None, statements: list[Statement], *, relation: str = "", note: str = ""
) -> Answer:
    from polymath.interface.answer import Answer

    conf = max((s.confidence for s in statements), default=0.0)
    if not statements and not note:
        note = "I could not work that out from what I have learned so far."
    return Answer(question, subject, relation or None, statements, conf, note)


def confidence(ans: Answerer, *triples: int) -> float:
    vals = [float(ans.db.scalar("SELECT confidence FROM triples WHERE id = ?", (t,), default=0.5)) for t in triples]
    return min(vals) if vals else 0.5


def norm(text: str) -> str:
    return norm_alias(text)
