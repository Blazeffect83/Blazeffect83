"""Phase 5: SGNS embeddings, SIF document vectors, IVF+int8 index, graph-generated quality checks, jobs."""

from __future__ import annotations

import random
import threading
import time

import numpy as np
import pytest

from polymath.core.jobs import JobContext
from polymath.core.scheduler import Job, Scheduler
from polymath.memory.documents import Document, DocumentStore
from polymath.memory.graph import KnowledgeGraph
from polymath.memory.vector_index import IVFIndex, assign_lists, kmeans, normalize, quantize
from polymath.perception import embed_jobs as ej
from polymath.perception.embeddings import SGNS, SIF, Vocab, sentence_tokens

ANIMALS = ["cat", "dog", "horse", "cow", "sheep", "goat"]
METALS = ["iron", "copper", "gold", "silver", "zinc", "tin"]


def toy_corpus(n: int = 3000, seed: int = 0) -> list[list[str]]:
    """Two topical clusters: animals co-occur with farm words, metals with mining words."""
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        if rng.random() < 0.5:
            out.append([rng.choice(ANIMALS), "eats", "grass", "on", "the", "farm", rng.choice(ANIMALS)])
        else:
            out.append([rng.choice(METALS), "is", "mined", "from", "the", "ore", rng.choice(METALS)])
    return out


def train_toy(epochs: int = 6) -> SGNS:
    sents = toy_corpus()
    counts: dict[str, int] = {}
    for s in sents:
        for w in s:
            counts[w] = counts.get(w, 0) + 1
    ws = sorted(counts, key=lambda w: -counts[w])
    model = SGNS(Vocab(ws, np.array([counts[w] for w in ws])), dim=24, seed=3)
    model.keep[:] = 1.0  # tiny corpus: no subsampling
    for e in range(epochs):
        c, o = model.pairs([model.encode(s) for s in sents], 3)
        model.train_pairs(c, o, lr=0.05 * (1 - e / epochs) + 0.005, batch=512)
    return model


def test_sgns_learns_topical_similarity_and_persists(tmp_path):
    model = train_toy()
    unit = model.normalized()
    sim = lambda a, b: float(unit[model.vocab.index[a]] @ unit[model.vocab.index[b]])  # noqa: E731
    within = np.mean([sim(a, b) for a in ANIMALS for b in ANIMALS if a != b])
    across = np.mean([sim(a, b) for a in ANIMALS for b in METALS])
    assert within > across + 0.3
    assert {w for w, _ in model.most_similar("cat", 4)} <= set(ANIMALS) | {"eats", "grass", "farm"}
    assert model.most_similar("unknownword") == [] and model.vector("nope") is None
    model.save(tmp_path, {"cursor": 7})
    loaded = SGNS.load(tmp_path)
    assert loaded is not None and loaded[1]["cursor"] == 7 and np.allclose(loaded[0].w_in, model.w_in)
    assert SGNS.load(tmp_path / "missing") is None
    bigger = Vocab([*model.vocab.words, "brand_new"], np.append(model.vocab.counts, 5))
    grown = model.regrow(bigger)
    assert np.allclose(grown.w_in[grown.vocab.index["cat"]], model.w_in[model.vocab.index["cat"]])
    assert Vocab.from_json(model.vocab.to_json()).words == model.vocab.words
    empty_c, _ = model.pairs([np.array([1])], 3)
    assert empty_c.size == 0 and model.train_pairs(empty_c, empty_c) == 0.0


def test_sif_document_vectors():
    model = train_toy()
    sif = SIF(model)
    docs = [[ANIMALS[i % 6], "farm", "grass"] for i in range(20)] + [[METALS[i % 6], "ore", "mined"] for i in range(20)]
    sif.fit_pc([v for d in docs if (v := sif.raw(d)) is not None])
    assert sif.pc is not None and abs(float(np.linalg.norm(sif.pc)) - 1) < 1e-4
    a, b, c = sif.embed(["cat", "farm"]), sif.embed(["dog", "grass"]), sif.embed(["iron", "ore"])
    assert a is not None and b is not None and c is not None
    assert float(a @ b) > float(a @ c)
    assert sif.embed(["zzz"]) is None and SIF(model).fit_pc([]) is None
    assert list(sentence_tokens("The cat sat on the mat. A dog ran.", set())) == [
        ["the", "cat", "sat", "on", "the", "mat"], ["a", "dog", "ran"]]
    assert list(sentence_tokens("new york is big", {"new york"})) == [["new_york", "is", "big"]]


