"""Embedding jobs: train SGNS continuously, embed documents/entities (SIF), maintain the IVF indexes,
and measure embedding quality with checks generated from the knowledge graph."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import numpy as np

from polymath.core.config import Config
from polymath.core.db import Database
from polymath.core.jobs import JobContext, JobOutcome
from polymath.memory.documents import DocumentStore
from polymath.memory.vector_index import IVFIndex
from polymath.perception.embeddings import SGNS, SIF, Vocab, sentence_tokens
from polymath.perception.phrases import load_phrases
from polymath.perception.tokenize import words

SAVE_EVERY = 300.0  # seconds between model checkpoints
MAX_EPOCHS = 5


def emb_dir(config: Config) -> Path:
    return config.paths.index_dir / "embeddings"


def doc_index(config: Config) -> IVFIndex:
    return IVFIndex(config.paths.index_dir / "ivf_docs", config.learning.embedding_dim)


def entity_index(config: Config) -> IVFIndex:
    return IVFIndex(config.paths.index_dir / "ivf_entities", config.learning.embedding_dim)


def load_model(ctx: JobContext) -> tuple[SGNS, dict[str, Any]] | None:
    cached = ctx.services.get("sgns")
    if cached is not None:
        model: SGNS = cached[0]
        state: dict[str, Any] = cached[1]
        return model, state
    loaded = SGNS.load(emb_dir(ctx.config))
    if loaded is not None:
        ctx.services["sgns"] = loaded
    return loaded


def train_job(ctx: JobContext) -> JobOutcome:
    """Job ``embed.train``: one slice of skip-gram training over documents (cursor resumes across slices)."""
    cfg = ctx.config.learning
    loaded = load_model(ctx)
    vocab = Vocab.from_db(ctx.db, min_count=cfg.embedding_min_count, max_size=cfg.embedding_vocab_max)
    if len(vocab) < 50:
        return JobOutcome(done=True, result={"skipped": "vocabulary too small", "vocab": len(vocab)})
    if loaded is None:
        model, state = SGNS(vocab, cfg.embedding_dim), {"cursor": 0, "epoch": 0, "tokens": 0, "saved_at": 0.0}
    else:
        model, state = loaded
        if len(vocab) > 1.2 * len(model.vocab):
            model = model.regrow(vocab)
    ctx.services["sgns"] = (model, state)
    newest = int(ctx.db.scalar("SELECT MAX(id) FROM documents WHERE stage=2", default=0))
    if int(state.get("epoch", 0)) >= MAX_EPOCHS:
        if newest <= int(state.get("trained_upto", 0)):
            return JobOutcome(done=True, result={"converged": True, "vocab": len(model.vocab)})
        state["epoch"], state["cursor"] = MAX_EPOCHS - 1, int(state.get("trained_upto", 0))  # absorb new docs
    store: DocumentStore = ctx.services["docs"]
    phrases = load_phrases(ctx.db)
    epoch = int(state.get("epoch", 0))
    lr = max(0.0025, 0.025 * (1 - epoch / MAX_EPOCHS))
    pairs_total = 0
    losses = []
    first = True
    while first or not ctx.should_stop():
        first = False
        rows = ctx.db.query(
            "SELECT id FROM documents WHERE id > ? AND stage = 2 ORDER BY id LIMIT 20", (int(state["cursor"]),)
        )
        if not rows:
            state["cursor"] = 0
            state["epoch"] = epoch = epoch + 1
            if epoch >= MAX_EPOCHS:
                state["trained_upto"] = newest
                break
            continue
        sents: list[np.ndarray] = []
        for r in rows:
            d = store.get(int(r["id"]))
            if d is not None:
                sents.extend(model.encode(s) for s in sentence_tokens(d.text[:200_000], phrases))
            state["cursor"] = int(r["id"])
        c, o = model.pairs(sents, ctx.config.learning.embedding_window)
        if c.size:
            losses.append(model.train_pairs(c, o, negatives=cfg.embedding_negatives, lr=lr))
            pairs_total += int(c.size)
        ctx.tick()
    if time.time() - float(state.get("saved_at", 0)) > SAVE_EVERY or ctx.stop_event.is_set() or epoch >= MAX_EPOCHS:
        state["saved_at"] = time.time()
        model.save(emb_dir(ctx.config), state)
    done = epoch >= MAX_EPOCHS
    return JobOutcome(
        done=done,
        value=pairs_total / 1e6,
        result={
            "pairs": pairs_total,
            "epoch": epoch,
            "cursor": state["cursor"],
            "vocab": len(model.vocab),
            "loss": round(float(np.mean(losses)), 4) if losses else None,
        },
    )


def _sif(ctx: JobContext, model: SGNS) -> SIF:
    key = id(model)
    cached = ctx.services.get("sif")
    if cached is not None and cached[0] == key:
        sif: SIF = cached[1]
        return sif
    path = emb_dir(ctx.config) / "sif_pc.npy"
    sif = SIF(model, pc=np.load(path) if path.exists() else None)
    if sif.pc is None:
        store: DocumentStore = ctx.services["docs"]
        samples: list[np.ndarray] = []
        for r in ctx.db.query("SELECT id FROM documents WHERE stage=2 ORDER BY RANDOM() LIMIT 2000"):
            d = store.get(int(r["id"]))
            if d and (v := sif.raw(words(d.text[:20_000]))) is not None:
                samples.append(v)
        pc = sif.fit_pc(samples)
        if pc is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            np.save(path, pc)
    ctx.services["sif"] = (key, sif)
    return sif


def embed_docs_job(ctx: JobContext) -> JobOutcome:
    """Job ``embed.docs``: SIF vectors for read documents → document index; article vectors → entity index."""
    loaded = load_model(ctx)
    if loaded is None:
        return JobOutcome(done=True, result={"skipped": "no embedding model yet"})
    model, _state = loaded
    sif = _sif(ctx, model)
    store: DocumentStore = ctx.services["docs"]
    didx, eidx = doc_index(ctx.config), entity_index(ctx.config)
    cursor = int(ctx.db.kv_get("embed_doc_cursor", 0))
    done_docs = 0
    first = True
    while first or not ctx.should_stop():
        first = False
        rows = ctx.db.query("SELECT id FROM documents WHERE id > ? AND stage=2 ORDER BY id LIMIT 200", (cursor,))
        if not rows:
            break
        ids, vecs, eids, evecs = [], [], [], []
        for r in rows:
            d = store.get(int(r["id"]))
            cursor = int(r["id"])
            if d is None:
                continue
            v = sif.embed(words(d.text[:50_000]))
            if v is None:
                continue
            ids.append(d.id)
            vecs.append(v)
            ent = ctx.db.scalar("SELECT id FROM entities WHERE doc_id=? LIMIT 1", (d.id,))
            if ent is not None:
                eids.append(int(ent))
                evecs.append(v)
        if ids:
            didx.add(np.array(ids, dtype=np.int64), np.vstack(vecs))
        if eids:
            eidx.add(np.array(eids, dtype=np.int64), np.vstack(evecs))
        done_docs += len(ids)
        ctx.db.kv_set("embed_doc_cursor", cursor)
        ctx.tick()
    remaining = int(ctx.db.scalar("SELECT COUNT(*) FROM documents WHERE id > ? AND stage=2", (cursor,), 0))
    return JobOutcome(
        done=remaining == 0, value=0.01 * done_docs, result={"embedded": done_docs, "remaining": remaining}
    )


def rebuild_index_job(ctx: JobContext) -> JobOutcome:
    out = {}
    for name, idx in (("docs", doc_index(ctx.config)), ("entities", entity_index(ctx.config))):
        if idx.needs_rebuild() or ctx.job.payload.get("force"):
            out[name] = idx.rebuild(nlist=ctx.config.learning.index_nlist)
            ctx.tick()
    return JobOutcome(done=True, value=0.5 if out else 0.0, result=out)


# ------------------------------------------------------------- quality checks


def quality_report(
    db: Database, model: SGNS, eidx: IVFIndex, *, max_checks: int = 300, seed: int = 0
) -> dict[str, Any]:
    """Embedding checks generated from the knowledge graph (no hand-made benchmark needed).

    * ``alias_similarity``: cosine of two single-word aliases of the same entity vs. random word pairs.
    * ``relation_hit@10``: for entity–entity facts, is the object among the subject's 10 nearest entities?
    * ``offset_consistency``: for one predicate, how parallel are the (object − subject) word offsets
      (country→capital style analogies) compared with random pairs.
    """
    rng = np.random.default_rng(seed)
    unit = model.normalized()
    vocab = model.vocab.index
    out: dict[str, Any] = {"vocab": len(model.vocab), "pairs_seen": model.pairs_seen}
    # 1) aliases
    rows = db.query(
        "SELECT entity_id, GROUP_CONCAT(alias, '|') AS al FROM aliases WHERE alias NOT LIKE '% %' "
        "GROUP BY entity_id HAVING COUNT(*) >= 2 LIMIT 20000"
    )
    syn = []
    for r in rows:
        al = [a for a in str(r["al"]).split("|") if a in vocab]
        if len(al) >= 2 and al[0] != al[1]:
            syn.append(float(unit[vocab[al[0]]] @ unit[vocab[al[1]]]))
        if len(syn) >= max_checks:
            break
    if syn:
        ids = rng.integers(0, len(model.vocab), size=(len(syn), 2))
        rand = [float(unit[a] @ unit[b]) for a, b in ids]
        out["alias_similarity"] = {
            "pairs": len(syn),
            "mean": round(float(np.mean(syn)), 4),
            "random_mean": round(float(np.mean(rand)), 4),
            "auc": round(_auc(syn, rand), 4),
        }
    # 2) relation neighbours in the entity index
    stats = eidx.stats()
    if stats["indexed"] + stats["tail"] >= 20:
        facts = db.query(
            "SELECT t.s, t.o FROM triples t WHERE t.o != 0 AND t.holdout=0 AND t.status='sourced' "
            "ORDER BY RANDOM() LIMIT ?",
            (max_checks * 5,),
        )
        hits = tested = 0
        vec_cache: dict[int, Any] = {}
        for f in facts:
            s, o = int(f["s"]), int(f["o"])
            if s not in vec_cache:
                vec_cache[s] = _entity_vector(eidx, s)
            if vec_cache[s] is None:
                continue
            nn = {i for i, _sc in eidx.search(vec_cache[s], 11, nprobe=32)}
            nn.discard(s)
            if o in nn:
                hits += 1
            tested += 1
            if tested >= max_checks:
                break
        total = stats["indexed"] + stats["tail"]
        if tested:
            out["relation_hit@10"] = {
                "tested": tested,
                "hit_rate": round(hits / tested, 4),
                "random_baseline": round(min(1.0, 10 / max(total, 1)), 6),
            }
    # 3) offset consistency for the predicate with most single-word pairs
    best = None
    for p in db.query("SELECT p, COUNT(*) AS n FROM triples WHERE o != 0 GROUP BY p ORDER BY n DESC LIMIT 15"):
        pairs = []
        for r in db.query(
            "SELECT es.label AS sl, eo.label AS ol FROM triples t JOIN entities es ON es.id=t.s "
            "JOIN entities eo ON eo.id=t.o WHERE t.p=? LIMIT 2000",
            (p["p"],),
        ):
            a, b = str(r["sl"]).lower(), str(r["ol"]).lower()
            if a in vocab and b in vocab and a != b:
                pairs.append((vocab[a], vocab[b]))
        if len(pairs) >= 10 and (best is None or len(pairs) > len(best[1])):
            best = (int(p["p"]), pairs[:max_checks])
    if best is not None:
        offs = np.vstack([unit[b] - unit[a] for a, b in best[1]])
        offs /= np.maximum(np.linalg.norm(offs, axis=1, keepdims=True), 1e-9)
        sims = offs @ offs.T
        iu = np.triu_indices(len(offs), 1)
        rnd = rng.integers(0, len(model.vocab), size=(len(best[1]), 2))
        roffs = np.vstack([unit[b] - unit[a] for a, b in rnd])
        roffs /= np.maximum(np.linalg.norm(roffs, axis=1, keepdims=True), 1e-9)
        rs = roffs @ roffs.T
        label = db.scalar("SELECT label FROM predicates WHERE id=?", (best[0],))
        out["offset_consistency"] = {
            "predicate": label,
            "pairs": len(best[1]),
            "mean_cosine": round(float(sims[iu].mean()), 4),
            "random_mean_cosine": round(float(rs[iu].mean()), 4),
        }
    return out


def _entity_vector(eidx: IVFIndex, entity_id: int) -> Any:
    """Recover an entity's stored vector from the index (exact id match in tail or lists)."""
    g = eidx._load()
    if g is not None and g["meta"]["n"]:
        pos = np.nonzero(np.asarray(g["ids"]) == entity_id)[0]
        if pos.size:
            i = int(pos[0])
            return np.asarray(g["codes"][i], dtype=np.float32) * float(g["scales"][i])
    tc, ts, ti = eidx._tail()
    pos = np.nonzero(ti == entity_id)[0]
    if pos.size:
        i = int(pos[-1])
        return tc[i].astype(np.float32) * ts[i]
    return None


