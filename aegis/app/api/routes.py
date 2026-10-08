"""JSON API. Every route except /health and /api/login requires authentication;
authorisation is enforced server-side on each request."""
from __future__ import annotations

import json
import os
import signal
import threading
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse

from ..agent.objectives import ObjectiveError, ObjectiveManager
from ..db import loads
from ..knowledge import search as ks
from ..knowledge.reports import daily_report, report_markdown
from ..maintenance import backups
from ..observability import metrics
from ..observability.health import health_report
from ..scheduler import schedules
from ..security.approvals import ApprovalError
from .authentication import SESSION_COOKIE
from .schemas import BudgetIn, ClarifyIn, LoginIn, ObjectiveIn, ResolveIn, TopicIn, TopicPatch

BUDGET_KEYS = ("daily_spend_limit_usd", "research_daily_spend_limit_usd", "max_requests_per_hour",
               "max_tokens_per_objective")


def services(request: Request):
    return request.app.state.services


def principal(request: Request) -> str:
    return request.app.state.auth.require(request)


def client_id(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def overview(request: Request) -> dict:
    s = request.app.state.services
    worker = request.app.state.worker
    counts = {r["status"]: r["n"] for r in s.db.query("SELECT status, COUNT(*) AS n FROM objectives GROUP BY status")}
    return {
        "agent": worker.status() if worker else {"running_objectives": [], "stopping": False},
        "objective_counts": counts,
        "active": s.db.query("SELECT id, goal, status, status_reason, updated_at FROM objectives WHERE status IN "
                             "('PLANNING','READY','RUNNING','RETRYING','VERIFYING') ORDER BY updated_at DESC LIMIT 10"),
        "waiting": s.db.query("SELECT id, goal, status, status_reason FROM objectives WHERE status IN "
                              "('WAITING_FOR_APPROVAL','PAUSED') ORDER BY updated_at DESC LIMIT 10"),
        "recent": s.db.query("SELECT id, goal, status, status_reason, finished_at FROM objectives WHERE status IN "
                             "('COMPLETED','FAILED','CANCELLED') ORDER BY finished_at DESC LIMIT 10"),
        "pending_approvals": len(s.approvals.list("pending")),
        "metrics": metrics.latest(s.db),
        "model": {"provider": s.settings.model_provider, "model": s.settings.model_name or None,
                  "local_only": s.settings.local_only, "available": s.router.available},
        "usage": s.router.usage_summary(1),
        "research": {
            "topics": s.db.scalar("SELECT COUNT(*) FROM research_topics WHERE enabled = 1"),
            "documents": s.db.scalar("SELECT COUNT(*) FROM documents"),
            "claims": s.db.scalar("SELECT COUNT(*) FROM claims"),
            "open_contradictions": s.db.scalar("SELECT COUNT(*) FROM relationships WHERE kind='contradicts' "
                                               "AND resolved = 0"),
            "paused": bool(s.db.kv_get("research_paused", False)),
        },
        "sandbox": s.sandbox.status(),
        "unread_notifications": s.db.scalar("SELECT COUNT(*) FROM notifications WHERE read = 0"),
    }


def apply_budget_overrides(s) -> None:
    for k, v in (s.db.kv_get("budget_overrides", {}) or {}).items():
        if k in BUDGET_KEYS and v is not None:
            setattr(s.settings, k, v)


def graceful_shutdown(app) -> None:
    def _go():
        w = app.state.worker
        if w:
            w.stop()
        os.kill(os.getpid(), signal.SIGTERM)
    threading.Thread(target=_go, daemon=True).start()


def build_router() -> APIRouter:
    r = APIRouter()

    @r.get("/health")
    def health(request: Request):
        rep = health_report(services(request))
        return Response(json.dumps({"ok": rep["ok"]}), status_code=200 if rep["ok"] else 503,
                        media_type="application/json")

    # -- auth ---------------------------------------------------------------
    @r.post("/api/login")
    def api_login(body: LoginIn, request: Request, response: Response):
        res = request.app.state.auth.login(body.username, body.password, client_id(request))
        if not res:
            raise HTTPException(401, "invalid credentials")
        sid, csrf = res
        s = services(request).settings
        response.set_cookie(SESSION_COOKIE, sid, httponly=True, secure=s.cookie_secure, samesite="strict",
                            max_age=s.session_ttl_hours * 3600, path="/")
        services(request).audit.record("auth.login", actor=f"user:{body.username}", result="ok")
        return {"ok": True, "csrf_token": csrf}

    @r.post("/api/logout")
    def api_logout(request: Request, response: Response, who: str = Depends(principal)):
        request.app.state.auth.logout(request.cookies.get(SESSION_COOKIE))
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"ok": True}

    # -- overview -----------------------------------------------------------
    @r.get("/api/status")
    def status(request: Request, who: str = Depends(principal)):
        return overview(request)

    @r.get("/api/health")
    def health_detail(request: Request, who: str = Depends(principal)):
        return health_report(services(request))

    # -- objectives ---------------------------------------------------------
    @r.post("/api/objectives", status_code=201)
    def create_objective(body: ObjectiveIn, request: Request, who: str = Depends(principal)):
        try:
            oid = ObjectiveManager(services(request)).create(actor=who, **body.model_dump())
        except ObjectiveError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"id": oid}

    @r.get("/api/objectives")
    def list_objectives(request: Request, status: str | None = None, limit: int = Query(100, le=500),
                        who: str = Depends(principal)):
        return ObjectiveManager(services(request)).list(status, limit)

    @r.get("/api/objectives/{oid}")
    def get_objective(oid: str, request: Request, who: str = Depends(principal)):
        o = ObjectiveManager(services(request)).get(oid)
        if not o:
            raise HTTPException(404, "objective not found")
        return o

    @r.post("/api/objectives/{oid}/{action}")
    def objective_action(oid: str, action: str, request: Request, body: ClarifyIn | None = None,
                         who: str = Depends(principal)):
        m = ObjectiveManager(services(request))
        try:
            if action == "pause":
                m.pause(oid, who)
            elif action == "resume":
                m.resume(oid, who)
            elif action == "cancel":
                m.cancel(oid, who)
            elif action == "retry":
                m.retry(oid, who)
            elif action == "clarify":
                if not body:
                    raise HTTPException(400, "answer required")
                m.clarify(oid, body.answer, who)
            else:
                raise HTTPException(404, "unknown action")
        except ObjectiveError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True, "status": services(request).state.objective_status(oid)}

    # -- approvals ----------------------------------------------------------
    @r.get("/api/approvals")
    def list_approvals(request: Request, status: str | None = None, who: str = Depends(principal)):
        return services(request).approvals.list(status)

    @r.post("/api/approvals/{aid}/{decision}")
    def decide(aid: str, decision: str, request: Request, who: str = Depends(principal)):
        if decision not in ("approve", "reject"):
            raise HTTPException(404, "unknown decision")
        s = services(request)
        try:
            row = s.approvals.decide(aid, decision == "approve", who)
        except ApprovalError as exc:
            raise HTTPException(409, str(exc)) from exc
        s.audit.record(f"approval.{row['status']}", actor=who, approval_id=aid, objective_id=row["objective_id"],
                       task_id=row["task_id"], tool=row["tool"], decision=row["status"])
        ObjectiveManager(s).on_approval_decided(row)
        return row

    # -- research -----------------------------------------------------------
    @r.get("/api/topics")
    def topics(request: Request, who: str = Depends(principal)):
        return [schedules.topic_view(t) for t in services(request).db.query("SELECT * FROM research_topics ORDER BY name")]

    @r.post("/api/topics", status_code=201)
    def create_topic(body: TopicIn, request: Request, who: str = Depends(principal)):
        s = services(request)
        if s.db.one("SELECT id FROM research_topics WHERE name = ?", (body.name,)):
            raise HTTPException(409, "topic exists")
        tid = schedules.create_topic(s.db, **body.model_dump())
        s.audit.record("topic.created", actor=who, args=body.model_dump())
        return {"id": tid}

    @r.patch("/api/topics/{tid}")
    def patch_topic(tid: int, body: TopicPatch, request: Request, who: str = Depends(principal)):
        s = services(request)
        fields = {k: v for k, v in body.model_dump().items() if v is not None}
        if not s.db.one("SELECT id FROM research_topics WHERE id = ?", (tid,)):
            raise HTTPException(404, "topic not found")
        if fields:
            schedules.update_topic(s.db, tid, **fields)
            s.audit.record("topic.updated", actor=who, args={"id": tid, **fields})
        return {"ok": True}

    @r.post("/api/topics/{tid}/run")
    def run_topic_now(tid: int, request: Request, who: str = Depends(principal)):
        s = services(request)
        if not s.db.one("SELECT id FROM research_topics WHERE id = ?", (tid,)):
            raise HTTPException(404, "topic not found")
        schedules.update_topic(s.db, tid, next_run_at="1970-01-01T00:00:00+00:00")
        return {"ok": True, "detail": "scheduled for the next worker cycle"}

    @r.post("/api/research/{action}")
    def research_toggle(action: str, request: Request, who: str = Depends(principal)):
        if action not in ("pause", "resume"):
            raise HTTPException(404, "unknown action")
        s = services(request)
        s.db.kv_set("research_paused", action == "pause")
        s.audit.record(f"research.{action}", actor=who)
        return {"ok": True, "paused": action == "pause"}

    @r.get("/api/search")
    def search(request: Request, q: str = "", kind: str = "documents", topic: str | None = None,
               domain: str | None = None, min_quality: float | None = None, limit: int = Query(20, le=100),
               who: str = Depends(principal)):
        db = services(request).db
        if kind == "claims":
            return ks.search_claims(db, q, limit=limit) if q else db.query(
                "SELECT * FROM claims ORDER BY id DESC LIMIT ?", (limit,))
        if kind == "experiments":
            return ks.search_experiments(db, q, limit)
        return ks.search_documents(db, q, topic=topic, domain=domain, min_quality=min_quality, limit=limit)

    @r.get("/api/documents/{did}")
    def document(did: int, request: Request, who: str = Depends(principal)):
        d = services(request).knowledge.get_document(did)
        if not d:
            raise HTTPException(404, "document not found")
        d["related"] = ks.related_documents(services(request).db, did)
        return d

    @r.delete("/api/documents/{did}")
    def delete_document(did: int, request: Request, who: str = Depends(principal)):
        s = services(request)
        s.knowledge.delete_document(did)
        s.audit.record("knowledge.document_deleted", actor=who, args={"document_id": did})
        return {"ok": True}

    @r.get("/api/claims/{cid}")
    def claim(cid: int, request: Request, who: str = Depends(principal)):
        c = services(request).knowledge.claim(cid)
        if not c:
            raise HTTPException(404, "claim not found")
        return c

    @r.get("/api/contradictions")
    def contradictions(request: Request, include_resolved: bool = False, who: str = Depends(principal)):
        return services(request).knowledge.contradictions(include_resolved)

    @r.post("/api/contradictions/{rid}/resolve")
    def resolve(rid: int, body: ResolveIn, request: Request, who: str = Depends(principal)):
        s = services(request)
        try:
            s.knowledge.resolve_contradiction(rid, body.keep_claim, body.reason)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        s.audit.record("knowledge.contradiction_resolved", actor=who, args={"id": rid, **body.model_dump()})
        return {"ok": True}

    @r.get("/api/reports/daily")
    def report(request: Request, hours: int = Query(24, ge=1, le=744), format: str = "json",
               who: str = Depends(principal)):
        rep = daily_report(services(request), hours)
        if format == "markdown":
            return PlainTextResponse(report_markdown(rep), media_type="text/markdown")
        return rep

    @r.get("/api/export/knowledge")
    def export(request: Request, who: str = Depends(principal)):
        data = services(request).knowledge.export()
        return Response(json.dumps(data, default=str, indent=1), media_type="application/json",
                        headers={"content-disposition": "attachment; filename=aegis-knowledge.json"})

    # -- system -------------------------------------------------------------
    @r.get("/api/metrics")
    def metrics_history(request: Request, hours: int = Query(24, le=24 * 30), who: str = Depends(principal)):
        from datetime import timedelta
        from ..db import now_iso
        return services(request).db.query("SELECT * FROM system_metrics WHERE created_at >= ? ORDER BY id",
                                          (now_iso(timedelta(hours=-hours)),))

    @r.get("/api/usage")
    def usage(request: Request, days: int = Query(1, ge=1, le=90), who: str = Depends(principal)):
        s = services(request)
        return {"summary": s.router.usage_summary(days),
                "by_objective": s.db.query(
                    "SELECT objective_id, purpose, COUNT(*) AS requests, SUM(input_tokens + output_tokens) AS tokens, "
                    "SUM(cost_usd) AS cost_usd FROM model_usage GROUP BY objective_id, purpose "
                    "ORDER BY MAX(id) DESC LIMIT 100")}

    @r.get("/api/budgets")
    def get_budgets(request: Request, who: str = Depends(principal)):
        st = services(request).settings
        return {k: getattr(st, k) for k in BUDGET_KEYS}

    @r.post("/api/budgets")
    def set_budgets(body: BudgetIn, request: Request, who: str = Depends(principal)):
        s = services(request)
        cur = s.db.kv_get("budget_overrides", {}) or {}
        cur.update({k: v for k, v in body.model_dump().items() if v is not None})
        s.db.kv_set("budget_overrides", cur)
        apply_budget_overrides(s)
        s.audit.record("budgets.updated", actor=who, args=cur)
        return {k: getattr(s.settings, k) for k in BUDGET_KEYS}

    @r.get("/api/logs")
    def logs(request: Request, lines: int = Query(200, ge=1, le=5000), who: str = Depends(principal)):
        p = services(request).settings.logs / "aegis.log"
        if not p.exists():
            return PlainTextResponse("")
        from collections import deque
        with open(p, encoding="utf-8", errors="replace") as f:
            return PlainTextResponse("".join(deque(f, maxlen=lines)))

    @r.get("/api/audit")
    def audit(request: Request, limit: int = Query(100, le=1000), objective_id: str | None = None,
              who: str = Depends(principal)):
        s = services(request)
        ok, bad = s.audit.verify_chain()
        return {"chain_ok": ok, "first_bad_id": bad, "events": s.audit.recent(limit, objective_id)}

    @r.get("/api/tools")
    def tools(request: Request, who: str = Depends(principal)):
        return services(request).tools.catalogue()

    @r.get("/api/config")
    def config(request: Request, who: str = Depends(principal)):
        return services(request).settings.redacted()

    @r.get("/api/notifications")
    def notifications(request: Request, unread: bool = False, who: str = Depends(principal)):
        return services(request).notifier.recent(100, unread)

    @r.post("/api/notifications/read")
    def notifications_read(request: Request, who: str = Depends(principal)):
        services(request).notifier.mark_read()
        return {"ok": True}

    @r.get("/api/backups")
    def list_backups(request: Request, who: str = Depends(principal)):
        return services(request).db.query("SELECT * FROM backups ORDER BY id DESC LIMIT 50")

    @r.post("/api/backups", status_code=201)
    def make_backup(request: Request, who: str = Depends(principal)):
        s = services(request)
        try:
            res = backups.create_backup(s.db, s.settings.backups, s.settings.backup_keep)
        except backups.BackupError as exc:
            raise HTTPException(500, str(exc)) from exc
        s.audit.record("system.backup", actor=who, result=res["path"])
        return res

    @r.get("/api/backups/{bid}/download")
    def download_backup(bid: int, request: Request, who: str = Depends(principal)):
        s = services(request)
        row = s.db.one("SELECT * FROM backups WHERE id = ?", (bid,))
        if not row or not Path(row["path"]).is_file():
            raise HTTPException(404, "backup not found")
        p = Path(row["path"]).resolve()
        if not p.is_relative_to(s.settings.backups):
            raise HTTPException(403, "backup outside backup directory")
        s.audit.record("system.backup_download", actor=who, args={"backup_id": bid})
        return FileResponse(p, filename=p.name, media_type="application/gzip")

    @r.post("/api/shutdown")
    def shutdown(request: Request, who: str = Depends(principal)):
        services(request).audit.record("system.shutdown", actor=who)
        graceful_shutdown(request.app)
        return {"ok": True, "detail": "graceful shutdown started"}

    return r


__all__ = ["build_router", "overview", "apply_budget_overrides", "graceful_shutdown", "loads"]
