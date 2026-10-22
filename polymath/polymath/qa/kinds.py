"""Kinds of things, and what they can do, with exceptions.

* "Is a penguin a bird?" · "Are whales mammals?" (instance of / subclass of / "is a kind of" read in text, followed
  upward, with the chain shown)
* "What kind of thing is a platypus?" (its kinds, and theirs)
* "Can penguins fly?" · "Do birds lay eggs?" · "Does a bat have wings?" The answer is inherited down the kinds:
  birds can fly, penguins are birds, so penguins can fly, unless something nearer says otherwise. "Penguins cannot
  fly" is nearer to penguin than "birds can fly", so the exception wins and the answer says so.
"""

from __future__ import annotations

import re
from collections import deque
from typing import TYPE_CHECKING

from polymath.memory.graph import Entity
from polymath.perception.semantic import KIND_KEY, MIN_TEXT_SOURCES, verb_base
from polymath.qa import common as c

if TYPE_CHECKING:
    from polymath.interface.answer import Answer, Answerer

KIND_KEYS = (c.INSTANCE_OF, c.SUBCLASS_OF, KIND_KEY)
MAX_DEPTH = 6
IS_A = re.compile(r"^(?:is|are|was|were) (?P<rest>.+)$")
KIND_OF = re.compile(
    r"^what (?:kind|type|sort) of (?:thing|animal|organism|object|entity)? ?(?:is|are|was|were) (?P<a>.+)$"
)
CAN = re.compile(r"^(?:can|could|do|does|did) (?P<rest>.+)$")


def try_answer(ans: Answerer, q: str, ql: str) -> Answer | None:
    if m := KIND_OF.match(ql):
        e = c.entity(ans, m["a"], KIND_KEYS)
        if e is None:
            return c.answer(q, None, [], note=f"I could not identify “{c.clean(m['a'])}”.")
        return kinds_of(ans, q, e)
    if m := CAN.match(ql):
        split = split_subject(ans, m["rest"])
        if split is not None and split[1]:
            return can(ans, q, split[0], split[1], ql.split(" ", 1)[0])
        return None
    if m := IS_A.match(ql):
        pair = split_pair(ans, m["rest"])
        if pair is not None:
            return is_a(ans, q, *pair)
    return None


def split_subject(ans: Answerer, rest: str) -> tuple[Entity, str] | None:
    """The longest leading phrase that names a thing exactly, and the verb phrase after it."""
    words = rest.split()
    for i in range(min(len(words) - 1, 6), 0, -1):
        e = c.strict_entity(ans, " ".join(words[:i]), KIND_KEYS)
        if e is not None:
            return e, " ".join(words[i:])
    return None


def split_pair(ans: Answerer, rest: str) -> tuple[Entity, Entity] | None:
    """ "a penguin a bird" / "whales mammals" / "a whale a kind of mammal" → (penguin, bird)."""
    rest = re.sub(r"\b(?:a |an )?(?:kind|type|sort|form) of\b", " ", rest)
    words = [w for w in rest.split() if w]
    for i in range(1, len(words)):
        left, right = " ".join(words[:i]), " ".join(words[i:])
        if left.endswith((" a", " an")) or right in {"a", "an"}:
            continue
        right = re.sub(r"^(?:a|an|the) ", "", right)
        a = c.strict_entity(ans, left, KIND_KEYS)
        b = c.strict_entity(ans, right) if a is not None else None
        if a is not None and b is not None and a.id != b.id:
            return a, b
    return None


def ancestors(ans: Answerer, e: Entity) -> dict[int, tuple[int, int, int]]:
    """Every kind above ``e``: kind → (depth, parent it was reached from, triple). Breadth-first, nearest first."""
    marks = ",".join("?" * len(KIND_KEYS))
    pids = [int(r["id"]) for r in ans.db.query(f"SELECT id FROM predicates WHERE key IN ({marks})", KIND_KEYS)]
    if not pids:
        return {}
    pm = ",".join("?" * len(pids))
    text_pid = int(ans.db.scalar("SELECT id FROM predicates WHERE key = ?", (KIND_KEY,), default=-1))
    seen: dict[int, tuple[int, int, int]] = {}
    queue = deque([(e.id, 0)])
    while queue:
        node, depth = queue.popleft()
        if depth >= MAX_DEPTH:
            continue
        for r in ans.db.query(
            f"SELECT id, o FROM triples WHERE s = ? AND p IN ({pm}) AND o != 0 AND holdout = 0 "
            f"AND status != 'disputed' AND (p != ? OR n_sources >= ?) ORDER BY confidence DESC LIMIT 12",
            [node, *pids, text_pid, MIN_TEXT_SOURCES],
        ):
            o = int(r["o"])
            if o != e.id and o not in seen:
                seen[o] = (depth + 1, node, int(r["id"]))
                queue.append((o, depth + 1))
    return seen


def chain(
    ans: Answerer, start: Entity, target: int, up: dict[int, tuple[int, int, int]]
) -> tuple[list[str], list[int]]:
    names, tids, node = [], [], target
    while node != start.id and node in up:
        _d, parent, tid = up[node]
        names.append(c.label(ans, node))
        tids.append(tid)
        node = parent
    return [start.label, *reversed(names)], list(reversed(tids))


