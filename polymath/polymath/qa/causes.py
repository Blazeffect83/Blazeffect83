"""Why questions: what causes something, and what it causes.

* "What causes malaria?" · "Why did the Great Depression happen?" · "What led to World War I?"
* "What does smoking cause?" · "What are the effects of deforestation?"

Causes come from Wikidata (has cause P828, has effect P1542, immediate cause P1478 / of P1536, contributing factor
P1479 / of P1537) and from text (``text:causes``, read by :mod:`polymath.perception.semantic`), each with its source.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from polymath.memory.graph import Entity
from polymath.perception.semantic import MIN_TEXT_SOURCES
from polymath.qa import common as c

if TYPE_CHECKING:
    from polymath.interface.answer import Answer, Answerer

# property → which side is the cause: "s" (the subject causes the object) or "o"
CAUSAL = {"text:causes": "s", "P1542": "s", "P1536": "s", "P1537": "s", "P828": "o", "P1478": "o", "P1479": "o"}
PREVENTS = "text:prevents"
CAUSES_Q = re.compile(
    r"^(?:what|which things?) (?:causes?|caused|leads? to|led to|results? in|resulted in|triggers?|triggered) "
    r"(?P<a>.+)$"
    r"|^what (?:are|were|is|was) (?:the )?(?:main |major |common |root )?(?:causes?|reasons?) (?:of|for|behind) "
    r"(?P<a2>.+)$"
    r"|^why (?:did|does|do|is|was|were|are) (?P<a3>.+?)(?: (?:happen|occur|start|begin|exist))?$"
)
EFFECTS_Q = re.compile(
    r"^what (?:does|did|do|can) (?P<a>.+?) (?:cause|lead to|result in|trigger)$"
    r"|^what (?:are|were|is|was) (?:the )?(?:main )?(?:effects?|consequences?|results?|impacts?) of (?P<a2>.+)$"
)
PREVENT_Q = re.compile(r"^(?:what|how can I|how do you) (?:prevents?|stops?) (?P<a>.+)$")
TOP = 6


def try_answer(ans: Answerer, q: str, ql: str) -> Answer | None:
    if m := EFFECTS_Q.match(ql):
        return answer(ans, q, m["a"] or m["a2"], effects=True)
    if m := PREVENT_Q.match(ql):
        return prevention(ans, q, m["a"])
    if m := CAUSES_Q.match(ql):
        return answer(ans, q, m["a"] or m["a2"] or m["a3"], effects=False)
    return None


def _unread(ans: Answerer, e: Entity, *, effects: bool) -> int:
    """Causes (or effects) known only by their Wikidata id."""
    keys = [k for k, side in CAUSAL.items() if not k.startswith("text:")]
    marks = ",".join("?" * len(keys))
    n = 0
    for r in ans.db.query(
        f"SELECT t.s, t.o, p.key FROM triples t JOIN predicates p ON p.id = t.p JOIN entities x ON x.id = "
        f"CASE WHEN t.s = ? THEN t.o ELSE t.s END WHERE p.key IN ({marks}) AND (t.s = ? OR t.o = ?) "
        f"AND x.kind = 'stub'",
        [e.id, *keys, e.id, e.id],
    ):
        cause_is_subject = CAUSAL[str(r["key"])] == "s"
        e_is_cause = (int(r["s"]) == e.id) == cause_is_subject
        n += e_is_cause == effects
    return n


def links(ans: Answerer, e: Entity, *, effects: bool) -> list[tuple[int, int, str]]:
    """(other entity, triple, property key): its causes, or its effects, strongest evidence first."""
    out: list[tuple[int, int, str]] = []
    keys = list(CAUSAL)
    marks = ",".join("?" * len(keys))
    for side in ("s", "o"):
        rows = ans.db.query(
            f"SELECT t.id, t.s, t.o, p.key FROM triples t JOIN predicates p ON p.id = t.p WHERE p.key IN ({marks}) "
            f"AND t.{side} = ? AND t.o != 0 AND t.holdout = 0 AND t.status != 'disputed' "
            f"AND (p.key NOT LIKE 'text:%' OR t.n_sources >= {MIN_TEXT_SOURCES}) "
            f"ORDER BY t.confidence DESC LIMIT 50",
            [*keys, e.id],
        )
        for r in rows:
            cause_side = CAUSAL[str(r["key"])]
            cause, effect = (int(r["s"]), int(r["o"])) if cause_side == "s" else (int(r["o"]), int(r["s"]))
            if effects and cause == e.id:
                out.append((effect, int(r["id"]), str(r["key"])))
            elif not effects and effect == e.id:
                out.append((cause, int(r["id"]), str(r["key"])))
    seen: set[int] = set()
    uniq = []
    for other, tid, key in out:
        if other not in seen and not re.fullmatch(r"[QP]\d+", c.label(ans, other)):
            seen.add(other)
            uniq.append((other, tid, key))
    return uniq


def answer(ans: Answerer, q: str, phrase: str, *, effects: bool) -> Answer:
    e = c.entity(ans, phrase, tuple(CAUSAL))
    if e is None:
        return c.answer(q, None, [], note=f"I could not identify “{c.clean(phrase)}”.")
    found = links(ans, e, effects=effects)
    if not found:
        what = "effects" if effects else "causes"
        unread = _unread(ans, e, effects=effects)
        note = (f"I know {unread} {what} of {e.label}, but have not read their names yet." if unread
                else f"I have not learned the {what} of {e.label} yet.")  # fmt: skip
        return c.answer(q, e.label, [], relation=what, note=note)
    statements = []
    for other, tid, key in found[:TOP]:
        name = c.label(ans, other)
        source = "read in text" if key.startswith("text:") else "Wikidata"
        text = f"{e.label} can lead to {name}." if effects else f"{name} is a cause of {e.label}."
        statements.append(c.statement(f"{text} ({source})", c.confidence(ans, tid), c.cites(ans, tid),
                                      kind="fact" if not key.startswith("text:") else "passage"))  # fmt: skip
    return c.answer(q, e.label, statements, relation="effects" if effects else "causes")


def prevention(ans: Answerer, q: str, phrase: str) -> Answer:
    e = c.entity(ans, phrase, (PREVENTS,))
    if e is None:
        return c.answer(q, None, [], note=f"I could not identify “{c.clean(phrase)}”.")
    rows = ans.db.query(
        "SELECT t.id, t.s FROM triples t JOIN predicates p ON p.id = t.p WHERE p.key = ? AND t.o = ? AND t.holdout = 0 "
        "AND t.status != 'disputed' AND t.n_sources >= ? ORDER BY t.confidence DESC LIMIT ?",
        (PREVENTS, e.id, MIN_TEXT_SOURCES, TOP),
    )
    if not rows:
        return c.answer(q, e.label, [], relation="prevention", note=f"I have not read what prevents {e.label} yet.")
    sts = [c.statement(f"{c.label(ans, int(r['s']))} helps prevent {e.label}.", c.confidence(ans, int(r["id"])),
                       c.cites(ans, int(r["id"])), kind="passage") for r in rows]  # fmt: skip
    return c.answer(q, e.label, sts, relation="prevention")
