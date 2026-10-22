"""Rules that earn their trust: every learned rule is judged by how its conclusions held up.

A rule ("capital ↔ capital of", "shares border with is symmetric", "located in is transitive") derives facts nobody
stated. ``reason.audit`` (every 6 hours) looks back at what each rule concluded (up to ``SAMPLE`` per rule):

* **confirmed**: a source later stated the same fact (it gained non-rule evidence);
* **refuted**: the relation has a single value and a source states a different one, or the conclusion became
  disputed. "Single value" is judged strictly (``strict_functional``: at least ``FUNCTIONAL_SUBJECTS`` subjects, of
  which ``FUNCTIONAL_SHARE`` have exactly one value, from sourced facts), because sparse data can make a many-valued
  relation look single-valued. Hidden quiz facts never count, so the self-test stays honest;
* otherwise it is still unknown.

From that, ``trust = (confirmed + 1) / (checked + 2)``. Inference uses it: a trusted rule's conclusions keep their
confidence, a doubtful rule's are weakened, and a rule with ``MIN_CHECKED`` or more judged conclusions and trust below
``DEMOTE_BELOW`` is **demoted**. A demoted rule draws no more conclusions, and the ones it alone produced are removed.
A single refuted conclusion is removed too, whatever the rule, when nothing but rules supports it. Removed conclusions
are remembered (``withdrawn``): inference never derives them again, and refuted ones keep counting against their rule,
so a rule cannot look clean again just because its mistakes were deleted. A demoted rule is trusted again only if its
record turns around (``PROMOTE_AT``). Its withdrawn conclusions are then allowed back. Everything is recorded in the
changelog. The confirmed and refuted counts by confidence also feed the self-tuner, which sets the
confidence a conclusion needs to be kept (``infer.min_confidence``).
"""

from __future__ import annotations

import time
from typing import Any

from polymath.core.db import Database
from polymath.core.jobs import JobContext, JobOutcome
from polymath.drive import changelog

SAMPLE = 3000
MIN_CHECKED = 8
DEMOTE_BELOW = 0.5
PROMOTE_AT = 0.65
TRUSTED = 0.8
FUNCTIONAL_SUBJECTS = 50
FUNCTIONAL_SHARE = 0.95


def strict_functional(db: Database) -> set[int]:
    """Relations that clearly have a single value: ≥ 50 subjects, ≥ 95 % of them with exactly one sourced value."""
    out = set()
    for r in db.query(
        "SELECT p, COUNT(*) AS subjects, SUM(n = 1) AS single FROM (SELECT p, s, COUNT(*) AS n FROM triples "
        "WHERE o != 0 AND status = 'sourced' AND holdout = 0 GROUP BY p, s) GROUP BY p HAVING subjects >= ?",
        (FUNCTIONAL_SUBJECTS,),
    ):
        if int(r["single"]) / int(r["subjects"]) >= FUNCTIONAL_SHARE:
            out.add(int(r["p"]))
    return out


def withdraw(db: Database, t: Any, key: tuple[str, int, int], reason: str) -> None:
    db.execute(
        "INSERT OR REPLACE INTO withdrawn(s, p, o, kind, rule_p, rule_q, reason, at) VALUES(?,?,?,?,?,?,?,?)",
        (int(t["s"]), int(t["p"]), int(t["o"]), *key, reason, time.time()),
    )
    remove(db, int(t["id"]))


def is_withdrawn(db: Database, s: int, p: int, o: int) -> bool:
    return bool(db.scalar("SELECT 1 FROM withdrawn WHERE s = ? AND p = ? AND o = ?", (s, p, o)))


def rule_key(kind: str, p: int, q: int | None) -> tuple[str, int, int]:
    """Inverse rules are one rule whichever way they are used."""
    if kind == "inverse" and q is not None:
        return (kind, min(p, int(q)), max(p, int(q)))
    return (kind, p, 0)


def prefixes(key: tuple[str, int, int]) -> list[str]:
    kind, p, q = key
    if kind == "inverse":
        return [f"inverse {p}->{q}:%", f"inverse {q}->{p}:%"]
    return [f"{kind} {p}:%"]


def label(db: Database, key: tuple[str, int, int]) -> str:
    def pl(pid: int) -> str:
        return str(db.scalar("SELECT label FROM predicates WHERE id = ?", (pid,), default=f"#{pid}"))

    kind, p, q = key
    if kind == "inverse":
        return f"“{pl(p)}” ↔ “{pl(q)}”"
    return f"“{pl(p)}” is {kind}"


