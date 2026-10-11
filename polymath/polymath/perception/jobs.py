"""Perception jobs: anchors → automaton → reading (sentences, entities, contexts, phrases, keywords) → learning."""

from __future__ import annotations

import json
import math
import random
import time
from typing import Any

from polymath.core.db import Database
from polymath.core.jobs import JobContext, JobOutcome
from polymath.memory.documents import DocumentStore, StoredDocument
from polymath.memory.graph import KnowledgeGraph, norm_alias
from polymath.perception import phrases as ph
from polymath.perception import semantic
from polymath.perception.entities import (
    AliasIndex,
    EntityLinker,
    context_vector,
    set_stopwords,
    tfidf_profile,
    train_from_anchors,
)
from polymath.perception.infobox import extract_infobox_triples
from polymath.perception.relations import learn_and_extract, normalize_middle
from polymath.perception.sentences import PunktModel, SentenceSplitter, train_model
from polymath.perception.tables import table_facts
from polymath.perception.textrank import keywords, summarize
from polymath.perception.tokenize import words

BATCH = 25
MAX_CONTEXTS_PER_DOC = 30


def resolve_title(
    db: Database, graph: KnowledgeGraph, title: str, *, create: bool = True, lang: str = "en"
) -> int | None:
    """Entity for a Wikipedia title, following redirects; unseen articles get a placeholder.

    A title in another language is found through Wikidata's links between languages (``wiki_sitelinks``) and never
    creates anything.
    """
    if lang != "en":
        target = db.scalar("SELECT target FROM wiki_redirects WHERE lang=? AND title=?", (lang, title)) or title
        qid = db.scalar("SELECT qid FROM wiki_sitelinks WHERE lang=? AND title=?", (lang, target))
        ent = graph.by_key(str(qid)) if qid else None
        return ent.id if ent is not None and ent.kind != "stub" else None
    target = db.scalar("SELECT target FROM wiki_redirects WHERE lang='en' AND title=?", (title,)) or title
    ent = graph.by_wiki_title(target)
    if ent is not None:
        return ent.id
    if not create:
        return None
    return graph.upsert_entity(f"wiki:{target}", target, kind="item", wiki_title=target)


def _stopwords(db: Database) -> set[str]:
    stops = ph.stopwords(db)
    set_stopwords(stops)
    return stops


# ------------------------------------------------------------------- anchors


def anchor_job(ctx: JobContext) -> JobOutcome:
    """Job ``perception.anchors``: harvest Wikipedia links (aliases, priors, co-links), profiles and infoboxes."""
    store: DocumentStore = ctx.services["docs"]
    graph = KnowledgeGraph(ctx.db)
    stats = {"docs": 0, "anchors": 0, "infobox_facts": 0, "table_facts": 0}
    first = True
    while first or not ctx.should_stop():
        first = False
        rows = ctx.db.query(
            "SELECT id FROM documents WHERE source='wikipedia' AND stage=0 AND state='indexed' ORDER BY id LIMIT ?",
            (BATCH,),
        )
        if not rows:
            break
        for r in rows:
            doc = store.get(int(r["id"]))
            if doc is None:
                continue
            lang = doc.lang or "en"
            if lang != "en":  # a second language: its infoboxes and tables cross-check what it knows
                me_id = resolve_title(ctx.db, graph, doc.title, lang=lang)
                foreign = lambda t, lang=lang: resolve_title(ctx.db, graph, t, lang=lang)  # noqa: E731
                if me_id is not None:
                    stats["infobox_facts"] += extract_infobox_triples(
                        ctx.db, graph, me_id, doc.id, doc.extra.get("infoboxes", []), foreign, prefix=f"{lang}:"
                    )
                stats["table_facts"] += table_facts(
                    ctx.db, graph, doc.id, doc.title, doc.extra.get("tables", []), foreign, prefix=f"{lang}:"
                )
                # stage 3: not read as English text (the linker, phrases and embeddings are English)
                ctx.db.execute("UPDATE documents SET stage=3 WHERE id=?", (doc.id,))
                stats["docs"] += 1
                continue
            stats["anchors"] += harvest_anchors(ctx.db, graph, doc)
            me = graph.by_wiki_title(doc.title)
            if me is not None:
                prof = tfidf_profile(ctx.db, doc.text)
                ctx.db.execute(
                    "INSERT OR REPLACE INTO entity_profiles(entity_id, terms) VALUES(?,?)", (me.id, json.dumps(prof))
                )
                stats["infobox_facts"] += extract_infobox_triples(
                    ctx.db,
                    graph,
                    me.id,
                    doc.id,
                    doc.extra.get("infoboxes", []),
                    lambda t: resolve_title(ctx.db, graph, t),
                )
            stats["table_facts"] += table_facts(
                ctx.db, graph, doc.id, doc.title, doc.extra.get("tables", []),
                lambda t: resolve_title(ctx.db, graph, t, create=False),
            )  # fmt: skip
            ctx.db.execute("UPDATE documents SET stage=1 WHERE id=?", (doc.id,))
            stats["docs"] += 1
            if ctx.should_stop():
                break
        ctx.tick()
    remaining = int(
        ctx.db.scalar(
            "SELECT COUNT(*) FROM documents WHERE source='wikipedia' AND stage=0 AND state='indexed'", default=0
        )
    )
    return JobOutcome(
        done=remaining == 0,
        value=0.2 * stats["docs"] + 0.01 * stats["anchors"],
        result={**stats, "remaining": remaining},
    )


