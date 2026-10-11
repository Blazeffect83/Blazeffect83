"""Hierarchical topic map built from the agent's own data.

Topics come from labels the sources attach (Wikipedia categories and the
category hierarchy from category pages, MeSH headings, OpenAlex topics and
concepts, Stack Exchange tags, Gutenberg subjects and bookshelves). Edges form a
DAG (cycles in the category graph are broken when levels are computed); document
counts are rolled up so every topic knows the size of its subtree.
"""

from __future__ import annotations

import re
from collections import defaultdict, deque
from typing import Any

from polymath.core.db import Database

# Maintenance / non-topical Wikipedia categories.
NOISE = re.compile(
    r"(^|\b)(articles?|pages?|wikipedia|webarchive|cs1|use (dmy|mdy)|all stub|stubs?$|short description|"
    r"living people|births$|deaths$|disambiguation|redirects?|templates?|commons category|coordinates|"
    r"good articles|featured articles|wikidata|lacking|unsourced|citation|dead links|infobox|navbox|"
    r"engvarb|vague|clean ?up|orphan|year of birth|year of death|place of birth|missing)",
    re.I,
)
_YEARISH = re.compile(r"^\d{1,4}s?( (bc|bce|ad|ce))?\b", re.I)


def clean_topic(name: str) -> str | None:
    n = " ".join(name.replace("_", " ").split()).strip(" .")
    if not n or len(n) > 120 or NOISE.search(n) or (_YEARISH.match(n) and len(n.split()) <= 2):
        return None
    return n[:1].upper() + n[1:]


def labels_from_meta(source: str, meta: dict[str, Any]) -> list[tuple[str, str]]:
    """(topic name, kind) pairs a document's metadata assigns."""
    out: list[tuple[str, str]] = []

    def add(values: Any, kind: str, limit: int) -> None:
        for v in (values or [])[:limit]:
            if isinstance(v, str) and (c := clean_topic(v.split(" -- ")[0])):
                out.append((c, kind))

    add(meta.get("categories"), "category", 20)
    add(meta.get("mesh"), "mesh", 10)
    add(meta.get("topics"), "concept", 5)
    add(meta.get("concepts"), "concept", 5)
    add([t.replace("-", " ") for t in meta.get("tags") or []], "tag", 6)
    add(meta.get("subjects"), "subject", 6)
    add([b.removeprefix("Category: ") for b in meta.get("bookshelves") or []], "subject", 4)
    return list(dict.fromkeys(out))


class TopicMap:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._cache: dict[str, int] = {}

    def topic(self, name: str, kind: str = "label") -> int:
        if name in self._cache:
            return self._cache[name]
        row = self.db.one("SELECT id FROM topics WHERE name=?", (name,))
        if row is None:
            tid = int(self.db.execute("INSERT INTO topics(name, kind) VALUES(?,?)", (name, kind)).lastrowid or 0)
        else:
            tid = int(row["id"])
        if len(self._cache) > 50_000:
            self._cache.clear()
        self._cache[name] = tid
        return tid

    def link(self, child: int, parent: int, source: str) -> None:
        if child != parent:
            self.db.execute(
                "INSERT OR IGNORE INTO topic_edges(child, parent, source) VALUES(?,?,?)", (child, parent, source)
            )

    def assign(self, doc_id: int, topic_id: int, weight: float = 1.0) -> bool:
        cur = self.db.execute(
            "INSERT OR IGNORE INTO doc_topics(doc_id, topic_id, weight) VALUES(?,?,?)", (doc_id, topic_id, weight)
        )
        if cur.rowcount:
            self.db.execute("UPDATE topics SET n_docs=n_docs+1 WHERE id=?", (topic_id,))
        return bool(cur.rowcount)

    def assign_from_meta(self, doc_id: int, source: str, meta: dict[str, Any]) -> int:
        n = 0
        for name, kind in labels_from_meta(source, meta):
            n += self.assign(doc_id, self.topic(name, kind))
        return n

    def import_category_edges(self, lang: str = "en") -> int:
        """Turn harvested category→parent links into topic edges (only for topics in use)."""
        n = 0
        rows = self.db.query(
            "SELECT w.child, w.parent FROM wiki_category_edges w JOIN topics t ON t.name=w.child WHERE w.lang=?",
            (lang,),
        )
        for r in rows:
            parent = clean_topic(r["parent"])
            child = clean_topic(r["child"])
            if parent and child:
                self.link(self.topic(child, "category"), self.topic(parent, "category"), "wikipedia")
                n += 1
        return n

    def recompute(self) -> dict[str, int]:
        """Levels (BFS depth from roots, cycles broken) and subtree document totals."""
        parents: dict[int, list[int]] = defaultdict(list)
        children: dict[int, list[int]] = defaultdict(list)
        for r in self.db.query("SELECT child, parent FROM topic_edges"):
            parents[int(r["child"])].append(int(r["parent"]))
            children[int(r["parent"])].append(int(r["child"]))
        n_docs = {int(r["id"]): int(r["n_docs"]) for r in self.db.query("SELECT id, n_docs FROM topics")}
        roots = [t for t in n_docs if not parents.get(t)]
        level: dict[int, int] = {}
        queue = deque((r, 0) for r in roots)
        while queue:
            t, lv = queue.popleft()
            if t in level:
                continue
            level[t] = lv
            for c in children.get(t, []):
                if c not in level:
                    queue.append((c, lv + 1))
        for t in n_docs:  # nodes only reachable through a cycle
            level.setdefault(t, 0)
        total = dict(n_docs)
        # deepest first: each node contributes to *one* parent (its shallowest) to avoid double counting
        for t in sorted(n_docs, key=lambda x: -level[x]):
            ps = [p for p in parents.get(t, []) if level.get(p, 0) < level[t]]
            if ps:
                best = min(ps, key=lambda p: (level[p], p))
                total[best] = total.get(best, 0) + total[t]
        self.db.executemany(
            "UPDATE topics SET level=?, n_total=? WHERE id=?", [(level[t], total[t], t) for t in n_docs]
        )
        return {"topics": len(n_docs), "roots": len(roots), "max_level": max(level.values(), default=0)}

    def children(self, topic_id: int) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.db.query(
                "SELECT t.* FROM topic_edges e JOIN topics t ON t.id=e.child WHERE e.parent=? ORDER BY t.n_total DESC",
                (topic_id,),
            )
        ]

    def path_to_root(self, topic_id: int, max_depth: int = 30) -> list[str]:
        path: list[str] = []
        seen = set()
        cur: int | None = topic_id
        while cur is not None and cur not in seen and len(path) < max_depth:
            seen.add(cur)
            row = self.db.one("SELECT name FROM topics WHERE id=?", (cur,))
            if row is None:
                break
            path.append(str(row["name"]))
            parent = self.db.one(
                "SELECT e.parent FROM topic_edges e JOIN topics t ON t.id=e.parent WHERE e.child=? "
                "ORDER BY COALESCE(t.level, 99), t.n_total DESC LIMIT 1",
                (cur,),
            )
            cur = int(parent["parent"]) if parent else None
        return path

    def top(self, limit: int = 20) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.query("SELECT * FROM topics ORDER BY n_total DESC LIMIT ?", (limit,))]
