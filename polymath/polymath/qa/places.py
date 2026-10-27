"""Geography from coordinates (Wikidata P625).

* "How far is Paris from Berlin?" · "What is the distance between Tokyo and Osaka?" (great-circle distance)
* "Which is further north, Paris or Seattle?" · "Is Rome south of Madrid?"
* "What is near the Eiffel Tower?" (known places within ``NEAR_KM``, nearest first)

``memory.geo`` keeps a table of every coordinate (``geo``), indexed by latitude, so "near" is a narrow range scan.
"""

from __future__ import annotations

import json
import math
import re
from typing import TYPE_CHECKING

from polymath.core.db import Database
from polymath.core.jobs import JobContext, JobOutcome
from polymath.memory.graph import Entity
from polymath.qa import common as c

if TYPE_CHECKING:
    from polymath.interface.answer import Answer, Answerer

EARTH_KM = 6371.0088
NEAR_KM = 50.0
TOP = 8
DISTANCE = re.compile(
    r"^(?:how far (?:is|are) (?P<a>.+?) from (?P<b>.+)|how far apart are (?P<a2>.+?) and (?P<b2>.+)"
    r"|(?:what is |what's )?the distance (?:between|from) (?P<a3>.+?) (?:and|to) (?P<b3>.+))$"
)
DIRECTION = re.compile(
    r"^(?:which|what) is (?:further|farther|more) (?P<dir>north|south|east|west)(?:ern|erly)?,? "
    r"(?P<a>.+?),? or (?P<b>.+)$"
)
IS_DIR = re.compile(
    r"^(?:is|are) (?P<a>.+?) (?:to the |further |farther )?(?P<dir>north|south|east|west) of (?P<b>.+)$"
)
NEAR = re.compile(r"^(?:what is |what's |what are |which places are )?(?:near|close to|nearby|around) (?P<a>.+)$")
NEAR2 = re.compile(r"^(?:what|which places) (?:is|are) (?:near|close to|nearby|around) (?P<a>.+)$")


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_KM * math.asin(min(1.0, math.sqrt(h)))


def coords(ans: Answerer, e: Entity) -> tuple[float, float, int] | None:
    for v in c.by_keys(ans, e, [c.COORDINATES]):
        if isinstance(v.value, dict) and "lat" in v.value:
            return float(v.value["lat"]), float(v.value["lon"]), v.triple
    return None


def try_answer(ans: Answerer, q: str, ql: str) -> Answer | None:
    if m := DISTANCE.match(ql):
        a, b = (m["a"] or m["a2"] or m["a3"]), (m["b"] or m["b2"] or m["b3"])
        return distance(ans, q, a, b)
    if m := DIRECTION.match(ql):
        return direction(ans, q, m["a"], m["b"], m["dir"], yes_no=False)
    if m := IS_DIR.match(ql):
        return direction(ans, q, m["a"], m["b"], m["dir"], yes_no=True)
    if m := NEAR.match(ql) or NEAR2.match(ql):
        return near(ans, q, m["a"])
    return None


def _pair(
    ans: Answerer, q: str, pa: str, pb: str
) -> tuple[Entity, Entity, tuple[float, float, int], tuple[float, float, int]] | Answer:
    a, b = c.entity(ans, pa, (c.COORDINATES,)), c.entity(ans, pb, (c.COORDINATES,))
    if a is None or b is None:
        return c.answer(q, None, [], note=f"I could not identify “{c.clean(pa if a is None else pb)}”.")
    ca, cb = coords(ans, a), coords(ans, b)
    if ca is None or cb is None:
        return c.answer(
            q, a.label, [], relation="location", note=f"I do not know where {(a if ca is None else b).label} is."
        )
    return a, b, ca, cb


def distance(ans: Answerer, q: str, pa: str, pb: str) -> Answer:
    got = _pair(ans, q, pa, pb)
    if not isinstance(got, tuple):
        return got
    a, b, ca, cb = got
    km = haversine(ca[0], ca[1], cb[0], cb[1])
    text = f"{a.label} and {b.label} are about {km:,.0f} km ({km / 1.609344:,.0f} miles) apart, as the crow flies."
    st = c.statement(text, c.confidence(ans, ca[2], cb[2]), c.cites(ans, ca[2], cb[2]), kind="inferred")
    return c.answer(q, a.label, [st], relation="distance")


