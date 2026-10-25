"""v0.11.0: what each drive holds, and relearning what a lost drive held, the most important first."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from polymath.core.scheduler import Scheduler
from polymath.memory import inventory as inv
from polymath.memory import pool as storage_pool
from polymath.memory.documents import Document, DocumentStore
from tests.test_storage import GB, Drives, ctx_for

WALRUS = "The walrus is a large marine mammal with tusks and whiskers. " * 20


def spill(db: Any, ids: list[int], vol: str) -> None:
    for i in ids:
        db.execute("UPDATE documents SET codec = 'spilled', body = NULL, meta = json_set(meta, '$.spill', json(?)) "
                   "WHERE id = ?", (json.dumps({"vol": vol, "pack": "pack-000001.bin", "off": 0, "len": 1}), i))  # fmt: skip


def volume(db: Any, vid: str, *, online: bool, last_seen: float, label: str = "") -> None:
    db.execute(
        "INSERT OR REPLACE INTO volumes(id, label, fstype, model, size_bytes, budget_bytes, dedicated, path, online, "
        "used_bytes, first_seen, last_seen) VALUES(?,?,?,?,?,?,1,?,?,0,?,?)",
        (vid, label or vid.upper(), "ext4", "SSD", 100 * GB, 50 * GB, f"/mnt/{vid}", int(online), last_seen - 10,
         last_seen),
    )  # fmt: skip


class Crawler:
    def __init__(self) -> None:
        self.added: list[str] = []
        self.frontier = self

    def add(self, url: str, priority: float = 0.0, depth: int = 0) -> None:
        self.added.append(url)


@pytest.fixture()
def lib(config, db):
    """Six documents on drive 'usb1': three Wikipedia articles (one with facts resting on it), two web pages and a
    Gutenberg book; one Wikipedia article stays on the brain's disk."""
    store = DocumentStore(db)
    ids = {}
    for key, source, title, url in (
        ("walrus", "wikipedia", "Walrus", "https://en.wikipedia.org/wiki/Walrus"),
        ("seal", "wikipedia", "Seal", "https://en.wikipedia.org/wiki/Seal"),
        ("orca", "wikipedia", "Orca", "https://en.wikipedia.org/wiki/Orca"),
        ("blog", "web", "Seal blog", "https://example.org/seals"),
        ("news", "feed", "Walrus news", "https://news.example.org/walrus"),
        ("book", "gutenberg", "Moby Dick", "https://www.gutenberg.org/ebooks/2701"),
        ("home", "wikipedia", "Penguin", "https://en.wikipedia.org/wiki/Penguin"),
    ):
        ids[key], _ = store.add(Document(source, key, title, f"{title}. " + WALRUS, "CC", url=url, lang="en"))
    spill(db, [ids[k] for k in ("walrus", "seal", "orca", "blog", "news", "book")], "usb1")
    from polymath.memory.graph import KnowledgeGraph

    g = KnowledgeGraph(db)
    a, b = g.upsert_entity("Q1", "Walrus"), g.upsert_entity("Q2", "Mammal")
    for n in range(3):
        g.add_triple(a, g.predicate(f"P{n}"), o=b, kind="pattern", source="text", doc_id=ids["walrus"])
    g.add_triple(a, g.predicate("P9"), o=b, kind="pattern", source="text", doc_id=ids["orca"])
    db.execute("INSERT INTO topics(name, n_docs, n_total) VALUES('Seals', 1, 1)")
    tid = int(db.scalar("SELECT id FROM topics WHERE name = 'Seals'"))
    db.execute("INSERT INTO doc_topics(doc_id, topic_id) VALUES(?, ?)", (ids["seal"], tid))
    return ids, tid


# ------------------------------------------------------------------ the inventory
def test_it_knows_what_each_drive_holds(lib, config, db, tmp_path):
    got = inv.volume_inventory(db, "usb1")
    assert got["documents"] == 6 and got["lost"] == 0 and got["facts"] == 4
    assert got["sources"] == {"wikipedia": 3, "web": 1, "feed": 1, "gutenberg": 1} and got["topics"] == ["Seals"]
    assert inv.volume_inventory(db, "nothing") == {"documents": 0, "chars": 0, "lost": 0, "sources": {}, "topics": [],
                                                   "facts": 0}  # fmt: skip
    drives = Drives(config)
    brain = drives.plug("usb1", label="T7")
    (brain / "raw" / "x").mkdir(parents=True)
    (brain / "raw" / "x" / "part.bz2").write_bytes(b"r" * 500)
    (brain / "backups").mkdir()
    (brain / "backups" / "polymath-20261025-030000.sqlite3.xz").write_bytes(b"b" * 200)
    (brain / "home-previous-17").mkdir()
    drives.pool()
    volume(db, "usb1", online=True, last_seen=time.time(), label="T7")
    volume(db, "gone", online=False, last_seen=time.time() - 60)
    out = inv.inventory_job(ctx_for(config, db, "storage.inventory"))
    assert out.result == {"drives": 2, "documents_on_drives": 6}
    kv = db.kv_get("inventory")["drives"]
    assert kv["usb1"]["online"] and kv["usb1"]["downloads"] == {"files": 1, "bytes": 500}
    assert kv["usb1"]["newest_backup"] == "polymath-20261025-030000.sqlite3.xz"
    assert kv["usb1"]["previous_brain"] == ["home-previous-17"] and not kv["gone"]["online"]
    on_drive = json.loads((brain / inv.INVENTORY_FILE).read_text())  # the drive says what it holds, anywhere
    assert on_drive["drive"] == "usb1" and on_drive["documents"] == 6 and on_drive["facts"] == 4


