"""Command-line interface (argparse)."""

from __future__ import annotations

import argparse
import json
import os
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
from polymath.version import Build, read_build


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
        "build": read_build().label(),
        "agent_build": hb.get("version"),
        "heartbeat_age_s": round(time.time() - float(hb.get("ts", 0)), 1) if hb else None,
        "agent_state": hb.get("state"),
        "cycle": hb.get("cycle"),
        "jobs": {"ready": st.ready, "queued": st.queued, "running": st.running, "done": st.done, "dead": st.dead},
        "ready_by_kind": st.ready_by_kind,
        "knowledge": _knowledge_summary(db),
        "storage": _storage_summary(db),
    }


def _storage_summary(db: Database) -> dict[str, Any] | None:
    if not db.scalar("SELECT 1 FROM sqlite_master WHERE name = 'volumes'"):
        return None
    row = db.one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(budget_bytes), 0) AS b FROM volumes WHERE online = 1 AND retired = 0"
    )
    spilled = int(db.scalar("SELECT COUNT(*) FROM documents WHERE codec = 'spilled'", default=0))
    return {
        "drives_online": int(row["n"]) if row else 0,
        "drive_budget_gb": round((row["b"] if row else 0) / 1e9, 1),
        "documents_on_drives": spilled,
    }


def _knowledge_summary(db: Database) -> dict[str, Any]:
    def table(name: str) -> bool:
        return bool(db.scalar("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)))

    out: dict[str, Any] = {}
    if table("documents"):
        out["documents"] = int(db.scalar("SELECT COUNT(*) FROM documents WHERE state!='duplicate'", default=0))
    if table("entities"):
        out["entities"] = int(db.scalar("SELECT COUNT(*) FROM entities", default=0))
    if table("triples"):
        out["triples"] = {
            r["status"]: int(r["n"]) for r in db.query("SELECT status, COUNT(*) AS n FROM triples GROUP BY status")
        }
    if table("quizzes"):
        q = db.one("SELECT created, n, accuracy, chance FROM quizzes ORDER BY id DESC LIMIT 1")
        out["last_quiz"] = dict(q) if q else None
    if table("vitals"):
        v = db.one("SELECT at, temp_c, load1, mode FROM vitals ORDER BY at DESC LIMIT 1")
        out["vitals"] = dict(v) if v else None
    return out


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
        web = int(  # same count as the report: near-duplicate pages do not count towards the target
            agent.db.scalar("SELECT COUNT(*) FROM documents WHERE source='web' AND state!='duplicate'", default=0)
        )
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


def _answerer(config: Config, db: Database) -> Any:
    from polymath.interface.answer import Answerer
    from polymath.perception.entities import AliasIndex, EntityLinker

    auto = AliasIndex(db, config.paths.index_dir).load()
    return Answerer(db, EntityLinker(db, auto) if auto else None)


def cmd_ask(config: Config, args: argparse.Namespace) -> int:
    """Answer from what the agent has learned. Never touches the network."""
    db = _db(config, readonly=True)
    try:
        ans = _answerer(config, db).ask(" ".join(args.question))
    finally:
        db.close()
    print(json.dumps(ans.to_dict(), indent=2) if args.json else ans.render())
    return 0 if ans.statements else 1


def cmd_topics(config: Config, args: argparse.Namespace) -> int:
    from polymath.drive.priority import top_topics, weakest_topics

    db = _db(config, readonly=True)
    try:
        rows = weakest_topics(db, args.n) if args.weakest else top_topics(db, args.n)
    finally:
        db.close()
    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return 0
    if not rows:
        print("no topic priorities yet (the agent computes them after it has read and organised documents)")
        return 0
    print(f"{'topic':<40} {'priority':>9} {'gap':>6} {'import.':>8} {'novelty':>8}")
    for r in rows:
        print(
            f"{str(r['name'])[:40]:<40} {r['priority']:>9.4f} {r['gap']:>6.3f} {r['importance']:>8.3f} "
            f"{r['novelty']:>8.3f}"
        )
    return 0


def why_dict(db: Database, query: str, n: int) -> dict[str, Any]:
    """Explain the agent's choices: a topic's priority, a fact's provenance, or recent decisions."""
    from polymath.drive.priority import explain_topic

    out: dict[str, Any] = {"query": query}
    if query:
        t = db.one("SELECT id FROM topics WHERE name = ? COLLATE NOCASE", (query,)) or db.one(
            "SELECT id FROM topics WHERE name LIKE ? ORDER BY n_total DESC LIMIT 1", (f"%{query}%",)
        )
        if t is not None:
            out["topic"] = explain_topic(db, int(t["id"]))
        jobs = [
            dict(r)
            for r in db.query(
                "SELECT id, kind, state, priority, payload FROM jobs WHERE payload LIKE ? ORDER BY id DESC LIMIT ?",
                (f"%{query}%", n),
            )
        ]
        if jobs:
            out["jobs"] = jobs
    out["decisions"] = [
        dict(r)
        for r in db.query(
            "SELECT at, context, chosen, reason, reward, cpu FROM decisions ORDER BY id DESC LIMIT ?", (n,)
        )
    ]
    return out


def cmd_why(config: Config, args: argparse.Namespace) -> int:
    db = _db(config, readonly=True)
    try:
        query = " ".join(args.query).strip()
        out = why_dict(db, query, args.n)
        if query and "topic" not in out and "jobs" not in out:
            ans = _answerer(config, db).ask(query)  # a fact question: show its provenance
            out["answer"] = ans.to_dict()
    finally:
        db.close()
    if args.json:
        print(json.dumps(out, indent=2, default=str))
        return 0
    if out.get("topic"):
        t = out["topic"]
        print(f"Topic “{t['name']}”: {t['formula']}")
        for k, v in sorted((t.get("evidence") or {}).items()):
            print(f"  {k}: {v}")
    elif out.get("topic", "missing") is None:
        print(f"“{query}” has no computed priority yet.")
    for j in out.get("jobs", []):
        print(f"  job #{j['id']} {j['kind']} [{j['state']}] priority {j['priority']}")
    if out.get("answer"):
        from polymath.interface.answer import Answer, Citation, Statement

        a = out["answer"]
        print(
            Answer(
                a["question"],
                a["subject"],
                a["relation"],
                [
                    Statement(s["text"], s["confidence"], s["kind"], [Citation(**c) for c in s["citations"]])
                    for s in a["statements"]
                ],
                a["confidence"],
                a["note"],
            ).render()
        )
    if out["decisions"]:
        print("Recent decisions:")
        for d in out["decisions"]:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(d["at"]))
            print(f"  {when}  {d['chosen']:<12} {d['reason']}" + (f"  → reward {d['reward']}" if d["reward"] else ""))
    return 0


def cmd_learn(config: Config, args: argparse.Namespace) -> int:
    """Ask the agent to research a topic or crawl a URL (handled by the running agent via its inbox)."""
    query = " ".join(args.query).strip()
    if not query:
        print("give a topic or a URL", file=sys.stderr)
        return 2
    check_storage(config)
    if agent_running(config):
        path = inbox.submit(config.paths.data_dir, {"type": "learn", "query": query})
        print(json.dumps({"queued_via": "inbox", "file": path.name, "query": query}))
        return 0
    db = Database(config.paths.db_path, busy_timeout_ms=60_000)
    db.migrate()
    job_id, created = Scheduler(db).enqueue(
        "drive.learn", {"query": query}, key=f"learn:{query}:{int(time.time())}", priority=3.5
    )
    db.close()
    print(json.dumps({"queued_via": "database", "job_id": job_id, "created": created, "query": query}))
    return 0


def cmd_report(config: Config, args: argparse.Namespace) -> int:
    from polymath.evaluation.report import collect, to_markdown, write_report

    if args.write:
        db = _db(config)
        try:
            res = write_report(db, config.paths.report_dir)
        finally:
            db.close()
        print(json.dumps(res))
        return 0
    db = _db(config, readonly=True)
    try:
        rep = collect(db, time.time() - args.hours * 3600)
    finally:
        db.close()
    print(json.dumps(rep, indent=2, default=str) if args.json else to_markdown(rep))
    return 0


def cmd_dashboard(config: Config, args: argparse.Namespace) -> int:
    from polymath.interface.dashboard import serve

    if args.host:
        config.dashboard.host = args.host
    if args.port is not None:
        config.dashboard.port = args.port
    return serve(config)


def dashboard_url(config: Config) -> str:
    """Where this machine reaches its own dashboard (a wildcard bind address means localhost)."""
    host = config.dashboard.host
    if host in {"", "0.0.0.0", "::"}:
        host = "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{config.dashboard.port}"


def main_disk_name(config: Config) -> str:
    try:
        home = json.loads((config.paths.data_dir / "home-drive.json").read_text(encoding="utf-8"))
        return f"main disk: drive {home.get('name') or home.get('id')} ({config.paths.data_dir})"
    except (OSError, ValueError, AttributeError):
        return f"main disk ({config.paths.data_dir})"


def storage_rows(config: Config) -> list[dict[str, Any]]:
    """The main disk and every adopted drive (plugged in or not), with budgets and use."""
    from polymath.body.guard import directory_bytes
    from polymath.memory import pool as storage_pool

    pool = storage_pool.for_config(config) or storage_pool.StoragePool(config)
    rows: list[dict[str, Any]] = [
        {
            "id": "main",
            "name": main_disk_name(config),
            "online": True,
            "budget_gb": round(config.body.disk_budget_gb, 1),
            "used_gb": round(directory_bytes(config.paths.data_dir) / 1e9, 2),
            "documents_here": None,
        }
    ]
    spilled: dict[str, int] = {}
    known: dict[str, Any] = {}
    if config.paths.db_path.exists():
        db = Database(config.paths.db_path, readonly=True)
        try:
            spilled = {
                str(r["v"]): int(r["n"])
                for r in db.query(
                    "SELECT json_extract(meta, '$.spill.vol') AS v, COUNT(*) AS n FROM documents "
                    "WHERE codec = 'spilled' GROUP BY v"
                )
            }
            if db.scalar("SELECT 1 FROM sqlite_master WHERE name = 'volumes'"):
                known = {str(r["id"]): dict(r) for r in db.query("SELECT * FROM volumes WHERE retired = 0")}
        finally:
            db.close()
    online = {v.id: v for v in pool.volumes(fresh=True)}
    for vid in sorted(set(online) | set(known)):
        v = online.get(vid)
        k = known.get(vid, {})
        rows.append(
            {
                "id": vid,
                "name": v.name if v else (k.get("label") or k.get("model") or vid[:8]),
                "online": v is not None,
                "budget_gb": round((v.budget_bytes if v else int(k.get("budget_bytes", 0))) / 1e9, 1),
                "used_gb": round((pool.used(v) if v else int(k.get("used_bytes", 0))) / 1e9, 2),
                "documents_here": spilled.get(vid, 0),
            }
        )
    return rows


def cmd_storage(config: Config, args: argparse.Namespace) -> int:
    """The storage pool: list it, retire a drive, and the root-only hooks udev and systemd call."""
    import os

    from polymath.body.volumes import Helper

    op = args.op
    if op == "list":
        rows = storage_rows(config)
        if args.json:
            print(json.dumps(rows, indent=2))
            return 0
        total = sum(r["budget_gb"] for r in rows if r["online"])
        print(f"{'id':<38} {'name':<34} {'state':<8} {'brain GB':>9} {'used GB':>8} {'docs':>8}")
        for r in rows:
            docs = "" if r["documents_here"] is None else f"{r['documents_here']:,}"
            state = "online" if r["online"] else "away"
            print(
                f"{r['id']:<38} {r['name'][:34]:<34} {state:<8} {r['budget_gb']:>9.1f} {r['used_gb']:>8.2f} {docs:>8}"
            )
        print(f"total brain space now: {total:.1f} GB")
        return 0
    if op == "retire":
        payload = {"volume": args.volume, "retire": True}
        if agent_running(config):
            path = inbox.submit(
                config.paths.data_dir,
                {"type": "enqueue", "kind": "body.recall", "payload": payload,
                 "key": f"recall:{args.volume}:{time.time_ns()}", "priority": 3.0},
            )  # fmt: skip
            print(json.dumps({"queued_via": "inbox", "file": path.name, **payload}))
            return 0
        check_storage(config)
        db = open_database(config.paths.db_path)
        job_id, _ = Scheduler(db).enqueue(
            "body.recall", payload, key=f"recall:{args.volume}:{time.time_ns()}", priority=3.0
        )
        db.close()
        print(json.dumps({"queued_via": "database", "job_id": job_id, **payload}))
        return 0
    if op == "format" and not args.yes:
        print("this erases the drive (it must hold no files): add --yes to go ahead", file=sys.stderr)
        return 2
    if os.geteuid() != 0:
        print(f"`polymath storage {op}` mounts and unmounts drives: run it with sudo", file=sys.stderr)
        return 2
    helper = Helper(config)
    if op == "format":
        out = helper.format_empty(args.volume, erase_files=args.erase_files)
    else:
        out = helper.eject(args.target) if op == "eject" else getattr(helper, op)(args.target)
    print(json.dumps(out.to_dict()))
    return 0 if out.action != "skipped" or op in {"attach", "detach"} else 1


def cmd_predictions(config: Config, args: argparse.Namespace) -> int:
    """Facts it guessed before reading them: confirmed, wrong, still open."""
    from polymath.reasoning import predictions

    db = _db(config, readonly=True)
    try:
        sb = predictions.scoreboard(db)
        open_rows = db.query(
            "SELECT pr.s, pr.o, pr.score, p.label AS pl FROM predictions pr JOIN predicates p ON p.id = pr.p "
            "WHERE pr.state = 'open' ORDER BY pr.score DESC LIMIT ?",
            (args.limit,),
        )

        def label(e: int) -> str:
            return str(db.scalar("SELECT label FROM entities WHERE id = ?", (e,), default="?"))

        pending = [f"{label(int(r['s']))} → {r['pl']} → {label(int(r['o']))} ({float(r['score']):.0%} sure)"
                   for r in open_rows]  # fmt: skip
    finally:
        db.close()
    if args.json:
        print(json.dumps(sb | {"open_guesses": pending}, indent=2))
        return 0
    print(predictions.describe(sb))
    for h in sb["recent_hits"]:
        print(f"  ✓ {h['subject']} → {h['relation']} → {h['guess']} ({h['confidence']:.0%} sure)")
    if pending:
        print("open:")
        for line in pending:
            print(f"  ? {line}")
    return 0


def cmd_tell(config: Config, args: argparse.Namespace) -> int:
    """A paragraph about something, written from the facts it learned, with sources."""
    from polymath.interface.tell import Teller

    db = _db(config, readonly=True)
    try:
        story = Teller(db).tell(" ".join(args.topic))
    finally:
        db.close()
    print(json.dumps(story.to_dict(), indent=2) if args.json else story.render())
    return 0 if story.paragraph else 1


def cmd_wear(config: Config, args: argparse.Namespace) -> int:
    """Disk writes per day (data and system disk) and how long an SD card lasts at this rate."""
    from polymath.body import wear

    db = _db(config, readonly=True)
    try:
        rows = wear.report(db, config)
    finally:
        db.close()
    home = config.paths.data_dir / "home-drive.json"
    if args.json:
        print(json.dumps({"devices": rows, "brain_on_drive": home.exists()}, indent=2))
        return 0
    if not rows:
        print("no measurements yet: the agent samples disk writes every 15 minutes")
    for r in rows:
        print(wear.describe(r) + f"  (Polymath: {wear.gb(r['agent_last_day_bytes'])})" * (r["role"] == "data"))
        if r.get("status") in {"high", "critical"}:
            print("  over budget: plug in a USB drive and the brain moves onto it, so the SD card only boots the Pi")
    if home.exists():
        print("the brain lives on a drive; the SD card only boots the Pi")
    return 0


def cmd_digest(config: Config, args: argparse.Namespace) -> int:
    """Today's "what I learned" digest: the last one written, or one computed now (``--now``)."""
    from polymath.evaluation import digest

    if args.write:
        db = _db(config)
        try:
            with db.transaction():
                d = digest.write_digest(db, cfg=config)
            day, data = d["day"], d
        finally:
            db.close()
    else:
        db = _db(config, readonly=True)
        try:
            last = None if args.now else digest.latest(db)
            if last is None:
                day, data = time.strftime("%Y-%m-%d") + " (so far)", digest.collect(db, time.time() - 86400, cfg=config)
            else:
                day, data = last["day"], last["data"]
        finally:
            db.close()
    print(json.dumps({"day": day, **data}, indent=2, default=str) if args.json else digest.to_markdown(data, day))
    return 0


def cmd_sites(config: Config, args: argparse.Namespace) -> int:
    """Open-web learning: which sites passed or failed vetting, and why; or check one site against the gate."""
    from polymath.senses import openweb

    db = _db(config, readonly=True)
    try:
        if args.check:
            host = args.check.lower().removeprefix("https://").removeprefix("http://").split("/")[0]
            from polymath.senses.crawler import site_of

            site = site_of(host)
            row = db.one("SELECT * FROM sites WHERE site=?", (site,))
            cites = db.scalar("SELECT citations FROM site_citations WHERE site=?", (site,), default=0)
            ready = openweb.blocklists_ready(db, config)
            print(json.dumps({
                "site": site,
                "safety_lists_loaded": ready,
                "on_safety_list": openweb.blocked(db, host) if ready else "unknown (lists not loaded yet)",
                "name_looks_unsafe": openweb.bad_name(host),
                "cited_by_wikipedia_articles": int(cites or 0),
                "verdict": dict(row) if row else "not vetted yet",
            }, indent=2, default=str))  # fmt: skip
            return 0
        where = "WHERE status = ?" if args.status else ""
        rows = db.query(
            f"SELECT site, status, citations, trust, reason, vetted_at FROM sites {where} "
            "ORDER BY vetted_at DESC LIMIT ?",
            ((args.status,) if args.status else ()) + (args.limit,),
        )
        counts = {r["status"]: r["n"] for r in db.query("SELECT status, COUNT(*) AS n FROM sites GROUP BY status")}
        loaded = db.kv_get("blocklists_loaded") or {}
    finally:
        db.close()
    if args.json:
        print(json.dumps({"counts": counts, "sites": [dict(r) for r in rows], "safety_lists": loaded}, indent=2))
        return 0
    lists = ", ".join(f"{v['name']} ({v['count']:,})" for v in loaded.values()) or "not loaded yet"
    print(f"safety lists: {lists}")
    print("sites: " + (", ".join(f"{n} {k}" for k, n in sorted(counts.items())) or "none vetted yet"))
    for r in rows:
        trust = "" if r["trust"] is None else f" trust {r['trust']:.2f}"
        print(f"  {r['status']:<9} {r['site']:<40} cited {r['citations']:>6,}{trust}  {r['reason']}")
    return 0


def cmd_version(config: Config, args: argparse.Namespace) -> int:
    """The installed build, and the build the running agent was started with."""
    installed = read_build()
    agent: dict[str, Any] = {}
    try:
        db = Database(config.paths.db_path, readonly=True)
        try:
            agent = db.kv_get("heartbeat") or {}
        finally:
            db.close()
    except Exception:  # no database yet, or not readable by this user: report what is installed
        agent = {}
    current = agent.get("build") == installed.key if agent.get("build") else None
    info = {
        "version": installed.version,
        "commit": installed.commit,
        "date": installed.date,
        "installed": installed.installed,
        "agent": agent.get("version"),
        "agent_current": current,
    }
    if args.json:
        print(json.dumps(info))
        return 0
    print(f"installed: polymath {installed.label()}" + (f" ({installed.date})" if installed.date else ""))
    if agent.get("version"):
        mark = "✓ running the installed build" if current else "↻ restart the agent to run the installed build"
        print(f"agent:     polymath {agent['version']}  {mark}")
    else:
        print("agent:     not running, or started before builds were recorded")
    return 0


def _reexec_feed(build: Build) -> None:  # pragma: no cover - replaces the process
    """Restart this feed process in the newly installed code (same arguments)."""
    os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])


