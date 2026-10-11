"""Big-brain mode, announced: when the brain gets a large drive, it says what it now reads and keeps.

The settings themselves are raised when the configuration loads (:func:`polymath.core.config.apply_capacity`):
word vectors over 400,000 words instead of 200,000, 40 OpenAlex and PubMed files instead of 2, 3,000 books instead of
200, fifteen Stack Exchange sites instead of three, and Spanish Wikipedia as a second language. Settings in
``polymath.toml`` are never overridden. This planner records the switch (and a switch back) in the changelog once.
"""

from __future__ import annotations

from typing import Any

from polymath.core.config import BIG_BRAIN, brain_tier
from polymath.drive import changelog


def planner(agent: Any) -> None:
    tier = brain_tier(agent.config)
    before = agent.db.kv_get("brain_tier")
    if before == tier:
        return
    agent.db.kv_set("brain_tier", tier)
    if before is None and tier == "normal":
        return  # nothing to announce on a first start with a normal budget
    gb = agent.config.body.disk_budget_gb
    if tier == "big":
        changelog.record(
            agent.db, "capacity", "big-brain mode", "adjusted",
            f"Switched to big-brain mode ({gb:,.0f} GB of room): 40 OpenAlex and PubMed files, 3,000 books, 15 Stack "
            f"Exchange sites, Spanish Wikipedia as a second language, and word vectors for 400,000 words",
            detail={"budget_gb": gb, "raised": [f"{a}.{b}" for a, b in BIG_BRAIN]},
        )  # fmt: skip
    else:
        changelog.record(agent.db, "capacity", "big-brain mode", "adjusted",
                         f"Back to normal mode ({gb:,.0f} GB of room)", detail={"budget_gb": gb})  # fmt: skip
