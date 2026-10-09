"""Command-line interface (argparse)."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from typing import Any

from polymath import __version__
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


def cmd_enqueue(config: Config, args: argparse.Namespace) -> int:
    db = Database(config.paths.db_path, busy_timeout_ms=60_000)
    db.migrate()
    payload = json.loads(args.payload) if args.payload else {}
    job_id, created = Scheduler(db).enqueue(args.kind, payload, key=args.key, priority=args.priority)
    print(json.dumps({"job_id": job_id, "created": created}))
    db.close()
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
