"""Question skills: answers that need more than looking up one fact.

Each skill recognises its own kind of question and answers it from the knowledge graph, with sources; when none
recognises a question, the general answerer (:class:`polymath.interface.answer.Answerer`) handles it as before.

* :mod:`~polymath.qa.numbers`: comparisons ("Is Everest taller than K2?"), ratios, population density.
* :mod:`~polymath.qa.places`: distances, directions, what is near a place.
* :mod:`~polymath.qa.when`: ages, which came first, who lived at the same time, what happened in a year.
* :mod:`~polymath.qa.causes`: what causes something, what it causes, what prevents it.
* :mod:`~polymath.qa.howto`: how to do something, as ordered steps from Stack Exchange.
* :mod:`~polymath.qa.kinds`: kinds of things, and what they can do (inherited, with exceptions).
* :mod:`~polymath.qa.chains`: multi-step questions ("the mayor of the capital of France").
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from polymath.qa import causes, chains, howto, kinds, numbers, places, when

if TYPE_CHECKING:
    from polymath.interface.answer import Answer, Answerer

SKILLS = (numbers, places, when, causes, howto, kinds, chains)  # most specific patterns first


def answer(ans: Answerer, question: str) -> Answer | None:
    """The first skill that recognises the question answers it; None leaves it to the general answerer."""
    q = question.strip().rstrip("?.! ").strip()
    ql = " ".join(q.lower().split())
    if not ql:
        return None
    for skill in SKILLS:
        got: Answer | None = skill.try_answer(ans, question, ql)
        if got is not None:
            return got
    return None