def cmd_feed(config: Config, args: argparse.Namespace) -> int:
    from polymath.core.loop import heartbeat_path
    from polymath.interface import feed

    if args.direct:
        source = feed.direct_source(config.paths.db_path, pulse=heartbeat_path(config))
        where = str(config.paths.db_path)
    else:
        where = args.url or dashboard_url(config)
        source = feed.http_source(where)
    return feed.run(
        source,
        interval=args.interval,
        color=False if args.no_color else None,
        fancy=False if args.plain else None,
        once=args.once,
        where=where,
        on_update=None if args.once else _reexec_feed,
    )


def cmd_backup(config: Config, args: argparse.Namespace) -> int:
    """Take a verified backup now (or list the existing ones)."""
    from polymath.body import maintenance

    if args.list:
        for b in maintenance.backups(config.paths.backup_dir):
            print(json.dumps({"path": str(b), "bytes": b.stat().st_size}))
        return 0
    if agent_running(config):
        path = inbox.submit(
            config.paths.data_dir,
            {
                "type": "enqueue",
                "kind": "body.backup",
                "payload": {},
                "key": f"cli-backup:{int(time.time())}",
                "priority": 3.0,
            },
        )
        print(json.dumps({"queued_via": "inbox", "file": path.name}))
        return 0
    agent = build_agent(config, planners=False)
    agent.start()
    agent.scheduler.enqueue("body.backup", {}, key=f"cli-backup:{int(time.time())}", priority=9.0)
    rec = agent.cycle()
    agent.shutdown()
    agent.db.close()
    print(json.dumps({"status": rec.status, "error": rec.error}))
    return 0 if rec.status == "done" else 1


