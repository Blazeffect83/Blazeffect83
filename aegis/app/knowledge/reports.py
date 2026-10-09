"""Research reports. Every number is a COUNT over actual database records."""
from __future__ import annotations

from datetime import timedelta

from ..db import loads, now_iso
from ..observability import metrics


def daily_report(services, hours: int = 24) -> dict:
    db = services.db
    since = now_iso(timedelta(hours=-hours))
    n = lambda sql, *p: int(db.scalar(sql, (since, *p)) or 0)  # noqa: E731
    rep = {
        "window_hours": hours,
        "since": since,
        "generated_at": now_iso(),
        "documents_examined": n("SELECT COUNT(*) FROM research_sources WHERE fetched_at >= ? "
                                "AND status IN ('fetched','duplicate','rejected','failed')"),
        "sources_accepted": n("SELECT COUNT(*) FROM documents WHERE retrieved_at >= ?"),
        "sources_rejected": n("SELECT COUNT(*) FROM research_sources WHERE fetched_at >= ? AND status = 'rejected'"),
        "duplicates": n("SELECT COUNT(*) FROM research_sources WHERE fetched_at >= ? AND status = 'duplicate'"),
        "near_duplicates": n("SELECT COUNT(*) FROM documents WHERE retrieved_at >= ? AND near_duplicate_of IS NOT NULL"),
        "research_failures": n("SELECT COUNT(*) FROM research_sources WHERE fetched_at >= ? AND status = 'failed'"),
        "new_knowledge_entries": n("SELECT COUNT(*) FROM claims WHERE created_at >= ?"),
        "entries_updated": n("SELECT COUNT(DISTINCT claim_id) FROM claim_history WHERE created_at >= ? "
                             "AND reason != 'created'"),
        "new_contradictions": n("SELECT COUNT(*) FROM relationships WHERE kind = 'contradicts' AND created_at >= ?"),
        "injection_flagged_documents": n("SELECT COUNT(*) FROM documents WHERE retrieved_at >= ? "
                                         "AND injection_flags != '[]'"),
    }
    rep["important_discoveries"] = db.query(
        "SELECT c.id, c.text, c.confidence, c.corroboration FROM claims c WHERE c.updated_at >= ? "
        "AND c.origin = 'source' AND c.confidence IN ('high','medium') ORDER BY c.corroboration DESC, c.id DESC "
        "LIMIT 10", (since,))
    rep["conflicting_claims"] = db.query(
        "SELECT r.id, a.text AS a_text, b.text AS b_text, r.note FROM relationships r JOIN claims a ON a.id = r.from_id "
        "JOIN claims b ON b.id = r.to_id WHERE r.kind = 'contradicts' AND r.resolved = 0 AND r.created_at >= ? "
        "LIMIT 10", (since,))
    questions = []
    for d in db.query("SELECT extraction FROM documents WHERE retrieved_at >= ? LIMIT 200", (since,)):
        questions += loads(d["extraction"], {}).get("open_questions", [])
    rep["unanswered_questions"] = list(dict.fromkeys(questions))[:10]
    rep["failures"] = db.query(
        "SELECT url, status_reason FROM research_sources WHERE fetched_at >= ? AND status IN ('failed','rejected') "
        "ORDER BY fetched_at DESC LIMIT 15", (since,))
    usage = db.one(
        "SELECT COUNT(*) AS requests, COALESCE(SUM(cost_usd),0) AS cost_usd, COALESCE(MAX(cost_is_estimate),0) "
        "AS estimate FROM model_usage WHERE created_at >= ?", (since,))
    rep["api_spending_usd"] = round(usage["cost_usd"], 6)
    rep["api_spending_is_estimate"] = bool(usage["estimate"])
    rep["api_requests"] = usage["requests"]
    m = db.one("SELECT AVG(cpu_percent) AS cpu, MAX(memory_percent) AS mem, MIN(disk_free_mb) AS disk_free, "
               "MAX(cpu_temp_c) AS temp, COUNT(*) AS samples FROM system_metrics WHERE created_at >= ?", (since,))
    if not m or not m["samples"]:
        snap = metrics.snapshot(services.settings.data_dir, cpu_interval=0.1)
        m = {"cpu": snap.cpu_percent, "mem": snap.memory_percent, "disk_free": snap.disk_free_mb,
             "temp": snap.cpu_temp_c, "samples": 0}
    rep["resources"] = {"avg_cpu_percent": m["cpu"], "max_memory_percent": m["mem"],
                        "min_disk_free_mb": m["disk_free"], "max_cpu_temp_c": m["temp"], "samples": m["samples"]}
    return rep


def report_markdown(rep: dict) -> str:
    lines = [f"# AEGIS research report ({rep['window_hours']} h ending {rep['generated_at']})", "",
             "| Metric | Count |", "|---|---|"]
    for k in ("documents_examined", "sources_accepted", "sources_rejected", "duplicates", "near_duplicates",
              "research_failures", "new_knowledge_entries", "entries_updated", "new_contradictions",
              "injection_flagged_documents", "api_requests"):
        lines.append(f"| {k.replace('_', ' ')} | {rep[k]} |")
    lines.append(f"| API spending (USD{', estimate' if rep['api_spending_is_estimate'] else ''}) | "
                 f"{rep['api_spending_usd']:.4f} |")
    r = rep["resources"]
    lines += ["", f"Resources: avg CPU {r['avg_cpu_percent']}%, max memory {r['max_memory_percent']}%, "
              f"min free disk {r['min_disk_free_mb']} MB, max temp {r['max_cpu_temp_c']} °C ({r['samples']} samples)"]
    if rep["important_discoveries"]:
        lines += ["", "## Important discoveries"]
        lines += [f"- [claim:{c['id']}] ({c['confidence']}, {c['corroboration']} sources) {c['text']}"
                  for c in rep["important_discoveries"]]
    if rep["conflicting_claims"]:
        lines += ["", "## Conflicting claims"]
        lines += [f"- {c['a_text']}  ⟂  {c['b_text']} ({c['note']})" for c in rep["conflicting_claims"]]
    if rep["unanswered_questions"]:
        lines += ["", "## Unanswered questions"] + [f"- {q}" for q in rep["unanswered_questions"]]
    if rep["failures"]:
        lines += ["", "## Failures"] + [f"- {f['url']}: {f['status_reason']}" for f in rep["failures"]]
    return "\n".join(lines) + "\n"
