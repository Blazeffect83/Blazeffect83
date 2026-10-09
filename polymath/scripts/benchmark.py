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
        "[loop]\nidle_sleep = 0.01\n[body]\nminecraft_check = false\n"
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


BENCHES: dict[str, Callable[[Path, str], Result]] = {
    "core": bench_core,
    "senses": bench_senses,
    "memory": bench_memory,
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
