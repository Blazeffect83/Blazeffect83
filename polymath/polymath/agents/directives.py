"""Directives: what an agent is for, in plain English, compiled into a kind and a scope.

No language model reads the directive — a small set of patterns does, the same way
the answering engine parses questions. Anything unrecognised becomes a research
directive, which is always meaningful.

=========  ============================================  ==============================================
kind       example directives                            what the agent then does
=========  ============================================  ==============================================
research   "research black holes", "become an expert     reads about the subject, predicts its facts,
           on Roman history", "learn about volcanoes"    tests itself, answers questions about it
watch      "watch news about SpaceX", "monitor           scans everything newly read for the subject,
           graphene", "keep an eye on Mars"              keeps a verified digest, reads more of it
verify     "fact-check populations", "verify capitals"   reviews disputed facts in scope and judges them
predict    "predict capitals", "fill in the country      guesses missing facts of a relation; rewarded
           of cities", "complete birth dates"            when the dumps later confirm the guess
answer     "answer questions about chemistry"            answers your questions; your 👍/👎 is the reward
=========  ============================================  ==============================================
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from polymath.core.db import Database

KINDS = ("research", "watch", "verify", "predict", "answer")

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "answer",
        re.compile(r"^(?:answer|handle|field|take)\s+(?:my\s+|all\s+)?questions?\s+(?:about|on|in)\s+(?P<s>.+)$"),
    ),
    (
        "watch",
        re.compile(
            r"^(?:watch|monitor|track|follow|keep\s+(?:an\s+)?eye\s+on|alert\s+me\s+(?:about|on))\s+"
            r"(?:(?:the\s+)?(?:news|updates|developments|papers|articles)\s+(?:about|on|for)\s+)?(?P<s>.+)$"
        ),
    ),
    (
        "verify",
        re.compile(
            r"^(?:fact[\s-]?check|verify|audit|check\s+the\s+facts\s+(?:about|on|of)|double[\s-]?check)\s+(?P<s>.+)$"
        ),
    ),
    (
        "predict",
        re.compile(
            r"^(?:predict|guess|infer|fill\s+in|complete|work\s+out)\s+(?:the\s+|missing\s+)*(?P<s>.+?)"
            r"(?:\s+(?:of|for)\s+(?P<x>.+))?$"
        ),
    ),
    (
        "research",
        re.compile(
            r"^(?:research|study|investigate|explore|read\s+(?:up\s+)?(?:about|on)|learn(?:\s+(?:about|everything\s+about))?|"
            r"become\s+(?:an?\s+)?(?:expert|specialist|authority)\s+(?:on|in)|master|dig\s+into|get\s+good\s+at)\s+(?P<s>.+)$"
        ),
    ),
]


@dataclass
class Directive:
    kind: str
    subject: str
    text: str
    qualifier: str | None = None  # predict: the kind of subject ("capital" *of countries*)


def parse(text: str) -> Directive:
    raw = " ".join(text.strip().split())
    t = raw.rstrip(".!?").strip()
    low = t.lower()
    for prefix in ("please ", "i want you to ", "can you ", "could you ", "you should ", "your job is to ", "go "):
        if low.startswith(prefix):
            t, low = t[len(prefix) :], low[len(prefix) :]
    for kind, pat in _PATTERNS:
        m = pat.match(low)
        if m:
            subj = t[m.start("s") : m.end("s")].strip(" '\"")
            qual = m.groupdict().get("x")
            qualifier = t[m.start("x") : m.end("x")].strip(" '\"") if qual else None
            if subj:
                return Directive(kind, subj, raw, qualifier)
    return Directive("research", t.strip(" '\""), raw)


def resolve_scope(db: Database, d: Directive) -> dict[str, Any]:
    """Ground a directive in what the agent already knows: entities, topics, relations, keywords."""
    from polymath.interface.answer import Answerer
    from polymath.memory.text_index import TextIndex

    a = Answerer(db)
    text = TextIndex(db)
    scope: dict[str, Any] = {"query": d.subject, "entities": [], "topics": [], "predicates": [], "keywords": []}
    if d.kind == "predict":
        rel = d.subject[:-1] if d.subject.endswith("s") and len(d.subject) > 4 else d.subject
        scope["predicates"] = (a.predicates_for(d.subject) or a.predicates_for(rel))[:3]
        if d.qualifier:
            cls = a.find_entity(d.qualifier[:-1] if d.qualifier.endswith("s") else d.qualifier)
            scope["class"] = cls.id if cls is not None else None
        return scope
    if d.kind == "verify":
        rel = d.subject[:-1] if d.subject.endswith("s") and len(d.subject) > 4 else d.subject
        scope["predicates"] = (a.predicates_for(d.subject) or a.predicates_for(rel))[:3]
    entity = a.find_entity(d.subject)
    if entity is not None:
        scope["entities"] = [entity.id]
    scope["topics"] = [
        int(r["id"])
        for r in db.query("SELECT id FROM topics WHERE name LIKE ? ORDER BY n_total DESC LIMIT 10", (f"%{d.subject}%",))
    ]
    scope["keywords"] = sorted(text.content_terms(d.subject))
    return scope


def describe(kind: str, subject: str, qualifier: str | None = None) -> str:
    return {
        "research": f"become an expert on {subject}",
        "watch": f"watch for anything new about {subject}",
        "verify": f"fact-check {subject}",
        "predict": f"predict the {subject}" + (f" of {qualifier}" if qualifier else ""),
        "answer": f"answer questions about {subject}",
    }[kind]
