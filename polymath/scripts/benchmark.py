#!/usr/bin/env python3
"""Polymath benchmark: measures each subsystem on this machine and prints JSON.

Usage: python scripts/benchmark.py [--only core,memory,...] [--scale small|full] [--out FILE]

Every number is measured here, now; nothing is estimated. ``--scale full``
uses the acceptance sizes from the build spec (10k docs, 1M vectors); the
default ``small`` scale is for quick checks.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polymath.body.systemd_notify import Notifier
from polymath.core.app import build_components
from polymath.core.config import load_config
from polymath.core.db import open_database
from polymath.core.loop import Agent
from polymath.core.scheduler import Scheduler

Result = dict[str, Any]


def _config(tmp: Path) -> Any:
    f = tmp / "bench.toml"
    f.write_text(
        f'[paths]\ndata_dir = "{tmp / "data"}"\nrequire_separate_mount = false\n'
        "[loop]\nidle_sleep = 0.01\n"
        "[senses]\ndefault_feeds = false\ndefault_seeds = false\nblocklists = []\n"
    )
    return load_config(f, env={})


def bench_core(tmp: Path, scale: str) -> Result:
    cfg = _config(tmp)
    db = open_database(cfg.paths.db_path)
    s = Scheduler(db)
    n = 2000 if scale == "small" else 20000
    t0 = time.perf_counter()
    with db.transaction():
        for i in range(n):
            s.enqueue("noop", {"i": i})
    enqueue_rate = n / (time.perf_counter() - t0)
    comps = build_components(cfg, db)
    agent = Agent(cfg, db, comps.registry, notifier=Notifier({}))
    cycles = 500 if scale == "small" else 5000
    t0 = time.perf_counter()
    ran = agent.run(max_cycles=cycles)
    dt = time.perf_counter() - t0
    out = {
        "enqueue_per_s": round(enqueue_rate),
        "noop_cycles": ran,
        "noop_cycles_per_s": round(ran / dt, 1),
        "cycle_overhead_ms": round(1000 * dt / ran, 3),
    }
    db.close()
    return out


def _synthetic_wikitext(i: int) -> str:
    para = (
        f"'''Topic {i}''' is a [[subject]] studied in [[Physics|physics]] and [[chemistry]] since {{{{circa|1900}}}}. "
        "It was described by [[Marie Curie]] in a paper<ref>{{cite journal|title=x}}</ref> that measured "
        "{{convert|12|km}} of samples. "
    )
    body = "\n\n".join(para * 3 for _ in range(12))
    return (
        "{{Infobox thing|name=Topic|field=[[Physics]]}}\n"
        + body
        + "\n== History ==\n"
        + body
        + "\n{| class=wikitable\n| a || b\n|}\n[[Category:Things]]"
    )


def bench_senses(tmp: Path, scale: str) -> Result:
    import bz2
    import os

    from polymath.memory.documents import Document, DocumentStore
    from polymath.senses.html_text import extract
    from polymath.senses.sevenzip import SevenZipFile
    from polymath.senses.streams import Bz2BlockReader, file_fetcher
    from polymath.senses.wikitext import clean_wikitext
    from tests.fixtures.builders import sevenzip

    n = 200 if scale == "small" else 2000
    pages = [_synthetic_wikitext(i) for i in range(n)]
    raw_bytes = sum(len(p) for p in pages)
    t0 = time.perf_counter()
    cleaned = [clean_wikitext(p) for p in pages]
    dt = time.perf_counter() - t0
    out: Result = {"wikitext_pages_per_s": round(n / dt, 1), "wikitext_mb_per_s": round(raw_bytes / dt / 1e6, 2)}
    article = "<p>" + "Words about a topic that matter to the reader of this article. " * 12 + "</p>"
    html = (
        "<html><head><title>T</title></head><body><nav><a href='/'>Home</a></nav><article>"
        + article * 3
        + "</article><footer>c</footer></body></html>"
    )
    t0 = time.perf_counter()
    for _ in range(n):
        extract(html, "https://example.org/")
    out["html_pages_per_s"] = round(n / (time.perf_counter() - t0), 1)
    blob = ("\n".join(c.text for c in cleaned) * 3).encode()
    comp = bz2.compress(blob, 9)
    path = tmp / "x.bz2"
    path.write_bytes(comp)
    t0 = time.perf_counter()
    total = sum(len(b.data) for b in Bz2BlockReader(file_fetcher(path), len(comp)).blocks(32))
    out["bz2_block_decode_mb_per_s"] = round(total / (time.perf_counter() - t0) / 1e6, 2)
    t0 = time.perf_counter()
    bz2.decompress(comp)
    out["bz2_stdlib_decode_mb_per_s"] = round(len(blob) / (time.perf_counter() - t0) / 1e6, 2)
    z = tmp / "x.7z"
    z.write_bytes(sevenzip({"Posts.xml": blob + os.urandom(1000)}))
    t0 = time.perf_counter()
    size = sum(len(c) for c in SevenZipFile(z).open("Posts.xml"))
    out["sevenzip_decode_mb_per_s"] = round(size / (time.perf_counter() - t0) / 1e6, 2)
    cfg = _config(tmp)
    db = open_database(cfg.paths.db_path)
    store = DocumentStore(db)
    t0 = time.perf_counter()
    with db.transaction():
        for i, c in enumerate(cleaned):
            store.add(Document("wikipedia", str(i), f"T{i}", c.text + str(i), "CC BY-SA 4.0", links=c.links))
    out["store_docs_per_s"] = round(n / (time.perf_counter() - t0), 1)
    db.close()
    return out


def _zipf_corpus(n_docs: int, seed: int = 7) -> list[str]:
    """Synthetic prose with a Zipfian vocabulary (realistic posting-list skew for FTS)."""
    import numpy as np

    rng = np.random.default_rng(seed)
    syll = ["ka", "lo", "mi", "ra", "te", "su", "no", "vi", "pe", "do", "an", "el", "or", "us", "in", "at"]
    vocab = sorted({"".join(rng.choice(syll, size=int(rng.integers(1, 5)))) for _ in range(40000)})
    ranks = np.arange(1, len(vocab) + 1)
    p = 1.0 / ranks**1.07
    p /= p.sum()
    docs = []
    for _ in range(n_docs):
        words = rng.choice(len(vocab), size=int(rng.integers(150, 900)), p=p)
        sents = [" ".join(vocab[w] for w in words[i : i + 18]) + "." for i in range(0, len(words), 18)]
        docs.append("\n\n".join(" ".join(sents[i : i + 4]) for i in range(0, len(sents), 4)))
    return docs


def _percentiles(samples: list[float]) -> dict[str, float]:
    s = sorted(samples)
    return {
        "p50_ms": round(1000 * s[len(s) // 2], 2),
        "p95_ms": round(1000 * s[int(len(s) * 0.95)], 2),
        "max_ms": round(1000 * s[-1], 2),
    }


def bench_memory(tmp: Path, scale: str) -> Result:
    import random

    import numpy as np

    from polymath.memory.dedup import NearDupIndex, minhash
    from polymath.memory.documents import Document, DocumentStore
    from polymath.memory.graph import KnowledgeGraph
    from polymath.memory.text_index import TextIndex

    n_docs = 2000 if scale == "small" else 10000
    cfg = _config(tmp)
    db = open_database(cfg.paths.db_path)
    store, ti, nd = DocumentStore(db), TextIndex(db), NearDupIndex(db)
    corpus = _zipf_corpus(n_docs)
    t0 = time.perf_counter()
    with db.transaction():
        for i, text in enumerate(corpus):
            did, _ = store.add(Document("web", str(i), f"Doc {i}", text, "test"))
            sig = minhash(text)
            nd.find(sig)
            nd.add(did, sig)
            ti.index(did, f"Doc {i}", text)
    index_rate = n_docs / (time.perf_counter() - t0)
    rng = random.Random(1)
    words = [w for d in corpus[:200] for w in d.split()[:60]]
    lat = []
    for _ in range(200):
        q = " ".join(rng.choice(words).strip(".") for _ in range(3))
        t0 = time.perf_counter()
        ti.search(q, limit=10)
        lat.append(time.perf_counter() - t0)
    out: Result = {
        "docs": n_docs,
        "chunks": db.scalar("SELECT COUNT(*) FROM chunks"),
        "index_docs_per_s": round(index_rate, 1),
        "fts_top10": _percentiles(lat),
    }
    # Graph: power-law degree distribution.
    n_ent = 20000 if scale == "small" else 200000
    n_tri = 5 * n_ent
    g = KnowledgeGraph(db)
    now = time.time()
    t0 = time.perf_counter()
    with db.transaction():
        db.executemany(
            "INSERT INTO entities(key, label, kind, updated) VALUES(?,?,'item',?)",
            [(f"Q{i}", f"Entity {i}", now) for i in range(n_ent)],
        )
        preds = [g.predicate(f"P{i}", f"pred {i}") for i in range(50)]
        nprng = np.random.default_rng(3)
        subj = (nprng.pareto(1.2, n_tri) * 10).astype(np.int64) % n_ent + 1
        obj = nprng.integers(1, n_ent + 1, n_tri)
        prd = nprng.choice(preds, n_tri)
        db.executemany(
            "INSERT OR IGNORE INTO triples(s, p, o, value, status, confidence, n_sources, created, updated) "
            "VALUES(?,?,?,'','sourced',0.8,1,?,?)",
            [(int(s), int(p), int(o), now, now) for s, p, o in zip(subj, prd, obj)],
        )
    out["graph_build_s"] = round(time.perf_counter() - t0, 2)
    out["entities"] = n_ent
    out["triples"] = db.scalar("SELECT COUNT(*) FROM triples")
    lat = []
    hubs = [int(x) for x in subj[:100]]  # includes the highest-degree nodes
    for e in hubs + [rng.randint(1, n_ent) for _ in range(100)]:
        t0 = time.perf_counter()
        g.neighbors(e, limit=50)
        lat.append(time.perf_counter() - t0)
    out["graph_neighbors_50"] = _percentiles(lat)
    out["db_mb"] = round(cfg.paths.db_path.stat().st_size / 1e6, 1)
    db.close()
    return out


def bench_perception(tmp: Path, scale: str) -> Result:
    import random

    from polymath.perception.ahocorasick import TokenAutomaton
    from polymath.perception.sentences import SentenceSplitter, train_model
    from polymath.perception.stem import stem
    from polymath.perception.tokenize import words

    corpus = _zipf_corpus(400 if scale == "small" else 2000)
    text = " ".join(corpus)
    t0 = time.perf_counter()
    toks = [w for d in corpus for w in words(d)]
    tok_rate = len(text) / (time.perf_counter() - t0)
    uniq = sorted(set(toks))
    t0 = time.perf_counter()
    for w in uniq:
        stem(w)
    stem_rate = len(uniq) / (time.perf_counter() - t0)
    splitter = SentenceSplitter(train_model(corpus[:200]))
    t0 = time.perf_counter()
    n_sent = sum(len(splitter.split(d)) for d in corpus)
    split_rate = len(text) / (time.perf_counter() - t0)
    rng = random.Random(2)
    n_pat = 20_000 if scale == "small" else 200_000
    auto = TokenAutomaton()
    for _ in range(n_pat):
        auto.add([rng.choice(uniq) for _ in range(rng.randint(1, 3))])
    t0 = time.perf_counter()
    auto.build()
    build_s = time.perf_counter() - t0
    stream = toks[:200_000]
    t0 = time.perf_counter()
    hits = sum(1 for _ in auto.finditer(stream))
    scan_rate = len(stream) / (time.perf_counter() - t0)
    return {
        "tokenize_chars_per_s": round(tok_rate),
        "porter_words_per_s": round(stem_rate),
        "sentences": n_sent,
        "sentence_split_chars_per_s": round(split_rate),
        "automaton_patterns": n_pat,
        "automaton_build_s": round(build_s, 2),
        "automaton_tokens_per_s": round(scan_rate),
        "automaton_hits": hits,
    }


def bench_embeddings(tmp: Path, scale: str) -> Result:
    import numpy as np

    from polymath.memory.vector_index import IVFIndex
    from polymath.perception.embeddings import SGNS, Vocab
    from polymath.perception.tokenize import words

    corpus = _zipf_corpus(500 if scale == "small" else 3000)
    sents = [[w.lower() for w in words(d)] for d in corpus]
    counts: dict[str, int] = {}
    for s_ in sents:
        for w in s_:
            counts[w] = counts.get(w, 0) + 1
    vocab_words = sorted(counts, key=lambda w: -counts[w])
    model = SGNS(Vocab(vocab_words, np.array([counts[w] for w in vocab_words])), dim=128, seed=1)
    c, o = model.pairs([model.encode(s_) for s_ in sents], 5)
    t0 = time.perf_counter()
    model.train_pairs(c, o, lr=0.025, batch=4096)
    sgns_rate = c.size / (time.perf_counter() - t0)
    n = 100_000 if scale == "small" else 1_000_000
    dim = 128
    rng = np.random.default_rng(4)
    centres = rng.normal(size=(1000, dim)).astype(np.float32)
    labels = rng.integers(0, 1000, n)
    x = centres[labels] + 0.35 * rng.normal(size=(n, dim)).astype(np.float32)
    x /= np.linalg.norm(x, axis=1, keepdims=True)
    idx = IVFIndex(tmp / "ivf", dim)
    t0 = time.perf_counter()
    for a in range(0, n, 100_000):
        idx.add(np.arange(a, min(n, a + 100_000), dtype=np.int64), x[a : a + 100_000])
    info = idx.rebuild()
    build_s = time.perf_counter() - t0
    queries = rng.integers(0, n, 200)
    lat, recall = [], []
    for qi in queries:
        q = x[qi] + 0.05 * rng.normal(size=dim).astype(np.float32)
        t0 = time.perf_counter()
        got = idx.search(q, 10, nprobe=32)
        lat.append(time.perf_counter() - t0)
        qn = q / np.linalg.norm(q)
        exact = set(np.argpartition(-(x @ qn), 10)[:10].tolist())
        recall.append(len(exact & {i for i, _s in got}) / 10)
    return {
        "sgns_pairs": int(c.size),
        "sgns_pairs_per_s": round(sgns_rate),
        "ivf_vectors": n,
        "ivf_lists": info.get("nlist"),
        "ivf_build_s": round(build_s, 1),
        "ivf_top10": _percentiles(lat),
        "ivf_recall_at_10": round(float(np.mean(recall)), 3),
    }


def bench_reasoning(tmp: Path, scale: str) -> Result:
    import random

    from polymath.memory.graph import KnowledgeGraph
    from polymath.reasoning.contradictions import functional_predicates_all
    from polymath.reasoning.inference import forward_chain, learn_rules
    from polymath.reasoning.reliability import truth_discovery

    cfg = _config(tmp)
    db = open_database(cfg.paths.db_path)
    g = KnowledgeGraph(db)
    n = 5000 if scale == "small" else 50000
    rng = random.Random(5)
    with db.transaction():
        part = g.predicate("P361", "part of")
        p31 = g.predicate("P31", "instance of")
        trans = g.upsert_entity("Q18647515", "transitive property")
        prop = g.upsert_entity("P361", "part of", kind="property")
        g.add_triple(prop, p31, o=trans, kind="wikidata", source="wikidata")
        ents = [g.upsert_entity(f"Q{100 + i}", f"Place {i}") for i in range(n)]
        for i in range(1, n):  # a forest of part-of chains (depth ~ log n)
            g.add_triple(ents[i], part, o=ents[rng.randrange(max(0, i - 50), i)], kind="wikidata", source="wikidata")
    t0 = time.perf_counter()
    with db.transaction():
        rules = learn_rules(db)
    rules_s = time.perf_counter() - t0
    t0 = time.perf_counter()
    with db.transaction():
        fc = forward_chain(db, max_new=20 * n)
    fc_s = time.perf_counter() - t0
    t0 = time.perf_counter()
    with db.transaction():
        td = truth_discovery(db, functional=functional_predicates_all(db))
    td_s = time.perf_counter() - t0
    out = {
        "facts": n - 1,
        "rules": rules,
        "learn_rules_s": round(rules_s, 2),
        "inferred": fc.get("new"),
        "forward_chain_s": round(fc_s, 2),
        "inferred_per_s": round(fc.get("new", 0) / max(fc_s, 1e-9)),
        "truth_discovery_s": round(td_s, 2),
        "truth_discovery": {k: v for k, v in td.items() if isinstance(v, int | float)} if isinstance(td, dict) else td,
    }
    db.close()
    return out


def bench_drive(tmp: Path, scale: str) -> Result:
    import random

    import numpy as np

    from polymath.core.jobs import JobRegistry, noop_handler
    from polymath.core.loop import BodyState, Observation
    from polymath.drive.bandit import BanditPolicy
    from polymath.drive.pagerank import sparse_pagerank

    n = 200_000 if scale == "small" else 2_000_000
    rng = np.random.default_rng(6)
    src = (rng.pareto(1.1, 5 * n) * 3).astype(np.int64) % n
    dst = rng.integers(0, n, 5 * n)
    t0 = time.perf_counter()
    pr = sparse_pagerank(src, dst, n)
    pr_s = time.perf_counter() - t0
    cfg = _config(tmp)
    db = open_database(cfg.paths.db_path)
    reg = JobRegistry()
    for i, action in enumerate(["read", "learn", "reason", "crawl", "plan", "evaluate"]):
        reg.register(f"k{i}", noop_handler, "", action=action)
    s = Scheduler(db)
    for i in range(6):
        s.enqueue(f"k{i}", {}, key=f"k{i}")
    pol = BanditPolicy(db, rng=random.Random(1))
    lat = []
    for _ in range(300):
        obs = Observation(time.time(), BodyState(), s.stats(), 1)
        t0 = time.perf_counter()
        pol.decide(obs, reg)
        lat.append(time.perf_counter() - t0)
    db.close()
    return {
        "pagerank_nodes": n,
        "pagerank_edges": int(src.size),
        "pagerank_s": round(pr_s, 2),
        "pagerank_sum": round(float(pr.sum()), 6),
        "bandit_decide": _percentiles(lat),
    }


def bench_evaluation(tmp: Path, scale: str) -> Result:
    from polymath.evaluation.quiz import mark_holdout, run_quiz
    from polymath.reasoning.link_prediction import LinkPredictor, calibrate
    from tests.fixtures.kb import build

    cfg = _config(tmp)
    db = open_database(cfg.paths.db_path)
    with db.transaction():
        build(db, n=60 if scale == "small" else 200)
        mark_holdout(db, 0.3)
    pred = LinkPredictor(db)
    t0 = time.perf_counter()
    with db.transaction():
        fit = calibrate(db, pred, size=40, seed=1)
    cal_s = time.perf_counter() - t0
    t0 = time.perf_counter()
    with db.transaction():
        res = run_quiz(db, pred, size=40, seed=2)
    quiz_s = time.perf_counter() - t0
    db.close()
    return {
        "calibration_questions": fit["n"],
        "calibration_s_per_question": round(cal_s / max(1, fit["n"]), 4),
        "quiz_questions": res["n"],
        "quiz_s_per_question": round(quiz_s / max(1, res["n"]), 4),
        "fixture_quiz_accuracy": res.get("accuracy"),
    }


def bench_dashboard(tmp: Path, scale: str) -> Result:
    import http.client
    import threading

    from polymath.interface.dashboard import make_server
    from tests.fixtures.kb import build

    cfg = _config(tmp)
    db = open_database(cfg.paths.db_path)
    with db.transaction():
        build(db, n=200)
        db.kv_set("heartbeat", {"ts": time.time() + 3600, "state": "running", "cycle": 1})
    db.close()
    server = make_server(cfg, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    out: Result = {}
    try:
        for path in ("/", "/health", "/api/overview", "/api/timeseries", "/api/topics", "/api/knowledge?q=Country07"):
            lat = []
            for _ in range(30):
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                t0 = time.perf_counter()
                c.request("GET", path)
                c.getresponse().read()
                lat.append(time.perf_counter() - t0)
                c.close()
            out[path] = _percentiles(lat)
        lat = []
        for i in range(15):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            body = json.dumps({"question": f"What is the capital of Country{i:02d}?"})
            t0 = time.perf_counter()
            c.request("POST", "/api/ask", body, {"Content-Type": "application/json"})
            c.getresponse().read()
            lat.append(time.perf_counter() - t0)
            c.close()
        out["POST /api/ask"] = _percentiles(lat)
    finally:
        server.shutdown()
        server.server_close()
    return out


def bench_body(tmp: Path, scale: str) -> Result:
    import os
    import threading

    from polymath.body.guard import Guard
    from polymath.body.maintenance import backup_job
    from polymath.core.jobs import JobContext
    from polymath.core.scheduler import Job

    cfg = _config(tmp)
    db = open_database(cfg.paths.db_path)
    g = Guard(cfg, db)
    lat = []
    for _ in range(200):
        t0 = time.perf_counter()
        g.observe()
        lat.append(time.perf_counter() - t0)
    mb = 50 if scale == "small" else 500
    with db.transaction():
        db.execute("CREATE TABLE filler(x BLOB)")
        db.executemany("INSERT INTO filler VALUES(?)", [(os.urandom(512) * 8,) for _ in range(mb * 256)])
    s = Scheduler(db)
    jid, _ = s.enqueue("body.backup", {}, key="b")
    ctx = JobContext(
        cfg, db, s, Job(jid, "body.backup", "b", {}, None, 0, 0, 0, 5, 0), time.monotonic() + 600, threading.Event(), {}
    )
    size = cfg.paths.db_path.stat().st_size
    t0 = time.perf_counter()
    with db.transaction():
        res = backup_job(ctx).result
    dt = time.perf_counter() - t0
    db.close()
    return {
        "guard_observe": _percentiles(lat),
        "backup_db_mb": round(size / 1e6, 1),
        "backup_s": round(dt, 2),
        "backup_mb_per_s": round(size / 1e6 / dt, 1),
        "backup_ratio": round(res["bytes"] / size, 3) if res.get("ok") else None,
    }


def bench_feed(tmp: Path, scale: str) -> Result:
    """The live feed: one poll when nothing is new, one after a burst of new facts, and the cached counts."""
    import numpy as np

    from polymath.interface.feed import Feed
    from polymath.memory.documents import Document, DocumentStore
    from polymath.memory.graph import KnowledgeGraph

    n_ent = 20_000 if scale == "small" else 200_000
    n_tri = 5 * n_ent
    cfg = _config(tmp)
    db = open_database(cfg.paths.db_path)
    g = KnowledgeGraph(db)
    store = DocumentStore(db)
    now = time.time()
    nprng = np.random.default_rng(5)

    def add_facts(n: int, base: int) -> None:
        subj = nprng.integers(1, n_ent + 1, n)
        obj = nprng.integers(1, n_ent + 1, n)
        prd = nprng.choice(preds, n)
        rows = [(int(s), int(p), int(o), f'"{base + i}"', now, now) for i, (s, p, o) in enumerate(zip(subj, prd, obj))]
        db.executemany(
            "INSERT OR IGNORE INTO triples(s, p, o, value, status, confidence, n_sources, created, updated) "
            "VALUES(?,?,?,?,'sourced',0.8,1,?,?)",
            rows,
        )
        db.executemany(
            "INSERT INTO provenance(triple_id, kind, source, detail, created) "
            "SELECT id, 'wikidata', 'wikidata', '', ? FROM triples WHERE value = ?",
            [(now, r[3]) for r in rows[:: max(1, n // 50)]],
        )

    with db.transaction():
        db.executemany(
            "INSERT INTO entities(key, label, kind, updated) VALUES(?,?,'item',?)",
            [(f"Q{i}", f"Entity {i}", now) for i in range(n_ent)],
        )
        preds = [g.predicate(f"P{i}", f"pred {i}") for i in range(50)]
        add_facts(n_tri, 0)
        for i in range(2000):
            store.add(Document("web", str(i), f"Doc {i}", f"text of document {i} " * 20, "test"))
    feed = Feed(db)
    t0 = time.perf_counter()
    cursor = feed.poll(None)["cursor"]
    cold = time.perf_counter() - t0  # first poll: full-table counts included
    idle = []
    for _ in range(20):
        t0 = time.perf_counter()
        cursor = feed.poll(cursor)["cursor"]
        idle.append(time.perf_counter() - t0)
    busy = []
    for k in range(5):
        with db.transaction():
            add_facts(5000, n_tri + 10_000 * (k + 1))
            for i in range(50):
                store.add(Document("web", f"new{k}-{i}", f"New {k}-{i}", f"fresh text {k} {i} " * 20, "test"))
        t0 = time.perf_counter()
        res = feed.poll(cursor)
        busy.append(time.perf_counter() - t0)
        cursor = res["cursor"]
    db.close()
    return {
        "triples": n_tri,
        "first_poll_with_full_counts_s": round(cold, 3),
        "poll_nothing_new": _percentiles(idle),
        "poll_after_5000_new_facts_and_50_docs": _percentiles(busy),
        "events_in_busy_poll": len(res["events"]),
    }


BENCHES: dict[str, Callable[[Path, str], Result]] = {
    "core": bench_core,
    "senses": bench_senses,
    "memory": bench_memory,
    "perception": bench_perception,
    "embeddings": bench_embeddings,
    "reasoning": bench_reasoning,
    "drive": bench_drive,
    "evaluation": bench_evaluation,
    "dashboard": bench_dashboard,
    "body": bench_body,
    "feed": bench_feed,
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", default="", help="comma-separated subset of: " + ",".join(BENCHES))
    ap.add_argument("--scale", choices=["small", "full"], default="small")
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)
    names = [n for n in args.only.split(",") if n] or list(BENCHES)
    report: Result = {
        "host": {"machine": platform.machine(), "python": platform.python_version(), "system": platform.platform()},
        "scale": args.scale,
        "results": {},
    }
    for name in names:
        with tempfile.TemporaryDirectory(prefix=f"pm-bench-{name}-") as tmp:
            t0 = time.perf_counter()
            res = BENCHES[name](Path(tmp), args.scale)
            res["bench_seconds"] = round(time.perf_counter() - t0, 2)
            report["results"][name] = res
            print(f"{name}: {json.dumps(res)}", file=sys.stderr)
    text = json.dumps(report, indent=2)
    if args.out:
        Path(args.out).write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
