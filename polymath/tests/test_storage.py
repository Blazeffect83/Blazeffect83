"""Storage pool: drives plugged in become brain space — discovery, body packs, spill/recall, placement, the
root helper's safety rules (with a fake blkid/lsblk/mount), CLI and live feed."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from polymath.body import maintenance as mt
from polymath.body import volumes as vols
from polymath.body.guard import Guard
from polymath.body.sensors import Vitals
from polymath.core.jobs import JobContext
from polymath.core.scheduler import Job, Scheduler
from polymath.interface import cli
from polymath.interface.feed import Feed, render_event, status_lines
from polymath.memory import pool as storage_pool
from polymath.memory.documents import Document, DocumentStore
from polymath.memory.text_index import TextIndex
from tests.fixtures.typing import some

GB = 1_000_000_000


def text_of(segs: list[tuple[str, str]]) -> str:
    return "".join(t for t, _ in segs)


class Drives:
    """Fake mounted drives under the config's mount_root (ismount answers from a set)."""

    def __init__(self, config: Any) -> None:
        self.config = config
        config.storage.mount_root = config.paths.data_dir.parent / "mnt"
        config.storage.reserve_fraction = 0.0
        self.mounted: set[str] = set()

    def plug(self, vol: str, *, budget: int = 50 * GB, label: str = "", **extra: Any) -> Path:
        mnt = self.config.storage.mount_root / vol
        brain = mnt / storage_pool.BRAIN
        brain.mkdir(parents=True, exist_ok=True)
        manifest = {"id": vol, "label": label, "fstype": "ext4", "model": "Test SSD", "size_bytes": 2 * budget,
                    "budget_bytes": budget, "dedicated": True, **extra}  # fmt: skip
        (brain / storage_pool.MANIFEST).write_text(json.dumps(manifest))
        self.mounted.add(str(mnt))
        return Path(brain)

    def unplug(self, vol: str) -> None:
        self.mounted.discard(str(self.config.storage.mount_root / vol))

    def pool(self) -> storage_pool.StoragePool:
        return storage_pool.activate(self.config, ismount=lambda p: p in self.mounted)


def ctx_for(config: Any, db: Any, kind: str, payload: dict[str, Any] | None = None) -> JobContext:
    s = Scheduler(db)
    jid, _ = s.enqueue(kind, payload or {}, key=f"{kind}:{time.time_ns()}")
    return JobContext(config=config, db=db, scheduler=s, job=Job(jid, kind, f"k{jid}", payload or {}, None, 0, 0, 0, 5, 0),
                      deadline=time.monotonic() + 30, stop_event=threading.Event(), services={}, on_tick=lambda: None)  # fmt: skip


def add_docs(db: Any, n: int, source: str = "web", perceived: bool = True) -> list[int]:
    store, text = DocumentStore(db), TextIndex(db)
    ids = []
    for i in range(n):
        body = f"Document {source} {i} tells about the walrus number {i}. " + "Long filler text here. " * 60
        did, _ = store.add(Document(source, f"{source}{i}", f"Title {source} {i}", body, "CC"))
        text.index(did, f"Title {source} {i}", body)
        ids.append(did)
    if perceived:
        db.execute("UPDATE documents SET state='perceived' WHERE state='new'")
    return ids


# ------------------------------------------------------------------ discovery and packs
def test_only_mounted_adopted_drives_count(config):
    d = Drives(config)
    d.plug("aaaa-1111", label="T7")
    (config.storage.mount_root / "not-mounted" / storage_pool.BRAIN).mkdir(parents=True)
    d.plug("retired-1", retired=True)
    junk = config.storage.mount_root / "junk"
    (junk / storage_pool.BRAIN).mkdir(parents=True)
    (junk / storage_pool.BRAIN / storage_pool.MANIFEST).write_text("{not json")
    d.mounted.add(str(junk))
    pool = d.pool()
    assert [v.id for v in pool.volumes()] == ["aaaa-1111"] and some(pool.find("aaaa-1111")).name == "T7"
    d.unplug("aaaa-1111")
    assert pool.volumes() != [] and pool.volumes(fresh=True) == []  # scans are cached briefly
    assert pool.totals()["brain_bytes"] == int(config.body.disk_budget_gb * GB)


