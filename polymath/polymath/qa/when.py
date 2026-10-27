"""Questions about time.

* "How old is Paris?" · "How old was Einstein when he died?" (age at death, or today)
* "Which came first, the telephone or the light bulb?" (the date each began)
* "Who was alive at the same time as Mozart?" (lifespans that overlap, best-known first)
* "What happened in 1969?" · "Who was born in 1879?" · "Who died in 1955?"
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

from polymath.memory.graph import Entity
from polymath.qa import common as c
from polymath.reasoning import temporal

if TYPE_CHECKING:
    from polymath.interface.answer import Answer, Answerer

AGE = re.compile(
    r"^how old (?:is|was|are|were) (?P<a>.+?)(?P<died> when (?:he|she|they|it) died| at (?:his|her|their) death)?$"
)
FIRST = re.compile(r"^(?:which|what|who) (?:came|was|happened|is|were) (?:first|earlier),? (?P<a>.+?),? or (?P<b>.+)$")
ALIVE = re.compile(
    r"^(?:who (?:was|were) alive (?:at the same time as|during the life of|when) |who lived at the same time as "
    r"|(?:who were |what were )?(?:the )?contemporaries of )(?P<a>.+)$"
)
YEAR = re.compile(
    r"^(?P<what>what happened|who was born|who were born|who died) in (?:the year )?(?P<y>\d{1,4})(?: (?P<bc>bc|bce))?$"
)
TOP = 8  # people or events listed
POOL = 200_000  # best-known entities searched for contemporaries and events


def try_answer(ans: Answerer, q: str, ql: str) -> Answer | None:
    if m := AGE.match(ql):
        e = c.entity(ans, m["a"], (c.BIRTH, *c.FIRST_DATES))
        return age(ans, q, e, c.clean(m["a"]), bool(m["died"]))
    if m := FIRST.match(ql):
        a, b = c.entity(ans, m["a"], c.FIRST_DATES), c.entity(ans, m["b"], c.FIRST_DATES)
        if a is None or b is None:
            return c.answer(q, None, [], note=f"I could not identify “{c.clean(m['a'] if a is None else m['b'])}”.")
        return first(ans, q, a, b)
    if m := ALIVE.match(ql):
        e = c.entity(ans, m["a"], (c.BIRTH,))
        if e is None:
            return c.answer(q, None, [], note=f"I could not identify “{c.clean(m['a'])}”.")
        return contemporaries(ans, q, e)
    if m := YEAR.match(ql):
        year = -int(m["y"]) if m["bc"] else int(m["y"])
        return in_year(ans, q, year, m["what"])
    return None


def _years(k: tuple[int, int, int]) -> float:
    return k[0] + (max(k[1], 1) - 1) / 12 + (max(k[2], 1) - 1) / 365


def age(ans: Answerer, q: str, e: Entity | None, phrase: str, at_death: bool) -> Answer:
    if e is None:
        return c.answer(q, None, [], note=f"I could not identify “{phrase}”.")
    born = c.date_of(ans, e, c.BIRTH)
    if born is not None:
        died = c.date_of(ans, e, c.DEATH)
        end = died[0] if died else temporal.date_key(temporal.today())
        assert end is not None
        years = int(_years(end) - _years(born[0]))
        cites = c.cites(ans, born[1].triple, *([died[1].triple] if died else []))
        if died:
            text = (f"{e.label} died at about {years} (born {temporal.human_date(born[1].value['time'])}, died "
                    f"{temporal.human_date(died[1].value['time'])}).")  # fmt: skip
        elif at_death:
            text = f"I have no date of death for {e.label}; born {temporal.human_date(born[1].value['time'])}."
            return c.answer(q, e.label, [c.statement(text, 0.5, cites)], relation="age")
        else:
            if years > 122:
                when_born = temporal.human_date(born[1].value["time"])
                note = f"{e.label} was born {when_born}, but I do not know when they died."
                return c.answer(q, e.label, [], relation="age", note=note)
            text = f"{e.label} is about {years} years old (born {temporal.human_date(born[1].value['time'])})."
        conf = c.confidence(ans, born[1].triple, *([died[1].triple] if died else []))
        return c.answer(q, e.label, [c.statement(text, conf, cites, kind="inferred")], relation="age")
    began = c.first_date(ans, e, (c.INCEPTION, c.PUBLISHED, c.START, c.POINT_IN_TIME, c.DISCOVERED))
    if began is None:
        return c.answer(q, e.label, [], relation="age", note=f"I do not know when {e.label} began.")
    k = temporal.date_key(began.value["time"])
    assert k is not None
    now = temporal.date_key(temporal.today())
    assert now is not None
    years = int(_years(now) - _years(k))
    ended = c.date_of(ans, e, c.DISSOLVED)
    if ended:
        lasted = int(_years(ended[0]) - _years(k))
        text = (f"{e.label} lasted about {lasted:,} years ({began.plabel} {temporal.human_date(began.value['time'])}, "
                f"until {temporal.human_date(ended[1].value['time'])}).")  # fmt: skip
        cites = c.cites(ans, began.triple, ended[1].triple)
    else:
        text = f"{e.label} is about {years:,} years old ({began.plabel} {temporal.human_date(began.value['time'])})."
        cites = c.cites(ans, began.triple)
    return c.answer(
        q, e.label, [c.statement(text, c.confidence(ans, began.triple), cites, kind="inferred")], relation="age"
    )


def first(ans: Answerer, q: str, a: Entity, b: Entity) -> Answer:
    da, db = c.first_date(ans, a), c.first_date(ans, b)
    if da is None or db is None:
        missing = a.label if da is None else b.label
        return c.answer(q, a.label, [], relation="date", note=f"I do not know when {missing} began.")
    ka, kb = temporal.date_key(da.value["time"]), temporal.date_key(db.value["time"])
    assert ka is not None and kb is not None
    (e1, d1), (e2, d2) = ((a, da), (b, db)) if ka <= kb else ((b, db), (a, da))
    gap = abs(ka[0] - kb[0])
    text = (f"{e1.label} came first: {d1.plabel} {temporal.human_date(d1.value['time'])}, about {gap:,} years before "
            f"{e2.label} ({d2.plabel} {temporal.human_date(d2.value['time'])}).")  # fmt: skip
    if ka == kb:
        text = f"Both began on {temporal.human_date(da.value['time'])}."
    conf = c.confidence(ans, da.triple, db.triple)
    return c.answer(q, a.label, [c.statement(text, conf, c.cites(ans, da.triple, db.triple))], relation="date")


def _pids(ans: Answerer, *keys: str) -> dict[str, int]:
    return {
        str(r["key"]): int(r["id"])
        for r in ans.db.query(f"SELECT id, key FROM predicates WHERE key IN ({','.join('?' * len(keys))})", keys)
    }


def lifespan(ans: Answerer, e: Entity) -> tuple[int, int, int, int | None] | None:
    born = c.date_of(ans, e, c.BIRTH)
    if born is None:
        return None
    died = c.date_of(ans, e, c.DEATH)
    return (
        born[0][0],
        born[1].triple,
        died[0][0] if died else time.localtime().tm_year,
        died[1].triple if died else None,
    )


def contemporaries(ans: Answerer, q: str, e: Entity) -> Answer:
    span = lifespan(ans, e)
    if span is None:
        return c.answer(q, e.label, [], relation="contemporaries", note=f"I do not know when {e.label} lived.")
    b0, bt, d0, dt = span
    p = _pids(ans, c.BIRTH, c.DEATH)
    if c.BIRTH not in p:
        return c.answer(q, e.label, [], note="I have not learned any dates of birth yet.")
    rows = ans.db.query(
        "SELECT x.id, x.label, b.value AS born, (SELECT d.value FROM triples d WHERE d.s = x.id AND d.p = ? "
        "AND d.holdout = 0 LIMIT 1) AS died FROM (SELECT id, label, pagerank FROM entities WHERE kind = 'item' "
        "ORDER BY pagerank DESC LIMIT ?) x JOIN triples b ON b.s = x.id AND b.p = ? AND b.holdout = 0 "
        "AND b.status != 'disputed' WHERE x.id != ? ORDER BY x.pagerank DESC",
        (p.get(c.DEATH, -1), POOL, p[c.BIRTH], e.id),
    )
    out = []
    seen = set()
    for r in rows:
        kb = temporal.date_key(_time(r["born"]))
        kd = temporal.date_key(_time(r["died"])) if r["died"] else None
        if kb is None or int(r["id"]) in seen:
            continue
        end = kd[0] if kd else time.localtime().tm_year
        if end - kb[0] > 122:
            continue
        overlap = min(d0, end) - max(b0, kb[0])
        if overlap >= 1:
            seen.add(int(r["id"]))
            out.append(f"{r['label']} ({_span(kb[0], kd[0] if kd else None)})")
        if len(out) >= TOP:
            break
    if not out:
        return c.answer(
            q, e.label, [], relation="contemporaries", note=f"I know of no one else alive in {e.label}'s lifetime."
        )
    text = f"{e.label} lived {_span(b0, d0 if dt else None)}. Alive at the same time: " + ", ".join(out) + "."
    cites = c.cites(ans, bt, *([dt] if dt else []))
    return c.answer(
        q, e.label, [c.statement(text, c.confidence(ans, bt), cites, kind="inferred")], relation="contemporaries"
    )


def _time(raw: object) -> str | None:
    import json

    try:
        v = json.loads(str(raw))
    except ValueError:
        return None
    return str(v.get("time")) if isinstance(v, dict) and v.get("time") else None


def _span(b: int, d: int | None) -> str:
    def y(n: int) -> str:
        return f"{-n} BC" if n < 0 else str(n)

    return f"{y(b)}–{y(d)}" if d is not None else f"born {y(b)}"


def in_year(ans: Answerer, q: str, year: int, what: str) -> Answer:
    keys = {"what happened": (c.POINT_IN_TIME, c.START, c.INCEPTION, c.PUBLISHED), "who died": (c.DEATH,)}.get(
        what, (c.BIRTH,)
    )
    p = _pids(ans, *keys)
    if not p:
        return c.answer(q, str(year), [], note="I have not learned dates like that yet.")
    y = f"-{-year}" if year < 0 else str(year)
    marks = ",".join("?" * len(p))
    rows = ans.db.query(
        f"SELECT x.label, t.id, t.value, pr.label AS plabel FROM (SELECT id, label, pagerank FROM entities "
        f"WHERE kind = 'item' ORDER BY pagerank DESC LIMIT ?) x JOIN triples t ON t.s = x.id AND t.p IN ({marks}) "
        f"AND t.holdout = 0 AND t.status != 'disputed' AND (t.value LIKE ? OR t.value LIKE ?) "
        f"JOIN predicates pr ON pr.id = t.p ORDER BY x.pagerank DESC LIMIT ?",
        [POOL, *p.values(), f'{{"time":"{y}-%', f'{{"time":"{y}"}}', TOP * 3],
    )
    items, tids, seen = [], [], set()
    for r in rows:
        if r["label"] in seen:
            continue
        seen.add(r["label"])
        items.append(f"{r['label']}" + (f" ({r['plabel']})" if what == "what happened" else ""))
        tids.append(int(r["id"]))
        if len(items) >= TOP:
            break
    shown = f"{-year} BC" if year < 0 else str(year)
    if not items:
        return c.answer(q, shown, [], note=f"I know of nothing dated {shown} yet.")
    head = {"what happened": f"In {shown}", "who died": f"Died in {shown}"}.get(what, f"Born in {shown}")
    text = f"{head}: " + ", ".join(items) + "."
    return c.answer(
        q, shown, [c.statement(text, c.confidence(ans, *tids[:3]), c.cites(ans, *tids[:3]))], relation="date"
    )
