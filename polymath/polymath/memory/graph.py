"""Knowledge graph of (subject, predicate, object|literal) triples with provenance.

Every triple has a status — ``sourced`` (stated by a source), ``inferred``
(derived by a rule) or ``disputed`` (conflicting evidence) — a confidence and
one provenance row per independent piece of evidence.
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Any

from polymath.core.db import Database

_SPACE = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]", re.U)


def norm_alias(text: str) -> str:
    """Surface-form normalisation used for every alias lookup."""
    t = unicodedata.normalize("NFKC", text).lower()
    t = _PUNCT.sub(" ", t)
    return _SPACE.sub(" ", t).strip()


def literal(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


@dataclass
class Entity:
    id: int
    key: str
    label: str
    description: str | None
    kind: str
    wiki_title: str | None
    doc_id: int | None


@dataclass
class Edge:
    triple_id: int
    predicate: str
    predicate_key: str
    direction: str  # "out" (entity is subject) | "in" (entity is object)
    other_id: int
    other_label: str
    value: Any
    status: str
    confidence: float


class KnowledgeGraph:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._pred_cache: dict[str, int] = {}

    # ---------------------------------------------------------------- entities
    def entity(self, entity_id: int) -> Entity | None:
        r = self.db.one("SELECT * FROM entities WHERE id=?", (entity_id,))
        return self._ent(r) if r else None

    def by_key(self, key: str) -> Entity | None:
        r = self.db.one("SELECT * FROM entities WHERE key=?", (key,))
        return self._ent(r) if r else None

    def by_wiki_title(self, title: str) -> Entity | None:
        r = self.db.one("SELECT * FROM entities WHERE wiki_title=? ORDER BY kind='stub' LIMIT 1", (title,))
        return self._ent(r) if r else None

    @staticmethod
    def _ent(r: Any) -> Entity:
        return Entity(int(r["id"]), r["key"], r["label"], r["description"], r["kind"], r["wiki_title"], r["doc_id"])

    def upsert_entity(
        self,
        key: str,
        label: str,
        *,
        description: str | None = None,
        kind: str = "item",
        wiki_title: str | None = None,
        doc_id: int | None = None,
    ) -> int:
        """Create or enrich an entity. A ``wiki:Title`` placeholder is merged into the Q-item when it appears."""
        now = time.time()
        row = self.db.one("SELECT id, kind FROM entities WHERE key=?", (key,))
        if row is None and wiki_title and key.startswith("Q"):
            placeholder = self.db.one("SELECT id FROM entities WHERE key=?", (f"wiki:{wiki_title}",))
            if placeholder is not None:
                self.db.execute("UPDATE entities SET key=? WHERE id=?", (key, placeholder["id"]))
                row = self.db.one("SELECT id, kind FROM entities WHERE key=?", (key,))
        if row is not None and wiki_title and key.startswith("Q"):
            placeholder = self.db.one("SELECT id FROM entities WHERE key=?", (f"wiki:{wiki_title}",))
            if placeholder is not None and placeholder["id"] != row["id"]:
                self.merge(int(row["id"]), int(placeholder["id"]))
        if row is None:
            cur = self.db.execute(
                "INSERT INTO entities(key, label, description, kind, wiki_title, doc_id, updated) "
                "VALUES(?,?,?,?,?,?,?)",
                (key, label, description, kind, wiki_title, doc_id, now),
            )
            return int(cur.lastrowid or 0)
        eid = int(row["id"])
        if kind == "stub" and row["kind"] != "stub":
            return eid  # never downgrade a real entity to a stub
        self.db.execute(
            "UPDATE entities SET label=?, description=COALESCE(?, description), kind=?, "
            "wiki_title=COALESCE(?, wiki_title), doc_id=COALESCE(?, doc_id), updated=? WHERE id=?",
            (label, description, kind, wiki_title, doc_id, now, eid),
        )
        return eid

    def merge(self, keep: int, drop: int) -> None:
        """Fold entity ``drop`` into ``keep``: aliases, triples (both directions), document link."""
        d = self.db.one("SELECT doc_id, wiki_title, description FROM entities WHERE id=?", (drop,))
        if d is None or keep == drop:
            return
        self.db.execute(
            "INSERT INTO aliases(alias, entity_id, source, count) SELECT alias, ?, source, count FROM aliases "
            "WHERE entity_id=? ON CONFLICT(alias, entity_id, source) DO UPDATE SET count=count+excluded.count",
            (keep, drop),
        )
        for col in ("s", "o"):
            for t in self.db.query(f"SELECT id, s, p, o, value FROM triples WHERE {col}=?", (drop,)):
                s, o = (keep, t["o"]) if col == "s" else (t["s"], keep)
                twin = self.db.one(
                    "SELECT id FROM triples WHERE s=? AND p=? AND o=? AND value=?", (s, t["p"], o, t["value"])
                )
                if twin is None:
                    self.db.execute(f"UPDATE triples SET {col}=? WHERE id=?", (keep, t["id"]))
                else:
                    self.db.execute(
                        "UPDATE OR IGNORE provenance SET triple_id=? WHERE triple_id=?", (twin["id"], t["id"])
                    )
                    self.db.execute(
                        "UPDATE triples SET n_sources=(SELECT COUNT(*) FROM provenance WHERE triple_id=?) WHERE id=?",
                        (twin["id"], twin["id"]),
                    )
                    self.db.execute("DELETE FROM triples WHERE id=?", (t["id"],))
        self.db.execute(
            "UPDATE entities SET doc_id=COALESCE(doc_id, ?), wiki_title=COALESCE(wiki_title, ?), "
            "description=COALESCE(description, ?) WHERE id=?",
            (d["doc_id"], d["wiki_title"], d["description"], keep),
        )
        self.db.execute("DELETE FROM entities WHERE id=?", (drop,))

    def stub(self, key: str) -> int:
        """Entity known only by reference (its own record has not been read yet)."""
        row = self.db.one("SELECT id FROM entities WHERE key=?", (key,))
        if row is not None:
            return int(row["id"])
        return self.upsert_entity(key, key, kind="stub")

    def add_alias(self, alias: str, entity_id: int, source: str, count: int = 1) -> None:
        a = norm_alias(alias)
        if not a or len(a) > 200:
            return
        self.db.execute(
            "INSERT INTO aliases(alias, entity_id, source, count) VALUES(?,?,?,?) "
            "ON CONFLICT(alias, entity_id, source) DO UPDATE SET count=count+excluded.count",
            (a, entity_id, source, count),
        )

    def candidates(self, surface: str, limit: int = 20) -> list[tuple[int, str, int]]:
        """(entity_id, label, total alias count) for a surface form, most common first."""
        rows = self.db.query(
            "SELECT a.entity_id, e.label, SUM(a.count) AS n FROM aliases a JOIN entities e ON e.id=a.entity_id "
            "WHERE a.alias=? GROUP BY a.entity_id ORDER BY n DESC LIMIT ?",
            (norm_alias(surface), limit),
        )
        return [(int(r["entity_id"]), r["label"], int(r["n"])) for r in rows]

    # -------------------------------------------------------------- predicates
    def predicate(self, key: str, label: str | None = None, *, datatype: str | None = None) -> int:
        if key in self._pred_cache and label is None:
            return self._pred_cache[key]
        row = self.db.one("SELECT id, label FROM predicates WHERE key=?", (key,))
        if row is None:
            cur = self.db.execute(
                "INSERT INTO predicates(key, label, datatype) VALUES(?,?,?)", (key, label or key, datatype)
            )
            pid = int(cur.lastrowid or 0)
        else:
            pid = int(row["id"])
            if label and row["label"] != label:
                self.db.execute(
                    "UPDATE predicates SET label=?, datatype=COALESCE(?, datatype) WHERE id=?", (label, datatype, pid)
                )
        self._pred_cache[key] = pid
        return pid

    def predicate_info(self, pid: int) -> dict[str, Any]:
        r = self.db.one("SELECT * FROM predicates WHERE id=?", (pid,))
        return dict(r) if r else {}

    # ----------------------------------------------------------------- triples
    def add_triple(
        self,
        s: int,
        p: int,
        *,
        o: int = 0,
        value: Any = None,
        status: str = "sourced",
        confidence: float = 0.6,
        kind: str,
        source: str,
        doc_id: int | None = None,
        detail: str = "",
        weight: float = 1.0,
    ) -> tuple[int, bool]:
        """Insert a triple or add evidence to it. Returns (triple_id, new_evidence)."""
        val = literal(value) if o == 0 and value is not None else ""
        now = time.time()
        row = self.db.one("SELECT id, status FROM triples WHERE s=? AND p=? AND o=? AND value=?", (s, p, o, val))
        if row is None:
            cur = self.db.execute(
                "INSERT INTO triples(s, p, o, value, status, confidence, n_sources, created, updated) "
                "VALUES(?,?,?,?,?,?,0,?,?)",
                (s, p, o, val, status, confidence, now, now),
            )
            tid = int(cur.lastrowid or 0)
        else:
            tid = int(row["id"])
            if row["status"] == "inferred" and status == "sourced":
                self.db.execute("UPDATE triples SET status='sourced', updated=? WHERE id=?", (now, tid))
        cur = self.db.execute(
            "INSERT OR IGNORE INTO provenance(triple_id, kind, doc_id, source, detail, weight, created) "
            "VALUES(?,?,?,?,?,?,?)",
            (tid, kind, doc_id or 0, source, detail[:500], weight, now),
        )
        added = bool(cur.rowcount)
        if added:
            self.db.execute("UPDATE triples SET n_sources=n_sources+1, updated=? WHERE id=?", (now, tid))
        return tid, added

    def set_status(self, triple_id: int, status: str, confidence: float | None = None) -> None:
        if confidence is None:
            self.db.execute("UPDATE triples SET status=?, updated=? WHERE id=?", (status, time.time(), triple_id))
        else:
            self.db.execute(
                "UPDATE triples SET status=?, confidence=?, updated=? WHERE id=?",
                (status, confidence, time.time(), triple_id),
            )

    def objects(self, s: int, p: int, *, include_holdout: bool = False) -> list[Any]:
        rows = self.db.query(
            "SELECT o, value FROM triples WHERE s=? AND p=?" + ("" if include_holdout else " AND holdout=0"), (s, p)
        )
        return [int(r["o"]) if r["o"] else json.loads(r["value"]) for r in rows]

    def neighbors(self, entity_id: int, *, limit: int = 50, include_holdout: bool = False) -> list[Edge]:
        """Outgoing and incoming edges with labels (index-backed; bounded by ``limit``)."""
        hold = "" if include_holdout else " AND t.holdout=0"
        out_rows = self.db.query(
            "SELECT t.id, p.label AS plabel, p.key AS pkey, t.o, COALESCE(e.label, '') AS olabel, t.value, t.status, "
            "t.confidence FROM triples t JOIN predicates p ON p.id=t.p LEFT JOIN entities e ON e.id=t.o "
            f"WHERE t.s=?{hold} ORDER BY t.confidence DESC LIMIT ?",
            (entity_id, limit),
        )
        in_rows = self.db.query(
            "SELECT t.id, p.label AS plabel, p.key AS pkey, t.s AS o, e.label AS olabel, '' AS value, t.status, "
            "t.confidence FROM triples t JOIN predicates p ON p.id=t.p JOIN entities e ON e.id=t.s "
            f"WHERE t.o=?{hold} ORDER BY t.confidence DESC LIMIT ?",
            (entity_id, limit),
        )
        edges = []
        for direction, rows in (("out", out_rows), ("in", in_rows)):
            for r in rows:
                edges.append(
                    Edge(
                        int(r["id"]),
                        r["plabel"],
                        r["pkey"],
                        direction,
                        int(r["o"]),
                        r["olabel"],
                        json.loads(r["value"]) if r["value"] else None,
                        r["status"],
                        float(r["confidence"]),
                    )
                )
        return edges[:limit]

    def provenance(self, triple_id: int) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.db.query(
                "SELECT p.*, d.title AS doc_title, d.url AS doc_url, d.license AS doc_license FROM provenance p "
                "LEFT JOIN documents d ON d.id=p.doc_id WHERE p.triple_id=? ORDER BY p.weight DESC",
                (triple_id,),
            )
        ]

    def stats(self) -> dict[str, int]:
        out = {
            "entities": int(self.db.scalar("SELECT COUNT(*) FROM entities", default=0)),
            "predicates": int(self.db.scalar("SELECT COUNT(*) FROM predicates", default=0)),
        }
        for r in self.db.query("SELECT status, COUNT(*) AS n FROM triples GROUP BY status"):
            out[f"triples_{r['status']}"] = int(r["n"])
        return out
