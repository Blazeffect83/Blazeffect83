"""Persistent background worker: objectives, research schedules, metrics,
backups and maintenance. Runs independently of any dashboard session."""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import timedelta

from ..agent import state_machine as sm
from ..db import now_iso, parse_iso, utcnow
from ..knowledge.reports import daily_report, report_markdown
from ..maintenance import backups, retention
from ..observability import metrics
from . import schedules
from .resource_limits import evaluate

log = logging.getLogger(__name__)


def reconcile(services) -> dict:
    """Repair state after an abrupt shutdown. Safe interrupted work is resumed;
    anything that might have had irreversible effects is paused for review."""
    db, s = services.db, services
    out = {"resumed": 0, "paused": 0, "requeued": 0}
    for run in db.query("SELECT * FROM tool_runs WHERE status = 'started'"):
        tool = None
        try:
            tool = s.tools.get(run["tool"])
        except Exception:
            pass
        safe = tool is not None and tool.idempotent and tool.tier <= 1
        db.execute("UPDATE tool_runs SET status = 'failed', error = 'interrupted by shutdown', "
                   "error_class = 'interrupted', finished_at = ? WHERE id = ?", (now_iso(), run["id"]))
        task = db.one("SELECT * FROM tasks WHERE id = ?", (run["task_id"],)) if run["task_id"] else None
        if task and task["status"] in (sm.RUNNING, sm.VERIFYING):
            if safe:
                # Re-run under a fresh attempt number (new idempotency key).
                s.state.transition_task(task["id"], sm.RETRYING, "interrupted by restart; safe to repeat")
                out["resumed"] += 1
            else:
                s.state.transition_task(task["id"], sm.PAUSED,
                                        "interrupted during an action that may not be safe to repeat")
                s.state.transition_objective(task["objective_id"], sm.PAUSED,
                                             f"interrupted during '{task['title']}' ({run['tool']}); "
                                             "review the workspace/system before resuming")
                out["paused"] += 1
    # Tasks stuck in RUNNING/VERIFYING without a started tool run (crash between state writes).
    for task in db.query("SELECT * FROM tasks WHERE status IN ('RUNNING','VERIFYING')"):
        if task["status"] == sm.VERIFYING and task["result"]:
            s.state.transition_task(task["id"], sm.RETRYING, "re-verifying after restart")
        else:
            s.state.transition_task(task["id"], sm.RETRYING, "interrupted by restart before execution")
        out["resumed"] += 1
    for o in db.query("SELECT id, status FROM objectives WHERE status IN ('RUNNING','RETRYING','VERIFYING','PLANNING')"):
        if o["status"] == sm.PLANNING:
            s.state.transition_objective(o["id"], sm.QUEUED, "planning interrupted by restart")
            out["requeued"] += 1
        elif o["status"] == sm.VERIFYING:
            s.state.transition_objective(o["id"], sm.READY, "verification interrupted by restart")
        else:
            s.state.transition_objective(o["id"], sm.READY, "resumed after restart")
    if any(out.values()):
        s.audit.record("system.reconcile", actor="system", result=str(out))
    return out


