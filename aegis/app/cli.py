"""`aegis` command-line interface (server-side administration)."""
from __future__ import annotations

import argparse
import getpass
import json
import re
import secrets
import sys
from pathlib import Path

from .config import load_settings


def _set_env_value(env_file: Path, key: str, value: str) -> None:
    lines = env_file.read_text().splitlines() if env_file.exists() else []
    pat = re.compile(rf"^\s*{re.escape(key)}\s*=")
    out, done = [], False
    for ln in lines:
        if pat.match(ln):
            out.append(f"{key}='{value}'")
            done = True
        else:
            out.append(ln)
    if not done:
        out.append(f"{key}='{value}'")
    env_file.write_text("\n".join(out) + "\n")
    try:
        env_file.chmod(0o640)
    except PermissionError:
        pass


def _env_path(args) -> Path:
    import os
    return Path(args.env_file or os.environ.get("AEGIS_ENV_FILE", ".env"))


def cmd_serve(args) -> int:
    import os
    from .main import run
    if args.env_file:
        os.environ["AEGIS_ENV_FILE"] = args.env_file
    run()
    return 0


def cmd_init(args) -> int:
    from .db import Database
    s = load_settings(_env_path(args))
    s.ensure_dirs()
    db = Database(s.db_path, s.migrations_dir)
    applied = db.migrate()
    print(f"database: {s.db_path}\napplied migrations: {applied or 'none (up to date)'}")
    return 0


def cmd_set_password(args) -> int:
    from .api.authentication import hash_password
    env = _env_path(args)
    pw = args.password or getpass.getpass("New admin password (min 10 chars): ")
    if not args.password and pw != getpass.getpass("Repeat: "):
        print("passwords do not match", file=sys.stderr)
        return 1
    try:
        h = hash_password(pw)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    _set_env_value(env, "ADMIN_PASSWORD_HASH", h)
    s = load_settings(env)
    if not s.session_secret:
        _set_env_value(env, "SESSION_SECRET", secrets.token_urlsafe(32))
    print(f"admin password hash written to {env}; restart AEGIS to apply")
    return 0


def cmd_create_token(args) -> int:
    from .api.authentication import hash_token
    env = _env_path(args)
    tok = "aegis_" + secrets.token_urlsafe(32)
    _set_env_value(env, "API_TOKEN_HASH", hash_token(tok))
    print("API token (shown once; only its hash is stored):\n" + tok)
    return 0


def cmd_env_report(args) -> int:
    from .maintenance.environment import environment_report, format_report
    rep = environment_report(load_settings(_env_path(args)))
    print(json.dumps(rep, indent=2) if args.json else format_report(rep))
    return 0


def _services(args):
    from .services import build_services
    return build_services(load_settings(_env_path(args)))


def cmd_backup(args) -> int:
    from .maintenance.backups import create_backup
    s = _services(args)
    res = create_backup(s.db, s.settings.backups, s.settings.backup_keep)
    print(json.dumps(res, indent=2))
    return 0


def cmd_verify(args) -> int:
    from .maintenance.backups import verify_backup
    ok, detail = verify_backup(Path(args.path))
    print(("OK: " if ok else "FAILED: ") + detail)
    return 0 if ok else 1


def cmd_restore(args) -> int:
    from .maintenance.backups import restore_backup
    s = load_settings(_env_path(args))
    if not args.yes:
        ans = input(f"Restore {args.path} over {s.db_path}? The service must be stopped. Type 'restore': ")
        if ans.strip() != "restore":
            print("aborted")
            return 1
    saved = restore_backup(Path(args.path), s.db_path)
    print(f"restored. previous database saved as {saved}")
    return 0


def cmd_load_topics(args) -> int:
    from .scheduler.schedules import load_topics_file
    s = _services(args)
    created = load_topics_file(s.db, Path(args.path))
    print(f"created topics: {created or 'none (all exist)'}")
    return 0


def cmd_verify_audit(args) -> int:
    s = _services(args)
    ok, bad = s.audit.verify_chain()
    n = s.db.scalar("SELECT COUNT(*) FROM audit_events")
    print(f"audit events: {n}; chain {'intact' if ok else f'BROKEN at id {bad}'}")
    return 0 if ok else 1


def cmd_status(args) -> int:
    from .observability.health import health_report
    print(json.dumps(health_report(_services(args)), indent=2, default=str))
    return 0


def cmd_research(args) -> int:
    from .agent.objectives import ObjectiveManager
    s = _services(args)
    params = {"hours": args.hours, "max_documents": args.max_documents,
              "allowed_domains": args.domain or [], "seed_urls": args.seed or []}
    oid = ObjectiveManager(s).create(args.topic, kind="deep_research", priority=6, research_params=params, actor="cli")
    print(f"deep research queued: {oid} ({args.hours:g} h budget). The running service picks it up; "
          f"follow it on the dashboard under Tasks, or: curl /api/objectives/{oid}/report")
    return 0


def cmd_export(args) -> int:
    s = _services(args)
    data = s.knowledge.export()
    Path(args.out).write_text(json.dumps(data, indent=1, default=str))
    print(f"exported {len(data['documents'])} documents, {len(data['claims'])} claims to {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="aegis", description="AEGIS autonomous research & execution agent")
    p.add_argument("--env-file", help="path to the configuration file (default: $AEGIS_ENV_FILE or ./.env)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the web dashboard, API and background worker").set_defaults(fn=cmd_serve)
    sub.add_parser("init", help="create directories and apply database migrations").set_defaults(fn=cmd_init)
    sp = sub.add_parser("set-password", help="set the dashboard admin password")
    sp.add_argument("--password", help=argparse.SUPPRESS)
    sp.set_defaults(fn=cmd_set_password)
    sub.add_parser("create-token", help="create an API bearer token").set_defaults(fn=cmd_create_token)
    er = sub.add_parser("env-report", help="read-only environment audit")
    er.add_argument("--json", action="store_true")
    er.set_defaults(fn=cmd_env_report)
    sub.add_parser("backup", help="create and verify a backup now").set_defaults(fn=cmd_backup)
    vb = sub.add_parser("verify-backup", help="verify a backup file")
    vb.add_argument("path")
    vb.set_defaults(fn=cmd_verify)
    rb = sub.add_parser("restore", help="restore a backup (stop the service first)")
    rb.add_argument("path")
    rb.add_argument("--yes", action="store_true")
    rb.set_defaults(fn=cmd_restore)
    lt = sub.add_parser("load-topics", help="load research topics from a JSON file")
    lt.add_argument("path")
    lt.set_defaults(fn=cmd_load_topics)
    sub.add_parser("verify-audit", help="verify the audit log hash chain").set_defaults(fn=cmd_verify_audit)
    sub.add_parser("status", help="print the health report").set_defaults(fn=cmd_status)
    rs = sub.add_parser("research", help="start a deep research campaign on a topic")
    rs.add_argument("topic")
    rs.add_argument("--hours", type=float, default=2.0)
    rs.add_argument("--max-documents", type=int, default=200)
    rs.add_argument("--domain", action="append", help="restrict to this domain (repeatable)")
    rs.add_argument("--seed", action="append", help="seed URL (repeatable)")
    rs.set_defaults(fn=cmd_research)
    ex = sub.add_parser("export-knowledge", help="export knowledge as JSON")
    ex.add_argument("out")
    ex.set_defaults(fn=cmd_export)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
