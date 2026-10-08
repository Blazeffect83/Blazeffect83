"""Builds the frozen tool registry from the built-in tool modules."""
from __future__ import annotations

import re

from pydantic import BaseModel, Field

from . import filesystem, python_runner, research_tools, system_monitor
from .registry import Tool, ToolContext, ToolRegistry, ToolResult


class CompareArgs(BaseModel):
    expected: str = Field(max_length=100_000)
    actual: str = Field(max_length=100_000)
    mode: str = Field(default="exact", pattern="^(exact|contains|regex|strip)$")


def _compare(a: CompareArgs, _ctx: ToolContext) -> ToolResult:
    if a.mode == "exact":
        ok = a.expected == a.actual
    elif a.mode == "strip":
        ok = a.expected.strip() == a.actual.strip()
    elif a.mode == "contains":
        ok = a.expected in a.actual
    else:
        try:
            ok = re.search(a.expected, a.actual) is not None
        except re.error as exc:
            return ToolResult(False, None, "invalid regex", error=str(exc), error_class="invalid_arguments")
    return ToolResult(ok, {"match": ok, "mode": a.mode}, "match" if ok else "mismatch",
                      error=None if ok else "expected and actual output differ", error_class=None if ok else
                      "check_failed")


def build_registry() -> ToolRegistry:
    reg = ToolRegistry()
    for t in (*filesystem.TOOLS, *python_runner.TOOLS, *research_tools.TOOLS, *system_monitor.TOOLS):
        reg.register(t)
    reg.register(Tool("compare_output", "Compare expected and actual output.", CompareArgs, _compare, tier=0))
    reg.freeze()
    return reg
