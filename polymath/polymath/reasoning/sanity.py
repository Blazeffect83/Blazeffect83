"""Facts that cannot be true, caught and set aside (``reason.sanity``).

Rules that need no learning:

* **died before born**: a date of death earlier than the date of birth;
* **too old**: a lifespan over ``MAX_AGE`` years;
* **from the future**: a birth, death or founding date after today;
* **child older than parent**: a child born before its parent was ``MIN_PARENT_AGE``;
* **part bigger than the whole**: a place with a larger area (or population) than the place it is located in.

And one that is learned: every numeric relation gets its normal range from its own values (``value_ranges``: median
and spread on a log scale), and a value far outside it (``ROBUST_Z`` robust standard deviations) is suspect: a
population of 3 for a capital city, a mountain 90 km high.

The weaker side of an impossible pair (lower confidence; read from text rather than Wikidata when tied) becomes
*disputed*, so it is no longer used or shown as fact. A clash between two Wikidata facts is only noted. Every case is
kept in ``sanity`` with its reason, and the changelog says how many it caught.
"""

from __future__ import annotations

import json
import math
import time
from statistics import median
from typing import Any

from polymath.core.db import Database
from polymath.core.jobs import JobContext, JobOutcome
from polymath.drive import changelog
from polymath.reasoning import temporal

MAX_AGE = 122
MIN_PARENT_AGE = 10
ROBUST_Z = 8.0  # how many robust standard deviations (log scale) count as impossible
MIN_RANGE_VALUES = 50
SAMPLE = 20_000
WINDOW = 200_000  # triple ids scanned per rule per run: the same cost on a small graph and a huge one
PREDICATES_PER_RUN = 25
KEYS = {"birth": "P569", "death": "P570", "inception": "P571", "child": "P40", "father": "P22", "mother": "P25",
        "located": "P131", "area": "P2046", "population": "P1082"}  # fmt: skip


def pids(db: Database) -> dict[str, int]:
    rows = db.query(f"SELECT id, key FROM predicates WHERE key IN ({','.join('?' * len(KEYS))})", list(KEYS.values()))
    by_key = {str(r["key"]): int(r["id"]) for r in rows}
    return {name: by_key[k] for name, k in KEYS.items() if k in by_key}


def wikidata_only(db: Database, triple_id: int) -> bool:
    kinds = {str(r["kind"]) for r in db.query("SELECT kind FROM provenance WHERE triple_id = ?", (triple_id,))}
    return kinds == {"wikidata"}


def weaker(db: Database, a: int, b: int) -> int:
    """Of two clashing facts, the one with less behind it."""
    ca = float(db.scalar("SELECT confidence FROM triples WHERE id = ?", (a,), default=0))
    cb = float(db.scalar("SELECT confidence FROM triples WHERE id = ?", (b,), default=0))
    if abs(ca - cb) > 1e-9:
        return a if ca < cb else b
    return b if wikidata_only(db, a) else a


def flag(db: Database, triple_id: int, rule: str, detail: str, *, other: int | None = None) -> str:
    """Dispute the fact (unless both sides are Wikidata's): returns the action taken, or "" if already known."""
    if db.scalar("SELECT 1 FROM sanity WHERE triple_id = ?", (triple_id,)):
        return ""
    both_wikidata = wikidata_only(db, triple_id) and (other is None or wikidata_only(db, other))
    action = "noted" if both_wikidata else "disputed"
    if action == "disputed":
        db.execute("UPDATE triples SET status = 'disputed', confidence = confidence * 0.5 WHERE id = ?", (triple_id,))
    db.execute(
        "INSERT INTO sanity(triple_id, rule, detail, action, at) VALUES(?,?,?,?,?)",
        (triple_id, rule, detail[:300], action, time.time()),
    )
    return action


def _date(raw: Any) -> tuple[int, int, int] | None:
    try:
        v = json.loads(str(raw))
    except ValueError:
        return None
    return temporal.date_key(v.get("time")) if isinstance(v, dict) else None


def _amount(raw: Any) -> tuple[float, str] | None:
    try:
        v = json.loads(str(raw))
        return float(v["amount"]), str(v.get("unit") or "")
    except (ValueError, KeyError, TypeError):
        return None


def _label(db: Database, eid: int) -> str:
    return str(db.scalar("SELECT label FROM entities WHERE id = ?", (eid,), default=f"#{eid}"))


def window(db: Database, rule: str) -> tuple[int, int]:
    """The next range of triple ids for a rule (wrapping round at the end), so every fact is checked in turn."""
    start = int(db.kv_get(f"sanity_window_{rule}", 0) or 0)
    top = int(db.scalar("SELECT MAX(id) FROM triples", default=0) or 0)
    end = start + WINDOW
    db.kv_set(f"sanity_window_{rule}", 0 if end > top else end)
    return start, end