# ------------------------------------------------------------------ a drive lost by accident
def test_a_drive_gone_for_two_hours_is_relearned_the_most_important_first(lib, config, db, monkeypatch):
    ids, tid = lib
    now = time.time()
    db.kv_set("user_interests", {str(tid): now + 86400})  # Christian asked about seals
    volume(db, "usb1", online=False, last_seen=now - 3600)  # gone an hour: maybe a replug, wait
    ctx = ctx_for(config, db, "storage.recover")
    ctx.services["crawler"] = Crawler()
    assert inv.recover_job(ctx).result["lost"] == {}
    volume(db, "usb1", online=False, last_seen=now - 3 * 3600)
    (config.paths.run_dir).mkdir(parents=True, exist_ok=True)
    (config.paths.run_dir / "drive-roles.json").write_text(json.dumps({"backup": "usb1"}))
    out = inv.recover_job(ctx)
    assert out.result["lost"] == {"usb1": 6}
    order = [int(r["doc_id"]) for r in db.query("SELECT doc_id FROM relearn ORDER BY priority DESC")]
    assert order[:3] == [ids["seal"], ids["walrus"], ids["orca"]]  # your interest, then what most facts rest on
    how = {int(r["doc_id"]): str(r["how"]) for r in db.query("SELECT doc_id, how FROM relearn")}
    assert how[ids["book"]] == "later" and how[ids["blog"]] == how[ids["news"]] == "web"
    assert ids["home"] not in how  # still on the brain's disk: nothing lost
    jobs = {str(r["kind"]): json.loads(r["payload"]) for r in db.query("SELECT kind, payload FROM jobs")}
    assert sorted(jobs["wikipedia.titles"]["titles"]) == ["Orca", "Seal", "Walrus"]
    assert "relearning what was on USB1" in jobs["wikipedia.titles"]["about"]
    assert sorted(ctx.services["crawler"].added) == ["https://example.org/seals", "https://news.example.org/walrus"]
    assert {"crawl.step", "body.backup", "sources.plan"} <= set(jobs)  # a new backup, the downloads planned again
    ev = db.one("SELECT event, detail FROM volume_events ORDER BY id DESC LIMIT 1")
    detail = json.loads(ev["detail"])
    assert ev["event"] == "lost" and detail["documents"] == 6 and detail["facts"] == 4 and detail["relearn"] == 5
    found = [i for i in __import__("polymath.core.issues", fromlist=["x"]).recent(db, 0) if "relearn:lost" in i["code"]]
    assert found and "most important first" in found[0]["text"]
    st = inv.relearn_status(db)
    assert st == {"waiting": 0, "queued": 5, "done": 0, "back": 0, "later": 1}
    assert inv.recover_job(ctx).result["lost"] == {}  # once


def test_a_relearned_document_replaces_the_lost_body_and_counts_as_back(lib, config, db):
    ids, _tid = lib
    now = time.time()
    volume(db, "usb1", online=False, last_seen=now - 3 * 3600)
    ctx = ctx_for(config, db, "storage.recover")
    inv.recover_job(ctx)
    store = DocumentStore(db)
    same = store.add(Document("wikipedia", "walrus", "Walrus", "Walrus. " + WALRUS, "CC", lang="en"))
    assert same == (ids["walrus"], "updated")
    row = db.one("SELECT codec, state, meta FROM documents WHERE id = ?", (ids["walrus"],))
    assert row["codec"] == "lzma" and "lost" not in json.loads(row["meta"])
    got = store.get(ids["walrus"])
    assert got is not None and got.text.startswith("Walrus.")  # readable again
    changed = store.add(Document("wikipedia", "seal", "Seal", "Seal, revised. " + WALRUS, "CC", lang="en"))
    assert changed == (ids["seal"], "updated")
    assert db.one("SELECT state FROM documents WHERE id = ?", (ids["seal"],))["state"] == "new"  # read again
    assert db.scalar("SELECT COUNT(*) FROM chunks WHERE doc_id = ?", (ids["seal"],)) == 0
    inv.recover_job(ctx)
    st = inv.relearn_status(db)
    assert st["done"] == 2 and st["queued"] == 1  # Orca still on its way
    assert st["waiting"] == 2  # the web pages wait for the crawler (none in this test)