def harvest_anchors(db: Database, graph: KnowledgeGraph, doc: StoredDocument) -> int:
    counts: dict[int, int] = {}
    surfaces: dict[str, int] = {}
    for a, b, target in doc.links:
        surface = norm_alias(doc.text[a:b])
        if not surface or len(surface) > 120:
            continue
        eid = resolve_title(db, graph, target)
        if eid is None:
            continue
        counts[eid] = counts.get(eid, 0) + 1
        surfaces.setdefault(surface, eid)
        graph.add_alias(surface, eid, "anchor")
    db.executemany(
        "INSERT INTO surface_stats(alias, linked, seen) VALUES(?,1,0) ON CONFLICT(alias) DO UPDATE SET linked=linked+1",
        [(s,) for s in surfaces],
    )
    db.executemany(
        "INSERT INTO doc_entities(doc_id, entity_id, count, score) VALUES(?,?,?,1.0) "
        "ON CONFLICT(doc_id, entity_id) DO UPDATE SET count=excluded.count",
        [(doc.id, e, c) for e, c in counts.items()],
    )
    return len(doc.links)


# ---------------------------------------------------------------- automaton


def automaton_job(ctx: JobContext) -> JobOutcome:
    """Job ``perception.automaton``: recompile the alias automaton when aliases grew or it aged."""
    idx = AliasIndex(ctx.db, ctx.config.paths.index_dir)
    if not ctx.job.payload.get("force") and not idx.stale():
        return JobOutcome(done=True, result={"rebuilt": False})
    info = idx.rebuild()
    ctx.services.pop("linker", None)
    return JobOutcome(done=True, value=1.0, result={"rebuilt": True, **info})


def _linker(ctx: JobContext) -> EntityLinker:
    built = (ctx.db.kv_get("alias_automaton") or {}).get("built")
    model_v = (ctx.db.kv_get("linker_model") or {}).get("trained_on")
    cached = ctx.services.get("linker")
    if cached is None or ctx.services.get("linker_version") != (built, model_v):
        auto = AliasIndex(ctx.db, ctx.config.paths.index_dir).load()
        cached = EntityLinker(ctx.db, auto)
        ctx.services["linker"] = cached
        ctx.services["linker_version"] = (built, model_v)
    linker: EntityLinker = cached
    return linker


