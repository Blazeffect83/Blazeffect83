"""Multi-step questions: follow a chain of relations, citing every step.

* "Who is the mayor of the capital of France?" → the capital of France is Paris; the mayor of Paris is …
* "What is the currency of the country where the Eiffel Tower is?" is out of reach; "of" chains are not:
  "What is the population of the capital of the largest …" works as far as each step names one thing.
* "Which river flows through the capital of France?" → the capital of France is Paris; rivers connected to Paris,
  ranked by how well the relation matches "flows through" (its label, or the phrasings learned from reading).
* "Who founded the company that …" style clauses are not parsed; "who <verb> <thing>" is: "Who founded Microsoft?"

Each hop takes the value that holds now (Berlin, not Bonn) and the best-supported one when there are several.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from polymath.memory.graph import Entity
from polymath.qa import common as c
from polymath.reasoning import temporal

if TYPE_CHECKING:
    from polymath.interface.answer import Answer, Answerer

NESTED = re.compile(r"^(?:what|who|which|where) (?:is|was|are|were) (?P<rest>.+ of .+ of .+)$")
COPULA = {"is", "are", "was", "were"}
WHICH = re.compile(r"^(?P<wh>which|what|who) (?P<rest>.+)$")
MAX_WORDS = 14


@dataclass
class Hop:
    subject: str
    relation: str
    value: str
    triple: int


def resolve(ans: Answerer, phrase: str, depth: int = 0) -> tuple[Entity, list[Hop]] | None:
    """A phrase that names one thing: "France", or "the capital of France" (any depth)."""
    phrase = c.clean(phrase)
    if not phrase or depth > 4:
        return None
    e = c.strict_entity(ans, phrase)
    if e is not None:
        return e, []
    for m in re.finditer(r" of ", phrase):
        rel, rest = phrase[: m.start()].strip(), phrase[m.end() :].strip()
        inner = resolve(ans, rest, depth + 1)
        if inner is None:
            continue
        x, hops = inner
        hop = follow(ans, x, rel)
        if hop is not None:
            value_entity, h = hop
            return value_entity, [*hops, h]
    return None


def follow(ans: Answerer, x: Entity, rel: str) -> tuple[Entity, Hop] | None:
    """The thing ``x``'s relation ``rel`` points to (current value first, then the best supported)."""
    rel = c.clean(rel)
    for p in ans.predicates_for(rel, [rel]):  # an exact relation name: no guessing inside a chain
        rows = ans.db.query(
            "SELECT t.id, t.o FROM triples t WHERE t.s = ? AND t.p = ? AND t.o != 0 AND t.holdout = 0 "
            "AND t.status != 'disputed' ORDER BY t.confidence DESC LIMIT 10",
            (x.id, p),
        )
        when = temporal.of(ans.db, [int(r["id"]) for r in rows])
        for r in temporal.current_first(rows, when):
            e = ans.graph.entity(int(r["o"]))
            if e is not None and not re.fullmatch(r"[QP]\d+", e.label):
                plabel = str(ans.db.scalar("SELECT label FROM predicates WHERE id = ?", (p,), default=rel))
                return e, Hop(x.label, plabel, e.label, int(r["id"]))
    return None


def try_answer(ans: Answerer, q: str, ql: str) -> Answer | None:
    if m := NESTED.match(ql):
        return nested(ans, q, m["rest"])
    if m := WHICH.match(ql):
        return relative(ans, q, m["wh"], m["rest"])
    return None


def _sentence(steps: list[str]) -> str:
    text = "; ".join(steps)
    return text[:1].upper() + text[1:] + "."


def nested(ans: Answerer, q: str, rest: str) -> Answer | None:
    """ "the mayor of the capital of France": resolve the inner part, then answer the outer relation."""
    rest = c.clean(rest)
    fallback: Answer | None = None
    for m in re.finditer(r" of ", rest):
        rel, inner_phrase = c.clean(rest[: m.start()]), rest[m.end() :]
        inner = resolve(ans, inner_phrase)
        if inner is None or (not inner[1] and " of " not in rel):
            continue  # a single step is the plain answerer's job
        x, hops = inner
        steps = [f"the {h.relation} of {h.subject} is {h.value}" for h in hops]
        tids = [h.triple for h in hops]
        facts = ans.facts(x, ans.predicates_for(rel, [rel]), limit=3)
        if not hops:  # "the head of government of Portugal": one step, but a relation whose name has "of" in it
            if facts:
                return c.answer(q, x.label, facts, relation=rel)
            continue
        if not facts:
            if fallback is None:
                note = f"{_sentence(steps)[:-1]}, but I have not learned the {rel} of {x.label} yet."
                fallback = c.answer(q, x.label, [], relation=rel, note=note)
            continue
        first = c.statement(_sentence(steps), c.confidence(ans, *tids), c.cites(ans, *tids))
        return c.answer(q, x.label, [first, *facts], relation=rel)
    return fallback