class Worker:
    def __init__(self, services):
        self.s = services
        self.stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool = ThreadPoolExecutor(max_workers=max(1, services.settings.concurrent_objectives),
                                        thread_name_prefix="objective")
        self._running: dict[str, Future] = {}
        self._research_lock = threading.Lock()
        self._research_future: Future | None = None
        self._research_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="research")
        self._last: dict[str, float] = {}
        self.resource_ok = True
        self.resource_reasons: list[str] = []

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        reconcile(self.s)
        self._thread = threading.Thread(target=self._loop, name="aegis-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 30.0) -> None:
        self.stop_event.set()
        if self._thread:
            self._thread.join(timeout)
        self._pool.shutdown(wait=True, cancel_futures=True)
        self._research_pool.shutdown(wait=True, cancel_futures=True)

    def _due(self, name: str, interval: float) -> bool:
        now = time.monotonic()
        if now - self._last.get(name, -1e9) >= interval:
            self._last[name] = now
            return True
        return False

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("worker tick failed")
            self.stop_event.wait(self.s.settings.worker_poll_seconds)

    # -- one iteration (also called directly by tests) ----------------------
    def tick(self, wait: bool = False) -> None:
        s = self.s
        s.db.kv_set("worker_heartbeat", now_iso())
        if self._due("metrics", s.settings.metrics_interval_seconds):
            self._metrics()
        s.approvals.expire_stale()
        self._objectives(wait)
        if self.resource_ok:
            self._research(wait)
        if self._due("maintenance", 300):
            self._maintenance()

    def _metrics(self) -> None:
        snap = metrics.snapshot(self.s.settings.data_dir)
        metrics.record(self.s.db, snap)
        v = evaluate(self.s.settings, snap)
        was_ok = self.resource_ok
        self.resource_ok, self.resource_reasons = v.ok, v.reasons
        self.s.db.kv_set("resource_pause", None if v.ok else v.reasons)
        if not v.ok and was_ok:
            self.s.notifier.notify("critical", "resource_pressure", "Resource limits exceeded; expensive work paused",
                                   "; ".join(v.reasons))

    def _objectives(self, wait: bool) -> None:
        for oid, fut in list(self._running.items()):
            if fut.done():
                self._running.pop(oid)
                if fut.exception():
                    log.error("objective %s crashed: %s", oid, fut.exception())
        if not self.resource_ok:
            return  # resource pressure: don't start new objective work
        capacity = self.s.settings.concurrent_objectives - len(self._running)
        if capacity <= 0:
            return
        rows = self.s.db.query(
            "SELECT id FROM objectives WHERE status IN ('QUEUED','READY','RETRYING') ORDER BY priority, updated_at "
            "LIMIT ?", (capacity + len(self._running),))
        for r in rows:
            if r["id"] in self._running or capacity <= 0:
                continue
            fut = self._pool.submit(self.s.controller.run, r["id"], self.stop_event)
            self._running[r["id"]] = fut
            capacity -= 1
        if wait:
            for fut in list(self._running.values()):
                fut.result()

    def _research(self, wait: bool) -> None:
        s = self.s
        if not s.settings.research_enabled or s.db.kv_get("research_paused", False):
            return
        if self._research_future and not self._research_future.done():
            return
        topics = schedules.due_topics(s.db)
        if not topics:
            return
        self._research_future = self._research_pool.submit(self._run_topics, topics)
        if wait:
            self._research_future.result()

    def _run_topics(self, topics: list[dict]) -> None:
        s = self.s
        for t in topics:
            if self.stop_event.is_set():
                return
            try:
                stats = s.research.run_topic(t)
                ok = stats.failed == 0 or stats.stored > 0
                schedules.mark_run(s.db, t, ok, f"examined {stats.examined}, stored {stats.stored}, "
                                   f"new claims {stats.claims_created}, contradictions {stats.contradictions}, "
                                   f"failed {stats.failed}")
                if stats.contradictions:
                    s.notifier.notify("info", "research_discovery", f"Contradictions found in '{t['name']}'",
                                      f"{stats.contradictions} new contradictory claim pairs flagged")
            except Exception as exc:
                log.exception("research topic %s failed", t["name"])
                schedules.mark_run(s.db, t, False, f"error: {exc}")
                if t["consecutive_failures"] + 1 >= 3:
                    s.notifier.notify("warning", "research_failures", f"Research topic '{t['name']}' keeps failing",
                                      str(exc)[:300])

    def _maintenance(self) -> None:
        s = self.s
        last = s.db.one("SELECT created_at FROM backups ORDER BY id DESC LIMIT 1")
        if not last or utcnow() - parse_iso(last["created_at"]) >= timedelta(hours=s.settings.backup_interval_hours):
            try:
                backups.create_backup(s.db, s.settings.backups, s.settings.backup_keep)
                s.audit.record("system.backup", actor="system", result="ok")
            except Exception as exc:
                log.exception("backup failed")
                s.notifier.notify("critical", "backup_failed", "Database backup failed", str(exc)[:300])
        day = now_iso()[:10]
        if s.db.kv_get("last_daily_maintenance") != day:
            s.db.kv_set("last_daily_maintenance", day)
            ok, detail = s.db.integrity_check()
            if not ok:
                s.notifier.notify("critical", "database_integrity", "Database integrity check failed", detail)
            ok_chain, bad = s.audit.verify_chain()
            if not ok_chain:
                s.notifier.notify("critical", "audit_tamper", "Audit log hash chain broken", f"first bad id {bad}")
            retention.apply_retention(s.db, s.settings.metrics_retention_days, s.settings.tool_output_retention_days)
            retention.cleanup_orphans(s.db)
            rep = daily_report(s)
            s.db.kv_set("last_daily_report", rep)
            s.db.kv_set("last_daily_report_md", report_markdown(rep))
            s.notifier.notify("info", "daily_report", "Daily research report ready",
                              f"{rep['sources_accepted']} documents, {rep['new_knowledge_entries']} new claims, "
                              f"{rep['new_contradictions']} contradictions")
            if utcnow().weekday() == 6:
                retention.vacuum(s.db)

    def status(self) -> dict:
        return {
            "running_objectives": list(self._running),
            "research_active": bool(self._research_future and not self._research_future.done()),
            "resource_ok": self.resource_ok,
            "resource_reasons": self.resource_reasons,
            "research_paused": bool(self.s.db.kv_get("research_paused", False)),
            "stopping": self.stop_event.is_set(),
        }


__all__ = ["Worker", "reconcile"]