def cmd_restore(config: Config, args: argparse.Namespace) -> int:
    """Replace the database with a backup. Refuses while the agent runs."""
    from pathlib import Path

    from polymath.body import maintenance

    if agent_running(config):
        print("the agent is running: stop it first (sudo systemctl stop polymath)", file=sys.stderr)
        return 5
    backup = Path(args.backup)
    if not backup.is_file():
        backup = config.paths.backup_dir / args.backup
    old = maintenance.restore(backup, config.paths.db_path)
    print(json.dumps({"restored": str(backup), "previous_database": str(old)}))
    return 0


def _agents_write(config: Config, payload: dict[str, Any]) -> tuple[str, Any]:
    """Apply an agent command: through the running agent's inbox, or directly when it is stopped."""
    from polymath.agents import society

    if agent_running(config):
        path = inbox.submit(
            config.paths.data_dir,
            {"type": "enqueue", "kind": "agents.command", "payload": payload,
             "key": f"agents:{payload['op']}:{time.time_ns()}", "priority": 4.0},
        )  # fmt: skip
        return "inbox", path.name
    check_storage(config)
    db = open_database(config.paths.db_path)
    try:
        with db.transaction():
            op = payload["op"]
            if op == "spawn":
                a = society.spawn(db, payload["directive"], name=payload.get("name"))
                return "database", a
            if op == "task":
                tid = society.give_task(db, payload["agent"], payload["text"])
                from polymath.agents import skills, store

                agent = store.get(db, payload["agent"])
                assert agent is not None
                import random

                skills.answer_pending(
                    skills.Runtime(db, Scheduler(db), config, {}, agent, random.Random())
                )  # answer now: nobody else will
                return "database", tid
            if op == "feedback":
                return "database", society.feedback(
                    db, int(payload["task"]), payload["verdict"], payload.get("note", "")
                )
            from polymath.agents import store

            agent = store.get(db, payload["agent"])
            if agent is None:
                raise KeyError(f"no agent called {payload['agent']!r}")
            store.set_status(db, agent.id, {"pause": "paused", "resume": "active", "retire": "retired"}[op], "by you")
            return "database", agent.name
    finally:
        db.close()


