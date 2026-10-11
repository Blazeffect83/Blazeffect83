"""Facts from Wikipedia's data tables (lists of tallest buildings, chemical elements, presidents …).

A wikitable becomes facts when one column names things (at least ``KEY_SHARE`` of its cells link to articles it
knows): each row is about that thing, and every other column with a usable header is a relation. A linked cell gives
a relation to another thing; otherwise the cell is read like an infobox value (a number with its unit, a date, a
year, text). Predicates are keyed ``table:<header>`` and learn their Wikidata meaning the same way infobox fields do
(``infobox_alignment``), so evidence from tables backs up the matching Wikidata facts.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from polymath.core.db import Database
from polymath.memory.graph import KnowledgeGraph
from polymath.perception.infobox import _align, parse_value

KEY_SHARE = 0.6
SKIP_HEADERS = {"", "#", "no", "no.", "nr", "rank", "notes", "note", "ref", "refs", "references", "image", "images",
                "photo", "picture", "map", "flag", "coat of arms", "remarks", "source", "sources", "citation", "links",
                "website", "comments", "description", "details", "other", "misc"}  # fmt: skip


def header_key(header: str) -> str:
    h = re.sub(r"\[.*?\]|\(.*?\)", "", header.lower())
    h = re.sub(r"[^a-z0-9 ]+", " ", h)
    return re.sub(r"\s+", "_", h.strip())


def key_column(table: dict[str, Any], resolve: Callable[[str], int | None]) -> int | None:
    rows = table["rows"]
    for col in range(len(table["headers"])):
        linked = sum(1 for r in rows if r[col].get("link") and resolve(r[col]["link"]) is not None)
        if linked >= KEY_SHARE * len(rows):
            return col
    return None


def table_facts(
    db: Database, graph: KnowledgeGraph, doc_id: int, doc_title: str, tables: list[dict[str, Any]],
    resolve: Callable[[str], int | None], *, prefix: str = "",
) -> int:  # fmt: skip
    """Add the facts of every usable table; returns the number of new evidence rows."""
    n = 0
    for table in tables:
        col = key_column(table, resolve)
        if col is None:
            continue
        headers = table["headers"]
        for row in table["rows"]:
            subject = resolve(row[col]["link"]) if row[col].get("link") else None
            if subject is None:
                continue
            for j, header in enumerate(headers):
                key = header_key(header)
                if j == col or key in SKIP_HEADERS or len(key) > 40:
                    continue
                cell = row[j]
                full = f"{prefix}{key}"
                pid = graph.predicate(f"table:{full}", header.strip()[:60] or key)
                detail = f"{doc_title} table: {header.strip()[:40]}"
                if cell.get("link"):
                    obj = resolve(cell["link"])
                    if obj is None or obj == subject:
                        continue
                    _tid, added = graph.add_triple(subject, pid, o=obj, confidence=0.6, kind="table",
                                                   source="wikipedia", doc_id=doc_id, detail=detail)  # fmt: skip
                    if added:
                        _align(db, f"table:{full}", subject, obj)
                    n += added
                elif cell.get("text") and str(cell["text"]).strip().lower() not in {"—", "–", "-", "n/a", "?", "tba"}:
                    for kind, val in parse_value(str(cell["text"])):
                        if kind == "entity":
                            continue
                        _tid, added = graph.add_triple(subject, pid, value=val, confidence=0.6, kind="table",
                                                       source="wikipedia", doc_id=doc_id, detail=detail)  # fmt: skip
                        n += added
    return n