def test_quantize_kmeans_and_assign():
    rng = np.random.default_rng(0)
    x = normalize(rng.normal(size=(500, 16)).astype(np.float32))
    codes, scales = quantize(x)
    back = codes.astype(np.float32) * scales[:, None]
    assert codes.dtype == np.int8 and float(np.abs(back - x).max()) < 0.01
    zero_codes, zero_scales = quantize(np.zeros((2, 4), np.float32))
    assert (zero_codes == 0).all() and np.allclose(zero_scales, 1 / 127)
    centers = normalize(rng.normal(size=(4, 16)).astype(np.float32))
    pts = normalize(np.repeat(centers, 50, axis=0) + 0.05 * rng.normal(size=(200, 16)).astype(np.float32))
    cent = kmeans(pts, 4, iters=10)
    labels = assign_lists(pts, cent)
    assert len(set(labels.tolist())) == 4 and all(len(set(labels[i * 50 : (i + 1) * 50])) == 1 for i in range(4))
    assert kmeans(pts[:3], 10).shape[0] == 3


def test_ivf_index_add_rebuild_search_and_recall(tmp_path):
    rng = np.random.default_rng(1)
    d, n = 32, 6000
    centers = normalize(rng.normal(size=(60, d)).astype(np.float32))
    x = normalize(centers[rng.integers(0, 60, n)] + 0.1 * rng.normal(size=(n, d)).astype(np.float32))
    idx = IVFIndex(tmp_path / "ivf", d)
    assert idx.search(x[0]) == [] and idx.stats()["generation"] == -1
    idx.add(np.arange(n), x)
    assert idx.needs_rebuild() and idx.stats()["tail"] == n
    tail_hits = idx.search(x[5], 3)
    assert tail_hits[0][0] == 5
    info = idx.rebuild()
    assert info["n"] == n and info["nlist"] == int(np.sqrt(n)) and not idx.needs_rebuild()
    recall = []
    for qi in rng.choice(n, 50, replace=False):
        exact = set(np.argsort(-(x @ x[qi]))[:10].tolist())
        got = {i for i, _ in idx.search(x[qi], 10, nprobe=8)}
        recall.append(len(exact & got) / 10)
    assert np.mean(recall) > 0.9
    # an updated vector in the tail overrides the indexed one; rebuild keeps only the newest
    idx.add(np.array([5]), -x[5:6])
    assert idx.search(-x[5], 1)[0][0] == 5
    assert all(i != 5 for i, _ in idx.search(x[5], 3, exclude={5}))
    idx.rebuild()
    assert idx.stats()["indexed"] == n and idx.search(-x[5], 1)[0][0] == 5
    # a torn final append (crash mid-write) is ignored safely
    with open(tmp_path / "ivf" / "tail_ids.i64", "ab") as fh:
        fh.write(np.array([99], np.int64).tobytes())
    assert idx.stats()["tail"] == 0
    idx.add(np.zeros(0, np.int64), np.zeros((0, d), np.float32))
    empty = IVFIndex(tmp_path / "empty", d)
    assert empty.rebuild()["n"] == 0 and empty.search(x[0]) == []


def ctx_for(config, db, kind, services, payload=None):
    s = Scheduler(db)
    key = f"{kind}:{time.time_ns()}"
    jid, _ = s.enqueue(kind, payload or {}, key=key)
    return JobContext(config=config, db=db, scheduler=s, job=Job(jid, kind, key, payload or {}, None, 0, 0, 0, 5, 0),
                      deadline=time.monotonic() + 60, stop_event=threading.Event(), services=services)


