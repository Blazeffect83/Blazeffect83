"""How-to questions, answered with ordered steps from Stack Exchange.

"How do I reverse a list in Python?" · "How to train a neural network?" · "What are the steps to …?"

The question is matched against the questions of filed answers (``howto_fts``); among the closest, an accepted answer
and a high score win. The steps are shown in order with the answer as the source (CC BY-SA).
"""

from __future__ import annotations

import json
import math
import re
from typing import TYPE_CHECKING, Any

from polymath.qa import common as c

if TYPE_CHECKING:
    from polymath.interface.answer import Answer, Answerer

HOW = re.compile(
    r"^(?:how (?:do|can|should|would|could) (?:i|you|one|we) |how to |what are the steps (?:to|for) |"
    r"what is the best way to |steps to )(?P<task>.+)$"
)
STOP = {"a", "an", "the", "to", "in", "on", "of", "for", "my", "your", "with", "and", "or", "is", "it", "do", "i"}


def try_answer(ans: Answerer, q: str, ql: str) -> Answer | None:
    m = HOW.match(ql)
    if not m:
        return None
    try:
        has = ans.db.scalar("SELECT 1 FROM howto LIMIT 1")
    except Exception:  # an older database without the table
        return None
    if not has:
        return None  # nothing filed yet: let the general answerer search the text
    return steps(ans, q, m["task"])


def steps(ans: Answerer, q: str, task: str) -> Answer | None:
    terms = [t for t in re.findall(r"[a-z0-9+#]+", task.lower()) if t not in STOP]
    if not terms:
        return None
    match = " OR ".join(f'"{t}"' for t in terms[:12])
    rows = ans.db.query(
        "SELECT h.doc_id, h.question, h.steps, h.score, h.accepted, h.site, bm25(howto_fts) AS rank FROM howto_fts "
        "JOIN howto h ON h.doc_id = howto_fts.doc_id WHERE howto_fts MATCH ? ORDER BY rank LIMIT 20",
        (match,),
    )
    if not rows:
        return None

    def score(r: Any) -> float:
        words = set(re.findall(r"[a-z0-9+#]+", str(r["question"]).lower()))
        cover = sum(t in words for t in terms) / len(terms)
        return float(cover * 3 + 0.8 * int(r["accepted"]) + 0.15 * math.log1p(max(0, int(r["score"]))))

    best = max(rows, key=score)
    words = set(re.findall(r"[a-z0-9+#]+", str(best["question"]).lower()))
    if sum(t in words for t in terms) / len(terms) < 0.5:
        return None  # nothing close enough: better the general answerer than a wrong recipe
    items = json.loads(str(best["steps"]))
    text = f"Steps for “{best['question']}”:\n" + "\n".join(f"  {i}. {s}" for i, s in enumerate(items, 1))
    doc = ans.db.one("SELECT title, url, license FROM documents WHERE id = ?", (int(best["doc_id"]),))
    from polymath.interface.answer import Citation

    cite = (
        [Citation(str(doc["title"]), doc["url"], str(doc["license"]), f"stackexchange · {best['site']}")] if doc else []
    )
    trust = ("an accepted answer" if best["accepted"] else "an answer") + f" with score {best['score']}"
    conf = min(0.9, 0.5 + 0.1 * int(best["accepted"]) + 0.05 * math.log1p(max(0, int(best["score"]))))
    return c.answer(q, str(best["question"]), [c.statement(text, conf, cite, kind="passage")], relation="how-to",
                    note=f"From {trust} on {best['site']}.")  # fmt: skip
