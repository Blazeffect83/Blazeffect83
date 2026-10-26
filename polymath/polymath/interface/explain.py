"""Explainable everything (v0.13.0): why does it believe this, and how did it come to?

``explain_fact`` traces one fact back as a readable chain:

* the fact itself: what it says, how sure it is, whether it was read, inferred or is disputed;
* every piece of evidence (``provenance``): the source and document (title, address, license), the extraction
  pattern or the sentence, or the rule and the premises it was inferred from, each premise explained in turn
  (to ``depth``);
* the job slice that recorded it (``provenance.job_id`` → ``cycles``: what kind of work, when, how it went), the
  decision that chose that work (``decisions``: the options weighed, the one chosen and why), and the agent that
  acted, if one did (``provenance.agent_id`` → ``agents``, with its kind and generation);
* what checked it since: the self-quiz holding it out, sanity checks, contradictions, a withdrawn rule.

``find_facts`` turns a question or "Subject relation" into fact ids. Shown by ``polymath why`` (``--fact ID`` for
one fact) and ``/api/why``. Read-only.
"""

from __future__ import annotations

import re
import time
from typing import Any

from polymath.memory.graph import norm_alias

MAX_EVIDENCE = 6


def _label(db: Any, eid: int) -> str:
    if not eid:
        return ""
    return str(db.scalar("SELECT label FROM entities WHERE id = ?", (eid,), default=f"#{eid}") or f"#{eid}")


def _when(t: Any) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(t))) if t else "?"


def fact_text(db: Any, row: Any) -> str:
    pred = db.one("SELECT label, key FROM predicates WHERE id = ?", (row["p"],))
    rel = str(pred["label"] or pred["key"]) if pred is not None else f"relation #{row['p']}"
    obj = _label(db, int(row["o"])) if int(row["o"] or 0) else str(row["value"])
    return f"{_label(db, int(row['s']))} — {rel} — {obj}"


def find_facts(db: Any, query: str, limit: int = 3) -> list[int]:
    """Fact ids for "#123", a number, or "Subject relation" (the subject's best match, then the relation)."""
    q = query.strip()
    if re.fullmatch(r"#?\d+", q):
        tid = int(q.lstrip("#"))
        return [tid] if db.scalar("SELECT 1 FROM triples WHERE id = ?", (tid,)) else []
    words = q.rstrip("?").split()
    for cut in range(len(words), 0, -1):  # the longest leading run of words that names a thing
        name = norm_alias(" ".join(words[:cut]))
        ent = db.one("SELECT entity_id FROM aliases WHERE alias = ? ORDER BY count DESC LIMIT 1", (name,))
        if ent is None:
            continue
        rest = " ".join(words[cut:]).lower()
        rows = db.query(
            "SELECT t.id, COALESCE(pr.label, pr.key) AS rel FROM triples t JOIN predicates pr ON pr.id = t.p "
            "WHERE t.s = ? AND t.holdout = 0 ORDER BY t.confidence DESC, t.id LIMIT 200",
            (int(ent["entity_id"]),),
        )
        hit = [int(r["id"]) for r in rows if rest and str(r["rel"] or "").lower() in rest]
        return (hit or [int(r["id"]) for r in rows])[:limit]
    return []


def _job(db: Any, job_id: int | None) -> dict[str, Any] | None:
    if not job_id:
        return None
    out: dict[str, Any] = {"id": int(job_id)}
    cyc = db.one("SELECT action, status, started, ended FROM cycles WHERE job_id = ? ORDER BY id LIMIT 1", (job_id,))
    job = db.one("SELECT kind, payload FROM jobs WHERE id = ?", (job_id,))
    if job is not None:
        out["kind"] = str(job["kind"])
    if cyc is not None:
        out.update(action=str(cyc["action"] or ""), status=str(cyc["status"]), at=float(cyc["started"] or 0))
    dec = db.one("SELECT at, context, options, chosen, reason FROM decisions WHERE job_id = ? LIMIT 1", (job_id,))
    if dec is not None:
        out["decision"] = {"at": float(dec["at"]), "context": str(dec["context"]), "chosen": str(dec["chosen"]),
                           "reason": str(dec["reason"]), "options": str(dec["options"])[:300]}  # fmt: skip
    return out