def trust_of(db: Database) -> dict[tuple[str, int, int], tuple[float, int, str]]:
    """(trust, checked, state) per rule key, for inference."""
    return {
        (str(r["kind"]), int(r["p"]), int(r["q"])): (float(r["trust"]), int(r["checked"]), str(r["state"]))
        for r in db.query("SELECT kind, p, q, trust, checked, state FROM rule_trust")
    }


def factor(entry: tuple[float, int, str] | None) -> float:
    """How much of a rule's confidence its conclusions keep (0 when demoted)."""
    if entry is None:
        return 1.0
    trust, checked, state = entry
    if state == "demoted":
        return 0.0
    if checked < MIN_CHECKED:
        return 1.0
    return min(1.0, trust / TRUSTED)


def conclusions(db: Database, key: tuple[str, int, int]) -> list[Any]:
    rows: list[Any] = []
    for pat in prefixes(key):
        rows += db.query(
            "SELECT t.id, t.s, t.p, t.o, t.status, t.confidence, "
            "EXISTS (SELECT 1 FROM provenance w WHERE w.triple_id = t.id AND w.kind != 'rule') AS stated "
            "FROM provenance v JOIN triples t ON t.id = v.triple_id WHERE v.kind = 'rule' AND v.detail LIKE ? "
            "ORDER BY v.id DESC LIMIT ?",
            (pat, SAMPLE),
        )
    return rows


def contradicted(db: Database, t: Any, functional: set[int]) -> int | None:
    """The id of a sourced, visible fact that contradicts conclusion ``t`` (None if nothing does)."""
    if int(t["p"]) not in functional or db.scalar(
        "SELECT 1 FROM fact_time WHERE triple_id = ? AND valid_to IS NOT NULL", (int(t["id"]),)
    ):
        return None  # a conclusion about the past (Bonn, capital until 1990) is not refuted by today's value
    r = db.one(
        "SELECT x.id FROM triples x WHERE x.s = ? AND x.p = ? AND x.o != ? AND x.o != 0 AND x.holdout = 0 "
        "AND x.status = 'sourced' AND EXISTS (SELECT 1 FROM provenance w WHERE w.triple_id = x.id "
        "AND w.kind != 'rule') AND NOT EXISTS (SELECT 1 FROM fact_time f WHERE f.triple_id = x.id "
        "AND f.valid_to IS NOT NULL) LIMIT 1",
        (int(t["s"]), int(t["p"]), int(t["o"])),
    )
    return None if r is None else int(r["id"])


def remove(db: Database, triple_id: int) -> None:
    db.execute("DELETE FROM provenance WHERE triple_id = ?", (triple_id,))
    db.execute("DELETE FROM triples WHERE id = ?", (triple_id,))


def _fact(db: Database, s: int, p: int, o: int) -> str:
    def lab(e: int) -> str:
        return str(db.scalar("SELECT label FROM entities WHERE id = ?", (e,), default="?"))

    return f"{lab(s)} → {db.scalar('SELECT label FROM predicates WHERE id = ?', (p,), default='?')} → {lab(o)}"


