"""v0.9.0 security audit: root never writes through a planted link, no terminal escapes, bounded decompression,
hardened units, a root-owned install."""

from __future__ import annotations

import configparser
import errno
import gzip
import io
import os
import stat
import subprocess
from pathlib import Path

import pytest

from polymath.body import heal
from polymath.body import volumes as vols
from polymath.core.safefs import safe_dir, safe_write
from polymath.interface.feed import Screen
from polymath.memory import pool as storage_pool
from polymath.senses import html_text
from polymath.senses.net import StreamResponse
from tests.test_storage import FakeSystem

ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------ core.safefs
def test_safe_write_writes_atomically_with_the_mode_asked_for(tmp_path):
    safe_write(tmp_path, "a.json", "{}", mode=0o640)
    assert (tmp_path / "a.json").read_text() == "{}" and stat.S_IMODE((tmp_path / "a.json").stat().st_mode) == 0o640
    safe_write(tmp_path, "a.json", b"x" * 300_000)  # bytes, more than one write() call may take
    assert (tmp_path / "a.json").read_bytes() == b"x" * 300_000
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.json"]  # no temporary file left behind
    for bad in ("", ".", "..", "sub/a.json"):
        with pytest.raises(ValueError):
            safe_write(tmp_path, bad, "x")
    safe_write(tmp_path, "own.json", "x", owner=(os.getuid(), os.getgid()))
    assert (tmp_path / "own.json").stat().st_uid == os.getuid()


def test_safe_write_never_follows_a_planted_link(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("precious")
    d = tmp_path / "d"
    d.mkdir()
    (d / f".m.json.{os.getpid()}.tmp").symlink_to(victim)  # the temporary name, planted
    safe_write(d, "m.json", "new")
    assert victim.read_text() == "precious" and (d / "m.json").read_text() == "new"
    (d / "m.json").unlink()
    (d / "m.json").symlink_to(victim)  # the final name, planted: the link is replaced, never written through
    safe_write(d, "m.json", "new")
    assert victim.read_text() == "precious" and not (d / "m.json").is_symlink()
    linked = tmp_path / "linked"
    linked.symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    (tmp_path / "elsewhere").mkdir()
    with pytest.raises(OSError) as exc:  # the directory itself is a link: refused
        safe_write(linked, "m.json", "new")
    assert exc.value.errno in {errno.ELOOP, errno.ENOTDIR}
    assert list((tmp_path / "elsewhere").iterdir()) == []


def test_a_failed_write_leaves_no_temporary_file(tmp_path, monkeypatch):
    def boom(fd: int, data: object) -> int:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "write", boom)
    with pytest.raises(OSError, match="No space"):
        safe_write(tmp_path, "m.json", "x")
    assert list(tmp_path.iterdir()) == []


def test_safe_dir_makes_a_folder_and_refuses_links_and_files(tmp_path):
    assert safe_dir(tmp_path / "b", mode=0o750) is True
    assert stat.S_IMODE((tmp_path / "b").stat().st_mode) == 0o750
    assert safe_dir(tmp_path / "b", owner=(os.getuid(), os.getgid())) is False
    (tmp_path / "etc").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "etc", target_is_directory=True)
    before = stat.S_IMODE((tmp_path / "etc").stat().st_mode)
    with pytest.raises(OSError):
        safe_dir(tmp_path / "link", mode=0o777)
    assert stat.S_IMODE((tmp_path / "etc").stat().st_mode) == before  # the link's target is untouched
    (tmp_path / "file").write_text("x")
    with pytest.raises(OSError):
        safe_dir(tmp_path / "file", mode=0o700)


# ------------------------------------------------------------------ the root helpers use it
def test_a_drive_whose_brain_folder_is_a_link_is_not_used(config, tmp_path):
    config.storage.mount_root = tmp_path / "mnt"
    fake = FakeSystem(tmp_path)
    h = vols.Helper(config, fake, edges=lambda dev: (b"data", b""), owner=(os.getuid(), os.getgid()),
                    mountinfo=tmp_path / "mountinfo", lock=tmp_path / "lock",
                    registry=tmp_path / "formatted-drives.json")  # fmt: skip
    fake.add("/dev/sda1", disk="sda", probe={"TYPE": "ext4", "UUID": "evil-1", "LABEL": "CRAFTED"})
    mnt = config.storage.mount_root / "evil-1"
    mnt.mkdir(parents=True)
    target = tmp_path / "system-dir"
    target.mkdir(mode=0o755)
    (mnt / storage_pool.BRAIN).symlink_to(target, target_is_directory=True)  # polymath-brain -> /etc, say
    out = h.attach("/dev/sda1")
    assert out.action == "skipped" and "not a plain folder" in out.detail
    assert list(target.iterdir()) == [] and stat.S_IMODE(target.stat().st_mode) == 0o755