def _splitter(ctx: JobContext) -> SentenceSplitter:
    raw = ctx.db.kv_get("punkt_model")
    key = (raw or {}).get("trained_tokens") if isinstance(raw, dict) else None
    if ctx.services.get("splitter_key") != key or "splitter" not in ctx.services:
        ctx.services["splitter"] = SentenceSplitter(PunktModel.from_json(raw))
        ctx.services["splitter_key"] = key
    sp: SentenceSplitter = ctx.services["splitter"]
    return sp


# ------------------------------------------------------------------- reading


def read_job(ctx: JobContext) -> JobOutcome:
    """Job ``perception.read``: sentences, entity links, relation contexts, phrases, keywords, summary."""
    store: DocumentStore = ctx.services["docs"]
    graph = KnowledgeGraph(ctx.db)
    splitter = _splitter(ctx)
    linker = _linker(ctx)
    stops = _stopwords(ctx.db)
    learner: ph.PhraseLearner = ctx.services.setdefault("phrase_learner", ph.PhraseLearner(ctx.db))
    stats = {"docs": 0, "links": 0, "contexts": 0, "sentences": 0}
    first = True
    while first or not ctx.should_stop():
        first = False
        rows = ctx.db.query(
            "SELECT id FROM documents WHERE state='indexed' AND stage < 2 AND (source != 'wikipedia' OR stage = 1) "
            "ORDER BY id LIMIT ?",
            (BATCH,),
        )
        if not rows:
            break
        for r in rows:
            doc = store.get(int(r["id"]))
            if doc is None:
                continue
            res = read_document(ctx.db, graph, linker, splitter, stops, learner, doc)
            for k in stats:
                stats[k] += res.get(k, 0)
            if ctx.should_stop():
                break
        ctx.tick()
    learner.flush()
    remaining = int(
        ctx.db.scalar(
            "SELECT COUNT(*) FROM documents WHERE state='indexed' AND stage < 2 "
            "AND (source != 'wikipedia' OR stage = 1)",
            default=0,
        )
    )
    return JobOutcome(
        done=remaining == 0, value=0.1 * stats["docs"] + 0.02 * stats["links"], result={**stats, "remaining": remaining}
    )


def read_document(
    db: Database,
    graph: KnowledgeGraph,
    linker: EntityLinker,
    splitter: SentenceSplitter,
    stops: set[str],
    learner: ph.PhraseLearner,
    doc: StoredDocument,
) -> dict[str, int]:
    text = doc.text[:200_000]  # books are read in their first 200k characters
    spans = splitter.spans(text)
    ctxv = context_vector(text, stops)
    links: list[tuple[int, int, int]] = []
    if doc.source == "wikipedia":
        for a, b, target in doc.links:
            if b <= len(text):
                eid = resolve_title(db, graph, target, create=False)
                if eid is not None:
                    links.append((a, b, eid))
        seen = {m.surface for m in linker.mentions(text, spans)}
        db.executemany(
            "INSERT INTO surface_stats(alias, linked, seen) VALUES(?,0,1) ON CONFLICT(alias) DO UPDATE SET seen=seen+1",
            [(s,) for s in seen],
        )
    taken = [(a, b) for a, b, _e in links]
    for lk in linker.link(text, spans, ctx=ctxv):
        if not any(lk.start < b and a < lk.end for a, b in taken):
            links.append((lk.start, lk.end, lk.entity_id))
    links.sort()
    if doc.source != "wikipedia":
        counts: dict[int, int] = {}
        for _a, _b, e in links:
            counts[e] = counts.get(e, 0) + 1
        db.executemany(
            "INSERT INTO doc_entities(doc_id, entity_id, count, score) VALUES(?,?,?,0.5) "
            "ON CONFLICT(doc_id, entity_id) DO UPDATE SET count=excluded.count",
            [(doc.id, e, c) for e, c in counts.items()],
        )
    n_ctx = 0
    now = time.time()
    rows = []
    props: list[tuple[int, str, int, int, str]] = []
    li = 0
    for sa, sb in spans:
        sent_links = []
        while li < len(links) and links[li][0] < sb:
            if links[li][0] >= sa and links[li][1] <= sb:
                sent_links.append(links[li])
            li += 1
        learner.add_sentence(words(text[sa:sb]))
        if sent_links:  # "birds can fly", "penguins cannot fly": what a kind of thing can or cannot do
            a0, b0, e0 = sent_links[0]
            got = semantic.category_props(text[sa:sb], (a0 - sa, b0 - sa, e0))
            if got is not None and semantic.is_class(db, got[0]):
                props.append((got[0], got[1], got[2], doc.id, text[sa:sb]))
        for i in range(len(sent_links)):
            for j in (i + 1, i + 2):
                if j >= len(sent_links) or n_ctx >= MAX_CONTEXTS_PER_DOC:
                    continue
                (a1, b1, e1), (a2, b2, e2) = sent_links[i], sent_links[j]
                if e1 == e2:
                    continue
                middle = normalize_middle(text[b1:a2])
                if not middle:
                    continue
                rows.append(
                    (
                        e1,
                        e2,
                        doc.id,
                        text[max(sa, a1 - 60) : a1],
                        middle,
                        text[b2 : min(sb, b2 + 60)],
                        text[sa:sb][:500],
                        now,
                    )
                )
                n_ctx += 1
    db.executemany(
        "INSERT INTO pair_contexts(e1, e2, doc_id, left_ctx, middle, right_ctx, sentence, created) "
        "VALUES(?,?,?,?,?,?,?,?)",
        rows,
    )
    semantic.store_props(db, props)
    sentences = [text[a:b] for a, b in spans[:400]]
    meta = dict(doc.meta)
    meta["keywords"] = keywords(text[:50_000], stops, top=10)
    meta["summary"] = [sentences[i][:400] for i in summarize(sentences[:120], stops, k=2)] if sentences else []
    meta["sentences"] = len(spans)
    db.execute(
        "UPDATE documents SET stage=2, meta=? WHERE id=?",
        (json.dumps(meta, ensure_ascii=False, separators=(",", ":")), doc.id),
    )
    return {"docs": 1, "links": len(links), "contexts": n_ctx, "sentences": len(spans)}