def agents_list(db: Database) -> list[dict[str, Any]]:
    from polymath.agents import store

    out = []
    for a in store.all_agents(db):
        out.append(
            {
                "name": a.name, "kind": a.kind, "directive": a.directive, "status": a.status, "level": a.level,
                "xp": round(a.xp, 2), "reward": round(a.reward_total, 2), "correct": a.tasks_correct,
                "wrong": a.tasks_wrong, "accuracy": None if a.accuracy is None else round(a.accuracy, 3),
                "generation": a.generation, "origin": a.origin,
                "scope": int(db.scalar("SELECT COUNT(*) FROM agent_scope WHERE agent_id=?", (a.id,), 0)),
            }
        )  # fmt: skip
    return out


def agent_detail(db: Database, ident: str, n: int = 15) -> dict[str, Any] | None:
    from polymath.agents import store
    from polymath.agents.directives import describe

    a = store.get(db, ident)
    if a is None:
        return None
    tasks = [
        {**dict(r), "payload": json.loads(r["payload"]), "result": json.loads(r["result"])}
        for r in db.query(
            "SELECT id, kind, action, state, reward, reason, created, payload, result FROM agent_tasks "
            "WHERE agent_id=? ORDER BY id DESC LIMIT ?",
            (a.id, n),
        )
    ]
    arms = [
        dict(r)
        for r in db.query(
            "SELECT action, n, ROUND(mean, 3) AS mean FROM agent_arms WHERE agent_id=? ORDER BY mean DESC", (a.id,)
        )
    ]
    return {
        **next(x for x in agents_list(db) if x["name"] == a.name),
        "purpose": describe(a.kind, a.subject, a.scope.get("qualifier")),
        "params": {k: v for k, v in a.params.items() if k != "cursor"},
        "parent": (store.get(db, a.parent).name if a.parent and store.get(db, a.parent) else None),  # type: ignore[union-attr]
        "preferences": arms,
        "tasks": tasks,
    }


