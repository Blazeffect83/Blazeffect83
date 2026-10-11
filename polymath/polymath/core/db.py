"""SQLite access: WAL mode, numbered migrations, nested transactions, kv store.

Power-loss safety comes from WAL journaling (a torn write can never corrupt
the database; at worst the last committed transactions are rolled back) and
from every job being idempotent so a rolled-back slice is simply redone.
"""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3
import threading
import time
from collections.abc import Iterator, Sequence
from importlib import resources
from pathlib import Path
from typing import Any

Params = Sequence[Any] | dict[str, Any]
_MIGRATION_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


class Database:
    """A single SQLite connection guarded by a re-entrant lock.

    The agent process owns one writable instance. The dashboard opens its own
    read-only instance (``readonly=True``); WAL lets it read while the agent
    writes.
    """

    def __init__(self, path: Path | str, *, readonly: bool = False, busy_timeout_ms: int = 10_000) -> None:
        self.path = Path(path)
        self.readonly = readonly
        if readonly:
            if not self.path.exists():
                raise FileNotFoundError(self.path)
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, isolation_level=None, check_same_thread=False)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        self._conn: sqlite3.Connection | None = conn
        self._lock = threading.RLock()
        self._depth = 0
        conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        if not readonly:
            mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(mode).lower() != "wal":  # pragma: no cover - only on exotic filesystems
                raise RuntimeError(f"could not enable WAL mode (got {mode})")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-32000")
        conn.execute("PRAGMA mmap_size=268435456")

    # ------------------------------------------------------------------ basics
    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise sqlite3.ProgrammingError("database is closed")
        return self._conn

    @property
    def is_open(self) -> bool:
        return self._conn is not None

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                if not self.readonly:
                    with contextlib.suppress(sqlite3.Error):
                        self._conn.execute("PRAGMA optimize")
                self._conn.close()
                self._conn = None

    def execute(self, sql: str, params: Params = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, params)

    def executemany(self, sql: str, rows: Sequence[Params]) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.executemany(sql, rows)

    def query(self, sql: str, params: Params = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.conn.execute(sql, params).fetchall())

    def one(self, sql: str, params: Params = ()) -> sqlite3.Row | None:
        with self._lock:
            row: sqlite3.Row | None = self.conn.execute(sql, params).fetchone()
            return row

    def scalar(self, sql: str, params: Params = (), default: Any = None) -> Any:
        row = self.one(sql, params)
        return default if row is None or row[0] is None else row[0]

    @property
    def in_transaction(self) -> bool:
        return self._depth > 0

    @contextlib.contextmanager
    def transaction(self, immediate: bool = True) -> Iterator[Database]:
        """Atomic block; nests via savepoints. Holds the lock for its duration."""
        with self._lock:
            if self._depth == 0:
                self.conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                name = None
            else:
                name = f"sp{self._depth}"
                self.conn.execute(f"SAVEPOINT {name}")
            self._depth += 1
            try:
                yield self
            except BaseException:
                self._depth -= 1
                if name is None:
                    self.conn.execute("ROLLBACK")
                else:
                    self.conn.execute(f"ROLLBACK TO {name}")
                    self.conn.execute(f"RELEASE {name}")
                raise
            self._depth -= 1
            if name is None:
                self.conn.execute("COMMIT")
            else:
                self.conn.execute(f"RELEASE {name}")

    # -------------------------------------------------------------- migrations
    def schema_version(self) -> int:
        return int(self.scalar("PRAGMA user_version", default=0))

    def migrate(self) -> list[str]:
        """Apply pending ``core/migrations/NNNN_name.sql`` files in order."""
        scripts = sorted(
            (int(m.group(1)), entry.name, entry.read_text(encoding="utf-8"))
            for entry in resources.files("polymath.core").joinpath("migrations").iterdir()
            if (m := _MIGRATION_RE.match(entry.name))
        )
        if not scripts:
            raise RuntimeError("no migration scripts found; the package is incomplete")
        applied: list[str] = []
        current = self.schema_version()
        for number, name, sql in scripts:
            if number <= current:
                continue
            with self._lock:
                # executescript commits implicitly, so the version bump is part of the script.
                self.conn.executescript(f"BEGIN IMMEDIATE;\n{sql}\nPRAGMA user_version={number};\nCOMMIT;")
            applied.append(name)
        return applied

    # ---------------------------------------------------------------------- kv
    def kv_get(self, key: str, default: Any = None) -> Any:
        raw = self.scalar("SELECT value FROM kv WHERE key=?", (key,))
        return default if raw is None else json.loads(raw)

    def kv_set(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO kv(key, value, updated) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
            (key, json.dumps(value), time.time()),
        )


def open_database(path: Path | str) -> Database:
    db = Database(path)
    db.migrate()
    return db
