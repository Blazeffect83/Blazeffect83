"""Memory jobs: index documents (passages, near-duplicates, topics), build the graph, maintain topics."""

from __future__ import annotations

import json
from typing import Any

from polymath.core.jobs import JobContext, JobOutcome
from polymath.memory.dedup import NearDupIndex, minhash, simhash
from polymath.memory.documents import DocumentStore
from polymath.memory.graph import KnowledgeGraph
from polymath.memory.text_index import TextIndex
from polymath.memory.topics import TopicMap

BATCH = 100


def index_documents(ctx: JobContext) -> JobOutcome:
    """Job ``memory.index``: new documents → passages + FTS, near-dup check, topics, wiki entities."""
    store: DocumentStore = ctx.services["docs"]
    tindex = TextIndex(ctx.db, store)
    near = NearDupIndex(ctx.db)
    topics = TopicMap(ctx.db)
    graph = KnowledgeGraph(ctx.db)
    stats = {"indexed": 0, "near_duplicates": 0, "chunks": 0}
    first = True
    while first or not ctx.should_stop():
        first = False
        rows = ctx.db.query("SELECT id FROM documents WHERE state='new' ORDER BY id LIMIT ?", (BATCH,))
        if not rows:
            break
        for r in rows:
            doc = store.get(int(r["id"]))
            if doc is None:
                continue
            sig = minhash(doc.text)
            dup = near.find(sig, exclude=doc.id)
            sh = simhash(doc.text)
            if dup is not None:
                ctx.db.execute(
                    "UPDATE documents SET state='duplicate', near_dup_of=?, simhash=? WHERE id=?", (dup[0], sh, doc.id)
                )
                stats["near_duplicates"] += 1
                continue
            if (doc.lang or "en") != "en":  # a second language: read for its infoboxes and tables only
                ctx.db.execute("UPDATE documents SET state='indexed', simhash=? WHERE id=?", (sh, doc.id))
                stats["indexed"] += 1
                continue
            stats["chunks"] += tindex.index(doc.id, doc.title, doc.text)
            near.add(doc.id, sig)
            topics.assign_from_meta(doc.id, doc.source, doc.meta)
            if doc.source == "wikipedia" and not doc.meta.get("disambiguation"):
                existing = graph.by_wiki_title(doc.title)
                key = existing.key if existing else f"wiki:{doc.title}"
                eid = graph.upsert_entity(
                    key,
                    existing.label if existing else doc.title,
                    description=doc.meta.get("short_description"),
                    kind="item",
                    wiki_title=doc.title,
                    doc_id=doc.id,
                )
                graph.add_alias(doc.title, eid, "title")
            ctx.db.execute("UPDATE documents SET state='indexed', simhash=? WHERE id=?", (sh, doc.id))
            stats["indexed"] += 1
            if ctx.should_stop():
                break
        ctx.tick()
    remaining = int(ctx.db.scalar("SELECT COUNT(*) FROM documents WHERE state='new'", default=0))
    return JobOutcome(
        done=remaining == 0, checkpoint={}, value=0.2 * stats["indexed"], result={**stats, "remaining": remaining}
    )


PROPERTY_META = {"P31", "P279", "P1647", "P1696", "P461"}  # instance of, subclass, subproperty, inverse, opposite


def _claims_to_triples(graph: KnowledgeGraph, eid: int, claims: dict[str, Any]) -> int:
    """Claims → sourced triples; their time qualifiers (``_when``) say when each fact holds (``fact_time``)."""
    from polymath.reasoning import temporal

    when = claims.get("_when") or {}
    n = 0
    for pid, values in claims.items():
        if pid.startswith("_"):
            continue
        p = graph.predicate(pid)
        times = {int(w[0]): w[1:] for w in when.get(pid, [])}
        for i, v in enumerate(values):
            if isinstance(v, str) and v[:1] in {"Q", "P"} and v[1:].isdigit():
                tid, added = graph.add_triple(
                    eid, p, o=graph.stub(v), confidence=0.9, kind="wikidata", source="wikidata", detail=pid
                )
            else:
                tid, added = graph.add_triple(
                    eid, p, value=v, confidence=0.9, kind="wikidata", source="wikidata", detail=pid
                )
            if i in times:
                temporal.record(graph.db, tid, *times[i])
            n += added
    return n