def relative(ans: Answerer, q: str, wh: str, rest: str) -> Answer | None:
    """ "which river flows through the capital of France" / "who founded Microsoft"."""
    words = rest.split()
    if len(words) < 2 or len(words) > MAX_WORDS:
        return None
    if wh == "who":
        human = _human(ans)
        classes = [(human, 0)] if human is not None else []
    else:
        classes = [
            (e, i) for i in range(1, min(4, len(words) - 1) + 1) if (e := c.strict_entity(ans, " ".join(words[:i])))
        ]
    for cls, i in classes:
        for j in range(i + 1, len(words)):
            verb, obj = " ".join(words[i:j]), " ".join(words[j:])
            if verb in COPULA:  # "which city is the capital of Portugal": the thing the phrase names
                named = _named_by(ans, q, cls, obj) if wh != "who" else None
                if named is not None:
                    return named
                continue
            target = resolve(ans, obj)
            if target is None:
                continue
            t, hops = target
            if wh == "who" and not hops and not _verb_relation(ans, verb):
                continue  # leave "who is X" and friends to the plain answerer
            found = connected(ans, t, cls, verb)
            if not found:
                continue
            steps = [f"the {h.relation} of {h.subject} is {h.value}" for h in hops]
            tids = [h.triple for h in hops]
            names = ", ".join(f"{name} ({plabel})" for name, plabel, _tid in found[:5])
            text = ((_sentence(steps) + " ") if steps else "") + (
                f"{'Who' if wh == 'who' else cls.label.capitalize() + 's'} connected to {t.label} that way: {names}."
            )
            all_t = tids + [tid for _n, _p, tid in found[:3]]
            return c.answer(q, t.label, [c.statement(text, c.confidence(ans, *all_t), c.cites(ans, *all_t[:4]))],
                            relation=verb)  # fmt: skip
    return None


def _named_by(ans: Answerer, q: str, cls: Entity, obj: str) -> Answer | None:
    target = resolve(ans, obj)
    if target is None or not target[1]:
        return None
    t, hops = target
    tids = [h.triple for h in hops]
    steps = [f"the {h.relation} of {h.subject} is {h.value}" for h in hops]
    kinds = {
        int(r["o"])
        for r in ans.db.query(
            "SELECT k.o FROM triples k JOIN predicates kp ON kp.id = k.p WHERE k.s = ? AND kp.key IN ('P31', 'P279')",
            (t.id,),
        )
    }
    # say so only when it is known to be something else
    kind = f" (as far as I know {t.label} is not a {cls.label})" if kinds and cls.id not in kinds else ""
    text = f"{_sentence(steps)} So: {t.label}{kind}."
    return c.answer(q, t.label, [c.statement(text, c.confidence(ans, *tids), c.cites(ans, *tids))], relation="identity")


def _human(ans: Answerer) -> Entity | None:
    return ans.graph.by_key(c.HUMAN)


def _verb_relation(ans: Answerer, verb: str) -> bool:
    return bool(ans.predicates_for(verb)) or bool(
        ans.db.scalar("SELECT 1 FROM patterns WHERE middle = ? LIMIT 1", (verb.lower(),))
    )


def connected(ans: Answerer, t: Entity, cls: Entity, verb: str) -> list[tuple[str, str, int]]:
    """Things of kind ``cls`` linked to ``t`` either way, best matching the verb phrase first."""
    kinds = {cls.id} | {
        int(r["s"])
        for r in ans.db.query(
            "SELECT t.s FROM triples t JOIN predicates p ON p.id = t.p WHERE p.key = ? AND t.o = ? LIMIT 200",
            (c.SUBCLASS_OF, cls.id),
        )
    }
    km = ",".join("?" * len(kinds))
    rows = ans.db.query(
        f"SELECT x.id AS other, x.label, x.pagerank, t.id AS tid, t.p, pr.label AS plabel FROM triples t "
        f"JOIN entities x ON x.id = CASE WHEN t.s = ? THEN t.o ELSE t.s END JOIN predicates pr ON pr.id = t.p "
        f"WHERE (t.s = ? OR t.o = ?) AND t.o != 0 AND t.holdout = 0 AND t.status != 'disputed' AND x.id != ? "
        f"AND EXISTS (SELECT 1 FROM triples k JOIN predicates kp ON kp.id = k.p WHERE k.s = x.id "
        f"AND kp.key = 'P31' AND k.o IN ({km})) LIMIT 400",
        [t.id, t.id, t.id, t.id, *kinds],
    )
    vwords = set(re.findall(r"[a-z]+", verb.lower())) - {"the", "a", "an", "is", "are", "was", "were", "of"}
    learned = {
        int(r["predicate_id"])
        for r in ans.db.query("SELECT predicate_id FROM patterns WHERE middle = ?", (verb.lower(),))
    }
    best: dict[int, tuple[float, str, str, int]] = {}
    for r in rows:
        plabel = str(r["plabel"])
        overlap = len(vwords & set(re.findall(r"[a-z]+", plabel.lower())))
        score = 2.0 * (int(r["p"]) in learned) + overlap + 0.001 * float(r["pagerank"] or 0)
        other = int(r["other"])
        if other not in best or score > best[other][0]:
            best[other] = (score, str(r["label"]), plabel, int(r["tid"]))
    ranked = sorted(best.values(), key=lambda v: -v[0])
    return [(name, plabel, tid) for _s, name, plabel, tid in ranked if not re.fullmatch(r"[QP]\d+", name)]
