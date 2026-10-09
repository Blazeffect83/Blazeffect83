"""Deterministic policy enforcement.

The policy layer sits between the agent and every tool. It is pure code: model
output can *propose* actions but can never change tiers, allowlists or
approvals. Tiers:

* 0 READ ONLY            — runs autonomously
* 1 RESTRICTED EXECUTION — runs autonomously inside workspace/sandbox limits
* 2 APPROVAL REQUIRED    — blocked until a human approves that exact action
* 3 PROHIBITED           — always denied

Tier-3 protection is structural: no registered tool can act on third-party
systems, read credentials or change AEGIS' own permissions, and the sandbox
has no network or host-credential access. ``screen_objective`` is only a
coarse early filter that refuses obviously out-of-scope objectives.
"""
from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from ..tools.registry import Tool

ALLOW = "allow"
REQUIRE_APPROVAL = "require_approval"
DENY = "deny"

TIER_NAMES = {0: "read-only", 1: "restricted execution", 2: "human approval required", 3: "prohibited"}

# Short, generic out-of-scope markers. Matching is case-insensitive substring.
_OUT_OF_SCOPE_MARKERS = (
    "malware", "ransomware", "keylogger", "botnet", "rootkit",
    "credential theft", "steal credentials", "steal passwords",
    "evade detection", "disable the audit", "bypass the approval", "bypass approval",
    "grant yourself", "escalate your own permissions", "self-replicate", "replicate yourself",
)


@dataclass
class Decision:
    verdict: str
    tier: int
    reason: str


def screen_objective(goal: str) -> str | None:
    """Return a refusal reason if the objective text is clearly out of scope."""
    text = " ".join(goal.lower().split())
    for marker in _OUT_OF_SCOPE_MARKERS:
        if marker in text:
            return f"objective appears out of scope (tier 3: '{marker}')"
    return None


class PolicyEngine:
    def decide(self, tool: Tool, args: BaseModel, allowed_tools: list[str] | None = None) -> Decision:
        tier = tool.effective_tier(args)
        if allowed_tools and tool.name not in allowed_tools:
            return Decision(DENY, tier, f"tool '{tool.name}' is not in this objective's allowed tools")
        if tier >= 3:
            return Decision(DENY, tier, "prohibited action")
        if tier == 2:
            return Decision(REQUIRE_APPROVAL, tier, f"tier 2 ({TIER_NAMES[2]})")
        return Decision(ALLOW, tier, f"tier {tier} ({TIER_NAMES[tier]})")