def is_a(ans: Answerer, q: str, a: Entity, b: Entity) -> Answer:
    up = ancestors(ans, a)
    if b.id in up:
        names, tids = chain(ans, a, b.id, up)
        path = " → ".join(names)
        text = f"Yes. {a.label} is a kind of {b.label}: {path}."
        return c.answer(
            q, a.label, [c.statement(text, c.confidence(ans, *tids), c.cites(ans, *tids[:3]))], relation="kind"
        )
    if not up:
        note = f"I have not learned what kind of thing {a.label} is yet."
        return c.answer(q, a.label, [], relation="kind", note=note)
    nearest = [kv for kv in sorted(up.items(), key=lambda kv: kv[1][0]) if _named(ans, kv[0])][:3]
    if not nearest:
        return c.answer(
            q, a.label, [], relation="kind", note=f"I have not learned what kind of thing {a.label} is yet."
        )
    kinds = ", ".join(c.label(ans, k) for k, _v in nearest)
    text = f"Not as far as I know: {a.label} is a kind of {kinds}, and none of those is {b.label}."
    tids = [v[2] for _k, v in nearest]
    return c.answer(
        q, a.label, [c.statement(text, 0.5 * c.confidence(ans, *tids), c.cites(ans, *tids))], relation="kind"
    )


def _named(ans: Answerer, eid: int) -> bool:
    return not re.fullmatch(r"[QP]\d+", c.label(ans, eid))


def kinds_of(ans: Answerer, q: str, e: Entity) -> Answer:
    up = ancestors(ans, e)
    direct = [k for k, v in up.items() if v[0] == 1 and _named(ans, k)][:4]
    if not direct:
        unread = sum(1 for _k, v in up.items() if v[0] == 1)
        s = "s" if unread != 1 else ""
        note = (f"{e.label} is a kind of {unread} thing{s} whose names I have not read yet." if unread
                else f"I have not learned what kind of thing {e.label} is yet.")  # fmt: skip
        return c.answer(q, e.label, [], relation="kind", note=note)
    parts = []
    tids = []
    for k in direct:
        _names, ts = chain(ans, e, k, up)
        higher = [h for h, v in sorted(up.items(), key=lambda kv: kv[1][0]) if v[1] == k and _named(ans, h)][:2]
        more = f" (a kind of {', '.join(c.label(ans, h) for h in higher)})" if higher else ""
        parts.append(f"{c.label(ans, k)}{more}")
        tids += ts
    text = f"{e.label} is a {' and a '.join(parts)}."
    return c.answer(q, e.label, [c.statement(text, c.confidence(ans, *tids), c.cites(ans, *tids[:3]))], relation="kind")


def can(ans: Answerer, q: str, e: Entity, verb_phrase: str, aux: str) -> Answer | None:
    prop = verb_base(re.sub(r"^(?:be able to|ever) ", "", verb_phrase.strip()))
    if aux in {"do", "does", "did"} and prop.startswith(("have ", "has ")):
        prop = "have " + prop.split(" ", 1)[1]
    up = ancestors(ans, e)
    order = [(e.id, 0), *sorted(((k, v[0]) for k, v in up.items()), key=lambda kv: kv[1])]
    hits: list[tuple[int, int, int, int, str, int]] = []  # depth, node, polarity, count, sentence, doc
    for node, depth in order:
        for r in ans.db.query(
            "SELECT polarity, count, sentence, doc_id FROM category_props WHERE entity_id = ? AND prop = ?",
            (node, prop),
        ):
            hits.append((depth, node, int(r["polarity"]), int(r["count"]), str(r["sentence"]), int(r["doc_id"])))
    if not hits:
        if aux in {"do", "does", "did"}:
            return None  # "did Einstein win …": a plain yes/no question for the text search
        return c.answer(q, e.label, [], relation=prop, note=f"I have not read whether {e.label} can {prop}.")
    nearest = min(h[0] for h in hits)
    at = [h for h in hits if h[0] == nearest]
    yes = sum(h[3] for h in at if h[2] == 1)
    no = sum(h[3] for h in at if h[2] == 0)
    verdict = 1 if yes > no else 0
    _d, node, _p, count, sentence, doc = max((h for h in at if h[2] == verdict), key=lambda h: h[3])
    who = e.label if node == e.id else c.label(ans, node)
    phrase = prop if verdict else f"not {prop}"
    if node == e.id:
        text = (
            f"{'Yes' if verdict else 'No'}: {who} can {phrase}"
            if not prop.startswith("have ")
            else (f"{'Yes' if verdict else 'No'}: {who} {'have' if verdict else 'do not have'} {prop[5:]}")
        )
    else:
        names, _t = chain(ans, e, node, up)
        verb = (f"can {phrase}" if not prop.startswith("have ") else
                ("have " if verdict else "do not have ") + prop[5:])  # fmt: skip
        text = f"{'Yes' if verdict else 'No'}, as far as I know: {who} {verb}, and {' → '.join(names)}"
    exceptions = [h for h in hits if h[0] > nearest and h[2] != verdict]
    if exceptions:
        far = c.label(ans, exceptions[0][1])
        text += f" (an exception: {far} generally {'can' if exceptions[0][2] else 'cannot'} {prop})"
    text += "."
    cites = _doc_cite(ans, doc)
    conf = min(0.9, 0.5 + 0.1 * count) * (1.0 if node == e.id else 0.85)
    st = c.statement(text, conf, cites, kind="fact" if node == e.id else "inferred")
    st2 = c.statement(f"From what I read: “{sentence[:200]}”", conf, cites, kind="passage")
    return c.answer(q, e.label, [st, st2], relation=prop)


def _doc_cite(ans: Answerer, doc_id: int) -> list:  # type: ignore[type-arg]
    from polymath.interface.answer import Citation

    r = ans.db.one("SELECT title, url, license, source FROM documents WHERE id = ?", (doc_id,))
    return [Citation(str(r["title"]), r["url"], str(r["license"]), str(r["source"]))] if r else []
