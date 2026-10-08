"""Server-rendered dashboard (optimised for iPad Safari). Every POST is
authenticated and CSRF-checked server-side."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..agent.objectives import ObjectiveError, ObjectiveManager
from ..api.authentication import SESSION_COOKIE
from ..api.routes import BUDGET_KEYS, apply_budget_overrides, graceful_shutdown, overview
from ..db import loads
from ..knowledge import search as ks
from ..knowledge.reports import daily_report, report_markdown
from ..maintenance import backups
from ..observability.health import health_report
from ..scheduler import schedules
from ..security.approvals import ApprovalError

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
TEMPLATES.env.filters["fromjson"] = lambda v: loads(v, v) if isinstance(v, str) else v
TEMPLATES.env.filters["pretty"] = lambda v: json.dumps(v, indent=2, default=str) if not isinstance(v, str) else v


def _lines(text: str | None) -> list[str]:
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


def build_dashboard() -> APIRouter:
    r = APIRouter()

    def page(request: Request, name: str, **ctx):
        ctx.update(request=request, csrf=getattr(request.state, "csrf", ""), active=name.split(".")[0])
        return TEMPLATES.TemplateResponse(request, name, ctx)

    def auth_get(request: Request) -> str | None:
        try:
            return request.app.state.auth.require(request)
        except HTTPException:
            return None

    async def auth_post(request: Request) -> tuple[str, dict]:
        form = dict(await request.form())
        request.state.form_csrf = form.get("csrf")
        who = request.app.state.auth.require(request)  # raises 401/403
        return who, form

    def back(url: str, msg: str | None = None, err: str | None = None):
        sep = "&" if "?" in url else "?"
        if msg:
            url += f"{sep}msg={quote(msg)}"
        elif err:
            url += f"{sep}err={quote(err)}"
        return RedirectResponse(url, status_code=303)

    def svc(request: Request):
        return request.app.state.services

    # -- auth ---------------------------------------------------------------
    @r.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        configured = bool(svc(request).settings.admin_password_hash)
        return page(request, "login.html", configured=configured)

    @r.post("/login")
    async def login_post(request: Request):
        form = dict(await request.form())
        client = request.client.host if request.client else "unknown"
        try:
            res = request.app.state.auth.login(str(form.get("username", "")), str(form.get("password", "")), client)
        except HTTPException as exc:
            return back("/login", err=exc.detail)
        if not res:
            return back("/login", err="Invalid username or password")
        sid, _csrf = res
        svc(request).audit.record("auth.login", actor=f"user:{form.get('username')}", result="ok")
        resp = RedirectResponse("/", status_code=303)
        s = svc(request).settings
        resp.set_cookie(SESSION_COOKIE, sid, httponly=True, secure=s.cookie_secure, samesite="strict",
                        max_age=s.session_ttl_hours * 3600, path="/")
        return resp

    @r.post("/logout")
    async def logout(request: Request):
        await auth_post(request)
        request.app.state.auth.logout(request.cookies.get(SESSION_COOKIE))
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    # -- overview -----------------------------------------------------------
    @r.get("/", response_class=HTMLResponse)
    def home(request: Request):
        if not auth_get(request):
            return RedirectResponse("/login", status_code=303)
        return page(request, "overview.html", o=overview(request),
                    notes=svc(request).notifier.recent(8))

    # -- tasks --------------------------------------------------------------
    @r.get("/tasks", response_class=HTMLResponse)
    def tasks(request: Request, status: str | None = None):
        if not auth_get(request):
            return RedirectResponse("/login", status_code=303)
        s = svc(request)
        return page(request, "tasks.html", objectives=ObjectiveManager(s).list(status, 200), status=status,
                    tools=s.tools.names())

    @r.post("/tasks")
    async def create_task(request: Request):
        who, f = await auth_post(request)
        try:
            oid = ObjectiveManager(svc(request)).create(
                str(f.get("goal", "")), kind=str(f.get("kind", "general")), priority=int(f.get("priority") or 5),
                success_criteria=_lines(f.get("success_criteria")), constraints=_lines(f.get("constraints")),
                allowed_tools=[t for t in str(f.get("allowed_tools", "")).replace(",", " ").split() if t],
                budget_usd=float(f["budget_usd"]) if f.get("budget_usd") else None,
                time_budget_minutes=int(f["time_budget_minutes"]) if f.get("time_budget_minutes") else None,
                actor=who)
        except (ObjectiveError, ValueError) as exc:
            return back("/tasks", err=str(exc))
        return back(f"/tasks/{oid}", msg="Objective submitted")

    @r.get("/tasks/{oid}", response_class=HTMLResponse)
    def task_detail(oid: str, request: Request):
        if not auth_get(request):
            return RedirectResponse("/login", status_code=303)
        o = ObjectiveManager(svc(request)).get(oid)
        if not o:
            raise HTTPException(404, "objective not found")
        runs = svc(request).db.query("SELECT tool, status, exit_code, duration_ms, error_class, started_at "
                                     "FROM tool_runs WHERE objective_id = ? ORDER BY id", (oid,))
        return page(request, "task_detail.html", o=o, runs=runs)

    @r.post("/tasks/{oid}/{action}")
    async def task_action(oid: str, action: str, request: Request):
        who, f = await auth_post(request)
        m = ObjectiveManager(svc(request))
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
                m.clarify(oid, str(f.get("answer", "")), who)
            else:
                raise HTTPException(404)
        except ObjectiveError as exc:
            return back(f"/tasks/{oid}", err=str(exc))
        return back(f"/tasks/{oid}", msg=f"{action} done")

    # -- approvals ----------------------------------------------------------
    @r.get("/approvals", response_class=HTMLResponse)
    def approvals(request: Request):
        if not auth_get(request):
            return RedirectResponse("/login", status_code=303)
        a = svc(request).approvals
        return page(request, "approvals.html", pending=a.list("pending"),
                    history=[x for x in a.list(None, 100) if x["status"] != "pending"])

    @r.post("/approvals/{aid}/{decision}")
    async def approval_decide(aid: str, decision: str, request: Request):
        who, _ = await auth_post(request)
        if decision not in ("approve", "reject"):
            raise HTTPException(404)
        s = svc(request)
        try:
            row = s.approvals.decide(aid, decision == "approve", who)
        except ApprovalError as exc:
            return back("/approvals", err=str(exc))
        s.audit.record(f"approval.{row['status']}", actor=who, approval_id=aid, objective_id=row["objective_id"],
                       task_id=row["task_id"], tool=row["tool"], decision=row["status"])
        ObjectiveManager(s).on_approval_decided(row)
        return back("/approvals", msg=f"Approval {row['status']}")

    # -- research -----------------------------------------------------------
    @r.get("/research", response_class=HTMLResponse)
    def research(request: Request, q: str = "", kind: str = "documents", topic: str | None = None):
        if not auth_get(request):
            return RedirectResponse("/login", status_code=303)
        s = svc(request)
        if kind == "claims":
            results = ks.search_claims(s.db, q, limit=50) if q else s.db.query(
                "SELECT * FROM claims ORDER BY id DESC LIMIT 50")
        else:
            results = ks.search_documents(s.db, q, topic=topic or None, limit=50)
        return page(request, "research.html", q=q, kind=kind, topic=topic, results=results,
                    topics=[schedules.topic_view(t) for t in s.db.query("SELECT * FROM research_topics ORDER BY name")],
                    contradictions=s.knowledge.contradictions()[:20],
                    paused=bool(s.db.kv_get("research_paused", False)),
                    policy={"allowed": s.settings.research_allowed_domains, "blocked": s.settings.research_blocked_domains})

    @r.post("/research/topics")
    async def topic_create(request: Request):
        who, f = await auth_post(request)
        s = svc(request)
        try:
            schedules.create_topic(
                s.db, name=str(f.get("name", "")).strip(), description=str(f.get("description", "")),
                query=str(f.get("query", "")), feeds=_lines(f.get("feeds")), seed_urls=_lines(f.get("seed_urls")),
                allowed_domains=_lines(f.get("allowed_domains")),
                interval_minutes=int(f.get("interval_minutes") or 720),
                max_docs_per_run=int(f.get("max_docs_per_run") or 10),
                freshness_days=int(f.get("freshness_days") or 365))
        except Exception as exc:
            return back("/research", err=f"could not create topic: {exc}")
        s.audit.record("topic.created", actor=who, args={"name": f.get("name")})
        return back("/research", msg="Topic created")

    @r.post("/research/topics/{tid}/{action}")
    async def topic_action(tid: int, action: str, request: Request):
        who, f = await auth_post(request)
        s = svc(request)
        if action == "run":
            schedules.update_topic(s.db, tid, next_run_at="1970-01-01T00:00:00+00:00")
        elif action in ("enable", "disable"):
            schedules.update_topic(s.db, tid, enabled=action == "enable")
        elif action == "frequency":
            try:
                schedules.update_topic(s.db, tid, interval_minutes=int(f.get("interval_minutes") or 720))
            except ValueError as exc:
                return back("/research", err=str(exc))
        elif action == "delete":
            s.db.execute("DELETE FROM research_topics WHERE id = ?", (tid,))
        else:
            raise HTTPException(404)
        s.audit.record(f"topic.{action}", actor=who, args={"id": tid})
        return back("/research", msg=f"Topic {action} done")

    @r.post("/research/{action}")
    async def research_toggle(action: str, request: Request):
        who, _ = await auth_post(request)
        if action not in ("pause", "resume"):
            raise HTTPException(404)
        svc(request).db.kv_set("research_paused", action == "pause")
        svc(request).audit.record(f"research.{action}", actor=who)
        return back("/research", msg=f"Research {action}d")

    @r.post("/research/contradictions/{rid}/resolve")
    async def resolve(rid: int, request: Request):
        who, f = await auth_post(request)
        keep = int(f["keep_claim"]) if f.get("keep_claim") else None
        svc(request).knowledge.resolve_contradiction(rid, keep, str(f.get("reason") or "reviewed by user"))
        svc(request).audit.record("knowledge.contradiction_resolved", actor=who, args={"id": rid, "keep": keep})
        return back("/research", msg="Contradiction resolved")

    @r.get("/documents/{did}", response_class=HTMLResponse)
    def document(did: int, request: Request):
        if not auth_get(request):
            return RedirectResponse("/login", status_code=303)
        d = svc(request).knowledge.get_document(did)
        if not d:
            raise HTTPException(404, "document not found")
        return page(request, "document.html", d=d, related=ks.related_documents(svc(request).db, did))

    @r.get("/claims/{cid}", response_class=HTMLResponse)
    def claim(cid: int, request: Request):
        if not auth_get(request):
            return RedirectResponse("/login", status_code=303)
        c = svc(request).knowledge.claim(cid)
        if not c:
            raise HTTPException(404, "claim not found")
        return page(request, "claim.html", c=c)

    @r.get("/reports", response_class=HTMLResponse)
    def reports(request: Request, hours: int = 24):
        if not auth_get(request):
            return RedirectResponse("/login", status_code=303)
        rep = daily_report(svc(request), max(1, min(744, hours)))
        return page(request, "report.html", rep=rep, md=report_markdown(rep), hours=hours)

    # -- system -------------------------------------------------------------
    @r.get("/system", response_class=HTMLResponse)
    def system(request: Request):
        if not auth_get(request):
            return RedirectResponse("/login", status_code=303)
        s = svc(request)
        p = s.settings.logs / "aegis.log"
        log_tail = ""
        if p.exists():
            from collections import deque
            with open(p, encoding="utf-8", errors="replace") as fh:
                log_tail = "".join(deque(fh, maxlen=150))
        ok, bad = s.audit.verify_chain()
        return page(request, "system.html", health=health_report(s), log_tail=log_tail,
                    budgets={k: getattr(s.settings, k) for k in BUDGET_KEYS}, usage=s.router.usage_summary(1),
                    backups=s.db.query("SELECT * FROM backups ORDER BY id DESC LIMIT 10"),
                    audit=s.audit.recent(40), chain_ok=ok, chain_bad=bad, tools=s.tools.catalogue(),
                    metrics=s.db.query("SELECT * FROM system_metrics ORDER BY id DESC LIMIT 60"))

    @r.post("/system/budgets")
    async def budgets(request: Request):
        who, f = await auth_post(request)
        s = svc(request)
        cur = s.db.kv_get("budget_overrides", {}) or {}
        try:
            for k in BUDGET_KEYS:
                if f.get(k) not in (None, ""):
                    cur[k] = float(f[k]) if "usd" in k else int(f[k])
                    if cur[k] < 0:
                        raise ValueError(f"{k} must be ≥ 0")
        except ValueError as exc:
            return back("/system", err=str(exc))
        s.db.kv_set("budget_overrides", cur)
        apply_budget_overrides(s)
        s.audit.record("budgets.updated", actor=who, args=cur)
        return back("/system", msg="Budgets updated")

    @r.post("/system/backup")
    async def backup(request: Request):
        who, _ = await auth_post(request)
        s = svc(request)
        try:
            res = backups.create_backup(s.db, s.settings.backups, s.settings.backup_keep)
        except backups.BackupError as exc:
            return back("/system", err=str(exc))
        s.audit.record("system.backup", actor=who, result=res["path"])
        return back("/system", msg=f"Backup created and verified ({Path(res['path']).name})")

    @r.post("/system/notifications/read")
    async def read_all(request: Request):
        await auth_post(request)
        svc(request).notifier.mark_read()
        return back("/", msg="Notifications marked read")

    @r.post("/system/shutdown")
    async def shutdown(request: Request):
        who, _ = await auth_post(request)
        svc(request).audit.record("system.shutdown", actor=who)
        graceful_shutdown(request.app)
        return HTMLResponse("<p style='font-family:sans-serif'>AEGIS is shutting down gracefully. "
                            "Start it again with <code>sudo systemctl start aegis</code>.</p>")

    return r
