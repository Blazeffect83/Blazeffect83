"""Infobox extraction: template parameters → sourced triples, aligned to Wikidata properties by evidence."""

from __future__ import annotations

import re
from typing import Any

from polymath.core.db import Database
from polymath.memory.graph import KnowledgeGraph
from polymath.senses.wikitext import clean_wikitext, normalize_title

SKIP_KEYS = re.compile(
    r"^(image|img|logo|caption|alt|map|mapsize|map_caption|pushpin|width|size|signature|coat_of_arms|flag|seal|"
    r"footnotes?|notes?|website|url|coordinates|coord|module|embed|label|data|header|above|below|bodystyle|"
    r"style|class|qid|iso|onlysourced|name_?\w*lang|native_name_lang|sortkey|.*_ref|ref.*|.*image.*|.*caption.*|"
    r".*_alt|.*_size|.*width.*|.*color.*|.*colour.*|blank\d*_name.*|.*_note|.*_footnote.*)$",
    re.I,
)
_LINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
_NUM = re.compile(r"^[~≈c.]*\s*([+-]?\d{1,3}(?:,\d{3})+|[+-]?\d+(?:\.\d+)?)\s*([^\d\s].{0,20})?$")
_YEAR = re.compile(r"^(?:c\.\s*)?(\d{1,4})\s*(BC|BCE|AD|CE)$|^(?:c\.\s*)?(\d{3,4})()$")
_DATE = re.compile(r"^(\d{1,2})\s+([A-Z][a-z]+)\s+(\d{3,4})$|^([A-Z][a-z]+)\s+(\d{1,2}),?\s+(\d{3,4})$")
MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ],
        1,
    )
}


def param_key(name: str) -> str:
    key = re.sub(r"[\s_]+", "_", name.strip().lower())
    return re.sub(r"\d+$", "", key).strip("_")


def parse_value(raw: str) -> list[tuple[str, Any]]:
    """Values of one infobox field: [("entity", title)] for links, else one literal."""
    links = [normalize_title(m.group(1)) for m in _LINK.finditer(raw)]
    links = [t for t in links if t and ":" not in t]
    if links:
        return [("entity", t) for t in links[:10]]
    text = clean_wikitext(raw).text.strip()
    if not text or len(text) > 120:
        return []
    if m := _DATE.match(text):
        if m.group(1):
            d, mon, y = int(m.group(1)), MONTHS.get(m.group(2)), int(m.group(3))
        else:
            mon, d, y = MONTHS.get(m.group(4)), int(m.group(5)), int(m.group(6))
        if mon:
            return [("value", {"time": f"{y:04d}-{mon:02d}-{d:02d}"})]
    if m := _YEAR.match(text):
        year = int(m.group(1) or m.group(3))
        return [("value", {"time": str(-year if m.group(2) in {"BC", "BCE"} else year)})]
    if m := _NUM.match(text):
        try:
            amount = float(m.group(1).replace(",", ""))
        except ValueError:
            amount = None
        if amount is not None:
            unit = (m.group(2) or "").strip() or None
            return [("value", {"amount": amount, "unit": unit})]
    return [("value", {"text": text})]


def extract_infobox_triples(
    db: Database,
    graph: KnowledgeGraph,
    subject: int,
    doc_id: int,
    infoboxes: list[dict[str, Any]],
    resolve_title: Any,
    *,
    prefix: str = "",
) -> int:
    """Add triples for every usable infobox field; returns the number of new evidence rows.

    With a language ``prefix`` ("es:"), a field whose Wikidata meaning is established (``aligned_property``) adds
    its value as evidence for that Wikidata relation, so a second Wikipedia confirms (or contests) what it knows.
    """
    n = 0
    for box in infoboxes:
        for raw_key, raw_val in (box.get("params") or {}).items():
            key = prefix + param_key(raw_key)
            if not key or SKIP_KEYS.match(key.removeprefix(prefix)) or not str(raw_val).strip():
                continue
            mapped = aligned_property(db, key) if prefix else None
            pid = graph.predicate(mapped) if mapped else graph.predicate(f"infobox:{key}", key.replace("_", " "))
            for kind, val in parse_value(str(raw_val)):
                if kind == "entity":
                    obj = resolve_title(val)
                    if obj is None or obj == subject:
                        continue
                    _tid, added = graph.add_triple(
                        subject,
                        pid,
                        o=obj,
                        confidence=0.7,
                        kind="infobox",
                        source="wikipedia",
                        doc_id=doc_id,
                        detail=f"{box.get('name')}:{key}",
                    )
                    if added:
                        _align(db, key, subject, obj)
                else:
                    _tid, added = graph.add_triple(
                        subject,
                        pid,
                        value=val,
                        confidence=0.7,
                        kind="infobox",
                        source="wikipedia",
                        doc_id=doc_id,
                        detail=f"{box.get('name')}:{key}",
                    )
                n += added
    return n


def _align(db: Database, key: str, s: int, o: int) -> None:
    for r in db.query(
        "SELECT p.key FROM triples t JOIN predicates p ON p.id=t.p WHERE t.s=? AND t.o=? AND p.key LIKE 'P%'", (s, o)
    ):
        db.execute(
            "INSERT INTO infobox_alignment(infobox_key, pid, agree) VALUES(?,?,1) "
            "ON CONFLICT(infobox_key, pid) DO UPDATE SET agree=agree+1",
            (key, r["key"]),
        )


def aligned_property(db: Database, infobox_key: str, min_agree: int = 3) -> str | None:
    """The Wikidata property an infobox field means, once evidence agrees often enough."""
    r = db.one(
        "SELECT pid, agree FROM infobox_alignment WHERE infobox_key=? ORDER BY agree DESC LIMIT 1", (infobox_key,)
    )
    return str(r["pid"]) if r and int(r["agree"]) >= min_agree else None