def test_body_packs_round_trip_and_detect_damage(config, monkeypatch):
    d = Drives(config)
    brain = d.plug("vol1")
    pool = d.pool()
    v = some(pool.find("vol1"))
    monkeypatch.setattr(storage_pool, "PACK_LIMIT", 60)
    refs = [pool.write_body(v, i, f"blob-{i}".encode() * 5, "lzma") for i in range(1, 4)]
    assert len({r["pack"] for r in refs}) == 3  # small limit: a new pack per record
    assert pool.read_body(refs[1], 2) == b"blob-2" * 5
    assert pool.read_body(refs[1], 99) is None  # wrong document id
    pack = brain / "bodies" / refs[2]["pack"]
    data = bytearray(pack.read_bytes())
    data[-1] ^= 0xFF
    pack.write_bytes(bytes(data))
    assert pool.read_body(refs[2], 3) is None  # crc mismatch
    assert pool.read_body({"vol": "gone", "pack": "x", "off": 0, "len": 1}, 1) is None
    assert pool.read_body(refs[0] | {"pack": "missing.bin"}, 1) is None
    assert pool.used(v) > 0 and pool.room(v) < v.budget_bytes


def test_choose_prefers_the_roomiest_drive_and_respects_need(config):
    d = Drives(config)
    d.plug("small", budget=2 * GB)
    d.plug("big", budget=40 * GB)
    pool = d.pool()
    assert some(pool.choose()).id == "big"
    assert pool.choose(100 * GB) is None


# ------------------------------------------------------------------ documents on drives
def test_spill_moves_bodies_reads_transparently_and_waits_for_unplugged_drives(config, db):
    d = Drives(config)
    d.plug("vol1")
    d.pool()
    ids = add_docs(db, 6) + add_docs(db, 2, source="wikipedia")
    fresh = add_docs(db, 1, source="feed", perceived=False)[0]
    before = {i: some(DocumentStore(db).get(i)).text for i in ids}
    config.body.disk_budget_gb = 1e-6  # the main disk is "full"
    out = mt.spill_job(ctx_for(config, db, "body.spill"))
    codecs = {int(r["id"]): r["codec"] for r in db.query("SELECT id, codec FROM documents")}
    assert out.result["moved"] == 8 and all(codecs[i] == "spilled" for i in ids)
    assert codecs[fresh] == "lzma"  # not perceived yet: stays on the main disk
    store = DocumentStore(db)
    assert {i: some(store.get(i)).text for i in ids} == before  # reads straight from the drive
    assert TextIndex(db).search("walrus number 3")[0].text  # passages still come back
    assert db.scalar("SELECT COUNT(*) FROM documents WHERE body IS NOT NULL AND codec='spilled'") == 0
    d.unplug("vol1")
    storage_pool.active()._scan_at = -1e18  # type: ignore[union-attr]
    gone = some(store.get(ids[0]))
    assert gone.text == "" and gone.meta["unavailable"] is True and gone.meta["spill"]["vol"] == "vol1"
    hits = [h for h in TextIndex(db).search("walrus number 3") if h.doc_id in ids]
    assert hits and all(h.text == "" for h in hits)  # passages wait for the drive; nothing breaks
    # an update to a document whose old text is away is deferred, never half-applied
    doc_id, status = store.add(Document("web", "web0", "Changed", "New text entirely " * 20, "CC"))
    assert (doc_id, status) == (ids[0], "unchanged")
    d.plug("vol1")
    storage_pool.active()._scan_at = -1e18  # type: ignore[union-attr]
    assert some(store.get(ids[0])).text == before[ids[0]]


