"""Role-based model routing with budget enforcement and usage accounting.

Policy decisions never depend on model output: the router only produces text
and structured data. Authorisation is enforced in :mod:`app.security.policy`.
"""
from __future__ import annotations

import logging
import threading
from datetime import timedelta
from typing import Callable

from ..config import Settings
from ..db import Database, now_iso, utcnow
from .base import BudgetExceeded, ErrorClass, Message, ModelError, ModelProvider, ModelResponse

log = logging.getLogger(__name__)

# Roles whose work is light enough for the cheaper model, when configured.
LIGHT_ROLES = {"extraction", "classification", "formatting", "summary"}
HEAVY_ROLES = {"planning", "debugging", "evaluation", "risk_assessment", "general"}


class Router:
    def __init__(self, settings: Settings, db: Database,
                 providers: dict[str, ModelProvider] | None = None,
                 notify: Callable[[str, str, str], None] | None = None):
        self.settings = settings
        self.db = db
        self._providers: dict[str, ModelProvider] = providers or {}
        self._lock = threading.Lock()
        self._notify = notify
        self._warned_day: str | None = None

    # -- provider construction ----------------------------------------------
    def provider(self, name: str | None = None) -> ModelProvider:
        name = name or self.settings.model_provider
        if name in self._providers:
            return self._providers[name]
        if name == "none":
            raise ModelError("no model provider configured (MODEL_PROVIDER=none)", ErrorClass.UNAVAILABLE)
        if self.settings.local_only and name in ("anthropic", "openai"):
            raise ModelError(f"local-only mode: paid provider '{name}' is disabled", ErrorClass.UNAVAILABLE)
        s = self.settings
        kw = {"timeout": s.model_timeout_seconds, "max_retries": s.model_max_retries}
        if name == "anthropic":
            from .anthropic_provider import AnthropicProvider
            p = AnthropicProvider(s.anthropic_api_key, s.model_name, base_url=s.anthropic_base_url, **kw)
        elif name == "openai":
            from .openai_provider import OpenAIProvider
            p = OpenAIProvider(s.openai_api_key, s.model_name, base_url=s.openai_base_url, **kw)
        elif name == "local":
            from .openai_provider import LocalProvider
            p = LocalProvider(s.local_model_base_url, s.model_name, api_key=s.local_model_api_key, **kw)
        elif name == "mock":
            from .mock_provider import MockProvider
            p = MockProvider()
        else:
            raise ModelError(f"unknown provider {name}", ErrorClass.UNAVAILABLE)
        with self._lock:
            self._providers[name] = p
        return p

    @property
    def available(self) -> bool:
        try:
            self.provider()
            return True
        except ModelError:
            return False

    def model_for(self, role: str) -> str | None:
        if role in LIGHT_ROLES and self.settings.model_name_light:
            return self.settings.model_name_light
        return None  # provider default

    # -- budgets ------------------------------------------------------------
    def spent_today(self, purpose_prefix: str | None = None) -> float:
        start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
        if purpose_prefix:
            return float(self.db.scalar(
                "SELECT COALESCE(SUM(cost_usd),0) FROM model_usage WHERE created_at >= ? AND purpose LIKE ?",
                (start, purpose_prefix + "%")) or 0)
        return float(self.db.scalar(
            "SELECT COALESCE(SUM(cost_usd),0) FROM model_usage WHERE created_at >= ?", (start,)) or 0)

    def requests_last_hour(self) -> int:
        return int(self.db.scalar(
            "SELECT COUNT(*) FROM model_usage WHERE created_at >= ?", (now_iso(timedelta(hours=-1)),)) or 0)

    def objective_tokens(self, objective_id: str) -> int:
        return int(self.db.scalar(
            "SELECT COALESCE(SUM(input_tokens + output_tokens),0) FROM model_usage WHERE objective_id = ?",
            (objective_id,)) or 0)

    def check_budget(self, purpose: str, objective_id: str | None, token_budget: int | None = None) -> None:
        s = self.settings
        spent = self.spent_today()
        if s.daily_spend_limit_usd >= 0 and spent >= s.daily_spend_limit_usd:
            raise BudgetExceeded(
                f"daily API spending limit reached (${spent:.4f} ≥ ${s.daily_spend_limit_usd:.2f}; estimate)")
        if purpose.startswith("research") and \
                self.spent_today("research") >= s.research_daily_spend_limit_usd:
            raise BudgetExceeded("daily research spending limit reached")
        if self.requests_last_hour() >= s.max_requests_per_hour:
            raise BudgetExceeded(f"hourly request limit reached ({s.max_requests_per_hour}/h)")
        if objective_id:
            limit = token_budget or s.max_tokens_per_objective
            used = self.objective_tokens(objective_id)
            if used >= limit:
                raise BudgetExceeded(f"objective token budget exhausted ({used} ≥ {limit})")
        warn_at = s.daily_spend_limit_usd * s.spend_warning_fraction
        day = now_iso()[:10]
        if s.daily_spend_limit_usd > 0 and spent >= warn_at and self._warned_day != day:
            self._warned_day = day
            if self._notify:
                self._notify("warning", "budget_warning",
                             f"API spend at ${spent:.4f} of ${s.daily_spend_limit_usd:.2f} daily limit (estimate)")

    def cost(self, model: str, input_tokens: int, output_tokens: int, provider: str) -> tuple[float, bool]:
        if provider in ("local", "mock"):
            return 0.0, False
        price = self.settings.model_price(model)
        if price is None:
            # Unknown price: conservative placeholder so limits still bite.
            price = (15.0, 75.0)
        inp, out = price
        return (input_tokens * inp + output_tokens * out) / 1_000_000, True

    def _record(self, *, objective_id, purpose, provider, model, resp: ModelResponse | None,
                success: bool, error_class: str | None, latency_ms: int | None) -> None:
        it = resp.input_tokens if resp else 0
        ot = resp.output_tokens if resp else 0
        cost, estimate = self.cost(model, it, ot, provider)
        self.db.insert("model_usage", {
            "objective_id": objective_id, "purpose": purpose, "provider": provider, "model": model,
            "input_tokens": it, "output_tokens": ot, "cost_usd": cost, "cost_is_estimate": int(estimate),
            "success": int(success), "error_class": error_class, "latency_ms": latency_ms,
            "created_at": now_iso(),
        })
        if objective_id and resp:
            self.db.execute(
                "UPDATE objectives SET tokens_used = tokens_used + ?, cost_usd = cost_usd + ? WHERE id = ?",
                (it + ot, cost, objective_id))

    # -- main entry ---------------------------------------------------------
    def generate(self, role: str, messages: list[Message] | str, *, system: str = "",
                 json_schema: dict | None = None, max_tokens: int = 2048,
                 objective_id: str | None = None, token_budget: int | None = None) -> ModelResponse:
        if isinstance(messages, str):
            messages = [Message("user", messages)]
        self.check_budget(role, objective_id, token_budget)
        prov_name = self.settings.model_provider
        try:
            return self._call(prov_name, role, messages, system, json_schema, max_tokens, objective_id)
        except ModelError as exc:
            fb = self.settings.fallback_provider
            if exc.error_class in (ErrorClass.BUDGET, ErrorClass.BAD_REQUEST, ErrorClass.REFUSAL) \
                    or fb in ("none", prov_name):
                raise
            # Only an explicitly configured fallback is used, never an implicit one.
            log.warning("provider %s failed (%s); using configured fallback %s", prov_name, exc.error_class, fb)
            return self._call(fb, role, messages, system, json_schema, max_tokens, objective_id)

    def _call(self, prov_name, role, messages, system, json_schema, max_tokens, objective_id):
        provider = self.provider(prov_name)
        model = self.model_for(role) or provider.model
        try:
            resp = provider.generate(messages, system=system, json_schema=json_schema,
                                     max_tokens=max_tokens, model=model)
        except ModelError as exc:
            self._record(objective_id=objective_id, purpose=role, provider=provider.name, model=model,
                         resp=None, success=False, error_class=exc.error_class, latency_ms=None)
            raise
        self._record(objective_id=objective_id, purpose=role, provider=provider.name, model=model,
                     resp=resp, success=True, error_class=None, latency_ms=resp.latency_ms)
        return resp

    def usage_summary(self, days: int = 1) -> dict:
        since = now_iso(timedelta(days=-days))
        row = self.db.one(
            "SELECT COUNT(*) AS requests, COALESCE(SUM(input_tokens),0) AS input_tokens, "
            "COALESCE(SUM(output_tokens),0) AS output_tokens, COALESCE(SUM(cost_usd),0) AS cost_usd, "
            "COALESCE(MAX(cost_is_estimate),0) AS estimate, COALESCE(SUM(1 - success),0) AS failures "
            "FROM model_usage WHERE created_at >= ?", (since,))
        row["spent_today"] = self.spent_today()
        row["daily_limit"] = self.settings.daily_spend_limit_usd
        return row