def _auc(pos: list[float], neg: list[float]) -> float:
    allv = np.array(pos + neg)
    ranks = allv.argsort().argsort() + 1
    rp = ranks[: len(pos)].sum()
    return float((rp - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def quality_job(ctx: JobContext) -> JobOutcome:
    loaded = load_model(ctx)
    if loaded is None:
        return JobOutcome(done=True, result={"skipped": "no embedding model yet"})
    report = quality_report(ctx.db, loaded[0], entity_index(ctx.config))
    report["at"] = time.time()
    history = ctx.db.kv_get("embedding_quality_history", [])[-59:]
    history.append(report)
    ctx.db.kv_set("embedding_quality", report)
    ctx.db.kv_set("embedding_quality_history", history)
    return JobOutcome(done=True, value=0.2, result=report)


def planner(agent: Any) -> None:
    s = agent.scheduler
    db: Database = agent.db
    if int(db.kv_get("ngram_tokens", 0)) >= 50_000:
        s.ensure_recurring("embed.train", 60, priority=1.5)
    if db.scalar("SELECT 1 FROM documents WHERE stage=2 AND id > ? LIMIT 1", (int(db.kv_get("embed_doc_cursor", 0)),)):
        s.ensure_recurring("embed.docs", 120, priority=1.4)
    s.ensure_recurring("embed.rebuild", 1800, priority=1.0)
    s.ensure_recurring("embed.quality", 6 * 3600, priority=0.5)