def test_spill_stops_when_every_drive_is_full_and_eviction_spills_first(config, db):
    d = Drives(config)
    d.plug("tiny", budget=800)
    d.pool()
    ids = add_docs(db, 10)
    config.body.disk_budget_gb = 1e-6
    out = mt.spill_job(ctx_for(config, db, "body.spill"))
    assert 0 < out.result["moved"] < 10 and out.result.get("drives_full") and out.done
    # eviction tries the drives first, then evicts what cannot move
    out = mt.evict_job(ctx_for(config, db, "body.evict"))
    assert "spilled_to_drives" in out.result and out.result["documents_evicted"] > 0
    codecs = [r["codec"] for r in db.query("SELECT codec FROM documents")]
    assert "spilled" in codecs and "evicted" in codecs
    assert all(some(DocumentStore(db).get(i)) for i in ids)


def test_spill_without_drives_is_a_no_op(config, db):
    add_docs(db, 3)
    config.body.disk_budget_gb = 1e-6
    assert mt.spill_job(ctx_for(config, db, "body.spill")).result["moved"] == 0
    config.body.disk_budget_gb = 200
    assert mt.spill_job(ctx_for(config, db, "body.spill")).result["moved"] == 0  # nothing to do


def test_retire_brings_bodies_back_and_never_readopts(config, db):
    d = Drives(config)
    brain = d.plug("vol1", label="Old HDD")
    pool = d.pool()
    ids = add_docs(db, 5)
    config.body.disk_budget_gb = 1e-6
    mt.spill_job(ctx_for(config, db, "body.spill"))
    texts = {i: some(DocumentStore(db).get(i)).text for i in ids}
    storage_pool.sync(db, pool)
    config.body.disk_budget_gb = 200  # room again on the main disk
    out = mt.recall_job(ctx_for(config, db, "body.recall", {"volume": "vol1", "retire": True}))
    assert out.done and out.result["recalled"] == 5 and out.result["retired"]
    assert db.scalar("SELECT COUNT(*) FROM documents WHERE codec='spilled'") == 0
    assert {i: some(DocumentStore(db).get(i)).text for i in ids} == texts
    assert not brain.exists() and "vol1" in storage_pool.ignored_ids(config)
    ev = db.one("SELECT event, detail FROM volume_events ORDER BY id DESC LIMIT 1")
    assert ev["event"] == "retired" and json.loads(ev["detail"])["recalled"] == 5
    with pytest.raises(Exception, match="not plugged in"):
        mt.recall_job(ctx_for(config, db, "body.recall", {"volume": "vol1"}))


def test_recall_without_room_evicts_what_does_not_fit(config, db):
    d = Drives(config)
    d.plug("vol1")
    d.pool()
    add_docs(db, 4)
    config.body.disk_budget_gb = 1e-6
    mt.spill_job(ctx_for(config, db, "body.spill"))
    out = mt.recall_job(ctx_for(config, db, "body.recall", {"volume": "vol1"}))  # still no room
    assert out.result["evicted"] == 4 and out.result["recalled"] == 0 and "retired" not in out.result
    assert not TextIndex(db).search("walrus")  # evicted bodies leave the index


# ------------------------------------------------------------------ placement of files
def test_downloads_and_backups_use_drives(config, db):
    d = Drives(config)
    brain = d.plug("vol1")
    pool = d.pool()
    old = config.paths.raw_dir / "a" / "kept.bz2"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"x")
    assert pool.raw_path("a/kept.bz2") == old.resolve()  # existing files stay where they are
    assert pool.raw_path("b/new.bz2") == (brain / "raw" / "b" / "new.bz2").resolve()
    partial = brain / "raw" / "c.bz2.part"
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.write_bytes(b"x")
    assert pool.raw_path("c.bz2") == (brain / "raw" / "c.bz2").resolve()  # resumes a partial download
    with pytest.raises(ValueError):
        pool.raw_path("../../etc/passwd")
    from polymath.senses.dumpfiles import raw_path

    assert raw_path(ctx_for(config, db, "dump.download"), "b/new.bz2") == pool.raw_path("b/new.bz2")
    db.kv_set("marker", 1)
    with db.transaction():
        out = mt.backup_job(ctx_for(config, db, "body.backup"))
    assert out.result["ok"] and Path(out.result["path"]).parent == brain / "backups"
    assert mt.backups(mt.backup_dirs(config))[0].name == Path(out.result["path"]).name
    d.unplug("vol1")
    pool._scan_at = -1e18
    assert pool.raw_path("b/new.bz2") == (config.paths.raw_dir / "b" / "new.bz2").resolve()
    assert pool.backup_dir() == config.paths.backup_dir


