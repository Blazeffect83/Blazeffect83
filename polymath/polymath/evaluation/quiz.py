"""Self-evaluation by quiz.

* **Hold-out**: a deterministic ~5 % of Wikidata entity facts (chosen by a hash
  of the triple id, so the split never drifts) are marked ``holdout``. Reasoning,
  inference, PageRank and answering never see them.
* **Questions**: phrased from the predicate's label (:func:`phrase_question`) — "What is the capital
  of France?", "What is Paris the capital of?", "What does 1789 follow?" — with the true object
  and three **sibling distractors** — other objects of the same predicate,
  preferring ones of the same type (shared ``instance of`` class), never ones
  that are also true for the subject. Only facts whose subject and answer have
  names are asked (an unnamed ``Q123`` cannot be read about).
* **Answering**: the agent's link predictor (:mod:`polymath.reasoning.link_prediction`)
  weighs graph, relatedness, association, text and embedding evidence with
  weights it learned by quizzing itself on *visible* facts (leave-one-out).
  Chance is 25 %; anything above is learned knowledge.
* Results per topic feed the curiosity drive's *gap* estimate.
"""

from __future__ import annotations

import hashlib
import json
import random
import time
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from polymath.core.db import Database
from polymath.reasoning.link_prediction import LinkPredictor

OPTIONS = 4


def is_holdout(triple_id: int, fraction: float) -> bool:
    h = int.from_bytes(hashlib.blake2b(str(triple_id).encode(), digest_size=4).digest(), "big")
    return h % 10_000 < int(fraction * 10_000)


def mark_holdout(db: Database, fraction: float, *, batch: int = 50_000) -> int:
    """Mark new eligible facts (entity-valued, Wikidata-sourced) as held out, by hash."""
    cursor = int(db.kv_get("holdout_cursor", 0))
    rows = db.query(
        "SELECT t.id FROM triples t JOIN entities s ON s.id=t.s WHERE t.id > ? AND t.o != 0 AND t.status='sourced' "
        "AND s.kind != 'property' AND EXISTS (SELECT 1 FROM provenance v WHERE v.triple_id=t.id AND "
        "v.kind='wikidata') ORDER BY t.id LIMIT ?",
        (cursor, batch),
    )
    chosen = [int(r["id"]) for r in rows if is_holdout(int(r["id"]), fraction)]
    db.executemany("UPDATE triples SET holdout=1 WHERE id=?", [(i,) for i in chosen])
    if rows:
        db.kv_set("holdout_cursor", int(rows[-1]["id"]))
    return len(chosen)


def _classes(db: Database, entity: int, p31: int | None) -> set[int]:
    if p31 is None:
        return set()
    return {int(r["o"]) for r in db.query("SELECT o FROM triples WHERE s=? AND p=? AND o != 0", (entity, p31))}


PREPOSITIONS = ("of", "in", "by", "for", "from", "to", "on", "at", "with", "as", "into", "after", "before")
# Labels that start with a third-person verb ("1789 follows 1788", "China shares border with India")
VERBS = {
    "follows": "follow",
    "replaces": "replace",
    "contains": "contain",
    "depicts": "depict",
    "uses": "use",
    "owns": "own",
    "produces": "produce",
    "includes": "include",
    "shares": "share",
    "has": "have",
}


def phrase_question(plabel: str, subject: str) -> str:
    """A readable question for (subject, predicate, ?) from the predicate's label alone.

    "capital" → "What is the capital of France?"; "capital of" → "What is Paris the capital of?";
    "followed by" → "What is 1789 followed by?"; "shares border with" → "What does China share border with?"
    """
    words = plabel.split()
    if not words:
        return f"What is related to {subject}?"
    first, last = words[0].lower(), words[-1].lower()
    if first in VERBS:
        return f"What does {subject} {' '.join([VERBS[first], *words[1:]])}?"
    if last == "of" and len(words) > 1 and not first.endswith("ed"):  # "capital of" → "the capital of"
        article = {"instance": "an", "subclass": "a", "member": "a", "part": ""}.get(first, "the")
        return f"What is {subject} {' '.join([article, *words] if article else words)}?"
    if last in PREPOSITIONS or (len(words) > 1 and first.endswith("ed")):
        return f"What is {subject} {' '.join(words)}?"
    return f"What is the {' '.join(words)} of {subject}?"


