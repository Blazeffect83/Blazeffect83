"""Who is doing this, right now (v0.13.0): the job slice and the agent, for the record of every fact.

The loop sets ``current_job`` around each slice and the society sets ``current_agent`` around each agent's turn;
``KnowledgeGraph.add_triple`` writes both into the evidence it records (``provenance.job_id``/``agent_id``), so
``polymath why`` can trace a fact back to the job, the decision that chose it, and the agent that acted.
"""

from __future__ import annotations

from contextvars import ContextVar

current_job: ContextVar[int | None] = ContextVar("current_job", default=None)
current_agent: ContextVar[int | None] = ContextVar("current_agent", default=None)
