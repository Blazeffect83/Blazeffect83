"""Reading meaning that is not a Wikidata relation: causes, kinds of things, and what kinds of things can do.

**Cause and effect** (``perception.semantic``). Between two linked things in a sentence, cue phrases mark a cause:
"smoking *causes* lung cancer", "the famine *led to* emigration", "the outage *was caused by* a storm" (the cause comes
second), "*due to*". Each becomes ``X causes Y`` (or ``X prevents Y``) with the sentence as evidence. The second thing
must end its noun phrase ("the *proton* donors" is not about protons), and a claim read in text is used in answers
only once a second sentence says it too (``MIN_TEXT_SOURCES``).

**Kinds of things** (Hearst patterns). "A penguin *is a* bird", "birds *such as* penguins", "penguins *and other*
birds" become ``X is a kind of Y``. The kind must be something known to be a class (an object of "instance of" or
"subclass of"), so "Paris is a city" counts and "Obama is a lawyer" does not.

**What kinds can do, with exceptions** (:func:`category_props`, called while reading). "Birds can fly",
"penguins cannot fly", "mammals have hair". A sentence that starts with a linked kind followed by can / cannot /
do not / have / lack is stored as a property of that kind with its polarity. :mod:`polymath.qa.kinds` answers
"Can penguins fly?" by walking up from penguin through its kinds; the nearest kind that says something wins, so an
exception overrides the rule it is an exception to.
"""

from __future__ import annotations

import re
import time
from typing import Any

from polymath.core.db import Database
from polymath.core.jobs import JobContext, JobOutcome
from polymath.memory.graph import KnowledgeGraph

# Strong cues only: "contributes to", "is responsible for", "because of" and "triggers" read too often as something
# other than a cause (a real-data run found "algorithm causes logic" and "chess causes software" from them).
_MOD = r"(?:(?:can|may|might|could|often|usually|sometimes|frequently|commonly|is known to|are known to|which|that|"
_MOD += r"also|eventually|ultimately|directly|<det>) )*"
CAUSES = re.compile(
    rf"^{_MOD}(?:cause[sd]?|lead(?:s)? to|led to|result(?:s|ed)? in|increase[sd]? the risk of|"
    r"(?:is|are|was|were) (?:<det> )?(?:main |major |leading |common |primary |direct )?causes? of|"
    r"give[s]? rise to|gave rise to)(?: <det>)?$"
)
CAUSED_BY = re.compile(
    rf"^{_MOD}(?:(?:is|are|was|were|can be|may be|often|usually|mostly|mainly) )*(?:caused by|due to|"
    r"result(?:s|ed)? from|(?:<det> )?(?:direct )?(?:result|consequence) of|brought on by)(?: <det>)?$"
)
# what may follow the second thing when it is the whole noun phrase ("… a mammal." / "… a mammal that …"), not the
# first word of a longer one ("… the proton donors")
PHRASE_END = re.compile(
    r"^(?:\W|$|\s*(?:in|of|from|that|which|who|whose|with|and|or|but|for|by|on|at|to|as|"
    r"when|while|where|because|since|after|before|during|is|are|was|were|has|have)\b)",
    re.I,
)
PREVENTS = re.compile(rf"^{_MOD}(?:prevent[s]?|prevented|protect[s]? against|reduce[sd]? the risk of)(?: <det>)?$")
IS_A = re.compile(
    r"^(?:is|was|are|were)(?: <det>)?"
    r"(?: (?:kind|type|sort|form|species|genus|breed|variety|member|class) of(?: <det>)?)?$"
)
AND_OTHER = re.compile(r"^(?:and|or) other$")
SUCH_AS = re.compile(r"^(?:<det> )?(?:such as|including|for example|e\.g\.|especially)$")
CAUSE_KEY, PREVENT_KEY, KIND_KEY = "text:causes", "text:prevents", "text:is_a"
MIN_TEXT_SOURCES = 2  # sentences that must agree before a cause or kind read in text is used
BATCH = 5000


def _predicate(graph: KnowledgeGraph, key: str) -> int:
    labels = {CAUSE_KEY: "causes", PREVENT_KEY: "prevents", KIND_KEY: "is a kind of"}
    return graph.predicate(key, labels[key])


def is_class(db: Database, eid: int) -> bool:
    """Known as a kind of thing: something is an instance of it, or it is a subclass (or has subclasses)."""
    return bool(
        db.scalar(
            "SELECT 1 FROM triples t JOIN predicates p ON p.id = t.p WHERE p.key IN ('P31', 'P279', ?) AND "
            "(t.o = ? OR (t.s = ? AND p.key = 'P279')) LIMIT 1",
            (KIND_KEY, eid, eid),
        )
    )


def is_person(db: Database, eid: int) -> bool:
    return bool(
        db.scalar(
            "SELECT 1 FROM triples t JOIN predicates p ON p.id = t.p JOIN entities c ON c.id = t.o "
            "WHERE t.s = ? AND p.key = 'P31' AND c.key = 'Q5' LIMIT 1",
            (eid,),
        )
    )


def classify(middle: str) -> tuple[str, bool] | None:
    """(predicate key, reversed?) for a normalised middle, or None. Reversed: the second thing is the subject."""
    m = middle.strip()
    if CAUSED_BY.match(m):
        return CAUSE_KEY, True
    if CAUSES.match(m):
        return CAUSE_KEY, False
    if PREVENTS.match(m):
        return PREVENT_KEY, False
    if IS_A.match(m) or AND_OTHER.match(m):
        return KIND_KEY, False
    if SUCH_AS.match(m):
        return KIND_KEY, True
    return None


