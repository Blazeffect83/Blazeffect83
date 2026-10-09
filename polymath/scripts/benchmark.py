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


BENCHES: dict[str, Callable[[Path, str], Result]] = {
    "core": bench_core,
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
