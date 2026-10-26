"""The hourly mirror (body.mirror): only changed chunks, a consistent snapshot, never a torn copy."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from polymath.body import mirror as mr
from polymath.core.app import build_agent


def make_db(path: Path, rows: int) -> None:
    con = sqlite3.connect(str(path))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
    con.executemany("INSERT INTO t(v) VALUES(?)", [("x" * 500,) for _ in range(rows)])
    con.commit()
    con.close()


def rows_in(path: Path) -> int:
    con = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    try:
        assert con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        return int(con.execute("SELECT COUNT(*) FROM t").fetchone()[0])
    finally:
        con.close()


def test_only_changed_chunks_are_written_and_the_copy_is_whole(tmp_path):
    src, out = tmp_path / "brain.sqlite3", tmp_path / "drive" / "mirror"
    make_db(src, 8000)  # about 4.5 MiB
    first = mr.copy_changed(src, out)
    assert first.ok and first.changed == first.chunks >= 4 and rows_in(out / mr.MIRROR_FILE) == 8000
    con = sqlite3.connect(str(src))
    con.execute("UPDATE t SET v = 'y' WHERE id = 1")
    con.commit()
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    second = mr.copy_changed(src, out)
    assert second.ok and 1 <= second.changed < second.chunks  # only what changed
    state = mr.read_state(out)
    assert state["complete"] and state["size"] == src.stat().st_size and len(state["hashes"]) == second.chunks
    assert (out / mr.MIRROR_FILE).read_bytes() == src.read_bytes()
    assert mr.copy_changed(src, out).changed == 0
    con = sqlite3.connect(str(src), isolation_level=None)
    con.execute("DELETE FROM t WHERE id > 100")
    con.execute("VACUUM")
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # it shrinks: the mirror shrinks with it
    con.close()
    assert mr.copy_changed(src, out).ok and (out / mr.MIRROR_FILE).read_bytes() == src.read_bytes()


def test_a_mirror_cut_short_is_never_taken_for_a_whole_one(tmp_path, monkeypatch):
    src, out, db = tmp_path / "brain.sqlite3", tmp_path / "mirror", tmp_path / "restored" / "polymath.sqlite3"
    db.parent.mkdir()
    make_db(src, 3000)
    mr.copy_changed(src, out)
    con = sqlite3.connect(str(src))
    con.execute("UPDATE t SET v = 'z'")
    con.commit()
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()

    def power_cut(fd: int) -> None:
        raise OSError("power cut")

    monkeypatch.setattr("polymath.body.mirror.os.fsync", power_cut)
    with pytest.raises(OSError):
        mr.copy_changed(src, out)
    monkeypatch.undo()
    assert mr.read_state(out)["complete"] is False
    with pytest.raises(RuntimeError, match="not a whole copy"):
        mr.restore_mirror(out, db)
    assert mr.mirrors_on(tmp_path) == []
    mr.copy_changed(src, out)  # the next run starts over: every chunk compared afresh
    db.write_bytes(b"old brain")
    old = mr.restore_mirror(out, db)
    assert old.read_bytes() == b"old brain" and rows_in(db) == 3000
    (out / mr.MIRROR_FILE).write_bytes(b"SQLite format 3\x00" + b"\x01" * 5000)
    with pytest.raises((RuntimeError, sqlite3.DatabaseError)):
        mr.restore_mirror(out, db)


class FakeDB:
    """The agent's connection as the mirror uses it."""

    def __init__(self, path: Path) -> None:
        self.con = sqlite3.connect(str(path), isolation_level=None)
        self.kv: dict[str, Any] = {}

    def one(self, sql: str) -> Any:
        return self.con.execute(sql).fetchone()

    def kv_set(self, k: str, v: Any) -> None:
        self.kv[k] = v

    def transaction(self) -> Any:
        import contextlib

        return contextlib.nullcontext(self)


