"""Tool registry. Tools are defined in code; neither the model nor any tool can
register, modify or re-tier tools at runtime."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import BaseModel, ValidationError


class ToolError(Exception):
    def __init__(self, message: str, error_class: str = "tool_error", retryable: bool = False):
        super().__init__(message)
        self.error_class = error_class
        self.retryable = retryable


@dataclass
class ToolResult:
    ok: bool
    output: Any = None
    summary: str = ""
    exit_code: int | None = None
    error: str | None = None
    error_class: str | None = None
    resource_usage: dict = field(default_factory=dict)


@dataclass
class ToolContext:
    objective_id: str | None
    task_id: str | None
    services: Any  # app.services.Services


@dataclass(frozen=True)
class Tool:
    name: str
    purpose: str
    input_model: type[BaseModel]
    run: Callable[[BaseModel, ToolContext], ToolResult]
    tier: int  # 0 read-only, 1 restricted execution, 2 approval required
    permissions: tuple[str, ...] = ()
    timeout_seconds: int = 60
    resource_limits: str = ""
    reversible: bool = True
    idempotent: bool = True  # safe to re-run automatically after a crash
    output_description: str = "JSON object"
    audit: str = "standard"  # standard | full-output
    # Optional dynamic escalation: returns a higher tier for risky arguments.
    tier_for: Callable[[BaseModel], int] | None = None
    # Describes effects for approval requests: (affected, consequences, operation)
    describe: Callable[[BaseModel], tuple[str, str, str]] | None = None

    def validate(self, args: dict) -> BaseModel:
        try:
            return self.input_model.model_validate(args or {})
        except ValidationError as exc:
            raise ToolError(f"invalid arguments for {self.name}: {exc.errors(include_url=False)}",
                            "invalid_arguments") from exc

    def effective_tier(self, args: BaseModel) -> int:
        t = self.tier
        if self.tier_for is not None:
            t = max(t, self.tier_for(args))
        return t

    def schema(self) -> dict:
        return {
            "name": self.name,
            "purpose": self.purpose,
            "tier": self.tier,
            "input_schema": self.input_model.model_json_schema(),
            "output": self.output_description,
            "permissions": list(self.permissions),
            "timeout_seconds": self.timeout_seconds,
            "resource_limits": self.resource_limits,
            "reversible": self.reversible,
            "idempotent": self.idempotent,
            "approval_required": self.tier >= 2,
            "audit": self.audit,
        }


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}
        self._frozen = False

    def register(self, tool: Tool) -> None:
        if self._frozen:
            raise ToolError("tool registry is frozen", "policy")
        if tool.tier not in (0, 1, 2):
            raise ToolError("tier 3 actions cannot be registered as tools", "policy")
        if tool.name in self._tools:
            raise ToolError(f"duplicate tool {tool.name}", "policy")
        self._tools[tool.name] = tool

    def freeze(self) -> None:
        self._frozen = True

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise ToolError(f"unknown tool '{name}'", "unknown_tool")
        return self._tools[name]

    def names(self) -> list[str]:
        return sorted(self._tools)

    def all(self) -> list[Tool]:
        return [self._tools[n] for n in self.names()]

    def catalogue(self, allowed: list[str] | None = None) -> list[dict]:
        return [t.schema() for t in self.all() if not allowed or t.name in allowed]