def test_the_heal_state_is_never_written_through_a_link(config, tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    config.paths.run_dir = run
    victim = tmp_path / "victim"
    victim.write_text("precious")
    (run / heal.PUBLIC).symlink_to(victim)
    h = heal.Healer(config, lambda args, **_: None, services=(), mounts_at=lambda p: [], sys_block=tmp_path,
                    state_path=tmp_path / "heal.json", clock=lambda: 1.0, sleep=lambda s: None,
                    which=lambda name: None)  # fmt: skip
    h.save(heal.HealState())
    assert victim.read_text() == "precious" and not (run / heal.PUBLIC).is_symlink()
    assert (tmp_path / "heal.json").exists()


# ------------------------------------------------------------------ what a page can put on the screen
def test_pages_cannot_smuggle_terminal_escapes_or_reversed_text():
    page = ("<html><head><title>News\x1b]0;pwned\x07 today</title></head><body><article><p>"
            + "Plain words about the river and its long history. " * 8
            + "\x1b[2J\x1b[H‮gnp.exe‬ and\x9b5m more.</p></article></body></html>")  # fmt: skip
    out = html_text.extract(page)
    assert out.title == "News]0;pwned today"
    for bad in ("\x1b", "\x07", "\x9b", "‮", "‬"):
        assert bad not in out.title and bad not in out.text
    assert "and5m more." in out.text and "\n" not in out.title


def test_the_feed_never_prints_control_characters():
    s = Screen(io.StringIO(), color=True, fancy=True)
    line = s.paint([("title\x1b]2;owned\x07", "bold"), (" ⁦x⁩ \x7f", "dim")], width=200)
    assert "]2;owned" in line and "\x07" not in line and "\x7f" not in line and "⁦" not in line
    assert "\x1b[" in line  # its own colours still work


# ------------------------------------------------------------------ bounded decompression
class FakeResp:
    def __init__(self, data: bytes) -> None:
        self.buf = io.BytesIO(data)

    def read(self, n: int = -1) -> bytes:
        return self.buf.read(n)


@pytest.mark.parametrize("wbits", [31, 15])
def test_a_small_compression_bomb_never_becomes_a_big_chunk(wbits):
    import zlib

    raw = b"\0" * 20_000_000
    if wbits == 31:
        body = gzip.compress(raw, compresslevel=9)
    else:
        c = zlib.compressobj(9, wbits=15)
        body = c.compress(raw) + c.flush()
    assert len(body) < 100_000
    sr = StreamResponse("https://x", 200, {}, FakeResp(body), zlib.decompressobj(wbits=wbits))  # type: ignore[arg-type]
    total, biggest = 0, 0
    for chunk in sr.iter_chunks(1 << 16):
        total += len(chunk)
        biggest = max(biggest, len(chunk))
    assert total == len(raw) and biggest <= 1 << 16 and sr.read_bytes == len(body)
    plain = StreamResponse("https://x", 200, {}, FakeResp(b"hello"))  # type: ignore[arg-type]
    assert plain.read(3) == b"hel" and plain.read() == b"lo" and plain.read_bytes == 5


# ------------------------------------------------------------------ units and the install
def unit(name: str) -> configparser.SectionProxy:
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str  # type: ignore[assignment,method-assign]
    cp.read(ROOT / "deploy" / name)
    return cp["Service"]


def test_the_agent_and_dashboard_units_are_locked_down():
    for name in ("polymath.service", "polymath-dashboard.service"):
        svc = unit(name)
        for key, value in {"NoNewPrivileges": "yes", "ProtectKernelLogs": "yes", "ProtectClock": "yes",
                           "ProtectHostname": "yes", "ProtectProc": "invisible", "CapabilityBoundingSet": "",
                           "AmbientCapabilities": "", "MemoryDenyWriteExecute": "yes",
                           "SystemCallFilter": "@system-service", "SystemCallErrorNumber": "EPERM",
                           "UMask": "0027", "RemoveIPC": "yes"}.items():  # fmt: skip
            assert svc.get(key) == value, (name, key)
    assert unit("polymath.service")["SocketBindDeny"] == "any"  # the agent never listens on anything
    dash = unit("polymath-dashboard.service")
    assert dash["IPAddressDeny"] == "any"
    allow = dash["IPAddressAllow"].split()
    assert {"localhost", "192.168.0.0/16", "10.0.0.0/8", "172.16.0.0/12"} <= set(allow)
    assert not {"any", "0.0.0.0/0", "::/0"} & set(allow)  # never the internet
    assert dash["StateDirectory"] == "polymath/access" and dash["StateDirectoryMode"] == "0700"


def test_the_root_helpers_switch_off_what_they_never_need():
    for name in ("polymath-heal.service", "polymath-volume@.service"):
        svc = unit(name)
        for key, value in {"NoNewPrivileges": "yes", "RestrictAddressFamilies": "AF_UNIX AF_NETLINK",
                           "IPAddressDeny": "any", "RestrictNamespaces": "yes", "RestrictSUIDSGID": "yes",
                           "LockPersonality": "yes", "MemoryDenyWriteExecute": "yes",
                           "SystemCallArchitectures": "native"}.items():  # fmt: skip
            assert svc.get(key) == value, (name, key)
        assert "PrivateMounts" not in svc and "ProtectSystem" not in svc  # its mounts must reach the system


def test_the_install_is_root_owned_and_sets_up_https():
    text = (ROOT / "install.sh").read_text()
    assert "--no-same-owner" in text and "--no-same-permissions" in text and "go-w" in text
    assert "access-cert.sh" in text and "access pair --from-install" in text
    assert 'host = "127.0.0.1"' in text  # an old config listening on every address is moved to the Pi only
    script = ROOT / "scripts" / "access-cert.sh"
    assert script.stat().st_mode & stat.S_IXUSR
    toml = (ROOT / "config" / "polymath.toml").read_text()
    assert 'host = "127.0.0.1"' in toml and "[access]" in toml


def test_polymath_access_runs_as_you(tmp_path):
    real = tmp_path / "polymath"
    real.write_text('#!/bin/sh\necho "polymath $*"\n')
    real.chmod(0o755)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "id").write_text('#!/bin/sh\nif [ "$1" = -u ]; then echo 1000; else echo christian; fi\n')
    (bindir / "id").chmod(0o755)
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "POLYMATH_BIN": str(real)}
    out = subprocess.run(["sh", str(ROOT / "deploy" / "polymath-cli"), "access", "pair"], env=env,
                         capture_output=True, text=True, timeout=30, check=True)  # fmt: skip
    assert out.stdout.strip() == "polymath access pair"  # no sudo: it asks the dashboard on loopback