# ------------------------------------------------------------------ bookkeeping, guard, feed, CLI
def test_sync_records_arrivals_departures_and_returns(config, db, caplog):
    caplog.set_level("INFO")  # logging extras must not collide with LogRecord fields (a real crash, once)
    d = Drives(config)
    d.plug("vol1", label="Samsung T7", budget=900 * GB)
    pool = d.pool()
    assert [e["event"] for e in storage_pool.sync(db, pool)] == ["added"]
    assert "storage drive added" in caplog.text
    assert storage_pool.sync(db, pool) == []  # nothing new
    d.unplug("vol1")
    pool._scan_at = -1e18
    assert [e["event"] for e in storage_pool.sync(db, pool)] == ["offline"]
    d.plug("vol1", label="Samsung T7", budget=900 * GB)
    pool._scan_at = -1e18
    assert [e["event"] for e in storage_pool.sync(db, pool)] == ["online"]
    f = Feed(db)
    events = [e for e in f.poll("v0")["events"] if e["kind"] == "storage"]
    texts = [text_of(render_event(e)) for e in events]
    assert "new drive Samsung T7 (1.80 TB ext4): 900.00 GB of it is now brain space" in texts[0]
    assert "was unplugged; its documents wait for it" in texts[1] and "is back" in texts[2]
    assert "storage" in text_of(render_event({"kind": "storage", "event": "full", "name": "X", "at": 0}))
    db.kv_set("disk_budget_bytes", 15 * GB)
    line2 = text_of(status_lines(f.status())[1])
    assert "brain 915.00 GB (1 drive)" in line2


def test_guard_watches_the_pool_and_asks_for_spills(config, db):
    d = Drives(config)
    d.plug("vol1")
    d.pool()
    add_docs(db, 3)
    config.body.disk_budget_gb = 1e-6
    g = Guard(config, db, sensors=lambda: Vitals(50.0, 0.5, 4, 1000.0, 500.0, 10.0))
    g.observe()
    kinds = {r["kind"] for r in db.query("SELECT kind FROM jobs")}
    assert "body.spill" in kinds and db.scalar("SELECT COUNT(*) FROM volumes") == 1
    assert db.kv_get("disk_budget_bytes") == int(1e-6 * GB)


