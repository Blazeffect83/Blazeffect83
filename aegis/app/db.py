"""SQLite access layer: per-thread connections, WAL, migrations, helpers."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def now_iso(delta: timedelta | None = None) -> str:
    t = utcnow() + (delta or timedelta())
    return t.isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def loads(value: str | None, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class Database:
    """Thread-safe SQLite wrapper. Each thread gets its own connection."""

    def __init__(self, path: Path | str, migrations_dir: Path | None = None):
        self.path = str(path)
        self.migrations_dir = Path(migrations_dir) if migrations_dir else None
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._all: list[sqlite3.Connection] = []
        self._all_lock = threading.Lock()

    # -- connections --------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        if self.path != ":memory:":
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        with self._all_lock:
            self._all.append(conn)
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._connect()
            self._local.conn = c
        return c

    def close(self) -> None:
        with self._all_lock:
            for c in self._all:
                try:
                    c.close()
                except sqlite3.Error:
                    pass
            self._all.clear()
        self._local = threading.local()

    # -- queries ------------------------------------------------------------
    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        return self._retry(lambda: self.conn.execute(sql, params))

    def query(self, sql: str, params: tuple | dict = ()) -> list[dict]:
        return [dict(r) for r in self._retry(lambda: self.conn.execute(sql, params).fetchall())]

    def one(self, sql: str, params: tuple | dict = ()) -> dict | None:
        row = self._retry(lambda: self.conn.execute(sql, params).fetchone())
        return dict(row) if row else None

    def scalar(self, sql: str, params: tuple | dict = ()) -> Any:
        row = self._retry(lambda: self.conn.execute(sql, params).fetchone())
        return row[0] if row else None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Serialised write transaction (BEGIN IMMEDIATE) with lock retry."""
        with self._write_lock:
            conn = self.conn
            self._retry(lambda: conn.execute("BEGIN IMMEDIATE"))
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    @staticmethod
    def _retry(fn, attempts: int = 6):
        delay = 0.05
        for i in range(attempts):
            try:
                return fn()
            except sqlite3.OperationalError as exc:
                msg = str(exc).lower()
                if ("locked" in msg or "busy" in msg) and i < attempts - 1:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise

    def insert(self, table: str, values: dict) -> int:
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        with self.transaction() as c:
            cur = c.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values()))
            return cur.lastrowid

    def update(self, table: str, key: str, key_value: Any, values: dict) -> None:
        sets = ", ".join(f"{k} = ?" for k in values)
        with self.transaction() as c:
            c.execute(f"UPDATE {table} SET {sets} WHERE {key} = ?", (*values.values(), key_value))

    # -- migrations ---------------------------------------------------------
    def migrate(self) -> list[str]:
        if not self.migrations_dir:
            raise RuntimeError("migrations_dir not configured")
        scripts = sorted(self.migrations_dir.glob("*.sql")) if self.migrations_dir.is_dir() else []
        if not scripts:
            # Never report "up to date" against an empty schema.
            raise RuntimeError(f"no migration scripts found in {self.migrations_dir}")
        self.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied = {r["version"] for r in self.query("SELECT version FROM schema_migrations")}
        done = []
        for path in scripts:
            version = path.stem
            if version in applied:
                continue
            sql = path.read_text(encoding="utf-8")
            with self._write_lock:
                conn = self.conn
                # executescript commits implicitly; wrap in explicit transaction.
                conn.executescript(
                    "BEGIN IMMEDIATE;\n" + sql
                    + f"\nINSERT INTO schema_migrations VALUES ('{version}', '{now_iso()}');\nCOMMIT;"
                )
            done.append(version)
        return done

    def integrity_check(self) -> tuple[bool, str]:
        res = self.scalar("PRAGMA integrity_check")
        fk = self.query("PRAGMA foreign_key_check")
        ok = res == "ok" and not fk
        detail = res if not fk else f"{res}; foreign key violations: {len(fk)}"
        return ok, detail

    # -- key/value ----------------------------------------------------------
    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM kv WHERE key = ?", (key,))
        return loads(row["value"], default) if row else default

    def kv_set(self, key: str, value: Any) -> None:
        with self.transaction() as c:
            c.execute(
                "INSERT INTO kv(key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, dumps(value), now_iso()),
            )
