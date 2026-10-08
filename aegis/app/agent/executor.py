"""Executes one task through the policy layer with approval checks,
idempotency, auditing and resource capture."""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass

from ..db import dumps, loads, now_iso
from ..security.approvals import ApprovalError
from ..security.policy import DENY, REQUIRE_APPROVAL
from ..security.redact import get_redactor
from ..tools.registry import ToolContext, ToolError, ToolResult

log = logging.getLogger(__name__)


@dataclass
class ExecOutcome:
    kind: str  # executed | needs_approval | denied | rejected
    result: ToolResult | None = None
    approval_id: str | None = None
    reason: str = ""


def idempotency_key(objective_id: str, task_id: str, attempt: int) -> str:
    return hashlib.sha256(f"{objective_id}:{task_id}:{attempt}".encode()).hexdigest()[:32]


class Executor:
    def __init__(self, services):
        self.s = services

    def execute(self, objective: dict, task: dict) -> ExecOutcome:
        s = self.s
        tool = s.tools.get(task["tool"])
        raw_args = loads(task["args"], {})
        try:
            args = tool.validate(raw_args)
        except ToolError as exc:
            return ExecOutcome("executed", ToolResult(False, None, "invalid arguments", error=str(exc),
                                                      error_class="invalid_arguments"))
        allowed = loads(objective["allowed_tools"], []) or None
        decision = s.policy.decide(tool, args, allowed)
        args_dump = args.model_dump(mode="json")
        audit_common = dict(objective_id=objective["id"], task_id=task["id"], tool=tool.name, args=args_dump)

        if decision.verdict == DENY:
            s.audit.record("tool.denied", decision=f"deny: {decision.reason}", **audit_common)
            return ExecOutcome("denied", reason=decision.reason)

        approval_id = None
        if decision.verdict == REQUIRE_APPROVAL:
            approval_id = task.get("approval_id")
            appr = s.approvals.get(approval_id) if approval_id else None
            s.approvals.expire_stale()
            if appr:
                appr = s.approvals.get(approval_id)
            if appr and appr["status"] == "rejected":
                s.audit.record("tool.approval_rejected", decision="rejected", approval_id=approval_id, **audit_common)
                return ExecOutcome("rejected", approval_id=approval_id, reason="action rejected by the user")
            if not appr or appr["status"] in ("expired", "consumed"):
                affected, consequences, operation = (tool.describe(args) if tool.describe else
                                                     ("see arguments", "see tool purpose", dumps(args_dump)))
                approval_id = s.approvals.request(
                    objective_id=objective["id"], task_id=task["id"], tool=tool.name, args=args_dump,
                    reason=f"Step '{task['title']}' of objective: {objective['goal'][:300]}",
                    affected=affected, consequences=consequences, reversible=tool.reversible,
                    operation=operation, scope=f"single execution of {tool.name} with exactly these arguments")
                s.audit.record("approval.requested", decision="require_approval", approval_id=approval_id,
                               **audit_common)
                s.notifier.notify("warning", "approval_required", f"Approval required: {tool.name}",
                                  f"Objective {objective['id']}: {task['title']}")
                return ExecOutcome("needs_approval", approval_id=approval_id, reason=decision.reason)
            if appr["status"] == "pending":
                return ExecOutcome("needs_approval", approval_id=approval_id, reason="awaiting decision")
            try:
                s.approvals.consume(approval_id, tool.name, args_dump)
            except ApprovalError as exc:
                s.audit.record("approval.invalid", decision=f"deny: {exc}", approval_id=approval_id, **audit_common)
                return ExecOutcome("denied", approval_id=approval_id, reason=f"approval invalid: {exc}")

        key = idempotency_key(objective["id"], task["id"], task["attempts"])
        prior = s.db.one("SELECT * FROM tool_runs WHERE idempotency_key = ?", (key,))
        if prior and prior["status"] in ("succeeded", "failed"):
            # Already executed (e.g. crash after the run, before the state update): reuse, don't repeat.
            out = loads(prior["output"], None)
            return ExecOutcome("executed", ToolResult(prior["status"] == "succeeded", out,
                                                      "reused recorded result", prior["exit_code"],
                                                      prior["error"], prior["error_class"]), approval_id)
        if prior and prior["status"] == "started":
            # A previous run of this exact attempt never finished (crash). Never repeat it blindly.
            return ExecOutcome("executed", ToolResult(False, None, "previous run was interrupted",
                                                      error="interrupted before completion",
                                                      error_class="interrupted"), approval_id)
        red = get_redactor()
        run_id = s.db.insert("tool_runs", {
            "idempotency_key": key, "objective_id": objective["id"], "task_id": task["id"], "tool": tool.name,
            "args": dumps(red.obj(args_dump)), "status": "started", "started_at": now_iso()})
        start = time.monotonic()
        try:
            result = tool.run(args, ToolContext(objective["id"], task["id"], s))
        except ToolError as exc:
            result = ToolResult(False, None, str(exc)[:300], error=str(exc), error_class=exc.error_class)
        except Exception as exc:  # tool bug: record, never crash the worker
            log.exception("tool %s crashed", tool.name)
            result = ToolResult(False, None, "tool crashed", error=f"{type(exc).__name__}: {exc}",
                                error_class="tool_error")
        duration = int((time.monotonic() - start) * 1000)
        output = dumps(red.obj(result.output)) if result.output is not None else None
        s.db.update("tool_runs", "id", run_id, {
            "status": "succeeded" if result.ok else "failed", "exit_code": result.exit_code,
            "output": output[:200_000] if output else None, "error": red.text(result.error),
            "error_class": result.error_class, "duration_ms": duration,
            "resource_usage": dumps(result.resource_usage), "finished_at": now_iso()})
        s.audit.record("tool.executed", decision=f"allow ({decision.reason})", approval_id=approval_id,
                       result=result.summary, exit_code=result.exit_code, duration_ms=duration,
                       error=result.error, resources=result.resource_usage, **audit_common)
        return ExecOutcome("executed", result, approval_id)