def test_cli_storage_list_retire_and_root_only_commands(config, db, tmp_path, capsys, monkeypatch):
    from tests.conftest import make_config

    cfg = make_config(tmp_path)
    path = str(tmp_path / "polymath.toml")
    assert cli.main(["--config", path, "init"]) == 0
    capsys.readouterr()
    assert cli.main(["--config", path, "storage", "list"]) == 0
    out = capsys.readouterr().out
    assert "main disk" in out and "total brain space now: 200.0 GB" in out
    assert cli.main(["--config", path, "storage", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["id"] == "main"
    assert cli.main(["--config", path, "storage", "retire", "vol1"]) == 0
    assert json.loads(capsys.readouterr().out)["queued_via"] == "database"
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert cli.main(["--config", path, "storage", "attach", "/dev/sdz1"]) == 2
    assert "run it with sudo" in capsys.readouterr().err
    assert cfg.paths.db_path.exists()


# ------------------------------------------------------------------ the root helper (fake system tools)
class FakeSystem:
    """Answers blkid / lsblk / findmnt / mount / mkfs like a Pi with an SD card and plugged-in drives."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[list[str]] = []
        self.devices: dict[str, dict[str, Any]] = {
            "/dev/mmcblk0p2": {"type": "part", "pkname": "mmcblk0", "probe": {"TYPE": "ext4", "UUID": "root-uuid"}},
            "/dev/mmcblk0": {"type": "disk", "probe": {"PTTYPE": "dos"}, "children": [{"path": "/dev/mmcblk0p2"}]},
        }
        self.mounts: dict[str, list[str]] = {"/dev/mmcblk0p2": ["/"]}

    def add(self, dev: str, *, disk: str | None = None, probe: dict[str, str] | None = None, **kw: Any) -> None:
        self.devices[dev] = {"type": "part" if disk else "disk", "pkname": disk, "probe": probe or {}, **kw}

    def __call__(self, args: list[str], *, input: str | None = None) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        ok = subprocess.CompletedProcess(args, 0, "", "")
        cmd = args[0]
        if cmd == "blkid":
            p = self.devices.get(args[-1], {}).get("probe") or {}
            return subprocess.CompletedProcess(args, 0 if p else 2, "\n".join(f"{k}={v}" for k, v in p.items()), "")
        if cmd == "lsblk":
            d = self.devices.get(args[-1])
            if d is None:
                return subprocess.CompletedProcess(args, 32, "", "not a block device")
            dev = {"name": Path(args[-1]).name, "path": args[-1], "type": d["type"], "pkname": d.get("pkname"),
                   "mountpoints": self.mounts.get(args[-1], [None]), "model": "Test SSD ", "serial": "S1",
                   "label": d.get("probe", {}).get("LABEL")}  # fmt: skip
            if d.get("children"):
                dev["children"] = d["children"]
            return subprocess.CompletedProcess(args, 0, json.dumps({"blockdevices": [dev]}), "")
        if cmd == "findmnt":
            src = "/dev/mmcblk0p2" if args[-1] in {"/", str(self.root / "data")} else ""
            return subprocess.CompletedProcess(args, 0 if src else 1, src, "")
        if cmd == "mount":
            self.mounts.setdefault(args[-2], []).append(args[-1])
            return ok
        if cmd == "sfdisk":
            part = args[-1] + "1"
            self.add(part, disk=Path(args[-1]).name)
            self.devices[args[-1]]["children"] = [{"path": part}]
            self.devices[args[-1]]["probe"] = {"PTTYPE": "gpt"}
            return ok
        if cmd == "mkfs.ext4":
            self.devices[args[-1]]["probe"] = {"TYPE": "ext4", "UUID": "new-uuid-1", "LABEL": "POLYMATH"}
            return ok
        if cmd == "systemctl":
            return subprocess.CompletedProcess(args, 0, "", "")
        return ok

    def ran(self, name: str) -> list[list[str]]:
        return [c for c in self.calls if c[0] == name]


@pytest.fixture()
def helper(config, tmp_path):
    config.storage.mount_root = tmp_path / "mnt"
    config.storage.reserve_fraction = 0.05
    config.storage.move_home = False  # moving the brain onto a drive: tests/test_wear.py
    fake = FakeSystem(tmp_path)
    blank = {"/dev/sdb": (b"\0" * 64, b"\0" * 64), "/dev/sdc1": (b"\0" * 64, b"\0" * 64)}
    h = vols.Helper(config, fake, edges=lambda dev: blank.get(dev, (b"data", b"")), owner=(os.getuid(), os.getgid()),
                    mountinfo=tmp_path / "mountinfo", lock=tmp_path / "lock")  # fmt: skip
    return h, fake


def test_helper_adopts_a_drive_with_files_as_shared(helper, config):
    h, fake = helper
    fake.add("/dev/sda1", disk="sda", probe={"TYPE": "exfat", "UUID": "1234-ABCD", "LABEL": "PHOTOS"})
    mnt = config.storage.mount_root / "1234-ABCD"
    (mnt / "DCIM").mkdir(parents=True)  # the drive already holds the user's files
    out = h.attach("/dev/sda1")
    assert out.action == "attached" and out.volume == "1234-ABCD" and "shared drive" in out.detail
    mount = fake.ran("mount")[0]
    assert mount[:3] == ["mount", "-t", "exfat"] and f"uid={os.getuid()}" in mount[4] and "noexec" in mount[4]
    m = json.loads((mnt / storage_pool.BRAIN / storage_pool.MANIFEST).read_text())
    assert m["dedicated"] is False and m["label"] == "PHOTOS" and m["device"] == "/dev/sda1"
    assert 0 < m["budget_bytes"] <= m["size_bytes"] * config.storage.shared_drive_share
    assert (mnt / "DCIM").exists()  # the user's files are untouched
    again = h.attach("/dev/sda1")
    assert again.action == "already"
    assert json.loads((mnt / storage_pool.BRAIN / storage_pool.MANIFEST).read_text())["adopted_at"] == m["adopted_at"]


def test_helper_formats_only_completely_blank_disks(helper, config):
    h, fake = helper
    fake.add("/dev/sdb")  # brand-new SSD: nothing at all on it
    out = h.attach("/dev/sdb")
    assert out.action == "attached" and "dedicated drive" in out.detail
    assert fake.ran("sfdisk")[0][-1] == "/dev/sdb" and fake.ran("mkfs.ext4")[0][-1] == "/dev/sdb1"
    m = json.loads((config.storage.mount_root / "new-uuid-1" / storage_pool.BRAIN / storage_pool.MANIFEST).read_text())
    assert m["dedicated"] is True and m["fstype"] == "ext4"
    fake.add("/dev/sdd")  # no signature, but not zeros: unknown data
    assert "not touching it" in h.attach("/dev/sdd").detail and len(fake.ran("mkfs.ext4")) == 1
    fake.add("/dev/sdc1", disk="sdc")  # a blank partition is formatted too
    config.storage.format_blank_disks = False
    assert "format_blank_disks is off" in h.attach("/dev/sdc1").detail
    config.storage.format_blank_disks = True
    assert h.attach("/dev/sdc1").action == "attached"


@pytest.mark.parametrize(
    ("dev", "setup", "why"),
    [
        ("/dev/mmcblk0p2", None, "internal or virtual"),
        ("/dev/loop3", None, "internal or virtual"),
        ("/dev/sde1", {"disk": "sde", "probe": {"TYPE": "swap", "UUID": "s"}}, "not one Polymath can use"),
        ("/dev/sde2", {"disk": "sde", "probe": {"TYPE": "crypto_LUKS", "UUID": "c"}}, "not one Polymath can use"),
        ("/dev/sdf", {"probe": {"PTTYPE": "gpt"}}, "each one is handled on its own"),
        ("/dev/sdg1", {"disk": "sdg", "probe": {"TYPE": "ext4"}}, "no filesystem UUID"),
        ("/dev/gone1", None, "went away"),
    ],
)
def test_helper_refuses_what_it_must_not_touch(helper, dev, setup, why):
    h, fake = helper
    if setup is not None:
        fake.add(dev, **setup)
    out = h.attach(dev)
    assert out.action == "skipped" and why in out.detail, out
    assert not fake.ran("mount") and not fake.ran("mkfs.ext4")


def test_helper_never_touches_the_system_disk_ignored_or_retired_drives(helper, config, tmp_path):
    h, fake = helper
    fake.add("/dev/sda1", disk="sda", probe={"TYPE": "ext4", "UUID": "sys-2"})
    fake.devices["/dev/sda1"]["pkname"] = "mmcblk0"  # pretend it sits on the SD card
    assert "system disk" in h.attach("/dev/sda1").detail
    fake.add("/dev/sdb1", disk="sdb", probe={"TYPE": "ext4", "UUID": "ign-1"})
    config.storage.ignore = ["ign-1"]
    assert "ignore list" in h.attach("/dev/sdb1").detail
    fake.add("/dev/sdc1", disk="sdc", probe={"TYPE": "ext4", "UUID": "ret-1"})
    brain = config.storage.mount_root / "ret-1" / storage_pool.BRAIN
    brain.mkdir(parents=True)
    (brain / storage_pool.MANIFEST).write_text(json.dumps({"id": "ret-1", "retired": True}))
    assert "retired" in h.attach("/dev/sdc1").detail and fake.ran("umount")
    config.storage.adopt = False
    assert "adopt is off" in h.attach("/dev/sdc1").detail


def test_helper_mounted_elsewhere_bind_or_refuse_and_too_small(helper, config, monkeypatch):
    h, fake = helper
    fake.add("/dev/sda1", disk="sda", probe={"TYPE": "ext4", "UUID": "posix-1"})
    fake.mounts["/dev/sda1"] = ["/media/christian/disk"]
    assert h.attach("/dev/sda1").action == "attached" and fake.ran("mount")[0][:2] == ["mount", "--bind"]
    fake.add("/dev/sdb1", disk="sdb", probe={"TYPE": "vfat", "UUID": "FAT-1"})
    fake.mounts["/dev/sdb1"] = ["/media/christian/STICK"]
    assert "cannot use" in h.attach("/dev/sdb1").detail
    monkeypatch.setattr(vols, "MIN_BUDGET", 10**18)
    fake.add("/dev/sdc1", disk="sdc", probe={"TYPE": "ext4", "UUID": "small-1"})
    out = h.attach("/dev/sdc1")
    assert "too little" in out.detail and not (config.storage.mount_root / "small-1").exists()


def test_helper_mount_and_format_failures_are_reported(helper, config):
    h, fake = helper
    real = fake.__call__

    def failing(args, *, input=None):
        if args[0] in {"mount", "mkfs.ext4"}:
            fake.calls.append(args)
            return subprocess.CompletedProcess(args, 32, "", f"{args[0]}: boom")
        return real(args, input=input)

    h.run = failing
    fake.add("/dev/sda1", disk="sda", probe={"TYPE": "ext4", "UUID": "m-1"})
    assert "mounting /dev/sda1 failed: mount: boom" in h.attach("/dev/sda1").detail
    assert not (config.storage.mount_root / "m-1").exists()
    fake.add("/dev/sdc1", disk="sdc")
    assert "formatting /dev/sdc1 failed" in h.attach("/dev/sdc1").detail


def test_helper_detach_and_eject(helper, config, tmp_path):
    h, fake = helper
    mnt = config.storage.mount_root / "vol1"
    mnt.mkdir(parents=True)
    (tmp_path / "mountinfo").write_text(
        f"36 25 8:1 / {mnt} rw,noatime - ext4 /dev/sda1 rw\n22 1 179:2 / / rw - ext4 /dev/mmcblk0p2 rw\nbad line\n"
    )
    assert h.mounts_of() == [(str(mnt), "/dev/sda1")]
    out = h.eject("vol1")
    assert out.action == "ejected" and fake.ran("systemctl")[0][-1] == "polymath-volume@sda1.service"
    assert fake.ran("umount")[0] == ["umount", "-l", str(mnt)]  # the fake unit did not unmount: detached directly
    assert h.eject("nope").action == "skipped"
    assert h.detach("/dev/sdx9").action == "skipped"
    h.mountinfo = tmp_path / "missing"
    assert h.mounts_of() == []


def test_read_edges_and_run_cmd(tmp_path):
    f = tmp_path / "disk.img"
    f.write_bytes(b"\0" * (3 << 20))
    head, tail = vols.read_edges(str(f))
    assert len(head) == len(tail) == vols.EDGE and not head.strip(b"\0")
    assert vols.run_cmd(["true"]).returncode == 0