def _agent(db: Any, agent_id: int | None) -> dict[str, Any] | None:
    if not agent_id:
        return None
    row = db.one("SELECT id, name, kind, generation, parent FROM agents WHERE id = ?", (agent_id,))
    return dict(row) if row is not None else {"id": int(agent_id), "name": "a retired agent"}


def explain_fact(db: Any, tid: int, *, depth: int = 2) -> dict[str, Any] | None:
    row = db.one("SELECT id, s, p, o, value, status, confidence, holdout, created FROM triples WHERE id = ?", (tid,))
    if row is None:
        return None
    out: dict[str, Any] = {"id": tid, "fact": fact_text(db, row), "status": str(row["status"]),
                           "confidence": round(float(row["confidence"]), 3), "since": float(row["created"]),
                           "held_out": bool(row["holdout"]), "evidence": [], "checks": []}  # fmt: skip
    for ev in db.query(
        "SELECT kind, doc_id, source, detail, weight, created, job_id, agent_id FROM provenance WHERE triple_id = ? "
        "ORDER BY weight DESC, id LIMIT ?",
        (tid, MAX_EVIDENCE),
    ):
        item: dict[str, Any] = {"kind": str(ev["kind"]), "source": str(ev["source"]), "detail": str(ev["detail"]),
                                "at": float(ev["created"])}  # fmt: skip
        if int(ev["doc_id"] or 0):
            doc = db.one("SELECT title, url, license, source FROM documents WHERE id = ?", (ev["doc_id"],))
            if doc is not None:
                item["document"] = dict(doc)
        if (job := _job(db, ev["job_id"])) is not None:
            item["job"] = job
        if (agent := _agent(db, ev["agent_id"])) is not None:
            item["agent"] = agent
        if ev["kind"] == "rule" and depth > 0:
            premises = [int(x) for x in re.findall(r"#(\d+)", str(ev["detail"]))]
            item["premises"] = [p for x in premises[:4] if (p := explain_fact(db, x, depth=depth - 1)) is not None]
        out["evidence"].append(item)
    for check in db.query("SELECT rule, detail, action FROM sanity WHERE triple_id = ? LIMIT 3", (tid,)):
        out["checks"].append(f"sanity check {check['rule']}: {check['detail']} ({check['action']})")
    if db.scalar("SELECT 1 FROM withdrawn WHERE s = ? AND p = ? AND o = ? LIMIT 1", (row["s"], row["p"], row["o"])):
        out["checks"].append("a conclusion like it was withdrawn before (its rule was refuted)")
    return out


def render(ex: dict[str, Any], indent: int = 0) -> list[str]:
    pad = "  " * indent
    lines = [f"{pad}{ex['fact']}   [{ex['status']}, {ex['confidence']:.0%} sure, since {_when(ex['since'])}]"]
    if ex.get("held_out"):
        lines.append(f"{pad}  (hidden from itself: the self-quiz asks about it)")
    for ev in ex["evidence"]:
        doc = ev.get("document")
        where = f"“{doc['title']}” ({doc['source']}, {doc['license']})" if doc else ev["source"]
        how = {"wikidata": "stated in Wikidata", "infobox": "read in an infobox of", "pattern": "read in",
               "rule": "inferred by"}.get(ev["kind"], ev["kind"])  # fmt: skip
        lines.append(f"{pad}  ← {how} {where if ev['kind'] != 'rule' else ev['detail'].split(':')[0]}"
                     + (f": {ev['detail'][:120]}" if ev["kind"] == "pattern" and ev["detail"] else ""))  # fmt: skip
        job = ev.get("job")
        if job:
            lines.append(f"{pad}     by job #{job['id']} {job.get('kind') or job.get('action') or ''} on "
                         f"{_when(job.get('at'))}")  # fmt: skip
            dec = job.get("decision")
            if dec:
                lines.append(f"{pad}     chosen because: {dec['reason']} (context {dec['context']})")
        agent = ev.get("agent")
        if agent:
            lines.append(f"{pad}     acting: agent {agent.get('name')} ({agent.get('kind', '?')}, generation "
                         f"{agent.get('generation', '?')})")  # fmt: skip
        for prem in ev.get("premises") or []:
            lines += render(prem, indent + 3)
    lines += [f"{pad}  ✓ {c}" for c in ex.get("checks") or []]
    return lines