def extract(db: Database, *, limit: int = BATCH) -> dict[str, int]:
    """Read new pair contexts (a cursor over their ids) for causes and kinds."""
    graph = KnowledgeGraph(db)
    cursor = int(db.kv_get("semantic_cursor", 0) or 0)
    rows = db.query(
        "SELECT id, e1, e2, doc_id, middle, right_ctx, sentence FROM pair_contexts WHERE id > ? ORDER BY id LIMIT ?",
        (cursor, limit),
    )
    stats = {"contexts": len(rows), "causes": 0, "prevents": 0, "kinds": 0}
    for r in rows:
        got = classify(str(r["middle"]))
        if got is None:
            continue
        key, rev = got
        s, o = (int(r["e2"]), int(r["e1"])) if rev else (int(r["e1"]), int(r["e2"]))
        if s == o or not PHRASE_END.match(str(r["right_ctx"] or "")):
            continue  # the second thing is only the start of a longer phrase: "the proton donors"
        if key == KIND_KEY and (not is_class(db, o) or is_person(db, s)):
            continue  # "Huxley was an English writer" says what he did, not what kind of thing he is
        _tid, added = graph.add_triple(
            s, _predicate(graph, key), o=o, confidence=0.5, kind="pattern", source="text", doc_id=int(r["doc_id"]),
            detail=f"{key.split(':')[1]} '{r['middle']}': {str(r['sentence'])[:300]}", weight=0.8,
        )  # fmt: skip
        stats[{CAUSE_KEY: "causes", PREVENT_KEY: "prevents", KIND_KEY: "kinds"}[key]] += added
    if rows:
        db.kv_set("semantic_cursor", int(rows[-1]["id"]))
    stats["remaining"] = int(len(rows) == limit)
    return stats


def semantic_job(ctx: JobContext) -> JobOutcome:
    total = {"causes": 0, "prevents": 0, "kinds": 0}
    while True:
        res = extract(ctx.db)
        for k in total:
            total[k] += res[k]
        ctx.tick()
        if not res["remaining"] or ctx.should_stop():
            break
    return JobOutcome(done=not res["remaining"], value=0.05 * sum(total.values()), result=total)


def planner(agent: Any) -> None:
    if agent.db.scalar("SELECT 1 FROM pair_contexts LIMIT 1"):
        agent.scheduler.ensure_recurring("perception.semantic", 3600, priority=0.8)


# ------------------------------------------------------------------ what kinds of things can do
PROP = re.compile(
    r"^\s*(?:the |all |most |many |some |a |an )?(?P<subj>[^,;:]{2,60}?)\s+"
    r"(?P<mod>can(?:not|'t)?|could(?:n't| not)?|cannot|are (?:not )?able to|are unable to|do(?:n't| not)?|"
    r"does(?:n't| not)?|never|always|usually|typically|generally|lack|have|has)\s+"
    r"(?P<verb>[a-z]+(?: [a-z]+)?)\b",
    re.I,
)
NEGATIVE = {"cannot", "can't", "couldn't", "could not", "are not able to", "are unable to", "don't", "do not",
            "doesn't", "does not", "never", "lack"}  # fmt: skip
STOP_VERBS = {"be", "been", "the", "a", "an", "to", "not", "also", "only", "become", "such", "this", "that",
              "it", "they", "many", "some", "more", "most", "very", "much", "both", "each", "its", "their"}  # fmt: skip
TRANSITIVE = {"have", "has", "lack", "lay", "eat", "build", "produce", "contain", "use", "make"}


def category_props(sentence: str, first_link: tuple[int, int, int] | None) -> tuple[int, str, int] | None:
    """(entity, property, polarity) when a sentence states what a kind of thing can or cannot do.

    ``first_link`` is (start, end, entity) of the first linked thing in the sentence; it must be the subject.
    """
    if first_link is None:
        return None
    m = PROP.match(sentence)
    if not m:
        return None
    a, b, eid = first_link
    if not (abs(m.start("subj") - a) <= 1 and b <= m.end("subj") + 1):
        return None  # the kind must be the sentence's subject
    mod = m["mod"].lower()
    words = m["verb"].lower().split()
    if not words or words[0] in STOP_VERBS:
        return None
    polarity = 0 if mod in NEGATIVE else 1
    if mod in {"have", "has", "lack"}:
        prop = "have " + words[0]
    elif words[0] in TRANSITIVE and len(words) > 1 and words[1] not in STOP_VERBS:
        prop = " ".join(words[:2])
    else:
        prop = words[0]
    if prop.startswith("has "):
        prop = "have " + prop[4:]
    return eid, verb_base(prop), polarity


def verb_base(prop: str) -> str:
    """ "flies" → "fly", "has feathers" → "have feathers": one form per property."""
    words = prop.split()
    v = words[0]
    if v == "has":
        v = "have"
    elif v.endswith("ies") and len(v) > 4:
        v = v[:-3] + "y"
    elif v.endswith(("ches", "shes", "sses", "xes")):
        v = v[:-2]
    elif v.endswith("s") and not v.endswith("ss") and len(v) > 3:
        v = v[:-1]
    return " ".join([v, *words[1:]])


def store_props(db: Database, found: list[tuple[int, str, int, int, str]]) -> None:
    """Rows of (entity, property, polarity, doc, sentence)."""
    now = time.time()
    db.executemany(
        "INSERT INTO category_props(entity_id, prop, polarity, count, doc_id, sentence, updated) VALUES(?,?,?,1,?,?,?) "
        "ON CONFLICT(entity_id, prop, polarity) DO UPDATE SET count = count + 1, updated = excluded.updated",
        [(e, p, pol, d, s[:400], now) for e, p, pol, d, s in found],
    )
