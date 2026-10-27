"""Nightly report: what the agent learned, how well it knows it, and where its effort went."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from polymath.core.db import Database


def _count(db: Database, sql: str, params: tuple[Any, ...] = ()) -> int:
    return int(db.scalar(sql, params, default=0))


def collect(db: Database, since: float) -> dict[str, Any]:
    out: dict[str, Any] = {"generated": time.time(), "since": since}
    out["documents"] = {
        r["source"]: {"total": int(r["n"]), "new": int(r["new"])}
        for r in db.query(
            "SELECT source, COUNT(*) AS n, SUM(fetched > ?) AS new FROM documents WHERE state != 'duplicate' "
            "GROUP BY source ORDER BY n DESC",
            (since,),
        )
    }
    out["graph"] = {
        "entities": _count(db, "SELECT COUNT(*) FROM entities"),
        "triples": {
            r["status"]: int(r["n"]) for r in db.query("SELECT status, COUNT(*) AS n FROM triples GROUP BY status")
        },
        "new_triples": _count(db, "SELECT COUNT(*) FROM triples WHERE created > ?", (since,)),
        "by_evidence": {
            r["kind"]: int(r["n"])
            for r in db.query("SELECT kind, COUNT(DISTINCT triple_id) AS n FROM provenance GROUP BY kind")
        },
        "holdout": _count(db, "SELECT COUNT(*) FROM triples WHERE holdout=1"),
    }
    quizzes = [
        dict(r)
        for r in db.query("SELECT id, created, n, correct, accuracy, chance FROM quizzes ORDER BY id DESC LIMIT 14")
    ]
    out["quiz"] = {"latest": quizzes[0] if quizzes else None, "history": quizzes[::-1]}
    if quizzes:
        latest = db.one("SELECT details FROM quizzes WHERE id=?", (quizzes[0]["id"],))
        out["quiz"]["details"] = json.loads(latest["details"]) if latest else {}
    out["linker"] = db.kv_get("linker_model", {}).get("validation") if db.kv_get("linker_model") else None
    out["embeddings"] = db.kv_get("embedding_quality")
    out["sources_reliability"] = {
        r["source"]: round(float(r["reliability"]), 3)
        for r in db.query("SELECT source, reliability FROM source_reliability ORDER BY reliability DESC")
    }
    out["top_topics"] = [
        {"topic": r["name"], "priority": round(float(r["priority"]), 4), "gap": round(float(r["gap"]), 3)}
        for r in db.query(
            "SELECT t.name, p.priority, p.gap FROM topic_priority p JOIN topics t ON t.id=p.topic_id "
            "ORDER BY p.priority DESC LIMIT 10"
        )
    ]
    out["effort"] = {
        r["action"]: {"cycles": int(r["n"]), "cpu_s": round(float(r["cpu"]), 1), "value": round(float(r["value"]), 2)}
        for r in db.query(
            "SELECT action, COUNT(*) AS n, SUM(cpu_seconds) AS cpu, SUM(value) AS value FROM cycles WHERE ended > ? "
            "GROUP BY action ORDER BY cpu DESC",
            (since,),
        )
    }
    out["bandit"] = [
        dict(r)
        for r in db.query(
            "SELECT context, action, n, ROUND(mean, 3) AS mean FROM bandit_arms ORDER BY context, mean DESC LIMIT 40"
        )
    ]
    out["disputes_open"] = _count(db, "SELECT COUNT(*) FROM triples WHERE status='disputed'")
    out["dead_jobs"] = [
        dict(r)
        for r in db.query("SELECT kind, last_error FROM jobs WHERE state='dead' AND updated > ? LIMIT 20", (since,))
    ]
    return out


def to_markdown(r: dict[str, Any]) -> str:
    day = time.strftime("%Y-%m-%d", time.localtime(r["generated"]))
    lines = [f"# Polymath nightly report — {day}", ""]
    lines += ["## Reading", "", "| source | documents | new |", "|---|---:|---:|"]
    lines += [f"| {s} | {v['total']:,} | {v['new']:,} |" for s, v in r["documents"].items()]
    g = r["graph"]
    lines += [
        "",
        "## Knowledge graph",
        "",
        f"- {g['entities']:,} entities; triples: " + ", ".join(f"{k} {v:,}" for k, v in g["triples"].items()),
        f"- {g['new_triples']:,} new triples since the last report; evidence: "
        + ", ".join(f"{k} {v:,}" for k, v in g["by_evidence"].items()),
        f"- {r['disputes_open']:,} disputed facts awaiting evidence; {g['holdout']:,} facts held out for quizzes",
    ]
    q = r["quiz"]
    lines += ["", "## Self-evaluation", ""]
    if q.get("latest"):
        lat = q["latest"]
        lines.append(
            f"- Latest quiz: **{lat['correct']}/{lat['n']} = {lat['accuracy']:.1%}** (chance {lat['chance']:.0%})"
        )
        trend = ", ".join(f"{h['accuracy']:.0%}" for h in q["history"])
        lines.append(f"- Trend (oldest → newest): {trend}")
        for m, v in (q.get("details") or {}).get("by_method", {}).items():
            lines.append(f"- answered via {m}: {v['n']} questions, {v['accuracy']:.0%} correct")
    else:
        lines.append("- No quiz yet (needs held-out facts with enough sibling answers).")
    if r.get("linker"):
        lv = r["linker"]
        lines.append(f"- Entity linker (held-out anchors): precision {lv.get('precision')}, recall {lv.get('recall')}")
    if r.get("embeddings"):
        e = r["embeddings"]
        if "relation_hit@10" in e:
            h = e["relation_hit@10"]
            lines.append(
                f"- Embeddings: fact neighbours hit@10 {h['hit_rate']:.1%} vs random {h['random_baseline']:.4%}"
            )
        if "alias_similarity" in e:
            lines.append(f"- Embeddings: alias-pair AUC {e['alias_similarity']['auc']}")
    lines += ["", "## Curiosity", ""]
    lines += [f"- {t['topic']} (priority {t['priority']}, gap {t['gap']})" for t in r["top_topics"]] or ["- (none yet)"]
    lines += ["", "## Effort", "", "| action | cycles | CPU s | value |", "|---|---:|---:|---:|"]
    lines += [f"| {a} | {v['cycles']} | {v['cpu_s']} | {v['value']} |" for a, v in r["effort"].items()]
    if r["sources_reliability"]:
        lines += ["", "## Learned source reliability", ""]
        lines += [f"- {s}: {v}" for s, v in r["sources_reliability"].items()]
    if r["dead_jobs"]:
        lines += ["", "## Problems", ""] + [f"- {d['kind']}: {d['last_error']}" for d in r["dead_jobs"]]
    return "\n".join(lines) + "\n"


def write_report(db: Database, report_dir: Path) -> dict[str, Any]:
    last = db.scalar("SELECT MAX(created) FROM reports", default=time.time() - 86400)
    data = collect(db, float(last))
    md = to_markdown(data)
    day = time.strftime("%Y-%m-%d", time.localtime(data["generated"]))
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"report-{day}.md"
    path.write_text(md, encoding="utf-8")
    (report_dir / f"report-{day}.json").write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    db.execute(
        "INSERT INTO reports(created, day, path, summary) VALUES(?,?,?,?) ON CONFLICT(day) DO UPDATE SET "
        "created=excluded.created, path=excluded.path, summary=excluded.summary",
        (
            data["generated"],
            day,
            str(path),
            json.dumps(
                {"quiz": data["quiz"].get("latest"), "documents": sum(v["total"] for v in data["documents"].values())}
            ),
        ),
    )
    return {"path": str(path), "bytes": len(md)}