def mirror_for(config: Any, tmp_path: Path, rows: int = 5000) -> tuple[mr.Mirror, Path, FakeDB]:
    db_path = Path(config.paths.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    make_db(db_path, rows)
    fake = FakeDB(db_path)
    m = mr.Mirror(config, fake)
    out = tmp_path / "drive" / "polymath-brain" / "backups" / mr.MIRROR_DIR
    m.target = lambda: out  # type: ignore[method-assign]
    return m, out, fake


def test_the_snapshot_holds_while_the_agent_keeps_writing(config, tmp_path, monkeypatch):
    m, out, fake = mirror_for(config, tmp_path)
    real = mr.copy_changed

    def slow(src: Path, dst: Path, tick: Any = None) -> mr.MirrorResult:
        writer = sqlite3.connect(str(src))  # the agent goes on learning while the mirror is copied
        writer.executemany("INSERT INTO t(v) VALUES(?)", [("new",) for _ in range(2000)])
        writer.commit()
        busy, _log, _done = writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        assert busy == 1  # the mirror's reader keeps the new pages out of the file
        writer.close()
        return real(src, dst)

    monkeypatch.setattr(mr, "copy_changed", slow)
    assert m.maybe_start() == "started"
    m.thread.join(30)  # type: ignore[union-attr]
    assert m.last is not None and m.last.ok and rows_in(out / mr.MIRROR_FILE) == 5000  # exactly the snapshot
    m.record()
    assert fake.kv["mirror"]["ok"] and fake.kv["mirror"]["changed"] >= 1
    assert m.maybe_start() == "not due"


def test_it_waits_for_a_free_log_skips_an_unchanged_brain_and_needs_a_drive(config, tmp_path):
    m, out, _fake = mirror_for(config, tmp_path)
    reader = sqlite3.connect(str(config.paths.db_path))
    w = sqlite3.connect(str(config.paths.db_path))
    w.execute("INSERT INTO t(v) VALUES('a')")
    w.commit()
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM t").fetchone()  # an old snapshot pins the log
    w.execute("INSERT INTO t(v) VALUES('b')")
    w.commit()
    assert m.maybe_start(wait=True) == "log busy" and m.next_at > time.time()
    reader.rollback()
    reader.close()
    w.close()
    m.next_at = 0.0
    assert m.maybe_start(wait=True) == "mirrored" and rows_in(out / mr.MIRROR_FILE) == 5002
    m.next_at = 0.0
    assert m.maybe_start(wait=True) == "unchanged"
    m.target = lambda: None  # type: ignore[method-assign]
    m.next_at = 0.0
    assert m.maybe_start() == "no drive for it"
    config.body.mirror_every_min = 0
    m.next_at = 0.0
    assert m.maybe_start() == "not due"


def test_no_room_is_said_and_a_failure_reported(config, tmp_path, monkeypatch):
    from collections import namedtuple

    m, _out, fake = mirror_for(config, tmp_path)
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr("polymath.body.mirror.shutil.disk_usage", lambda _p: usage(100, 99, 1))
    assert m.maybe_start() == "no room"
    reported: list[str] = []
    monkeypatch.setattr("polymath.core.issues.report", lambda db, area, code, text, **k: reported.append(code))
    m.record()
    assert fake.kv["mirror"]["ok"] is False and reported == ["mirror_failed"]


def test_where_the_mirror_goes(config, tmp_path, monkeypatch):
    from polymath.memory import pool as storage_pool

    m = mr.Mirror(config, None)
    assert m.target() is None  # no pool
    config.paths.db_path.parent.mkdir(parents=True, exist_ok=True)
    config.paths.db_path.write_bytes(b"x")

    class Pool:
        def __init__(self, base: Path) -> None:
            self.base = base

        def backup_dir(self, need: int = 0) -> Path:
            return self.base

    monkeypatch.setattr(storage_pool, "for_config", lambda cfg: Pool(config.paths.backup_dir))
    assert m.target() is None  # backups next to the database: a mirror there dies with it
    monkeypatch.setattr(storage_pool, "for_config", lambda cfg: Pool(tmp_path / "elsewhere" / "backups"))
    monkeypatch.setattr(mr, "_same_disk", lambda a, b: False)
    assert m.target() == tmp_path / "elsewhere" / "backups" / mr.MIRROR_DIR
    config.storage.sd_only = True
    assert m.target() is None


def test_the_loop_survives_a_broken_mirror(config, monkeypatch):
    agent = build_agent(config, planners=False)
    agent.start()

    def boom(**_k: Any) -> str:
        raise RuntimeError("mirror broke")

    monkeypatch.setattr(agent.mirror, "maybe_start", boom)
    agent.cycle()
    assert agent.mirror.next_at > time.time()
    agent.shutdown()
    agent.db.close()
    assert json.dumps(mr.MirrorResult(True, "ok").as_dict())