def cmd_agents(config: Config, args: argparse.Namespace) -> int:
    op = args.agents_op
    if op in {"list", "show"}:
        db = _db(config, readonly=True)
        try:
            if op == "list":
                rows = agents_list(db)
                if args.json:
                    print(json.dumps(rows, indent=2))
                elif not rows:
                    print('no agents yet — try: polymath agents spawn "research black holes"')
                else:
                    print(
                        f"{'agent':<32} {'kind':<9} {'lvl':>3} {'xp':>7} {'net':>7} {'right':>6} {'wrong':>6} "
                        f"{'status':<8}"
                    )
                    for r in rows:
                        print(f"{r['name'][:32]:<32} {r['kind']:<9} {r['level']:>3} {r['xp']:>7.1f} "
                              f"{r['reward']:>7.1f} "
                              f"{r['correct']:>6} {r['wrong']:>6} {r['status']:<8}")  # fmt: skip
                return 0
            detail = agent_detail(db, args.name)
        finally:
            db.close()
        if detail is None:
            print(f"no agent called {args.name!r}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(detail, indent=2, default=str))
            return 0
        acc = "–" if detail["accuracy"] is None else f"{detail['accuracy']:.0%}"
        print(f"{detail['name']} — {detail['purpose']}  [{detail['status']}, level {detail['level']}, "
              f"{detail['xp']} XP, net reward {detail['reward']}, accuracy {acc}, "
              f"scope {detail['scope']} entities]")  # fmt: skip
        if detail["parent"]:
            print(f"  forked from {detail['parent']} (generation {detail['generation']})")
        print("  learned preferences: " + (", ".join(f"{p['action']} {p['mean']:+.2f} (n={p['n']})"
                                                     for p in detail["preferences"]) or "none yet"))  # fmt: skip
        for t in detail["tasks"]:
            what = t["payload"].get("question") or t["payload"].get("title") or t["payload"].get("text") or ""
            if t["kind"] == "user" and t["result"].get("rendered"):
                what = f"{what} → {t['result']['rendered'].splitlines()[0]}"
            print(f"  #{t['id']:<6} {t['kind']:<9} {t['state']:<8} {t['reward']:+.2f}  {str(what)[:90]}")
        return 0
    if op == "spawn":
        payload: dict[str, Any] = {"op": "spawn", "directive": " ".join(args.directive), "name": args.name}
    elif op == "task":
        payload = {"op": "task", "agent": args.name, "text": " ".join(args.text)}
    elif op == "feedback":
        payload = {"op": "feedback", "task": args.task, "verdict": args.verdict, "note": " ".join(args.note or [])}
    else:
        payload = {"op": op, "agent": args.name}
    try:
        via, res = _agents_write(config, payload)
    except KeyError as exc:
        print(str(exc).strip("'\""), file=sys.stderr)
        return 2
    if via == "inbox":
        print(json.dumps({"queued_via": "inbox", "file": res, "hint": "the running agent applies it within a cycle"}))
        return 0
    if op == "spawn":
        from polymath.agents.directives import describe

        print(f"spawned {res.name}: {describe(res.kind, res.subject, res.scope.get('qualifier'))} "
              f"(scope {len(res.scope.get('entities', []))} seed entities, "
              f"{len(res.scope.get('predicates', []))} relations)")  # fmt: skip
    elif op == "task":
        db = _db(config, readonly=True)
        try:
            row = db.one("SELECT result FROM agent_tasks WHERE id=?", (res,))
        finally:
            db.close()
        rendered = json.loads(row["result"]).get("rendered") if row else None
        print(f"task #{res}:\n{rendered or '(queued)'}\n\nrate it: polymath agents feedback {res} correct|wrong")
    else:
        print(json.dumps(res, default=str))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="polymath", description="Polymath autonomous learning agent")
    p.add_argument("--config", help="path to polymath.toml (default /etc/polymath/polymath.toml)")
    p.add_argument("--version", action="version", version=f"polymath {read_build().label()}")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the data directory and database").set_defaults(func=cmd_init)

    r = sub.add_parser("run", help="run the agent loop (what systemd starts)")
    r.add_argument("--max-cycles", type=int, default=None, help="stop after N job cycles")
    r.add_argument("--until-idle", action="store_true", help="stop when no job is ready")
    r.set_defaults(func=cmd_run)

    sub.add_parser("status", help="agent health and queue summary").set_defaults(func=cmd_status)

    vr = sub.add_parser("version", help="installed version and commit, and whether the agent runs it")
    vr.add_argument("--json", action="store_true")
    vr.set_defaults(func=cmd_version)

    a = sub.add_parser("ask", help="ask what the agent knows (offline, with citations)")
    a.add_argument("question", nargs="+")
    a.add_argument("--json", action="store_true")
    a.set_defaults(func=cmd_ask)

    t = sub.add_parser("topics", help="what it wants to learn next")
    t.add_argument("--weakest", action="store_true", help="topics with the largest knowledge gap")
    t.add_argument("-n", type=int, default=15)
    t.add_argument("--json", action="store_true")
    t.set_defaults(func=cmd_topics)

    w = sub.add_parser("why", help="explain a topic's priority, a fact's provenance, or recent decisions")
    w.add_argument("query", nargs="*")
    w.add_argument("-n", type=int, default=10)
    w.add_argument("--json", action="store_true")
    w.set_defaults(func=cmd_why)

    lr = sub.add_parser("learn", help="research a topic (or crawl a URL) as a priority")
    lr.add_argument("query", nargs="+")
    lr.set_defaults(func=cmd_learn)

    rp = sub.add_parser("report", help="learning report (default: last 24 h, Markdown)")
    rp.add_argument("--hours", type=float, default=24.0)
    rp.add_argument("--json", action="store_true")
    rp.add_argument("--write", action="store_true", help="write the nightly report files now")
    rp.set_defaults(func=cmd_report)

    bk = sub.add_parser("backup", help="take a verified, compressed database backup now")
    bk.add_argument("--list", action="store_true", help="list existing backups")
    bk.set_defaults(func=cmd_backup)

    rs = sub.add_parser("restore", help="replace the database with a backup (agent stopped)")
    rs.add_argument("backup", help="backup file (path, or name inside the backup directory)")
    rs.set_defaults(func=cmd_restore)

    ag = sub.add_parser("agents", help="the agent society: spawn agents with directives, give tasks, reward them")
    asub = ag.add_subparsers(dest="agents_op", required=True)
    sp = asub.add_parser("spawn", help='e.g. "research black holes", "watch news about SpaceX", "predict capitals"')
    sp.add_argument("directive", nargs="+")
    sp.add_argument("--name", default=None)
    al = asub.add_parser("list", help="all agents with level, XP and verified results")
    al.add_argument("--json", action="store_true")
    sh = asub.add_parser("show", help="one agent: purpose, learned preferences, recent tasks")
    sh.add_argument("name")
    sh.add_argument("--json", action="store_true")
    tk = asub.add_parser("task", help="give an agent a question or task")
    tk.add_argument("name")
    tk.add_argument("text", nargs="+")
    fb = asub.add_parser("feedback", help="reward or penalise a task: correct | wrong")
    fb.add_argument("task", type=int)
    fb.add_argument("verdict", choices=["correct", "wrong"])
    fb.add_argument("note", nargs="*")
    for ctl in ("pause", "resume", "retire"):
        asub.add_parser(ctl, help=f"{ctl} an agent").add_argument("name")
    ag.set_defaults(func=cmd_agents)

    sto = sub.add_parser("storage", help="drives in the brain: list, retire, eject (attach/detach run from udev)")
    stsub = sto.add_subparsers(dest="op", required=True)
    stl = stsub.add_parser("list", help="the main disk and every drive, with brain space and use")
    stl.add_argument("--json", action="store_true")
    str_ = stsub.add_parser("retire", help="bring a drive's documents back, then it can be unplugged for good")
    str_.add_argument("volume", help="drive id from `polymath storage list`")
    for name, helptext in (
        ("eject", "unmount a drive cleanly before unplugging it (sudo)"),
        ("attach", "add a plugged-in device to the brain (sudo; udev runs this)"),
        ("detach", "release an unplugged device (sudo; systemd runs this)"),
    ):
        stsub.add_parser(name, help=helptext).add_argument("target", help="drive id (eject) or /dev/… device")
    stf = stsub.add_parser("format", help="reformat an EMPTY drive for Linux so the brain can move onto it (sudo)")
    stf.add_argument("volume", help="drive id from `polymath storage list`")
    stf.add_argument("--yes", action="store_true", help="really reformat it (refused if it holds files)")
    stf.add_argument("--erase-files", action="store_true", help="also when it holds files: they are erased")
    sto.set_defaults(func=cmd_storage)

    pr = sub.add_parser("predictions", help="facts it guessed before reading them, and how many came true")
    pr.add_argument("--limit", type=int, default=10, help="open guesses to list")
    pr.add_argument("--json", action="store_true")
    pr.set_defaults(func=cmd_predictions)

    te = sub.add_parser("tell", help="a paragraph about something, written from what it learned (with sources)")
    te.add_argument("topic", nargs="+")
    te.add_argument("--json", action="store_true")
    te.set_defaults(func=cmd_tell)

    we = sub.add_parser("wear", help="disk writes per day and how long the SD card lasts at this rate")
    we.add_argument("--json", action="store_true")
    we.set_defaults(func=cmd_wear)

    dg = sub.add_parser("digest", help='"what I learned today": the daily digest')
    dg.add_argument("--now", action="store_true", help="compute one for the last 24 h now instead of the last one")
    dg.add_argument("--write", action="store_true", help="write today's digest now (agent stopped)")
    dg.add_argument("--json", action="store_true")
    dg.set_defaults(func=cmd_digest)

    si = sub.add_parser("sites", help="open-web learning: vetted, refused and dropped sites, and why")
    si.add_argument("--status", choices=["approved", "probation", "refused", "dropped", "retry"])
    si.add_argument("--limit", type=int, default=30)
    si.add_argument("--check", metavar="DOMAIN", help="check one site against the gate (offline)")
    si.add_argument("--json", action="store_true")
    si.set_defaults(func=cmd_sites)

    fd = sub.add_parser("feed", help="live feed of what it is learning (what the desktop terminal shows)")
    fd.add_argument("--url", default=None, help="dashboard to read from (default: this machine's)")
    fd.add_argument("--direct", action="store_true", help="read the database directly instead of the dashboard")
    fd.add_argument("--interval", type=float, default=1.5, help="seconds between polls (default 1.5)")
    fd.add_argument("--plain", action="store_true", help="plain scrolling lines, no pinned header")
    fd.add_argument("--no-color", action="store_true", help="no colours (also: NO_COLOR=1)")
    fd.add_argument("--once", action="store_true", help="print what is new once and exit")
    fd.set_defaults(func=cmd_feed)

    d = sub.add_parser("dashboard", help="serve the dashboard (what polymath-dashboard.service starts)")
    d.add_argument("--host", default=None)
    d.add_argument("--port", type=int, default=None)
    d.set_defaults(func=cmd_dashboard)

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
    from polymath.memory import pool as storage_pool

    storage_pool.activate(config)  # documents on plugged-in drives read transparently
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