def _pairs(db: Database, rule: str, p1: int, p2: int) -> list[Any]:
    """Facts of two relations about the same subject, for the anchor facts in this rule's window."""
    lo, hi = window(db, rule)
    return db.query(
        "SELECT a.id AS aid, a.s, a.o, a.value AS av, x.id AS xid, x.value AS xv FROM triples a JOIN triples x "
        "ON x.s = a.s AND x.p = ? WHERE a.id > ? AND a.id <= ? AND a.p = ? AND a.holdout = 0 AND x.holdout = 0 "
        "AND a.status != 'disputed' AND x.status != 'disputed'",
        (p2, lo, hi, p1),
    )


def check_dates(db: Database, p: dict[str, int]) -> list[str]:
    out: list[str] = []
    now = temporal.date_key(temporal.today()) or (9999, 0, 0)
    if "birth" in p and "death" in p:
        for r in _pairs(db, "life", p["birth"], p["death"]):
            b, d = _date(r["av"]), _date(r["xv"])
            if b is None or d is None:
                continue
            who = _label(db, int(r["s"]))
            if d < b:
                bad = weaker(db, int(r["aid"]), int(r["xid"]))
                died, born = temporal.human_date(_t(r["xv"])), temporal.human_date(_t(r["av"]))
                why = f"{who}: died {died} before being born {born}"
                if flag(
                    db, bad, "died before born", why, other=int(r["xid"]) if bad == int(r["aid"]) else int(r["aid"])
                ):
                    out.append(why)
            elif d[0] - b[0] > MAX_AGE:
                bad = weaker(db, int(r["aid"]), int(r["xid"]))
                why = f"{who}: a lifespan of {d[0] - b[0]} years"
                if flag(db, bad, "too old", why, other=int(r["xid"]) if bad == int(r["aid"]) else int(r["aid"])):
                    out.append(why)
    for name in ("birth", "death", "inception"):
        if name not in p:
            continue
        lo, hi = window(db, f"future_{name}")
        rows = db.query(
            "SELECT id, s, value FROM triples WHERE id > ? AND id <= ? AND p = ? AND holdout = 0 "
            "AND status != 'disputed'",
            (lo, hi, p[name]),
        )
        for r in rows:
            k = _date(r["value"])
            if k is not None and k[0] > now[0] + 1:
                why = f"{_label(db, int(r['s']))}: {name} in {k[0]}, in the future"
                if flag(db, int(r["id"]), "from the future", why):
                    out.append(why)
    return out


def _t(raw: Any) -> str:
    try:
        return str(json.loads(str(raw)).get("time", ""))
    except (ValueError, AttributeError):
        return ""


def check_parents(db: Database, p: dict[str, int]) -> list[str]:
    """A child born before its parent could have had children."""
    out: list[str] = []
    if "birth" not in p:
        return out
    for rel, child_is_object in (("child", True), ("father", False), ("mother", False)):
        if rel not in p:
            continue
        lo, hi = window(db, rel)
        rows = db.query(
            "SELECT r.id, r.s, r.o, pb.value AS sb, ob.value AS obv FROM triples r "
            "JOIN triples pb ON pb.s = r.s AND pb.p = ? JOIN triples ob ON ob.s = r.o AND ob.p = ? "
            "WHERE r.id > ? AND r.id <= ? AND r.p = ? AND r.holdout = 0 AND r.status != 'disputed'",
            (p["birth"], p["birth"], lo, hi, p[rel]),
        )
        for r in rows:
            ks, ko = _date(r["sb"]), _date(r["obv"])
            if ks is None or ko is None:
                continue
            parent_k, child_k = (ks, ko) if child_is_object else (ko, ks)
            parent, child = (int(r["s"]), int(r["o"])) if child_is_object else (int(r["o"]), int(r["s"]))
            if child_k[0] - parent_k[0] < MIN_PARENT_AGE:
                why = (f"{_label(db, child)} (born {child_k[0]}) cannot be a child of {_label(db, parent)} "
                       f"(born {parent_k[0]})")  # fmt: skip
                if flag(db, int(r["id"]), "child older than parent", why):
                    out.append(why)
    return out


