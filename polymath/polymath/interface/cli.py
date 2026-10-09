"""Command-line interface (argparse)."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from typing import Any

from polymath import __version__
from polymath.core import inbox
from polymath.core.app import StorageError, build_agent, check_storage
from polymath.core.config import Config, ConfigError, load_config
from polymath.core.db import Database, open_database
from polymath.core.logging import setup_logging
from polymath.core.scheduler import Scheduler


def _db(config: Config, *, readonly: bool = False) -> Database:
    if readonly:
        return Database(config.paths.db_path, readonly=True)
    check_storage(config)
    db = open_database(config.paths.db_path)
    return db


def cmd_init(config: Config, args: argparse.Namespace) -> int:
    db = _db(config)
    print(json.dumps({"db": str(config.paths.db_path), "schema_version": db.schema_version()}))
    db.close()
    return 0


def cmd_run(config: Config, args: argparse.Namespace) -> int:
    agent = build_agent(config)
    agent.install_signal_handlers()
    ran = agent.run(max_cycles=args.max_cycles, until_idle=args.until_idle)
    agent.db.close()
    print(json.dumps({"job_cycles": ran, "last_cycle": agent.cycle_no}))
    return 0


def status_dict(db: Database) -> dict[str, Any]:
    sched = Scheduler(db)
    st = sched.stats()
    hb = db.kv_get("heartbeat") or {}
    return {
        "version": __version__,
        "heartbeat_age_s": round(time.time() - float(hb.get("ts", 0)), 1) if hb else None,
        "agent_state": hb.get("state"),
        "cycle": hb.get("cycle"),
        "jobs": {"ready": st.ready, "queued": st.queued, "running": st.running, "done": st.done, "dead": st.dead},
        "ready_by_kind": st.ready_by_kind,
    }


def cmd_status(config: Config, args: argparse.Namespace) -> int:
    db = _db(config, readonly=True)
    print(json.dumps(status_dict(db), indent=2))
    db.close()
    return 0


def agent_running(config: Config) -> bool:
    """True when a live agent owns the database (fresh heartbeat, not stopped)."""
    try:
        db = Database(config.paths.db_path, readonly=True)
    except (FileNotFoundError, OSError):
        return False
    try:
        hb = db.kv_get("heartbeat") or {}
    finally:
        db.close()
    return (
        bool(hb) and hb.get("state") != "stopped" and time.time() - float(hb.get("ts", 0)) < config.loop.heartbeat_stale
    )


def submit_job(
    config: Config, kind: str, payload: dict[str, Any], *, key: str | None, priority: float
) -> dict[str, Any]:
    """Hand a job to the agent: via the inbox while it runs (no lock contention), else directly."""
    if agent_running(config):
        path = inbox.submit(
            config.paths.data_dir,
            {"type": "enqueue", "kind": kind, "payload": payload, "key": key, "priority": priority},
        )
        return {"queued_via": "inbox", "file": path.name}
    db = Database(config.paths.db_path, busy_timeout_ms=60_000)
    db.migrate()
    job_id, created = Scheduler(db).enqueue(kind, payload, key=key, priority=priority)
    db.close()
    return {"queued_via": "database", "job_id": job_id, "created": created}


def cmd_enqueue(config: Config, args: argparse.Namespace) -> int:
    check_storage(config)
    payload = json.loads(args.payload) if args.payload else {}
    print(json.dumps(submit_job(config, args.kind, payload, key=args.key, priority=args.priority)))
    return 0


def cmd_sources(config: Config, args: argparse.Namespace) -> int:
    from polymath.senses.sources import SOURCES

    for src in SOURCES:
        print(
            json.dumps(
                {
                    "name": src.name,
                    "license": src.license,
                    "license_url": src.license_url,
                    "homepage": src.homepage,
                    "description": src.description,
                }
            )
        )
    return 0


def sample_report(db: Database, n: int) -> dict[str, Any]:
    rows = db.query(
        "SELECT source, COUNT(*) AS n, SUM(license IS NOT NULL AND license != '') AS licensed, "
        "COUNT(DISTINCT license) AS licenses, SUM(nchars) AS chars FROM documents WHERE state!='duplicate' "
        "GROUP BY source ORDER BY source"
    )
    per = {
        r["source"]: {
            "items": r["n"],
            "with_license": r["licensed"],
            "distinct_licenses": r["licenses"],
            "chars": r["chars"],
        }
        for r in rows
    }
    wd = int(db.scalar("SELECT COUNT(*) FROM wd_entities", default=0))
    per["wikidata"] = {"items": wd, "with_license": wd, "distinct_licenses": 1, "chars": None}
    for name, info in per.items():
        examples = db.query("SELECT title, license FROM documents WHERE source=? LIMIT 3", (name,))
        info["examples"] = [dict(e) for e in examples]
        info["target_met"] = info["items"] >= n
    jobs = {r["state"]: r["n"] for r in db.query("SELECT state, COUNT(*) AS n FROM jobs GROUP BY state")}
    dead = [dict(r) for r in db.query("SELECT kind, key, last_error FROM jobs WHERE state='dead' LIMIT 20")]
    return {"target": n, "sources": per, "jobs": jobs, "dead": dead}


def cmd_sample(config: Config, args: argparse.Namespace) -> int:
    from polymath.core.scheduler import Scheduler
    from polymath.senses.crawler import host_of
    from polymath.senses.sources import SAMPLE_FEEDS, SAMPLE_SEEDS, enqueue_sample

    wanted = [s.strip() for s in args.sources.split(",") if s.strip()]
    if "feed" in wanted and not config.senses.feeds:
        config.senses.feeds = list(SAMPLE_FEEDS)
    if "web" in wanted and not config.senses.seeds:
        config.senses.seeds = list(SAMPLE_SEEDS)
        config.senses.crawl_allow_domains = sorted({host_of(u) for u in SAMPLE_SEEDS})
    agent = build_agent(config, planners=False)
    agent.install_signal_handlers()
    agent.start()
    sched = Scheduler(agent.db)
    tag = args.tag or f"sample{args.n}"
    notes = enqueue_sample(agent.db, agent.services["http"], config, wanted, args.n, tag)
    if "feed" in wanted:
        sched.enqueue("feeds.poll", {"interval": 0}, key=f"{tag}:feeds", priority=5)
    crawler = agent.services["crawler"]
    if "web" in wanted:
        for url in config.senses.seeds:
            crawler.frontier.add(url, priority=1.0)
    deadline = time.time() + args.max_minutes * 60
    crawl_round = 0
    while time.time() < deadline and not agent.stop_event.is_set():
        rec = agent.cycle()
        if rec.status != "idle":
            continue
        web = int(agent.db.scalar("SELECT COUNT(*) FROM documents WHERE source='web'", default=0))
        if "web" in wanted and web < args.n and crawler.frontier.pending():
            nxt_fetch = crawler.frontier.next_time()
            if nxt_fetch is not None and nxt_fetch - time.time() > 0.2:
                agent.stop_event.wait(min(5.0, nxt_fetch - time.time()))  # every host is resting
            crawl_round += 1
            sched.enqueue("crawl.step", {}, key=f"{tag}:crawl:{crawl_round}", priority=1)
            continue
        nxt = sched.next_wakeup()
        if nxt is not None and nxt - time.time() < 900:
            agent.stop_event.wait(max(0.1, min(5.0, nxt - time.time())))
            continue
        break
    agent.shutdown()
    report = sample_report(agent.db, args.n)
    report["resolved"] = notes
    out = config.paths.report_dir / f"sample-{int(time.time())}.json"
    out.write_text(json.dumps(report, indent=2, default=str))
    agent.db.close()
    print(json.dumps(report, indent=2, default=str))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="polymath", description="Polymath autonomous learning agent")
    p.add_argument("--config", help="path to polymath.toml (default /etc/polymath/polymath.toml)")
    p.add_argument("--version", action="version", version=f"polymath {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the data directory and database").set_defaults(func=cmd_init)

    r = sub.add_parser("run", help="run the agent loop (what systemd starts)")
    r.add_argument("--max-cycles", type=int, default=None, help="stop after N job cycles")
    r.add_argument("--until-idle", action="store_true", help="stop when no job is ready")
    r.set_defaults(func=cmd_run)

    sub.add_parser("status", help="agent health and queue summary").set_defaults(func=cmd_status)

    sub.add_parser("sources", help="list data sources and their licenses").set_defaults(func=cmd_sources)

    sm = sub.add_parser("sample", help="collect an N-item sample from each source (acceptance check)")
    sm.add_argument("--sources", default="wikipedia,wikidata,openalex,pubmed,gutenberg,stackexchange,feed,web")
    sm.add_argument("--n", type=int, default=1000)
    sm.add_argument("--max-minutes", type=float, default=180.0)
    sm.add_argument("--tag", default="", help="job-key tag (re-run a sample on the same database)")
    sm.set_defaults(func=cmd_sample)

    e = sub.add_parser("enqueue", help="(advanced) add a raw job")
    e.add_argument("kind")
    e.add_argument("--payload", default="")
    e.add_argument("--key", default=None)
    e.add_argument("--priority", type=float, default=0.0)
    e.set_defaults(func=cmd_enqueue)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    setup_logging(config.log_level)
    try:
        rc: int = args.func(config, args)
    except StorageError as exc:
        print(f"storage error: {exc}", file=sys.stderr)
        return 3
    except FileNotFoundError as exc:
        print(f"not initialised: {exc} (run `polymath init`)", file=sys.stderr)
        return 4
    return rc


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