def audit(db: Database, tick: Any = None) -> dict[str, Any]:
    now = time.time()
    functional = strict_functional(db)
    keys = {
        rule_key(str(r["kind"]), int(r["p"]), None if r["q"] is None else int(r["q"]))
        for r in db.query("SELECT kind, p, q FROM rules WHERE kind IN ('transitive', 'inverse', 'symmetric')")
    }
    keys |= {(str(r["kind"]), int(r["p"]), int(r["q"])) for r in db.query("SELECT kind, p, q FROM rule_trust")}
    known = trust_of(db)
    buckets: dict[str, list[int]] = {}
    out: dict[str, Any] = {"rules": 0, "checked": 0, "removed": 0, "demoted": [], "promoted": []}
    removed_examples: list[str] = []
    withdrawn = 0
    for key in sorted(keys):
        confirmed = refuted = 0
        wrong: list[Any] = []
        for t in conclusions(db, key):
            outcome = None
            if int(t["stated"]):
                outcome = True
            elif str(t["status"]) == "disputed" or contradicted(db, t, functional) is not None:
                outcome = False
                if str(t["status"]) in {"inferred", "disputed"}:
                    wrong.append(t)
            if outcome is None:
                continue
            confirmed += outcome
            refuted += not outcome
            edge = f"{min(0.95, int(float(t['confidence']) * 20) / 20):.2f}"
            b = buckets.setdefault(edge, [0, 0])
            b[0 if outcome else 1] += 1
        refuted += int(
            db.scalar(
                "SELECT COUNT(*) FROM withdrawn WHERE kind = ? AND rule_p = ? AND rule_q = ? AND reason = 'refuted'",
                key,
                default=0,
            )
        )  # fmt: skip — earlier mistakes still count
        checked = confirmed + refuted
        trust = (confirmed + 1) / (checked + 2)
        state = known.get(key, (0.5, 0, "active"))[2]
        name = label(db, key)
        if state == "active" and checked >= MIN_CHECKED and trust < DEMOTE_BELOW:
            state = "demoted"
            gone = 0
            refuted_ids = {int(w["id"]) for w in wrong}
            for pat in prefixes(key):  # what only this rule supported goes with it
                for r in db.query(
                    "SELECT DISTINCT t.id, t.s, t.p, t.o FROM provenance v JOIN triples t ON t.id = v.triple_id "
                    "WHERE v.kind = 'rule' AND v.detail LIKE ? AND t.status != 'sourced' AND NOT EXISTS "
                    "(SELECT 1 FROM provenance w WHERE w.triple_id = t.id AND w.kind != 'rule')",
                    (pat,),
                ):
                    withdraw(db, r, key, "refuted" if int(r["id"]) in refuted_ids else "demoted")
                    gone += 1
            changelog.record(
                db, "rules", name, "demoted",
                f"Stopped trusting the rule {name}: of {checked} conclusions checked, {refuted} were contradicted by "
                f"sources; removed {gone} facts only it had worked out",
                before=trust, detail={"confirmed": confirmed, "refuted": refuted, "removed": gone},
            )  # fmt: skip
            out["demoted"].append(name)
            out["removed"] += gone
            wrong = []
        elif state == "demoted" and checked >= MIN_CHECKED and trust >= PROMOTE_AT:
            state = "active"
            db.execute("DELETE FROM withdrawn WHERE kind = ? AND rule_p = ? AND rule_q = ? AND reason = 'demoted'", key)
            db.kv_set("inference_cursor", 0)  # let it draw its conclusions again
            changelog.record(db, "rules", name, "promoted",
                             f"Trusts the rule {name} again: {confirmed} of {checked} conclusions confirmed",
                             after=trust)  # fmt: skip
            out["promoted"].append(name)
        for t in wrong:  # a contradicted conclusion with nothing but rules behind it is withdrawn
            if not db.scalar("SELECT 1 FROM triples WHERE id = ?", (int(t["id"]),)):
                continue
            if len(removed_examples) < 3:
                removed_examples.append(_fact(db, int(t["s"]), int(t["p"]), int(t["o"])))
            withdraw(db, t, key, "refuted")
            out["removed"] += 1
            withdrawn += 1
        db.execute(
            "INSERT INTO rule_trust(kind, p, q, checked, confirmed, refuted, trust, state, updated) "
            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(kind, p, q) DO UPDATE SET checked = excluded.checked, "
            "confirmed = excluded.confirmed, refuted = excluded.refuted, trust = excluded.trust, "
            "state = excluded.state, updated = excluded.updated",
            (*key, checked, confirmed, refuted, round(trust, 4), state, now),
        )
        out["rules"] += 1
        out["checked"] += checked
        if tick:
            tick()
    if withdrawn:
        changelog.record(
            db, "rules", "contradicted conclusions", "corrected",
            f"Withdrew {withdrawn} conclusion{'s' if withdrawn != 1 else ''} that sources contradicted, "
            f"e.g. {removed_examples[0]}",
            detail={"examples": removed_examples, "withdrawn": withdrawn},
        )  # fmt: skip
    db.kv_set("rule_audit_buckets", buckets)
    return out


def audit_job(ctx: JobContext) -> JobOutcome:
    res = audit(ctx.db, tick=ctx.tick)
    return JobOutcome(done=True, value=0.05 + 0.05 * res["removed"] ** 0.5, result=res)


def planner(agent: Any) -> None:
    if agent.db.scalar("SELECT 1 FROM provenance WHERE kind = 'rule' LIMIT 1"):
        agent.scheduler.ensure_recurring("reason.audit", 6 * 3600, priority=1.1)