def make_question(db: Database, triple: Any, rng: random.Random, p31: int | None) -> dict[str, Any] | None:
    s, p, o = int(triple["s"]), int(triple["p"]), int(triple["o"])
    true_for_s = {int(r["o"]) for r in db.query("SELECT o FROM triples WHERE s=? AND p=? AND o != 0", (s, p))}
    siblings = [
        int(r["o"])
        for r in db.query(
            "SELECT DISTINCT t.o FROM triples t JOIN entities e ON e.id=t.o WHERE t.p=? AND t.o != 0 AND "
            "e.kind='item' AND e.label NOT GLOB 'Q[0-9]*' LIMIT 400",
            (p,),
        )
    ]
    siblings = [x for x in siblings if x not in true_for_s]
    if len(siblings) < OPTIONS - 1:
        return None
    cls = _classes(db, o, p31)
    same_type = [x for x in siblings if cls and _classes(db, x, p31) & cls]
    pool = same_type if len(same_type) >= OPTIONS - 1 else siblings
    distractors = rng.sample(pool, OPTIONS - 1)
    options = [o, *distractors]
    rng.shuffle(options)
    labels = {
        int(r["id"]): str(r["label"])
        for r in db.query(
            f"SELECT id, label FROM entities WHERE id IN ({','.join('?' * (len(options) + 1))})", [*options, s]
        )
    }
    plabel = str(db.scalar("SELECT label FROM predicates WHERE id=?", (p,), default="?"))
    return {
        "triple_id": int(triple["id"]),
        "subject": s,
        "predicate": p,
        "answer": o,
        "options": options,
        "question": phrase_question(plabel, labels.get(s, "?")),
        "predicate_label": plabel,
        "labels": [{"entity": x, "label": labels.get(x, "?")} for x in options],
    }


def questionable_sql(*, holdout: bool) -> str:
    """Entity-valued Wikidata facts whose subject and object are named (quiz / calibration candidates)."""
    return (
        "SELECT t.id, t.s, t.p, t.o FROM triples t JOIN entities s ON s.id=t.s JOIN entities o ON o.id=t.o "
        f"WHERE t.holdout={int(holdout)} AND t.o != 0 AND t.status != 'disputed' AND s.kind='item' AND "
        "o.kind='item' AND s.label NOT GLOB 'Q[0-9]*' AND o.label NOT GLOB 'Q[0-9]*' AND EXISTS "
        "(SELECT 1 FROM provenance v WHERE v.triple_id=t.id AND v.kind='wikidata')"
    )


def run_quiz(
    db: Database,
    predictor: LinkPredictor,
    *,
    size: int = 50,
    seed: int | None = None,
    tick: Callable[[], None] | None = None,
) -> dict[str, Any]:
    rng = random.Random(seed if seed is not None else int(time.time()))
    p31 = db.scalar("SELECT id FROM predicates WHERE key='P31'")
    held = db.query(f"{questionable_sql(holdout=True)} ORDER BY RANDOM() LIMIT ?", (size * 4,))
    questions = []
    for t in held:
        q = make_question(db, t, rng, int(p31) if p31 is not None else None)
        if q is not None:
            questions.append(q)
        if len(questions) >= size:
            break
    if not questions:
        return {"n": 0, "note": "no held-out facts with enough siblings yet"}
    cur = db.execute(
        "INSERT INTO quizzes(created, n, correct, accuracy, chance, details) VALUES(?,?,?,?,?,?)",
        (time.time(), len(questions), 0, 0.0, 1 / OPTIONS, "{}"),
    )
    quiz_id = int(cur.lastrowid or 0)
    by_method: dict[str, list[int]] = defaultdict(list)
    by_pred: dict[str, list[int]] = defaultdict(list)
    alone: dict[str, list[int]] = defaultdict(list)  # each evidence source's own top pick, when it had a view
    correct = 0
    for q in questions:
        pred = predictor.predict(q["subject"], q["predicate"], q["options"])
        if tick is not None:
            tick()
        chosen, conf, method = pred.best, pred.confidence, pred.method
        ok = int(chosen == q["answer"])
        correct += ok
        by_method[method].append(ok)
        for m, dist in pred.by_method.items():
            top = max(q["options"], key=lambda o: dist.get(o, 0.0))
            alone[m].append(int(top == q["answer"]))
        by_pred[q["predicate_label"]].append(ok)
        topic = db.scalar(
            "SELECT dt.topic_id FROM entities e JOIN doc_topics dt ON dt.doc_id=e.doc_id WHERE e.id=? "
            "ORDER BY dt.weight DESC LIMIT 1",
            (q["subject"],),
        )
        db.execute(
            "INSERT INTO quiz_answers(quiz_id, triple_id, question, options, answer, chosen, correct, method, "
            "confidence, topic_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                quiz_id,
                q["triple_id"],
                q["question"],
                json.dumps(q["labels"]),
                q["answer"],
                chosen,
                ok,
                method,
                round(conf, 4),
                topic,
            ),
        )
    details = {
        "by_method": {m: {"n": len(v), "accuracy": round(sum(v) / len(v), 3)} for m, v in by_method.items()},
        "by_predicate": {
            p: {"n": len(v), "accuracy": round(sum(v) / len(v), 3)}
            for p, v in sorted(by_pred.items(), key=lambda kv: -len(kv[1]))[:15]
        },
        "evidence_alone": {m: {"n": len(v), "accuracy": round(sum(v) / len(v), 3)} for m, v in alone.items()},
        "weights": predictor.weights,
    }
    acc = correct / len(questions)
    db.execute(
        "UPDATE quizzes SET correct=?, accuracy=?, details=? WHERE id=?", (correct, acc, json.dumps(details), quiz_id)
    )
    return {
        "quiz_id": quiz_id,
        "n": len(questions),
        "correct": correct,
        "accuracy": round(acc, 4),
        "chance": 1 / OPTIONS,
        **details,
    }
