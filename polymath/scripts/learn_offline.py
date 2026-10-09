#!/usr/bin/env python3
"""Run the agent's *learning* pipeline (no network) until it has nothing left to do.

Uses the real agent loop with only the memory/perception/learning planners, so
it exercises exactly what runs on the Pi — minus fetching new data. Useful for
acceptance runs on a copy of a real database.

Usage: python scripts/learn_offline.py --config polymath.toml [--minutes 60] [--full]

``--full`` adds reasoning, curiosity (PageRank, priorities) and self-evaluation
(hold-out, quiz, report) — everything the agent does that needs no network.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polymath.core.app import build_components, check_storage
from polymath.core.config import load_config
from polymath.core.db import open_database
from polymath.core.logging import setup_logging
from polymath.core.loop import Agent
from polymath.drive import jobs as djobs
from polymath.drive.bandit import BanditPolicy
from polymath.evaluation import jobs as vjobs
from polymath.memory import jobs as memjobs
from polymath.perception import embed_jobs as ejobs
from polymath.perception import jobs as pjobs
from polymath.reasoning import jobs as rjobs

LEARNING_PLANNERS = [(5.0, memjobs.planner), (5.0, pjobs.planner), (5.0, ejobs.planner)]
FULL_PLANNERS = [*LEARNING_PLANNERS, (5.0, rjobs.planner), (5.0, djobs.planner), (5.0, vjobs.planner)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", required=True)
    ap.add_argument("--minutes", type=float, default=60.0)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--full", action="store_true", help="also reason, rank topics and quiz itself")
    ap.add_argument("--summary", default="", help="also write the summary JSON to this file")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    setup_logging("WARNING" if args.quiet else cfg.log_level)
    check_storage(cfg)
    db = open_database(cfg.paths.db_path)
    comps = build_components(cfg, db, planners=False)
    planners = FULL_PLANNERS if args.full else LEARNING_PLANNERS
    agent = Agent(cfg, db, comps.registry, services=comps.services, planners=list(planners), policy=BanditPolicy(db))
    agent.install_signal_handlers()
    agent.start()
    db.execute(
        "DELETE FROM jobs WHERE state='queued' AND kind IN ('sources.plan','crawl.step','feeds.poll',"
        "'dump.download','gutenberg.books')"
    )
    deadline = time.time() + args.minutes * 60
    ran: Counter[str] = Counter()
    seconds: dict[str, float] = {}
    idle = 0
    last_print: float = time.time()
    while time.time() < deadline and not agent.stop_event.is_set():
        t0 = time.monotonic()
        rec = agent.cycle()
        if rec.status in {"idle", "paused"}:
            idle += 1
            if idle >= 3:
                break
            agent._planner_due = [0.0] * len(agent.planners)  # replan immediately
            continue
        idle = 0
        ran[rec.kind or "?"] += 1
        seconds[rec.kind or "?"] = seconds.get(rec.kind or "?", 0.0) + time.monotonic() - t0
        if time.time() - last_print > 30:
            print(json.dumps({"cycles": dict(ran)}), file=sys.stderr)
            last_print = time.time()
    agent.shutdown()
    summary = {
        "cycles": dict(ran),
        "seconds": {k: round(v, 1) for k, v in seconds.items()},
        "documents": {
            r["k"]: r["n"]
            for r in db.query("SELECT state || '/' || stage AS k, COUNT(*) AS n FROM documents GROUP BY k")
        },
        "graph": {
            "entities": db.scalar("SELECT COUNT(*) FROM entities"),
            "aliases": db.scalar("SELECT COUNT(*) FROM aliases"),
            "triples": {
                r["status"]: r["n"] for r in db.query("SELECT status, COUNT(*) AS n FROM triples GROUP BY status")
            },
            "pattern_triples": db.scalar("SELECT COUNT(DISTINCT triple_id) FROM provenance WHERE kind='pattern'"),
            "infobox_triples": db.scalar("SELECT COUNT(DISTINCT triple_id) FROM provenance WHERE kind='infobox'"),
        },
        "linker_model": db.kv_get("linker_model"),
        "phrases": db.scalar("SELECT COUNT(*) FROM phrases"),
        "topics": db.scalar("SELECT COUNT(*) FROM topics"),
        "rules": [
            dict(r)
            for r in db.query(
                "SELECT r.kind, p.label AS p, r.q, r.confidence, r.support FROM rules r JOIN predicates p ON p.id=r.p "
                "ORDER BY r.support DESC LIMIT 15"
            )
        ],
        "quizzes": [dict(r) for r in db.query("SELECT id, n, correct, accuracy, chance, details FROM quizzes")],
        "top_topics": [
            dict(r)
            for r in db.query(
                "SELECT t.name, p.priority, p.gap, p.importance FROM topic_priority p JOIN topics t ON t.id=p.topic_id "
                "ORDER BY p.priority DESC LIMIT 10"
            )
        ],
        "bandit": [
            dict(r) for r in db.query("SELECT context, action, n, mean FROM bandit_arms ORDER BY n DESC LIMIT 20")
        ],
        "dead_jobs": [dict(r) for r in db.query("SELECT kind, last_error FROM jobs WHERE state='dead' LIMIT 10")],
    }
    text = json.dumps(summary, indent=2, default=str)
    print(text)
    if args.summary:
        Path(args.summary).write_text(text)
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