def test_a_lost_drive_that_comes_back_is_readable_again_and_relearning_stops(lib, config, db):
    _ids, _tid = lib
    now = time.time()
    volume(db, "usb1", online=False, last_seen=now - 3 * 3600)
    ctx = ctx_for(config, db, "storage.recover")
    inv.recover_job(ctx)
    volume(db, "usb1", online=True, last_seen=now)
    out = inv.recover_job(ctx)
    assert out.result["back"] == {"usb1": 6}
    assert db.scalar("SELECT COUNT(*) FROM documents WHERE json_extract(meta, '$.lost') IS NOT NULL") == 0
    assert inv.relearn_status(db)["back"] == 6
    assert db.one("SELECT event FROM volume_events ORDER BY id DESC LIMIT 1")["event"] == "back"


def test_what_waits_too_long_comes_back_later_and_the_daily_cap_holds(lib, config, db, monkeypatch):
    now = time.time()
    volume(db, "usb1", online=False, last_seen=now - 3 * 3600)
    ctx = ctx_for(config, db, "storage.recover")
    monkeypatch.setattr(inv, "PER_DAY", 2)
    inv.recover_job(ctx)
    assert inv.relearn_status(db)["queued"] == 2  # two a day here
    db.execute("UPDATE relearn SET queued = ? WHERE state = 'queued'", (now - inv.QUEUED_STALE_S - 60,))
    inv.recover_job(ctx)
    assert db.scalar("SELECT COUNT(*) FROM relearn WHERE how = 'later'") == 3  # the two stale ones and the book


def test_never_in_sd_card_mode_or_on_standby(config, db):
    config.storage.sd_only = True
    assert "left alone" in inv.recover_job(ctx_for(config, db, "storage.recover")).result["skipped"]
    agent = type("A", (), {"config": config, "scheduler": Scheduler(db)})()
    inv.planner(agent)
    assert db.scalar("SELECT COUNT(*) FROM jobs") == 1  # only the job queued above
    config.storage.sd_only = False
    inv.planner(agent)
    assert {r["kind"] for r in db.query("SELECT kind FROM jobs")} >= {"storage.inventory", "storage.recover"}


# ------------------------------------------------------------------ everyone sees it
def test_the_feed_doctor_and_dashboard_say_what_was_lost_and_how_much_is_back(lib, config, db):
    from polymath.interface import doctor
    from polymath.interface.dashboard import DashboardData
    from polymath.interface.feed import Feed, render_event

    now = time.time()
    volume(db, "usb1", online=False, last_seen=now - 3 * 3600, label="T7")
    inv.recover_job(ctx_for(config, db, "storage.recover"))
    events = [e for e in Feed(db).poll(None)["events"] if e.get("kind") == "storage"]
    text = "".join(t for t, _s in render_event(events[-1]))
    assert "drive T7 is still gone: it held 6 documents (4 facts rest on them); relearning 5" in text
    volume(db, "usb1", online=True, last_seen=now, label="T7")
    inv.recover_job(ctx_for(config, db, "storage.recover"))
    events = [e for e in Feed(db).poll(None)["events"] if e.get("kind") == "storage"]
    assert "drive T7 is back: its 6 documents are readable again" in "".join(t for t, _s in render_event(events[-1]))
    db.execute("UPDATE relearn SET state = 'queued'")
    checks = [c for c in doctor.run_checks(config, "http://x", health=lambda u: ("refused", 0.0, None))]
    assert not [c for c in checks if c.name == "relearning"]  # no live snapshot: nothing claimed
    st = {"relearn": inv.relearn_status(db)}
    from polymath.interface import snapshot

    path = snapshot.path_for(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "written": time.time(), "seq": 0, "status": st, "events": []}))
    checks = [c for c in doctor.run_checks(config, "http://x", health=lambda u: ("refused", 0.0, None))]
    assert any(c.name == "relearning" and "to go (the most important first)" in c.detail for c in checks)
    config.paths.data_dir.mkdir(parents=True, exist_ok=True)
    assert any("A drive was lost" in x for x in DashboardData(config).storage(db)["lines"])


def test_pool_files_on_a_drive(tmp_path: Path):
    brain = tmp_path / storage_pool.BRAIN
    (brain / "bodies").mkdir(parents=True)
    (brain / "bodies" / "pack-000001.bin").write_bytes(b"p" * 100)
    got = inv.files_on(brain)
    assert got["bodies"] == {"files": 1, "bytes": 100} and got["downloads"] == {"files": 0, "bytes": 0}
    assert got["newest_backup"] == "" and got["previous_brain"] == []
