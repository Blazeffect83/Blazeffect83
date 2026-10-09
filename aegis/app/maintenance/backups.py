"""Online SQLite backups (sqlite3 backup API — consistent while running),
verification and restore."""
from __future__ import annotations

import gzip
import hashlib
import logging
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from ..db import Database, now_iso

log = logging.getLogger(__name__)


class BackupError(Exception):
    pass


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def create_backup(db: Database, backup_dir: Path, keep: int = 7) -> dict:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    raw = backup_dir / f"aegis-{stamp}.db"
    dest = sqlite3.connect(str(raw))
    try:
        db.conn.backup(dest)
    finally:
        dest.close()
    gz = raw.with_suffix(".db.gz")
    with open(raw, "rb") as fi, gzip.open(gz, "wb", compresslevel=6) as fo:
        shutil.copyfileobj(fi, fo)
    raw.unlink()
    gz.chmod(0o600)
    digest = _sha256(gz)
    (gz.parent / (gz.name + ".sha256")).write_text(f"{digest}  {gz.name}\n")
    ok, detail = verify_backup(gz)
    bid = db.insert("backups", {"path": str(gz), "size_bytes": gz.stat().st_size, "sha256": digest,
                                "verified": int(ok), "verify_detail": detail, "created_at": now_iso()})
    prune(backup_dir, keep)
    if not ok:
        raise BackupError(f"backup verification failed: {detail}")
    return {"id": bid, "path": str(gz), "sha256": digest, "verified": ok, "detail": detail}


def verify_backup(path: Path) -> tuple[bool, str]:
    path = Path(path)
    sidecar = path.parent / (path.name + ".sha256")
    if sidecar.exists():
        expected = sidecar.read_text().split()[0]
        if expected != _sha256(path):
            return False, "checksum mismatch"
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "verify.db"
        try:
            with gzip.open(path, "rb") as fi, open(tmp, "wb") as fo:
                shutil.copyfileobj(fi, fo)
        except (OSError, EOFError) as exc:
            return False, f"cannot decompress: {exc}"
        try:
            c = sqlite3.connect(str(tmp))
            res = c.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            counts = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                      for t in ("objectives", "documents", "claims", "audit_events") if t in tables}
            c.close()
        except sqlite3.DatabaseError as exc:
            return False, f"not a valid database: {exc}"
        if res != "ok":
            return False, f"integrity_check: {res}"
        if "schema_migrations" not in tables:
            return False, "missing schema_migrations"
        return True, "ok " + ", ".join(f"{k}={v}" for k, v in counts.items())


def prune(backup_dir: Path, keep: int) -> list[str]:
    files = sorted(backup_dir.glob("aegis-*.db.gz"))
    removed = []
    for f in files[:-keep] if keep > 0 else []:
        f.unlink(missing_ok=True)
        (f.parent / (f.name + ".sha256")).unlink(missing_ok=True)
        removed.append(f.name)
    return removed


def restore_backup(backup: Path, db_path: Path) -> Path:
    """Restore a verified backup over db_path. The service must be stopped.
    The current database is preserved as <db>.pre-restore-<stamp>."""
    ok, detail = verify_backup(backup)
    if not ok:
        raise BackupError(f"refusing to restore unverified backup: {detail}")
    db_path = Path(db_path)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    saved = db_path.with_name(db_path.name + f".pre-restore-{stamp}")
    if db_path.exists():
        shutil.copy2(db_path, saved)
    for suffix in ("-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    tmp = db_path.with_name(db_path.name + ".restoring")
    with gzip.open(backup, "rb") as fi, open(tmp, "wb") as fo:
        shutil.copyfileobj(fi, fo)
    tmp.replace(db_path)
    return saved
