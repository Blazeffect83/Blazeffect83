"""Error classification and bounded retry decisions."""
from __future__ import annotations

from dataclasses import dataclass

TRANSIENT = {"timeout", "network", "rate_limit", "server", "fetch_failed", "database_locked"}
# Persistent failures that a revised plan may fix (code bugs, wrong args, missing files).
REPLANNABLE = {"tests_failed", "nonzero_exit", "invalid_arguments", "not_found", "no_tests", "check_failed",
               "fetch_rejected", "tool_error", "install_failed", "path_violation", "too_large", "unknown_tool"}
# Never retried or replanned around automatically.
BLOCKING = {"policy", "sandbox_unavailable", "approval_rejected", "budget", "prohibited", "unavailable"}


@dataclass
class RetryDecision:
    action: str  # retry | replan | fail
    delay_seconds: float = 0.0
    reason: str = ""


def decide(error_class: str | None, attempts: int, max_attempts: int, *, tool_tier: int,
           tool_reversible: bool, tool_idempotent: bool) -> RetryDecision:
    ec = error_class or "tool_error"
    if ec in BLOCKING:
        return RetryDecision("fail", reason=f"blocking error ({ec})")
    if ec in TRANSIENT:
        # Approval-gated or irreversible non-idempotent actions are never repeated automatically.
        if tool_tier >= 2 or (not tool_reversible and not tool_idempotent and tool_tier > 1):
            return RetryDecision("fail", reason="transient failure on an action that is not safe to repeat")
        if attempts < max_attempts:
            return RetryDecision("retry", delay_seconds=min(60.0, 2.0 ** attempts), reason=f"transient ({ec})")
        return RetryDecision("replan", reason=f"transient error persisted after {attempts} attempts")
    return RetryDecision("replan", reason=f"persistent error ({ec})")