def direction(ans: Answerer, q: str, pa: str, pb: str, way: str, *, yes_no: bool) -> Answer:
    got = _pair(ans, q, pa, pb)
    if not isinstance(got, tuple):
        return got
    a, b, ca, cb = got
    axis = 0 if way in {"north", "south"} else 1
    va, vb = ca[axis], cb[axis]
    diff = (va - vb + 540) % 360 - 180 if axis == 1 else va - vb  # east/west: the shorter way round
    more_a = diff > 0 if way in {"north", "east"} else diff < 0
    winner, loser = (a, b) if more_a else (b, a)
    unit = "latitude" if axis == 0 else "longitude"
    lead = ("Yes. " if more_a else "No. ") if yes_no else ""
    mine, theirs = (ca[axis], cb[axis]) if more_a else (cb[axis], ca[axis])
    where = f"{unit} {_deg(mine, axis)} against {_deg(theirs, axis)}"
    text = f"{lead}{winner.label} is further {way} than {loser.label} ({where})."
    st = c.statement(text, c.confidence(ans, ca[2], cb[2]), c.cites(ans, ca[2], cb[2]), kind="inferred")
    return c.answer(q, a.label, [st], relation="direction")


def _deg(v: float, axis: int) -> str:
    hemi = ("N" if v >= 0 else "S") if axis == 0 else ("E" if v >= 0 else "W")
    return f"{abs(v):.2f}° {hemi}"


def near(ans: Answerer, q: str, phrase: str) -> Answer:
    e = c.entity(ans, phrase, (c.COORDINATES,))
    if e is None:
        return c.answer(q, None, [], note=f"I could not identify “{c.clean(phrase)}”.")
    here = coords(ans, e)
    if here is None:
        return c.answer(q, e.label, [], relation="location", note=f"I do not know where {e.label} is.")
    lat, lon, tid = here
    dlat = NEAR_KM / 111.0
    dlon = NEAR_KM / (111.0 * max(0.05, math.cos(math.radians(lat))))
    rows = ans.db.query(
        "SELECT g.entity_id, g.lat, g.lon, x.label, x.pagerank FROM geo g JOIN entities x ON x.id = g.entity_id "
        "WHERE g.lat BETWEEN ? AND ? AND g.lon BETWEEN ? AND ? AND g.entity_id != ? AND x.kind = 'item' LIMIT 5000",
        (lat - dlat, lat + dlat, lon - dlon, lon + dlon, e.id),
    )
    found = []
    for r in rows:
        km = haversine(lat, lon, float(r["lat"]), float(r["lon"]))
        if km <= NEAR_KM and not re.fullmatch(r"[QP]\d+", str(r["label"])):
            found.append((km, -float(r["pagerank"] or 0), str(r["label"])))
    found.sort()
    if not found:
        return c.answer(
            q, e.label, [], relation="nearby", note=f"I know of nothing else within {NEAR_KM:.0f} km of {e.label}."
        )
    shown = ", ".join(f"{name} ({km:,.0f} km)" if km >= 1 else f"{name} (under 1 km)" for km, _p, name in found[:TOP])
    text = f"Near {e.label}: {shown}."
    return c.answer(
        q, e.label, [c.statement(text, c.confidence(ans, tid), c.cites(ans, tid), kind="inferred")], relation="nearby"
    )


# ------------------------------------------------------------------ the coordinate table
WINDOW = 500_000  # triple ids scanned per step


def index_coordinates(db: Database, *, window: int = WINDOW) -> dict[str, int]:
    """Copy new coordinate facts into ``geo``: one window of triple ids per call, up to the newest fact."""
    pid = db.scalar("SELECT id FROM predicates WHERE key = ?", (c.COORDINATES,))
    if pid is None:
        return {"indexed": 0, "remaining": 0}
    cursor = int(db.kv_get("geo_cursor", 0) or 0)
    top = int(db.scalar("SELECT MAX(id) FROM triples", default=0) or 0)
    hi = min(top, cursor + window)
    rows = db.query(
        "SELECT s, value FROM triples WHERE id > ? AND id <= ? AND p = ? AND holdout = 0 AND status != 'disputed'",
        (cursor, hi, int(pid)),
    )
    n = 0
    for r in rows:
        try:
            v = json.loads(str(r["value"]))
            lat, lon = float(v["lat"]), float(v["lon"])
        except (ValueError, KeyError, TypeError):
            continue
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            db.execute("INSERT OR IGNORE INTO geo(entity_id, lat, lon) VALUES(?,?,?)", (int(r["s"]), lat, lon))
            n += 1
    db.kv_set("geo_cursor", hi)
    return {"indexed": n, "remaining": int(hi < top)}


def geo_job(ctx: JobContext) -> JobOutcome:
    total = 0
    while True:
        res = index_coordinates(ctx.db)
        total += res["indexed"]
        ctx.tick()
        if not res["remaining"] or ctx.should_stop():
            break
    return JobOutcome(done=not res["remaining"], value=0.01 * total**0.5, result={"indexed": total})


def planner(agent: object) -> None:
    db = agent.db  # type: ignore[attr-defined]
    if db.scalar("SELECT 1 FROM predicates WHERE key = ?", (c.COORDINATES,)):
        agent.scheduler.ensure_recurring("memory.geo", 3600, priority=0.6)  # type: ignore[attr-defined]
