"""Request schemas (validated input for every state-changing endpoint)."""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class ObjectiveIn(BaseModel):
    goal: str = Field(min_length=5, max_length=5000)
    kind: str = Field(default="general", pattern="^(general|research|coding|monitor)$")  # deep: /api/research/deep
    priority: int = Field(default=5, ge=1, le=9)
    success_criteria: list[str] = Field(default_factory=list, max_length=20)
    constraints: list[str] = Field(default_factory=list, max_length=20)
    allowed_tools: list[str] = Field(default_factory=list, max_length=50)
    deadline: str | None = None
    budget_tokens: int | None = Field(default=None, ge=1000, le=10_000_000)
    budget_usd: float | None = Field(default=None, ge=0, le=1000)
    time_budget_minutes: int | None = Field(default=None, ge=1, le=7 * 24 * 60)


class ClarifyIn(BaseModel):
    answer: str = Field(min_length=1, max_length=2000)


class TopicIn(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    description: str = Field(default="", max_length=1000)
    query: str = Field(default="", max_length=300)
    feeds: list[str] = Field(default_factory=list, max_length=50)
    seed_urls: list[str] = Field(default_factory=list, max_length=100)
    allowed_domains: list[str] = Field(default_factory=list, max_length=100)
    interval_minutes: int = Field(default=720, ge=15, le=60 * 24 * 30)
    max_docs_per_run: int = Field(default=10, ge=1, le=100)
    freshness_days: int = Field(default=365, ge=1, le=3650)
    priority: int = Field(default=5, ge=1, le=9)
    enabled: bool = True


class TopicPatch(BaseModel):
    description: str | None = Field(default=None, max_length=1000)
    query: str | None = Field(default=None, max_length=300)
    feeds: list[str] | None = None
    seed_urls: list[str] | None = None
    allowed_domains: list[str] | None = None
    interval_minutes: int | None = Field(default=None, ge=15, le=60 * 24 * 30)
    max_docs_per_run: int | None = Field(default=None, ge=1, le=100)
    freshness_days: int | None = Field(default=None, ge=1, le=3650)
    priority: int | None = Field(default=None, ge=1, le=9)
    enabled: bool | None = None


class ResolveIn(BaseModel):
    keep_claim: int | None = None
    reason: str = Field(min_length=3, max_length=500)


class BudgetIn(BaseModel):
    daily_spend_limit_usd: float | None = Field(default=None, ge=0, le=1000)
    research_daily_spend_limit_usd: float | None = Field(default=None, ge=0, le=1000)
    max_requests_per_hour: int | None = Field(default=None, ge=0, le=100_000)
    max_tokens_per_objective: int | None = Field(default=None, ge=1000, le=100_000_000)


class LoginIn(BaseModel):
    username: str = Field(max_length=100)
    password: str = Field(max_length=500)


class DeepResearchIn(BaseModel):
    topic: str = Field(min_length=5, max_length=2000)
    hours: float = Field(default=2.0, gt=0, le=48)
    max_documents: int = Field(default=200, ge=1, le=5000)
    max_depth: int = Field(default=2, ge=0, le=4)
    max_per_domain: int = Field(default=15, ge=1, le=500)
    sources: list[str] = Field(default_factory=lambda: ["wikipedia", "openalex", "searxng", "web", "links"])
    allowed_domains: list[str] = Field(default_factory=list, max_length=200)
    seed_urls: list[str] = Field(default_factory=list, max_length=200)
    stop_on_saturation: bool = True
    priority: int = Field(default=6, ge=1, le=9)
    budget_usd: float | None = Field(default=None, ge=0, le=1000)

    @field_validator("sources")
    @classmethod
    def _sources(cls, v):
        bad = set(v) - {"wikipedia", "openalex", "searxng", "web", "links"}
        if bad or not v:
            raise ValueError(f"sources must be a non-empty subset of wikipedia, openalex, searxng, web, links ({bad})")
        return v
