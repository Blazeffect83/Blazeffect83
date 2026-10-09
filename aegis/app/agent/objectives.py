"""Objective manager: creation and user lifecycle actions."""
from __future__ import annotations

from ..db import dumps, loads, new_id, now_iso
from . import state_machine as sm

KINDS = ("general", "research", "coding", "monitor", "deep_research")


class ObjectiveError(Exception):
    pass


class ObjectiveManager:
    def __init__(self, services):
        self.s = services

    def create(self, goal: str, *, kind: str = "general", priority: int = 5, success_criteria=None,
               constraints=None, allowed_tools=None, deadline: str | None = None, budget_tokens: int | None = None,
               budget_usd: float | None = None, time_budget_minutes: int | None = None, actor: str = "user",
               research_params: dict | None = None) -> str:
        goal = (goal or "").strip()
        if len(goal) < 5:
            raise ObjectiveError("goal is too short")
        if len(goal) > 5000:
            raise ObjectiveError("goal is too long (max 5000 characters)")
        if kind not in KINDS:
            raise ObjectiveError(f"kind must be one of {KINDS}")
        tools = [t for t in (allowed_tools or []) if t]
        unknown = [t for t in tools if t not in self.s.tools.names()]
        if unknown:
            raise ObjectiveError(f"unknown tools: {unknown}")
        oid = new_id("obj")
        ts = now_iso()
        self.s.db.insert("objectives", {
            "id": oid, "goal": goal, "kind": kind, "priority": max(1, min(9, int(priority))),
            "success_criteria": dumps([c for c in (success_criteria or []) if c]),
            "constraints": dumps([c for c in (constraints or []) if c]), "allowed_tools": dumps(tools),
            "deadline": deadline or None, "budget_tokens": budget_tokens, "budget_usd": budget_usd,
            "time_budget_minutes": time_budget_minutes, "status": sm.QUEUED, "status_reason": "submitted",
            "checkpoint": dumps({"params": research_params}) if kind == "deep_research" else None,
            "created_at": ts, "updated_at": ts})
        self.s.db.insert("task_events", {"objective_id": oid, "task_id": None, "from_status": None,
                                         "to_status": sm.QUEUED, "reason": "submitted", "created_at": ts})
        self.s.audit.record("objective.created", actor=actor, objective_id=oid, args={"goal": goal[:500], "kind": kind})
        return oid

    def get(self, oid: str) -> dict | None:
        o = self.s.db.one("SELECT * FROM objectives WHERE id = ?", (oid,))
        if not o:
            return None
        for k, d in (("success_criteria", []), ("constraints", []), ("allowed_tools", []), ("plan", None),
                     ("checkpoint", None)):
            o[k] = loads(o[k], d)
        o["tasks"] = self.s.db.query("SELECT * FROM tasks WHERE objective_id = ? ORDER BY plan_version, seq", (oid,))
        for t in o["tasks"]:
            for k, d in (("args", {}), ("checks", []), ("result", None), ("verification", None)):
                t[k] = loads(t[k], d)
        o["events"] = self.s.state.events(oid)
        if o["kind"] == "deep_research":
            from ..research.campaign import Campaign
            raw = self.s.db.one("SELECT * FROM objectives WHERE id = ?", (oid,))
            o["campaign"] = Campaign(self.s).progress(raw)
            o["checkpoint"] = None  # large; exposed via the summarised progress instead
        o["approvals"] = self.s.db.query("SELECT id, tool, status, created_at, expires_at FROM approvals "
                                         "WHERE objective_id = ? ORDER BY created_at", (oid,))
        return o

    def list(self, status: str | None = None, limit: int = 100) -> list[dict]:
        if status:
            return self.s.db.query("SELECT * FROM objectives WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                                   (status, limit))
        return self.s.db.query("SELECT * FROM objectives ORDER BY created_at DESC LIMIT ?", (limit,))

    def _audit(self, action: str, oid: str, actor: str, detail: str = "") -> None:
        self.s.audit.record(f"objective.{action}", actor=actor, objective_id=oid, result=detail or None)

    def pause(self, oid: str, actor: str = "user") -> None:
        st = self.s.state.objective_status(oid)
        if st in sm.TERMINAL or st == sm.PAUSED:
            raise ObjectiveError(f"cannot pause an objective that is {st}")
        self.s.state.transition_objective(oid, sm.PAUSED, f"paused by {actor}")
        self._audit("paused", oid, actor)

    def resume(self, oid: str, actor: str = "user") -> None:
        o = self.s.db.one("SELECT status, plan_version, clarification_question FROM objectives WHERE id = ?", (oid,))
        if not o or o["status"] != sm.PAUSED:
            raise ObjectiveError("only paused objectives can be resumed")
        if o["clarification_question"]:
            raise ObjectiveError("this objective is waiting for a clarification answer")
        self.s.db.execute("UPDATE tasks SET status = 'READY', status_reason = ? WHERE objective_id = ? "
                          "AND status = 'PAUSED'", (f"resumed by {actor}", oid))
        target = sm.READY if o["plan_version"] > 0 else sm.QUEUED
        self.s.state.transition_objective(oid, target, f"resumed by {actor}")
        self._audit("resumed", oid, actor)

    def cancel(self, oid: str, actor: str = "user") -> None:
        st = self.s.state.objective_status(oid)
        if st is None or st in sm.TERMINAL:
            raise ObjectiveError(f"cannot cancel an objective that is {st}")
        self.s.state.transition_objective(oid, sm.CANCELLED, f"cancelled by {actor}")
        self.s.db.execute("UPDATE tasks SET status = 'CANCELLED', status_reason = 'objective cancelled' "
                          "WHERE objective_id = ? AND status NOT IN ('COMPLETED','FAILED','CANCELLED')", (oid,))
        self.s.db.execute("UPDATE approvals SET status = 'expired' WHERE objective_id = ? AND status IN "
                          "('pending','approved')", (oid,))
        self._audit("cancelled", oid, actor)

    def clarify(self, oid: str, answer: str, actor: str = "user") -> None:
        o = self.s.db.one("SELECT * FROM objectives WHERE id = ?", (oid,))
        if not o or not o["clarification_question"]:
            raise ObjectiveError("objective is not waiting for clarification")
        cons = loads(o["constraints"], [])
        cons.append(f"Clarification — Q: {o['clarification_question']} A: {answer.strip()[:2000]}")
        self.s.db.execute("UPDATE objectives SET constraints = ?, clarification_question = NULL WHERE id = ?",
                          (dumps(cons), oid))
        self.s.state.transition_objective(oid, sm.QUEUED, "clarification provided")
        self._audit("clarified", oid, actor)

    def retry(self, oid: str, actor: str = "user") -> None:
        st = self.s.state.objective_status(oid)
        if st != sm.FAILED:
            raise ObjectiveError("only failed objectives can be retried")
        self.s.db.execute("UPDATE objectives SET replans = 0, started_at = NULL, finished_at = NULL WHERE id = ?",
                          (oid,))
        self.s.state.transition_objective(oid, sm.QUEUED, f"retry requested by {actor}")
        self._audit("retried", oid, actor)

    def on_approval_decided(self, approval: dict) -> None:
        """Wake the objective after a human decision (approve or reject)."""
        oid = approval.get("objective_id")
        if oid:
            self.s.state.transition_objective(oid, sm.READY, f"approval {approval['id']} {approval['status']}",
                                              expect=sm.WAITING_FOR_APPROVAL)
