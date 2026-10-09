"""Agent execution controller.

OBSERVE → REASON → PLAN → ACT → VERIFY → LEARN → REPEAT, with explicit
persisted states, bounded retries/replans, budget and time limits, approval
gating and a completion report backed by recorded evidence.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import timedelta

from ..db import dumps, loads, new_id, now_iso, parse_iso, utcnow
from ..models.base import BudgetExceeded, ErrorClass, ModelError
from ..security.policy import screen_objective
from . import retry_policy
from . import state_machine as sm
from .evaluator import model_judgement, run_checks
from .executor import Executor
from .planner import Plan, PlanError, Planner
from ..tools.registry import ToolContext

log = logging.getLogger(__name__)

RUNNABLE = {sm.QUEUED, sm.PLANNING, sm.READY, sm.RUNNING, sm.RETRYING, sm.VERIFYING}
# States the worker may move out of; user-set PAUSED/CANCELLED are never overridden.
WORKER_OWNED = RUNNABLE


class StopRun(Exception):
    pass


class Controller:
    def __init__(self, services):
        self.s = services
        self.sm = services.state
        self.planner = Planner(services)
        self.executor = Executor(services)

    # -- helpers ------------------------------------------------------------
    def _obj(self, oid: str) -> dict:
        return self.s.db.one("SELECT * FROM objectives WHERE id = ?", (oid,))

    def _tasks(self, oid: str, plan_version: int | None = None) -> list[dict]:
        if plan_version is None:
            return self.s.db.query("SELECT * FROM tasks WHERE objective_id = ? ORDER BY plan_version, seq", (oid,))
        return self.s.db.query("SELECT * FROM tasks WHERE objective_id = ? AND plan_version = ? ORDER BY seq",
                               (oid, plan_version))

    def _move(self, oid: str, to: str, reason: str = "", expect=None, **extra) -> None:
        if not self.sm.transition_objective(oid, to, reason, expect=expect, extra=extra or None):
            raise StopRun(f"objective state changed externally (wanted {to})")

    def _materialize(self, o: dict, plan: Plan) -> None:
        version = o["plan_version"] + 1
        ts = now_iso()
        with self.s.db.transaction() as c:
            c.execute("UPDATE tasks SET status = 'CANCELLED', status_reason = 'superseded by new plan', "
                      "updated_at = ? WHERE objective_id = ? AND status IN ('READY','PAUSED','WAITING_FOR_APPROVAL')",
                      (ts, o["id"]))
            for i, st in enumerate(plan.steps + plan.final_checks):
                c.execute(
                    "INSERT INTO tasks(id, objective_id, seq, plan_version, title, tool, args, expected, checks, "
                    "status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'READY', ?, ?)",
                    (new_id("tsk"), o["id"], i, version,
                     ("[verify] " if i >= len(plan.steps) else "") + st.title, st.tool, dumps(st.args),
                     st.expected, dumps(st.checks), ts, ts))
            crit = loads(o["success_criteria"], []) or plan.success_criteria
            c.execute("UPDATE objectives SET plan = ?, plan_version = ?, success_criteria = ? WHERE id = ?",
                      (dumps(plan.as_dict()), version, dumps(crit), o["id"]))

    def _check_limits(self, o: dict) -> None:
        tb = o["time_budget_minutes"] or self.s.settings.objective_time_budget_minutes
        started = parse_iso(o["started_at"])
        # Deep research tracks its own *active* research time and finishes (not fails) when it is used.
        if o["kind"] != "deep_research" and started and utcnow() - started > timedelta(minutes=tb):
            self._finish(o, sm.FAILED, f"time budget of {tb} minutes expired")
            raise StopRun("time budget")
        executed = int(self.s.db.scalar("SELECT COALESCE(SUM(attempts),0) FROM tasks WHERE objective_id = ?",
                                        (o["id"],)) or 0)
        if executed >= self.s.settings.max_steps_per_objective * 2:
            self._finish(o, sm.FAILED, f"step limit reached ({executed} executions)")
            raise StopRun("step limit")
        if o["budget_usd"] is not None and o["cost_usd"] >= o["budget_usd"]:
            self._pause(o, f"objective spending budget reached (${o['cost_usd']:.4f})")
            raise StopRun("budget")

    def _pause(self, o: dict, reason: str) -> None:
        if not self.sm.transition_objective(o["id"], sm.PAUSED, reason, expect=WORKER_OWNED | {sm.WAITING_FOR_APPROVAL}):
            return
        self.s.notifier.notify("warning", "objective_paused", f"Objective paused: {o['goal'][:80]}", reason)

    def _sleep(self, seconds: float, stop: threading.Event | None) -> None:
        if stop is not None:
            if stop.wait(seconds):
                raise StopRun("shutdown")
        else:
            import time
            time.sleep(min(seconds, 0.01))

    # -- learning -----------------------------------------------------------
    def _learn(self, o: dict, task: dict, result, passed: bool, details: list[dict]) -> None:
        args = loads(task["args"], {})
        brief = {k: (v if not isinstance(v, str) or len(v) < 200 else v[:200] + "…") for k, v in args.items()}
        if passed:
            lesson = f"succeeded: {result.summary}"[:500]
        else:
            failed = [d["detail"] for d in details if not d["passed"]]
            lesson = f"failed ({result.error_class}): {(result.error or '; '.join(failed))[:800]}"
        self.s.knowledge.record_experiment(
            objective_id=o["id"], task_id=task["id"], context=f"{o['goal'][:500]} :: {task['title']}",
            procedure=f"{task['tool']} {json.dumps(brief, default=str)}", outcome="success" if passed else "failure",
            lesson=lesson, error_class=None if passed else (result.error_class or "check_failed"))

    # -- main loop ----------------------------------------------------------
    def run(self, oid: str, stop: threading.Event | None = None, max_iterations: int = 500) -> str:
        try:
            for _ in range(max_iterations):
                if stop is not None and stop.is_set():
                    break
                o = self._obj(oid)
                if o is None or o["status"] not in RUNNABLE:
                    break
                self._check_limits(o)
                st = o["status"]
                if o["kind"] == "deep_research":
                    if self._deep_research(o, stop):
                        continue
                    break
                if st == sm.QUEUED:
                    self._plan(o)
                elif st == sm.PLANNING:
                    self._plan(o, already_planning=True)
                elif st in (sm.READY, sm.RUNNING, sm.RETRYING):
                    self._step(o, stop)
                elif st == sm.VERIFYING:
                    self._verify(o)
        except StopRun as exc:
            log.info("objective %s run stopped: %s", oid, exc)
        except BudgetExceeded as exc:
            o = self._obj(oid)
            self._pause(o, str(exc))
            self.s.notifier.notify("critical", "budget_exhausted", "API budget limit reached", str(exc))
        return self.sm.objective_status(oid)

    # -- deep research campaigns ------------------------------------------
    def _deep_research(self, o: dict, stop) -> bool:
        """One campaign step. Returns True to keep looping, False to return to the worker."""
        from ..research.campaign import Campaign
        camp = Campaign(self.s)
        st = o["status"]
        if st in (sm.QUEUED, sm.PLANNING):
            refusal = screen_objective(o["goal"])
            if st == sm.QUEUED:
                extra = {} if o["started_at"] else {"started_at": now_iso()}
                self._move(o["id"], sm.PLANNING, "preparing research questions", expect=sm.QUEUED, **extra)
            if refusal:
                self.s.audit.record("objective.refused", objective_id=o["id"], decision="deny", result=refusal)
                self._finish(self._obj(o["id"]), sm.FAILED, refusal)
                return False
            camp.initialize(self._obj(o["id"]))
            self._move(o["id"], sm.READY, "research campaign initialised", expect=sm.PLANNING, plan_version=1)
            self.s.audit.record("research.campaign_started", objective_id=o["id"], result=o["goal"][:300])
            return True
        if st == sm.VERIFYING:
            report = camp.report(o, final=True)
            stop_reason = camp.progress(o)["stop_reason"] or "completed"
            self._write_report(o["id"], report)
            self._finish(o, sm.COMPLETED, f"research complete: {stop_reason}", summary=report)
            return False
        self._move(o["id"], sm.RUNNING, "researching", expect=st)

        def keep_going() -> bool:
            if stop is not None and stop.is_set():
                return False
            if self.s.db.kv_get("resource_pause"):
                return False
            return self.sm.objective_status(o["id"]) == sm.RUNNING

        result = camp.run_slice(self._obj(o["id"]), keep_going)
        if result == "finished":
            self._move(o["id"], sm.VERIFYING, "writing research report", expect=sm.RUNNING)
            return True
        # Yield the worker (time slice over, shutdown, resource pressure); stay resumable.
        self.sm.transition_objective(o["id"], sm.READY, "research continues (time slice)", expect=sm.RUNNING)
        return False

    def _write_report(self, oid: str, report: str) -> None:
        ws = self.s.settings.workspace / oid
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "report.md").write_text(report, encoding="utf-8")

    def _plan(self, o: dict, already_planning: bool = False) -> None:
        refusal = screen_objective(o["goal"])
        if refusal:
            self.s.audit.record("objective.refused", objective_id=o["id"], decision="deny", result=refusal)
            if not already_planning:
                self._move(o["id"], sm.PLANNING, "screening", expect=sm.QUEUED)
            self._finish(self._obj(o["id"]), sm.FAILED, refusal)
            return
        if not already_planning:
            extra = {} if o["started_at"] else {"started_at": now_iso()}
            self._move(o["id"], sm.PLANNING, "generating plan", expect=sm.QUEUED, **extra)
        o = self._obj(o["id"])
        try:
            plan = self.planner.plan(o)
        except BudgetExceeded:
            raise
        except (PlanError, ModelError) as exc:
            cls = getattr(exc, "error_class", "plan")
            if cls in ErrorClass.TRANSIENT:
                self._move(o["id"], sm.QUEUED, f"planning deferred: {exc}", expect=sm.PLANNING)
                raise StopRun("transient planning failure")
            self._finish(o, sm.FAILED, f"planning failed: {exc}")
            return
        if plan.clarification_needed:
            self._move(o["id"], sm.PAUSED, "clarification required", expect=sm.PLANNING,
                       clarification_question=plan.clarification_needed)
            self.s.notifier.notify("warning", "clarification_required", "Objective needs clarification",
                                   plan.clarification_needed)
            return
        self._materialize(o, plan)
        self.s.audit.record("objective.planned", objective_id=o["id"],
                            result=f"{len(plan.steps)} steps, {len(plan.final_checks)} checks ({plan.source})")
        self._move(o["id"], sm.READY, f"plan v{o['plan_version'] + 1} ready", expect=sm.PLANNING)

    def _replan(self, o: dict, failure: str) -> None:
        o = self._obj(o["id"])
        if o["replans"] >= self.s.settings.max_replans:
            self._finish(o, sm.FAILED, f"repeated failures: {o['replans']} replans exhausted; last: {failure[:500]}")
            self.s.notifier.notify("warning", "repeated_failures", f"Objective failed: {o['goal'][:80]}", failure[:300])
            return
        self._move(o["id"], sm.PLANNING, f"replanning: {failure[:300]}", expect={sm.RUNNING, sm.VERIFYING, sm.READY},
                   replans=o["replans"] + 1)
        hist = []
        for t in self._tasks(o["id"]):
            r = loads(t["result"], {}) or {}
            hist.append({"title": t["title"], "tool": t["tool"], "status": t["status"],
                         "result_summary": r.get("summary"), "error": r.get("error")})
        try:
            plan = self.planner.replan(self._obj(o["id"]), hist, failure)
        except BudgetExceeded:
            raise
        except (PlanError, ModelError) as exc:
            self._finish(self._obj(o["id"]), sm.FAILED, f"replanning failed: {exc}; original failure: {failure[:500]}")
            return
        if plan.clarification_needed:
            self._move(o["id"], sm.PAUSED, "clarification required", expect=sm.PLANNING,
                       clarification_question=plan.clarification_needed)
            return
        self._materialize(self._obj(o["id"]), plan)
        self._move(o["id"], sm.READY, f"plan v{o['plan_version'] + 1} ready after replanning", expect=sm.PLANNING)

    def _step(self, o: dict, stop) -> None:
        tasks = self._tasks(o["id"], o["plan_version"])
        task = next((t for t in tasks if t["status"] in (sm.READY, sm.RETRYING, sm.WAITING_FOR_APPROVAL)), None)
        if task is None:
            if tasks and all(t["status"] == sm.COMPLETED for t in tasks):
                self._move(o["id"], sm.VERIFYING, "all steps completed", expect={sm.READY, sm.RUNNING, sm.RETRYING})
            else:
                bad = [t for t in tasks if t["status"] in (sm.FAILED, sm.PAUSED, sm.CANCELLED)]
                self._finish(o, sm.FAILED, f"no runnable steps ({len(bad)} failed/paused)")
            return
        if o["status"] != sm.RUNNING:
            self._move(o["id"], sm.RUNNING, f"running: {task['title']}", expect=o["status"])
        attempts = task["attempts"] + (0 if task["status"] == sm.WAITING_FOR_APPROVAL else 1)
        self.sm.transition_task(task["id"], sm.RUNNING, "executing", extra={"attempts": attempts})
        task = self.s.db.one("SELECT * FROM tasks WHERE id = ?", (task["id"],))
        outcome = self.executor.execute(o, task)

        if outcome.kind == "needs_approval":
            self.sm.transition_task(task["id"], sm.WAITING_FOR_APPROVAL, outcome.reason,
                                    extra={"approval_id": outcome.approval_id})
            self._move(o["id"], sm.WAITING_FOR_APPROVAL, f"waiting for approval {outcome.approval_id} "
                       f"to run {task['tool']}", expect=sm.RUNNING)
            raise StopRun("waiting for approval")
        if outcome.kind in ("denied", "rejected"):
            self.sm.transition_task(task["id"], sm.FAILED, outcome.reason,
                                    extra={"result": dumps({"ok": False, "error": outcome.reason,
                                                            "error_class": "policy"})})
            self._finish(self._obj(o["id"]), sm.FAILED, f"{task['tool']}: {outcome.reason}")
            return

        result = outcome.result
        self.sm.transition_task(task["id"], sm.VERIFYING, "checking result",
                                extra={"result": dumps({"ok": result.ok, "summary": result.summary,
                                                        "error": (result.error or "")[:6000],
                                                        "error_class": result.error_class,
                                                        "exit_code": result.exit_code,
                                                        "output": _trim(result.output)})})
        passed, details = run_checks(loads(task["checks"], []), result,
                                     ToolContext(o["id"], task["id"], self.s))
        self._learn(o, task, result, passed, details)
        if passed:
            self.sm.transition_task(task["id"], sm.COMPLETED, "verified", extra={"verification": dumps(details)})
            self._move(o["id"], sm.READY, f"completed: {task['title']}", expect=sm.RUNNING)
            return

        failure_text = result.error or "; ".join(d["detail"] for d in details if not d["passed"])
        ec = result.error_class or "check_failed"
        if ec == "interrupted":
            self.sm.transition_task(task["id"], sm.PAUSED, "interrupted run; needs review")
            self._pause(self._obj(o["id"]), f"step '{task['title']}' was interrupted; review before resuming")
            raise StopRun("interrupted")
        tool = self.s.tools.get(task["tool"])
        decision = retry_policy.decide(ec, attempts, self.s.settings.max_step_attempts,
                                       tool_tier=tool.tier, tool_reversible=tool.reversible,
                                       tool_idempotent=tool.idempotent)
        if decision.action == "retry":
            self.sm.transition_task(task["id"], sm.RETRYING, decision.reason, extra={"verification": dumps(details)})
            self._move(o["id"], sm.RETRYING, f"retrying '{task['title']}' in {decision.delay_seconds:.0f}s",
                       expect=sm.RUNNING)
            self._sleep(decision.delay_seconds, stop)
            return
        self.sm.transition_task(task["id"], sm.FAILED, decision.reason, extra={"verification": dumps(details)})
        msg = f"step '{task['title']}' failed ({ec}): {failure_text[:2500]}"
        if decision.action == "replan":
            self._replan(o, msg)
        elif ec == "budget":
            self._pause(self._obj(o["id"]), msg)
        else:
            self._finish(self._obj(o["id"]), sm.FAILED, msg)

    def _verify(self, o: dict) -> None:
        criteria = loads(o["success_criteria"], [])
        tasks = self._tasks(o["id"], o["plan_version"])
        evidence = []
        for t in tasks:
            r = loads(t["result"], {}) or {}
            evidence.append(f"- {t['title']} [{t['tool']}] → {t['status']}: {r.get('summary')} "
                            f"{json.dumps(r.get('output'), default=str)[:600]}")
        judge = model_judgement(self.s, o, criteria, "\n".join(evidence))
        if judge is not None and not judge.get("criteria_met", False):
            unmet = "; ".join(judge.get("unmet_criteria") or []) or judge.get("explanation", "")
            self._replan(o, f"verification: success criteria not met: {unmet[:1500]}")
            return
        if o["replans"]:
            failures = self.s.db.query(
                "SELECT lesson FROM experiments WHERE objective_id = ? AND outcome = 'failure' ORDER BY id", (o["id"],))
            self.s.knowledge.record_experiment(
                objective_id=o["id"], task_id=None, context=o["goal"][:1000],
                procedure="final plan: " + " → ".join(t["title"] for t in tasks)[:1500], outcome="success",
                lesson=("resolved after replanning; earlier failures: "
                        + " | ".join(f["lesson"][:200] for f in failures[-3:]))[:2000])
        summary = self._report(o, tasks, judge)
        self._finish(o, sm.COMPLETED, "success criteria verified", summary=summary)

    def _report(self, o: dict, tasks: list[dict], judge: dict | None) -> str:
        lines = [f"Objective: {o['goal']}", f"Result: COMPLETED after {o['replans']} replan(s).", "Evidence:"]
        for t in tasks:
            r = loads(t["result"], {}) or {}
            lines.append(f"- {t['title']}: {r.get('summary') or t['status']}")
        if judge:
            lines.append(f"Verifier: {judge.get('explanation', '')[:800]}")
        lines.append(f"Model usage: {o['tokens_used']} tokens, ${o['cost_usd']:.4f} (estimate).")
        return "\n".join(lines)

    def _finish(self, o: dict, status: str, reason: str, summary: str | None = None) -> None:
        if summary is None:
            tasks = self._tasks(o["id"])
            done = sum(1 for t in tasks if t["status"] == sm.COMPLETED)
            summary = (f"Objective: {o['goal']}\nResult: {status} — {reason}\n"
                       f"Steps completed: {done}/{len(tasks)}")
        ok = self.sm.transition_objective(o["id"], status, reason, expect=WORKER_OWNED,
                                          extra={"completion_summary": summary})
        if not ok:
            return
        self.s.audit.record(f"objective.{status.lower()}", objective_id=o["id"], result=reason[:1000])
        kind = "objective_completed" if status == sm.COMPLETED else "objective_failed"
        level = "info" if status == sm.COMPLETED else "warning"
        self.s.notifier.notify(level, kind, f"Objective {status.lower()}: {o['goal'][:80]}", reason[:500])


def _trim(output, limit: int = 8000):
    if output is None:
        return None
    text = json.dumps(output, default=str)
    if len(text) <= limit:
        return output
    return {"truncated": True, "preview": text[:limit]}
