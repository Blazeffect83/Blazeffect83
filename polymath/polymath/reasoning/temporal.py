"""Time awareness: when a fact holds.

Wikidata says when many statements hold: Germany's capital is Berlin, and Bonn was capital *until 1990*
(qualifiers start time P580, end time P582, point in time P585). Ingestion keeps those times
(:func:`polymath.senses.wikidata.parse_entity`) and stores them per fact in ``fact_time``.

* A fact whose ``valid_to`` is in the past is *ended*: it is shown as "was … (until 1990)", it never competes with
  the current value of a single-valued relation (no false contradiction), and the current value comes first.
* A conclusion worked out by a rule inherits the validity of its premises (the latest start, the earliest end).
* Dates compare as numbers ``(year, month, day)``, so 44 BC sorts before 1 AD.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

from polymath.core.db import Database

MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December")  # fmt: skip
_DATE = re.compile(r"(-?)(\d+)(?:-(\d\d))?(?:-(\d\d))?")


def date_key(s: str | None) -> tuple[int, int, int] | None:
    """``"1990-10-03"`` → (1990, 10, 3); ``"-44"`` → (-44, 0, 0); None when it is not a date."""
    m = _DATE.fullmatch(str(s or "").strip())
    if not m:
        return None
    sign, year, month, day = m.groups()
    return (-int(year) if sign else int(year), int(month or 0), int(day or 0))


def today() -> str:
    return time.strftime("%Y-%m-%d")


def human_date(s: str | None) -> str:
    """``"1990-10-03"`` → "3 October 1990", ``"1990-10"`` → "October 1990", ``"-44"`` → "44 BC"."""
    k = date_key(s)
    if k is None:
        return str(s or "")
    year, month, day = k
    y = f"{-year} BC" if year < 0 else str(year)
    if month and day:
        return f"{day} {MONTHS[month - 1]} {y}"
    if month:
        return f"{MONTHS[month - 1]} {y}"
    return y


@dataclass(frozen=True)
class Validity:
    valid_from: str | None = None
    valid_to: str | None = None
    at_time: str | None = None

    def ended(self, now: str | None = None) -> bool:
        end = date_key(self.valid_to)
        return end is not None and end <= (date_key(now or today()) or (0, 0, 0))

    def suffix(self) -> str:
        """ " (until 1990)", " (1949–1990)", " (since 2005)", " (in 2004)"; "" when nothing is known."""
        f, t = (
            human_date(self.valid_from) if self.valid_from else "",
            human_date(self.valid_to) if self.valid_to else "",
        )
        if f and t:
            return f" ({f}–{t})"
        if t:
            return f" (until {t})"
        if f:
            return f" (since {f})"
        if self.at_time:
            return f" (in {human_date(self.at_time)})"
        return ""


def record(db: Database, triple_id: int, valid_from: str | None, valid_to: str | None, at_time: str | None) -> None:
    if not (valid_from or valid_to or at_time):
        return
    db.execute(
        "INSERT OR REPLACE INTO fact_time(triple_id, valid_from, valid_to, at_time) VALUES(?,?,?,?)",
        (triple_id, valid_from, valid_to, at_time),
    )


def of(db: Database, triple_ids: list[int]) -> dict[int, Validity]:
    """The validity of each fact that has one."""
    out: dict[int, Validity] = {}
    ids = list(dict.fromkeys(int(t) for t in triple_ids))
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        marks = ",".join("?" * len(chunk))
        for r in db.query(
            f"SELECT triple_id, valid_from, valid_to, at_time FROM fact_time WHERE triple_id IN ({marks})", chunk
        ):
            out[int(r["triple_id"])] = Validity(r["valid_from"], r["valid_to"], r["at_time"])
    return out


def inherit(db: Database, premises: list[int], conclusion: int) -> None:
    """A conclusion holds while all its premises hold: the latest start and the earliest end."""
    vs = list(of(db, premises).values())
    if not vs:
        return
    starts = [v.valid_from for v in vs if date_key(v.valid_from)]
    ends = [v.valid_to for v in vs if date_key(v.valid_to)]
    points = [v.at_time for v in vs if v.at_time]
    record(
        db,
        conclusion,
        max(starts, key=lambda s: date_key(s) or (0, 0, 0)) if starts else None,
        min(ends, key=lambda s: date_key(s) or (0, 0, 0)) if ends else None,
        points[0] if len(points) == 1 and len(vs) == 1 else None,
    )


def ended_ids(db: Database, triple_ids: list[int], now: str | None = None) -> set[int]:
    return {t for t, v in of(db, triple_ids).items() if v.ended(now)}


def current_first(rows: list[Any], validity: dict[int, Validity], key: str = "id") -> list[Any]:
    """Facts that still hold before ended ones (the order within each group is kept)."""
    return sorted(rows, key=lambda r: validity.get(int(r[key]), Validity()).ended())