# ------------------------------------------------------------------ learning


def train_sentences_job(ctx: JobContext) -> JobOutcome:
    """Job ``perception.train_sentences``: learn abbreviations/orthography from a random document sample."""
    store: DocumentStore = ctx.services["docs"]
    ids = [
        int(r["id"])
        for r in ctx.db.query(
            "SELECT id FROM documents WHERE state!='duplicate' AND codec!='evicted' ORDER BY RANDOM() LIMIT ?",
            (int(ctx.job.payload.get("sample", 400)),),
        )
    ]
    texts = []
    for i in ids:
        d = store.get(i)
        if d:
            texts.append(d.text[:100_000])
        if len(texts) % 50 == 0:
            ctx.tick()
    model = train_model(texts)
    ctx.db.kv_set("punkt_model", json.loads(model.to_json()))
    ctx.services.pop("splitter", None)
    return JobOutcome(
        done=True,
        value=1.0,
        result={"docs": len(texts), "abbreviations": len(model.abbreviations), "tokens": model.trained_tokens},
    )


def train_phrases_job(ctx: JobContext) -> JobOutcome:
    n = ph.recompute_phrases(ctx.db)
    _stopwords(ctx.db)
    return JobOutcome(done=True, value=0.5, result={"phrases": n})


def train_linker_job(ctx: JobContext) -> JobOutcome:
    """Job ``perception.train_linker``: fit the disambiguation model on held-in Wikipedia anchors."""
    store: DocumentStore = ctx.services["docs"]
    graph = KnowledgeGraph(ctx.db)
    linker = _linker(ctx)
    _stopwords(ctx.db)
    sample = int(ctx.job.payload.get("docs", 200))
    ids = [
        int(r["id"])
        for r in ctx.db.query(
            "SELECT id FROM documents WHERE source='wikipedia' AND stage>=1 ORDER BY RANDOM() LIMIT ?", (sample,)
        )
    ]
    rng = random.Random(int(ctx.job.payload.get("seed", 7)))
    docs = []
    for i in ids:
        d = store.get(i)
        if d is None:
            continue
        anchors = []
        for a, b, target in d.links:
            eid = resolve_title(ctx.db, graph, target, create=False)
            if eid is not None:
                anchors.append((a, b, eid))
        rng.shuffle(anchors)
        docs.append((d.id, d.text, sorted(anchors[:60])))
        ctx.tick()
    model = train_from_anchors(linker, docs)
    ctx.db.kv_set("linker_model", model.to_dict())
    ctx.services.pop("linker", None)
    return JobOutcome(done=True, value=2.0, result=model.to_dict())


