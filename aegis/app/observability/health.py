"""Health checks used by /health, the dashboard and the health_check tool."""
from __future__ import annotations

from datetime import timedelta

from ..db import now_iso, parse_iso, utcnow
from ..scheduler.resource_limits import evaluate
from . import metrics


def health_report(services) -> dict:
    checks: dict[str, dict] = {}
    s = services
    try:
        s.db.scalar("SELECT 1")
        checks["database"] = {"ok": True}
    except Exception as exc:  # pragma: no cover - db failure
        checks["database"] = {"ok": False, "detail": str(exc)}
    snap = metrics.snapshot(s.settings.data_dir, cpu_interval=0.1)
    verdict = evaluate(s.settings, snap)
    checks["resources"] = {"ok": verdict.ok, "detail": "; ".join(verdict.reasons) or "within limits",
                           "metrics": snap.as_dict()}
    hb = s.db.kv_get("worker_heartbeat")
    hb_dt = parse_iso(hb) if hb else None
    alive = hb_dt is not None and utcnow() - hb_dt < timedelta(seconds=max(60, s.settings.worker_poll_seconds * 6))
    checks["worker"] = {"ok": alive, "last_heartbeat": hb, "paused": bool(s.db.kv_get("research_paused", False))}
    sb = s.sandbox.status()
    checks["sandbox"] = {"ok": sb["backend"] in ("bwrap", "rlimit"), **sb}
    last_backup = s.db.one("SELECT created_at, verified FROM backups ORDER BY id DESC LIMIT 1")
    stale = True
    if last_backup:
        stale = utcnow() - parse_iso(last_backup["created_at"]) > timedelta(hours=s.settings.backup_interval_hours * 2)
    checks["backups"] = {"ok": bool(last_backup) and not stale and bool(last_backup and last_backup["verified"]),
                         "last": last_backup}
    checks["model_provider"] = {"ok": s.router.available, "provider": s.settings.model_provider,
                                "local_only": s.settings.local_only}
    ok = checks["database"]["ok"] and checks["worker"]["ok"]
    return {"ok": ok, "checked_at": now_iso(), "checks": checks}