def test_embedding_jobs_end_to_end_with_quality_report(config, db):
    config.learning.embedding_dim = 16
    config.learning.embedding_min_count = 1
    store = DocumentStore(db)
    g = KnowledgeGraph(db)
    sents = toy_corpus(1500)
    for i in range(30):
        body = ". ".join(" ".join(s) for s in sents[i * 50 : (i + 1) * 50]) + "."
        did, _ = store.add(Document("web", str(i), f"D{i}", body, "x"))
        db.execute("UPDATE documents SET stage=2, state='indexed' WHERE id=?", (did,))
        e = g.upsert_entity(f"Q{i}", ANIMALS[i % 6] if i % 2 == 0 else METALS[i % 6], doc_id=did)
        g.add_alias(f"alias{i}", e, "alias")
    counts: dict[str, int] = {}
    for s in sents:
        for w in s:
            counts[w] = counts.get(w, 0) + 1
    db.executemany("INSERT INTO ngram_counts(n, gram, count) VALUES(1,?,?)", list(counts.items()))
    assert ej.train_job(ctx_for(config, db, "embed.train", {"docs": store})).result.get("skipped")  # < 50 words
    for k in range(50):
        db.execute("INSERT INTO ngram_counts(n, gram, count) VALUES(1,?,1)", (f"filler{k}",))
    services: dict = {"docs": store}
    assert ej.embed_docs_job(ctx_for(config, db, "embed.docs", services)).result["skipped"]
    for _ in range(ej.MAX_EPOCHS + 1):
        out = ej.train_job(ctx_for(config, db, "embed.train", services))
    assert out.done and (ej.emb_dir(config) / "state.json").exists()
    assert ej.train_job(ctx_for(config, db, "embed.train", services)).result.get("converged")
    services.pop("sgns")  # reload from disk like a restart would
    emb = ej.embed_docs_job(ctx_for(config, db, "embed.docs", services))
    assert emb.done and emb.result["embedded"] == 30 and (ej.emb_dir(config) / "sif_pc.npy").exists()
    reb = ej.rebuild_index_job(ctx_for(config, db, "embed.rebuild", services, {"force": True}))
    assert reb.result["docs"]["n"] == 30 and reb.result["entities"]["n"] == 30
    p = g.predicate("P1", "related")
    for i in range(0, 28, 2):
        g.add_triple(g.by_key(f"Q{i}").id, p, o=g.by_key(f"Q{i + 2}").id, kind="wikidata", source="wikidata")  # type: ignore[union-attr]
    q = ej.quality_job(ctx_for(config, db, "embed.quality", services))
    assert "relation_hit@10" in q.result and q.result["relation_hit@10"]["tested"] > 0
    assert db.kv_get("embedding_quality")["vocab"] > 0 and len(db.kv_get("embedding_quality_history")) == 1
    assert ej._auc([0.9, 0.8], [0.1, 0.2]) == 1.0


def test_embedding_planner(config, db):
    from polymath.body.systemd_notify import Notifier
    from polymath.core.app import build_components
    from polymath.core.loop import Agent

    comps = build_components(config, db)
    assert {"embed.train", "embed.docs", "embed.rebuild", "embed.quality"} <= set(comps.registry.kinds())
    agent = Agent(config, db, comps.registry, services=comps.services, notifier=Notifier({}))
    db.kv_set("ngram_tokens", 60_000)
    ej.planner(agent)
    kinds = {r["kind"] for r in db.query("SELECT kind FROM jobs")}
    assert {"embed.train", "embed.rebuild", "embed.quality"} <= kinds and "embed.docs" not in kinds


@pytest.mark.parametrize("dim", [16, 64])
def test_index_dimension_guard(tmp_path, dim):
    idx = IVFIndex(tmp_path / f"i{dim}", dim)
    rng = np.random.default_rng(dim)
    v = normalize(rng.normal(size=(10, dim)).astype(np.float32))
    idx.add(np.arange(10), v)
    assert idx.search(v[3], 1)[0][0] == 3
