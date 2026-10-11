"""Number sense: compare things by their quantities, work out ratios and derived quantities.

* "Is Mount Everest taller than K2?" · "Which is bigger, France or Spain?" · "Who is older, Newton or Leibniz?"
* "How many times bigger is Russia than France?" · "How much taller is Everest than K2?"
* "What is the population density of France?" (population ÷ area)

Quantities are converted to one unit per dimension first (:mod:`polymath.reasoning.units`), so 29,032 ft and
8,849 m compare correctly. Ages compare by the date something began (born, founded, published).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from polymath.memory.graph import Entity
from polymath.qa import common as c
from polymath.reasoning import temporal, units

if TYPE_CHECKING:
    from polymath.interface.answer import Answer, Answerer

# adjective → (relations to compare, in order of preference; +1 when the adjective means "more")
ADJ: dict[str, tuple[list[str], int]] = {
    "taller": (["height", "elevation above sea level", "elevation"], 1),
    "higher": (["elevation above sea level", "height", "elevation"], 1),
    "shorter": (["height", "length"], -1),
    "lower": (["elevation above sea level", "height"], -1),
    "bigger": (["area", "population", "mass", "length"], 1),
    "larger": (["area", "population", "mass", "length"], 1),
    "smaller": (["area", "population", "mass", "length"], -1),
    "heavier": (["mass", "weight"], 1),
    "lighter": (["mass", "weight"], -1),
    "longer": (["length", "duration"], 1),
    "deeper": (["depth", "maximum depth"], 1),
    "shallower": (["depth", "maximum depth"], -1),
    "faster": (["speed", "maximum speed", "top speed"], 1),
    "slower": (["speed", "maximum speed", "top speed"], -1),
    "hotter": (["temperature", "average temperature", "surface temperature"], 1),
    "warmer": (["temperature", "average temperature", "surface temperature"], 1),
    "colder": (["temperature", "average temperature", "surface temperature"], -1),
    "wider": (["width"], 1),
    "narrower": (["width"], -1),
    "more populous": (["population"], 1),
    "less populous": (["population"], -1),
}
AGE = {"older": 1, "younger": -1}
_ADJ = "|".join(sorted([*ADJ, *AGE], key=len, reverse=True))
YES_NO = re.compile(rf"^(?:is|are|was|were) (?P<a>.+?) (?P<adj>{_ADJ}) than (?P<b>.+)$")
WHICH = re.compile(rf"^(?:which|who|what) (?:is|was|are|were) (?:the )?(?P<adj>{_ADJ}),? (?P<a>.+?),? or (?P<b>.+)$")
RATIO = re.compile(rf"^how many times (?P<adj>{_ADJ}) (?:is|are|was|were) (?P<a>.+?) than (?P<b>.+)$")
DIFF = re.compile(rf"^how much (?P<adj>{_ADJ}) (?:is|are|was|were) (?P<a>.+?) than (?P<b>.+)$")
DENSITY = re.compile(r"^(?:what is |what's )?(?:the )?population density of (?P<a>.+)$")


def quantity(ans: Answerer, e: Entity, labels: list[str]) -> tuple[float, str, c.Value] | None:
    """(value in the base unit, dimension, the fact) for the first relation this entity has a number for."""
    for v in c.by_labels(ans, e, labels):
        if isinstance(v.value, dict) and "amount" in v.value:
            base = units.to_base(float(v.value["amount"]), v.value.get("unit"))
            if base is not None:
                return base[0], base[1], v
    return None


def try_answer(ans: Answerer, q: str, ql: str) -> Answer | None:
    m = DENSITY.match(ql)
    if m:
        return density(ans, q, c.clean(m["a"]))
    for pat, mode in ((YES_NO, "yes/no"), (WHICH, "which"), (RATIO, "ratio"), (DIFF, "diff")):
        m = pat.match(ql)
        if m:
            need = c.FIRST_DATES if m["adj"] in AGE else _keys(ans, ADJ[m["adj"]][0])
            a, b = c.entity(ans, m["a"], need), c.entity(ans, m["b"], need)
            if a is None or b is None or a.id == b.id:
                missing = m["a"] if a is None else m["b"]
                return c.answer(q, None, [], note=f"I could not identify “{c.clean(missing)}”.")
            if m["adj"] in AGE:
                return age_compare(ans, q, a, b, m["adj"], mode)
            return compare(ans, q, a, b, m["adj"], mode)
    return None


def _keys(ans: Answerer, labels: list[str]) -> list[str]:
    """Property keys behind relation names (to pick, of several things called alike, the one with a value)."""
    keys = []
    for lab in labels:
        for p in ans.predicates_for(lab, [lab]):
            keys.append(str(ans.db.scalar("SELECT key FROM predicates WHERE id = ?", (p,), default="")))
    return [k for k in keys if k]


def compare(ans: Answerer, q: str, a: Entity, b: Entity, adj: str, mode: str) -> Answer:
    labels, sign = ADJ[adj]
    found = None
    for lab in labels:  # the first relation both have, in the same dimension
        qa, qb = quantity(ans, a, [lab]), quantity(ans, b, [lab])
        if qa and qb and qa[1] == qb[1]:
            found = (lab, qa, qb)
            break
    if found is None:
        have = a.label if quantity(ans, a, labels) else b.label if quantity(ans, b, labels) else None
        only = f" (only of {have})." if have else "."
        note = f"I do not know the {labels[0]} of both {a.label} and {b.label}{only}"
        return c.answer(q, a.label, [], relation=labels[0], note=note)
    lab, (va, dim, fa), (vb, _d, fb) = found
    sa, sb = units.show(va, dim), units.show(vb, dim)
    more = va > vb if sign > 0 else va < vb
    winner, loser = (a, b) if more else (b, a)
    wv, lv = (sa, sb) if more else (sb, sa)
    gap = abs(va - vb)
    rel = gap / max(abs(va), abs(vb)) if max(abs(va), abs(vb)) else 0.0
    conf = c.confidence(ans, fa.triple, fb.triple)
    cites = c.cites(ans, fa.triple, fb.triple)
    if va == vb:
        text = f"Neither: {a.label} and {b.label} have the same {lab} ({sa})."
    elif mode == "ratio":
        big, small = max(va, vb), min(va, vb)
        if small <= 0:
            return c.answer(q, a.label, [], relation=lab, note="One of the two is zero, so there is no ratio.")
        hi, lo = (a, b) if va >= vb else (b, a)
        shown = f"{units.show(big, dim)} against {units.show(small, dim)}"
        text = f"{hi.label} is about {big / small:,.1f} times the {lab} of {lo.label} ({shown})."
    elif mode == "diff":
        word = adj if winner is a else _opposite(adj)
        text = f"{winner.label} is {word} by {units.show(gap, dim)}: {wv} against {lv} ({rel:.0%})."
    else:
        lead = ("Yes. " if winner is a else "No. ") if mode == "yes/no" else ""
        by = f"by {units.show(gap, dim)} ({rel:.0%})"
        text = f"{lead}{winner.label} ({wv}) is {adj} than {loser.label} ({lv}), {by}; compared by {lab}."
    return c.answer(q, a.label, [c.statement(text, conf, cites)], relation=lab)


def _opposite(adj: str) -> str:
    pairs = {"taller": "shorter", "higher": "lower", "bigger": "smaller", "larger": "smaller", "heavier": "lighter",
             "longer": "shorter", "deeper": "shallower", "faster": "slower", "hotter": "colder", "warmer": "colder",
             "wider": "narrower", "more populous": "less populous", "older": "younger"}  # fmt: skip
    rev = {v: k for k, v in pairs.items()}
    return pairs.get(adj) or rev.get(adj, adj)


def age_compare(ans: Answerer, q: str, a: Entity, b: Entity, adj: str, mode: str) -> Answer:
    da, db = c.first_date(ans, a), c.first_date(ans, b)
    if da is None or db is None:
        return c.answer(q, a.label, [], relation="age", note=f"I do not know when both {a.label} and {b.label} began.")
    ka, kb = temporal.date_key(da.value["time"]), temporal.date_key(db.value["time"])
    assert ka is not None and kb is not None
    older, younger = (a, b) if ka < kb else (b, a)
    od, yd = (da, db) if ka < kb else (db, da)
    winner = older if adj == "older" else younger
    lead = ("Yes. " if winner is a else "No. ") if mode == "yes/no" else ""
    years = abs(ka[0] - kb[0])
    text = (
        f"{lead}{older.label} is older: {od.plabel} {temporal.human_date(od.value['time'])}, against "
        f"{younger.label}'s {yd.plabel} {temporal.human_date(yd.value['time'])} (about {years:,} years apart)."
    )
    if ka == kb:
        text = f"They began at the same time: {temporal.human_date(da.value['time'])}."
    conf = c.confidence(ans, da.triple, db.triple)
    return c.answer(q, a.label, [c.statement(text, conf, c.cites(ans, da.triple, db.triple))], relation="age")


def density(ans: Answerer, q: str, phrase: str) -> Answer:
    e = c.entity(ans, phrase, _keys(ans, ["population", "area"]))
    if e is None:
        return c.answer(q, None, [], note=f"I could not identify “{phrase}”.")
    pop, area = quantity(ans, e, ["population"]), quantity(ans, e, ["area"])
    if not pop or not area or area[1] != "area" or area[0] <= 0:
        note = f"I need both the population and the area of {e.label}, and do not know both yet."
        return c.answer(q, e.label, [], relation="population density", note=note)
    per_km2 = pop[0] / (area[0] / 1e6)
    text = (
        f"About {units.show(per_km2, 'count')} people per km² ({units.show(per_km2 * 2.589988, 'count')} per sq mi): "
        f"a population of {units.show(pop[0], 'count')} on {units.show(area[0], 'area')}."
    )
    conf = c.confidence(ans, pop[2].triple, area[2].triple)
    st = c.statement(text, conf, c.cites(ans, pop[2].triple, area[2].triple), kind="inferred")
    return c.answer(q, e.label, [st], relation="population density")