def relations_job(ctx: JobContext) -> JobOutcome:
    res = learn_and_extract(ctx.db, max_contexts=int(ctx.job.payload.get("max_contexts", 50_000)), tick=ctx.tick)
    return JobOutcome(done=True, value=0.05 * res["extracted"] + 0.1, result=res)


def maybe_reread(db: Database, factor: float = 2.0) -> int:
    """When the alias automaton has grown ``factor``-fold since documents were read, read them again.

    Early documents were read with a small dictionary; doubling-based epochs bound the total re-reading
    work to O(log(final size)) passes.
    """
    info = db.kv_get("alias_automaton") or {}
    patterns = int(info.get("patterns", 0))
    epoch = int(db.kv_get("read_epoch_patterns", 0))
    if not patterns or epoch == 0:
        if patterns:
            db.kv_set("read_epoch_patterns", patterns)
        return 0
    if patterns < factor * epoch:
        return 0
    n = db.execute("UPDATE documents SET stage = CASE WHEN source='wikipedia' THEN 1 ELSE 0 END WHERE stage=2").rowcount
    db.kv_set("read_epoch_patterns", patterns)
    return int(n)


def planner(agent: Any) -> None:
    s = agent.scheduler
    db: Database = agent.db
    if db.scalar("SELECT 1 FROM documents WHERE source='wikipedia' AND stage=0 AND state='indexed' LIMIT 1"):
        s.ensure_recurring("perception.anchors", 30, priority=2.2)
    idx = AliasIndex(db, agent.config.paths.index_dir)
    if idx.stale():
        # one rebuild per 10% of alias growth (and at least daily): keyed by size, not by clock slot
        total = int(db.scalar("SELECT COUNT(*) FROM aliases", default=0))
        bucket = int(math.log(max(total, 1)) / math.log(1.1))
        day = int(time.time() // 86400)
        s.enqueue("perception.automaton", {}, key=f"perception.automaton:{bucket}:{day}", priority=2.6)
    if (
        not db.kv_get("punkt_model")
        and int(db.scalar("SELECT COUNT(*) FROM documents WHERE state='indexed'", default=0)) >= 100
    ):
        s.ensure_recurring("perception.train_sentences", 7 * 86400, priority=2.7)
    elif db.kv_get("punkt_model"):
        s.ensure_recurring("perception.train_sentences", 7 * 86400, priority=0.4)
    anchors_pending = db.scalar(
        "SELECT 1 FROM documents WHERE source='wikipedia' AND stage=0 AND state='indexed' LIMIT 1"
    )
    ready = db.kv_get("punkt_model") and idx.path.exists() and not anchors_pending and not idx.stale()
    maybe_reread(db)
    if ready and db.scalar(
        "SELECT 1 FROM documents WHERE state='indexed' AND stage < 2 AND (source != 'wikipedia' OR stage = 1) LIMIT 1"
    ):
        s.ensure_recurring("perception.read", 30, priority=2.0)
    if int(db.scalar("SELECT COUNT(*) FROM documents WHERE source='wikipedia' AND stage>=1", default=0)) >= 50:
        s.ensure_recurring("perception.train_linker", 86400, priority=1.2)
    s.ensure_recurring("perception.train_phrases", 86400, priority=0.6)
    if db.scalar("SELECT 1 FROM pair_contexts LIMIT 1"):
        s.ensure_recurring("perception.relations", 3600, priority=1.0)