def build_graph(ctx: JobContext) -> JobOutcome:
    """Job ``memory.graph``: Wikidata entities → entities, aliases, predicates, sourced triples; redirects → aliases."""
    graph = KnowledgeGraph(ctx.db)
    stats = {"entities": 0, "triples": 0, "redirect_aliases": 0}
    first = True
    while first or not ctx.should_stop():
        first = False
        rows = ctx.db.query("SELECT * FROM wd_entities WHERE state='new' ORDER BY qid LIMIT ?", (BATCH,))
        if not rows:
            break
        for r in rows:
            claims = json.loads(r["claims"])
            qid = str(r["qid"])
            if qid.startswith("P"):
                graph.predicate(qid, r["label"], datatype=(claims.get("_datatype") or [None])[0])
                pent = graph.upsert_entity(qid, r["label"], description=r["description"], kind="property")
                for a in [r["label"], *json.loads(r["aliases"])]:
                    graph.add_alias(a, pent, "label")
                # the property's own semantics (class, inverse, super-property) feed rule learning
                meta = {k: v for k, v in claims.items() if k in PROPERTY_META}
                stats["triples"] += _claims_to_triples(graph, pent, meta)
            else:
                doc_id = None
                if r["enwiki"]:
                    doc_id = ctx.db.scalar(
                        "SELECT id FROM documents WHERE source='wikipedia' AND title=? AND state!='duplicate' LIMIT 1",
                        (r["enwiki"],),
                    )
                eid = graph.upsert_entity(
                    qid, r["label"], description=r["description"], kind="item", wiki_title=r["enwiki"], doc_id=doc_id
                )
                graph.add_alias(r["label"], eid, "label")
                for a in json.loads(r["aliases"]):
                    graph.add_alias(a, eid, "alias")
                if r["enwiki"] and r["enwiki"] != r["label"]:
                    graph.add_alias(r["enwiki"], eid, "title")
                stats["triples"] += _claims_to_triples(graph, eid, claims)
            ctx.db.execute("UPDATE wd_entities SET state='linked' WHERE qid=?", (qid,))
            stats["entities"] += 1
        ctx.tick()
    stats["redirect_aliases"] = link_redirects(ctx, graph)
    remaining = int(ctx.db.scalar("SELECT COUNT(*) FROM wd_entities WHERE state='new'", default=0))
    return JobOutcome(
        done=remaining == 0,
        value=0.05 * stats["entities"] + 0.02 * stats["triples"],
        result={**stats, "remaining": remaining},
    )


def link_redirects(ctx: JobContext, graph: KnowledgeGraph, batch: int = 2000) -> int:
    """Redirect titles become aliases of their target entity (cursor wraps so late targets get linked)."""
    cursor = str(ctx.db.kv_get("redirect_cursor", ""))
    rows = ctx.db.query(
        "SELECT r.title, e.id AS eid FROM wiki_redirects r JOIN entities e ON e.wiki_title=r.target "
        "WHERE r.title > ? ORDER BY r.title LIMIT ?",
        (cursor, batch),
    )
    for r in rows:
        graph.add_alias(r["title"], int(r["eid"]), "redirect")
    ctx.db.kv_set("redirect_cursor", rows[-1]["title"] if len(rows) == batch else "")
    return len(rows)


def maintain_topics(ctx: JobContext) -> JobOutcome:
    """Job ``memory.topics``: import category hierarchy edges, recompute levels and subtree sizes."""
    tm = TopicMap(ctx.db)
    edges = tm.import_category_edges()
    stats = tm.recompute()
    return JobOutcome(done=True, value=0.1, result={"edges_imported": edges, **stats})


def planner(agent: Any) -> None:
    s = agent.scheduler
    db = agent.db
    if db.scalar("SELECT 1 FROM documents WHERE state='new' LIMIT 1"):
        s.ensure_recurring("memory.index", 30, priority=2.5)
    if db.scalar("SELECT 1 FROM wd_entities WHERE state='new' LIMIT 1"):
        s.ensure_recurring("memory.graph", 30, priority=2.4)
    s.ensure_recurring("memory.topics", 3600, priority=0.5)