def check_parts(db: Database, p: dict[str, int]) -> list[str]:
    """A place bigger (or more populous) than the place it lies in."""
    out: list[str] = []
    if "located" not in p:
        return out
    for measure in ("area", "population"):
        if measure not in p:
            continue
        lo, hi = window(db, f"part_{measure}")
        rows = db.query(
            "SELECT l.s, l.o, a.id AS aid, a.value AS av, w.id AS wid, w.value AS wv FROM triples l "
            "JOIN triples a ON a.s = l.s AND a.p = ? JOIN triples w ON w.s = l.o AND w.p = ? "
            "WHERE l.id > ? AND l.id <= ? AND l.p = ? AND l.holdout = 0 AND l.status != 'disputed' "
            "AND a.status != 'disputed' AND w.status != 'disputed' AND NOT EXISTS (SELECT 1 FROM fact_time f "
            "WHERE f.triple_id = l.id AND f.valid_to IS NOT NULL)",
            (p[measure], p[measure], lo, hi, p["located"]),
        )
        for r in rows:
            part, whole = _amount(r["av"]), _amount(r["wv"])
            if part is None or whole is None or part[1] != whole[1] or whole[0] <= 0:
                continue
            if part[0] > whole[0] * 1.05:
                bad = weaker(db, int(r["aid"]), int(r["wid"]))
                why = (f"{_label(db, int(r['s']))} ({measure} {part[0]:,.0f}) is bigger than {_label(db, int(r['o']))} "
                       f"({whole[0]:,.0f}), which it lies in")  # fmt: skip
                other = int(r["wid"]) if bad == int(r["aid"]) else int(r["aid"])
                if flag(db, bad, "part bigger than whole", why, other=other):
                    out.append(why)
    return out


def learn_ranges(db: Database) -> list[str]:
    """Learn the normal range of a few numeric relations per run, then flag values far outside it."""
    preds = [int(r["id"]) for r in db.query("SELECT id FROM predicates WHERE datatype = 'quantity' ORDER BY id")]
    if not preds:
        return []
    start = int(db.kv_get("sanity_range_cursor", 0) or 0)
    todo = [x for x in preds if x > start][:PREDICATES_PER_RUN] or preds[:PREDICATES_PER_RUN]
    db.kv_set("sanity_range_cursor", todo[-1] if todo[-1] != preds[-1] else 0)
    out: list[str] = []
    now = time.time()
    for pid in todo:
        rows = db.query(
            "SELECT id, s, value FROM triples WHERE p = ? AND o = 0 AND holdout = 0 AND status != 'disputed' LIMIT ?",
            (pid, SAMPLE),
        )
        by_unit: dict[str, list[tuple[int, int, float]]] = {}
        for r in rows:
            am = _amount(r["value"])
            if am is not None and math.isfinite(am[0]):
                by_unit.setdefault(am[1], []).append((int(r["id"]), int(r["s"]), am[0]))
        plabel = str(db.scalar("SELECT label FROM predicates WHERE id = ?", (pid,), default="?"))
        for unit, vals in by_unit.items():
            if len(vals) < MIN_RANGE_VALUES:
                continue
            positive = sum(v > 0 for _t, _s, v in vals) / len(vals) >= 0.99
            logs = [math.log10(v) for _t, _s, v in vals if v > 0]
            if len(logs) < MIN_RANGE_VALUES:
                continue
            med = median(logs)
            mad = median(abs(x - med) for x in logs)
            if mad <= 0:
                continue
            spread = ROBUST_Z * 1.4826 * mad
            lo, hi = med - spread, med + spread
            db.execute(
                "INSERT OR REPLACE INTO value_ranges(p, unit, n, median, mad, lo, hi, positive, updated) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (pid, unit, len(vals), med, mad, 10**lo, 10**hi, int(positive), now),
            )
            for tid, s, v in vals:
                if (v <= 0 and positive) or (v > 0 and not lo <= math.log10(v) <= hi):
                    normal = f"{10**lo:,.3g} to {10**hi:,.3g}"
                    why = f"{_label(db, s)}: {plabel} {v:,.6g} is far outside its normal range ({normal})"
                    if flag(db, tid, "outside the normal range", why) == "disputed":
                        out.append(why)
    return out


def check(db: Database) -> dict[str, Any]:
    p = pids(db)
    found = check_dates(db, p) + check_parents(db, p) + check_parts(db, p) + learn_ranges(db)
    if found:
        changelog.record(
            db, "facts", "impossible facts", "corrected",
            f"Caught {len(found)} fact{'s' if len(found) != 1 else ''} that cannot be true and set "
            f"{'them' if len(found) != 1 else 'it'} aside, e.g. {found[0]}",
            detail={"examples": found[:5], "count": len(found)},
        )  # fmt: skip
    return {"caught": len(found), "examples": found[:5]}


def sanity_job(ctx: JobContext) -> JobOutcome:
    res = check(ctx.db)
    return JobOutcome(done=True, value=0.02 + 0.05 * min(res["caught"], 20) ** 0.5, result=res)


def planner(agent: Any) -> None:
    if agent.db.scalar("SELECT 1 FROM triples LIMIT 1"):
        agent.scheduler.ensure_recurring("reason.sanity", 3 * 3600, priority=1.0)
