"""Contradiction detection.

A contradiction is learned, not declared: a predicate is *functional* when the
graph itself shows that almost every subject has exactly one value (see
:func:`polymath.perception.relations.functional_predicates`, extended here to
literal values). For such a predicate, a subject with several incompatible
values is a conflict:

* entity objects: different entities;
* dates: different days (values of different precision that agree on the
  shared prefix — "1950" vs "1950-05-03" — are compatible);
* quantities: relative difference above 5 % in the same unit.

Within a conflict the clearly best-supported value (confidence margin ≥ 0.25)
stays ``sourced``; the others, or all of them when no value is clearly better,
become ``disputed``. Disputes are re-evaluated on every run, so new evidence can
settle them.
"""

from __future__ import annotations

import json
from typing import Any

from polymath.core.db import Database
from polymath.reasoning import temporal

MARGIN = 0.25
REL_TOL = 0.05


def functional_predicates_all(db: Database, threshold: float = 0.9, min_subjects: int = 20) -> set[int]:
    """Predicates (entity- or literal-valued) where at least ``threshold`` of subjects have one value."""
    out = set()
    for r in db.query(
        "SELECT p, COUNT(*) AS subjects, SUM(n = 1) AS single FROM (SELECT p, s, COUNT(*) AS n FROM triples "
        "WHERE holdout = 0 GROUP BY p, s) GROUP BY p HAVING subjects >= ?",
        (min_subjects,),
    ):
        if int(r["single"]) / int(r["subjects"]) >= threshold:
            out.add(int(r["p"]))
    return out


def compatible(a: tuple[int, str], b: tuple[int, str]) -> bool:
    (oa, va), (ob, vb) = a, b
    if oa or ob:
        return oa == ob
    try:
        x, y = json.loads(va), json.loads(vb)
    except ValueError:
        return va == vb
    if isinstance(x, dict) and isinstance(y, dict):
        if "time" in x and "time" in y:
            tx, ty = str(x["time"]), str(y["time"])
            short = min(len(tx), len(ty))
            return tx[:short] == ty[:short]
        if "amount" in x and "amount" in y:
            if x.get("unit") != y.get("unit"):
                return True  # different units: not comparable here
            ax, ay = float(x["amount"]), float(y["amount"])
            return bool(abs(ax - ay) <= REL_TOL * max(abs(ax), abs(ay), 1e-9))
        if "text" in x and "text" in y:
            return str(x["text"]).strip().lower() == str(y["text"]).strip().lower()
    return bool(x == y)


def _one_record_lists_several(db: Database, clusters: list[list[Any]]) -> bool:
    """True when one record (a Wikidata entity, one article's infobox, one document) asserts two different values.

    A functional relation is learned statistically ("most subjects have one value"), but when the source we trust
    most lists several values for *this* subject — English is spoken in many countries; a war had many
    locations — they are deliberate, not a contradiction. Disputes need two sources that disagree."""
    seen: dict[tuple[str, int], int] = {}
    for i, cl in enumerate(clusters):
        ids = [int(r["id"]) for r in cl]
        q = ",".join("?" * len(ids))
        for r in db.query(f"SELECT DISTINCT source, doc_id FROM provenance WHERE triple_id IN ({q})", ids):
            key = (str(r["source"]), int(r["doc_id"] or 0))
            if key[0] == "wikidata":
                key = ("wikidata", 0)  # one entity record per subject
            if seen.setdefault(key, i) != i:
                return True
    return False


def detect(db: Database, functional: set[int], *, limit_groups: int = 200_000) -> dict[str, Any]:
    stats = {"groups_checked": 0, "conflicts": 0, "disputed": 0, "settled": 0}
    if not functional:
        return stats
    q = ",".join("?" * len(functional))
    groups = db.query(
        f"SELECT s, p FROM triples WHERE holdout=0 AND p IN ({q}) GROUP BY s, p HAVING COUNT(*) > 1 "
        "UNION SELECT s, p FROM triples WHERE status='disputed' GROUP BY s, p LIMIT ?",
        [*functional, limit_groups],
    )
    for g in groups:
        stats["groups_checked"] += 1
        rows = db.query(
            "SELECT id, o, value, status, confidence FROM triples WHERE s=? AND p=? AND holdout=0", (g["s"], g["p"])
        )
        ended = temporal.ended_ids(db, [int(r["id"]) for r in rows])
        for r in rows:  # a value that held earlier (Bonn, capital until 1990) does not compete with today's
            if int(r["id"]) in ended and r["status"] == "disputed":
                db.execute("UPDATE triples SET status='sourced' WHERE id=?", (r["id"],))
                stats["settled"] += 1
        rows = [r for r in rows if int(r["id"]) not in ended]
        # cluster mutually compatible values (precision variants of one date are one value)
        clusters: list[list[Any]] = []
        for r in sorted(rows, key=lambda r: -float(r["confidence"])):
            key = (int(r["o"]), str(r["value"]))
            for cl in clusters:
                if compatible(key, (int(cl[0]["o"]), str(cl[0]["value"]))):
                    cl.append(r)
                    break
            else:
                clusters.append([r])
        if len(clusters) >= 2 and _one_record_lists_several(db, clusters):
            clusters = [[r for cl in clusters for r in cl]]  # multi-valued for this subject: nothing to dispute
        if len(clusters) < 2:
            for r in rows:  # a previously disputed value that no longer conflicts is settled
                if r["status"] == "disputed":
                    db.execute("UPDATE triples SET status='sourced' WHERE id=?", (r["id"],))
                    stats["settled"] += 1
            continue
        stats["conflicts"] += 1
        scores = [max(float(r["confidence"]) for r in cl) for cl in clusters]
        order = sorted(range(len(clusters)), key=lambda i: -scores[i])
        winner = order[0] if scores[order[0]] - scores[order[1]] >= MARGIN else None
        for i, cl in enumerate(clusters):
            status = "sourced" if i == winner else "disputed"
            for r in cl:
                if r["status"] == "inferred" and status == "sourced":
                    continue
                if r["status"] != status:
                    db.execute("UPDATE triples SET status=? WHERE id=?", (status, r["id"]))
                    stats["disputed"] += status == "disputed"
    return stats


def disputes(db: Database, limit: int = 50) -> list[dict[str, Any]]:
    """Human-readable list of open disputes (for the dashboard and reports)."""
    out = []
    for g in db.query("SELECT s, p FROM triples WHERE status='disputed' GROUP BY s, p LIMIT ?", (limit,)):
        rows = db.query(
            "SELECT t.id, t.o, t.value, t.confidence, e.label AS olabel, p.label AS plabel, s.label AS slabel "
            "FROM triples t JOIN predicates p ON p.id=t.p JOIN entities s ON s.id=t.s LEFT JOIN entities e "
            "ON e.id=t.o WHERE t.s=? AND t.p=? AND t.holdout=0 ORDER BY t.confidence DESC",
            (g["s"], g["p"]),
        )
        if rows:
            out.append(
                {
                    "subject": rows[0]["slabel"],
                    "predicate": rows[0]["plabel"],
                    "values": [
                        {
                            "triple": r["id"],
                            "value": r["olabel"] or json.loads(r["value"] or "null"),
                            "confidence": r["confidence"],
                        }
                        for r in rows
                    ],
                }
            )
    return out
