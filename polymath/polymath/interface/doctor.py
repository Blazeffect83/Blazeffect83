"""``polymath doctor``: is every part of Polymath working, and if not, the one thing to do about it.

It runs as you (no sudo): it asks the dashboard over HTTP, reads the agent's snapshot in ``/run/polymath``, and
opens the database read-only only if you may. Each check prints one line: ✓ fine, ! worth knowing, ✗ broken,
with the fix after it. Nothing is changed.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from polymath.core.config import HOME_FILE, MOVED_FILE, Config

OK, WARN, BAD = "ok", "warn", "bad"
MARK = {OK: "✓", WARN: "!", BAD: "✗"}
ASCII_MARK = {OK: "ok", WARN: "!!", BAD: "XX"}


@dataclass
class Check:
    name: str
    level: str
    detail: str
    fix: str = ""


class DeadStorage(OSError):
    """The path's storage does not answer (Input/output error and the like): a drive that dropped out."""


def _exists(path: Path) -> bool | None:
    """Whether ``path`` exists; None when you may not look (``Path.exists`` raises for that). Raises
    ``DeadStorage`` when the storage itself does not answer."""
    from polymath.core.app import DEAD_MOUNT_ERRNOS

    try:
        path.stat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        if exc.errno in DEAD_MOUNT_ERRNOS:
            raise DeadStorage(exc.errno, exc.strerror) from exc
        return None
    return True


def brain_check(data: Path) -> Check:
    """Where the brain is, as far as you may look; a dead or frozen mount (the drive dropped out) is named."""
    from polymath.core import fsprobe

    state = fsprobe.probe(data, timeout=5.0)
    if fsprobe.unhealthy(state):
        return Check("brain", BAD, f"its storage at {data} does not answer ({fsprobe.describe(state)}): the drive "
                     "froze or dropped out", "Polymath brings it back by itself within a few minutes (storage heal); "
                     "now: sudo polymath storage heal")  # fmt: skip
    try:
        moved, home = _exists(data / MOVED_FILE), _exists(data / HOME_FILE)
    except DeadStorage as exc:
        return Check("brain", BAD, f"its storage at {data} does not answer ({exc.strerror}): the drive dropped out "
                     "and the old connection is dead", "sudo polymath storage heal   (or sudo reboot)")  # fmt: skip
    if moved is None or home is None:  # the brain belongs to the polymath user: you may not look inside
        return Check("brain", OK, f"at {data} (its files belong to the polymath user)")
    if moved and not home:
        try:
            name = json.loads((data / MOVED_FILE).read_text(encoding="utf-8")).get("name") or "its drive"
        except (OSError, ValueError):
            name = "its drive"
        return Check("brain", BAD, f"the brain lives on {name}, which is not mounted",
                     "plug the drive in (or replug it): Polymath mounts it and starts by itself")  # fmt: skip
    if home:
        return Check("brain", OK, f"on its drive, mounted at {data}")
    return Check("brain", OK, f"at {data}")


def sd_card_check(config: Config, st: dict[str, Any] | None = None) -> Check:
    """SD-card mode: since when, how much room, and how to go back to the SSD. The agent's own snapshot says it
    best (as you, the brain's directory cannot be read); the configuration and the marker otherwise."""
    from polymath.core import sdcard

    sd = (st or {}).get("sd_card") or {}
    mark = sdcard.read_marker(config) or {}
    started = sd.get("since") or mark.get("started")
    since = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(started))) if started else "now"
    room = float(sd.get("budget_gb") or config.body.disk_budget_gb)
    try:
        free = f"; {shutil.disk_usage(config.paths.data_dir).free / 1e9:.1f} GB free on the card"
    except OSError:
        free = ""
    new = f"; {int(sd['documents_new']):,} documents read here so far" if sd.get("documents_new") else ""
    return Check("sd card", WARN, f"SD-card mode since {since}: the whole brain runs on the SD card "
                 f"({room:.1f} GB of room{free}{new}); the SSD is not used",
                 "once the SSD works again: cd ~/polymath-src/polymath && sudo ./scripts/sd-card-mode.sh off   "
                 "(what it learned here moves onto the SSD by itself)")  # fmt: skip


def http_health(url: str, timeout: float = 6.0) -> tuple[str, float, dict[str, Any] | None]:
    """(``answered`` | ``timeout`` | ``refused`` | ``error: …``, seconds, body) for ``<url>/health``."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    t0 = time.monotonic()
    try:
        with opener.open(url.rstrip("/") + "/health", timeout=timeout) as resp:
            body = json.loads(resp.read() or b"{}")
        return "answered", time.monotonic() - t0, body
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read() or b"{}")
        except ValueError:
            body = None
        return "answered", time.monotonic() - t0, body
    except urllib.error.URLError as exc:
        reason = str(exc.reason)
        kind = "timeout" if "timed out" in reason else "refused" if "refused" in reason.lower() else f"error: {reason}"
        return kind, time.monotonic() - t0, None
    except (OSError, ValueError) as exc:
        kind = "timeout" if "timed out" in str(exc) else f"error: {exc}"
        return kind, time.monotonic() - t0, None


def run_checks(
    config: Config, url: str, *, health: Callable[[str], tuple[str, float, Any]] = http_health
) -> list[Check]:
    from polymath.interface import snapshot

    checks: list[Check] = []
    data = config.paths.data_dir
    restart_dash = "sudo systemctl restart polymath-dashboard   (details: journalctl -u polymath-dashboard -n 40)"
    restart_agent = "sudo systemctl restart polymath   (details: journalctl -u polymath -n 60)"

    # where the brain lives
    checks.append(brain_check(data))
    # the agent, through its own snapshot (needs nothing but /run)
    st, age = snapshot.read_status(snapshot.path_for(config))
    if config.storage.sd_only:  # SD-card mode (core.sdcard): chosen, but the SSD is out of use until it is switched off
        checks.append(sd_card_check(config, st))

    agent_up = st is not None and age is not None and age <= snapshot.STALE_S
    if st is None:
        checks.append(Check("agent", BAD, "not running (no live snapshot in /run/polymath)", restart_agent))
    elif not agent_up:
        checks.append(Check("agent", BAD, f"not running: its snapshot is {int((age or 0) // 60)} min old",
                            restart_agent))  # fmt: skip
    elif st.get("state") == "paused" and st.get("paused_reason"):
        reason = str(st["paused_reason"])
        fix = ("check the drive: is it plugged in, and does `lsblk` list it (sda)? A charge-only cable or a loose "
               "plug shows nothing. Polymath starts by itself once it is mounted"
               if "drive" in reason else "")  # fmt: skip
        checks.append(Check("agent", WARN, f"waiting: {reason}", fix))
    else:
        doing = st.get("activity") or st.get("state") or "running"
        checks.append(Check("agent", OK, f"running: {doing}"))
        sb = st.get("standby") or {}
        if sb:
            since = time.strftime("%H:%M", time.localtime(float(sb["since"]))) if sb.get("since") else "a while"
            read = f", {int(sb['documents']):,} documents read there" if sb.get("documents") is not None else ""
            fix = "bring the SSD back (see the brain line): what it read moves onto it by itself"
            detail = f"learning on the standby brain on the SD card since {since}{read}"
            checks.append(Check("standby", WARN, detail, fix))
        net = st.get("net") or {}
        if net.get("online") is False:
            since = time.strftime("%H:%M", time.localtime(float(net["since"]))) if net.get("since") else "a while"
            checks.append(Check("internet", WARN, f"none since {since}: studying what it already has",
                                "nothing to do: it resumes reading when the internet is back"))  # fmt: skip
        else:
            checks.append(Check("internet", OK, "reachable"))

    # the dashboard (and so the normal feed)
    kind, secs, body = health(url)
    if kind == "answered":
        slow = secs > 3
        fix = "it is slow: the feed may lag; it restarts itself if it hangs" if slow else ""
        checks.append(Check("dashboard", WARN if slow else OK, f"answers at {url} in {secs:.1f} s", fix))
        if isinstance(body, dict) and body.get("error", "").startswith("database unavailable"):
            checks.append(Check("database", BAD, "the dashboard cannot open the database", restart_dash))
    elif kind == "timeout":
        checks.append(Check("dashboard", BAD, f"no answer from {url} within 6 s (hung or overloaded)", restart_dash))
    elif kind == "refused":
        checks.append(Check("dashboard", BAD, f"not running (nothing listens at {url})", restart_dash))
    else:
        checks.append(Check("dashboard", BAD, f"{url}: {kind}", restart_dash))

    problems = (st or {}).get("issues") or []  # the caretaker's inbox, carried by the snapshot
    if problems:
        bad = any(p.get("status") == "bad" for p in problems)
        when = time.strftime("%H:%M", time.localtime(float(problems[0].get("at") or 0)))
        fix = "the live feed shows each as a 'problem' line"
        known = problems[0].get("known") or []
        if known:  # a known problem: the caretaker's knowledge (agents/faults.toml) says what to do
            from polymath.agents import faults

            f = faults.knowledge().by_id.get(str(known[0]))
            step = f.next_step() if f else None
            if f and step:
                fix = f"known problem: {faults.describe_step(step)}   (more: polymath caretaker show {f.id})"
        checks.append(Check("problems", BAD if bad else WARN,
                            f"{len(problems)} in the last 24 h; latest at {when}: {problems[0].get('text', '')}",
                            fix))  # fmt: skip

    orin = (st or {}).get("caretaker") or {}
    if agent_up and orin:  # Orin the caretaker: what it is working on, and what it asks of you
        open_ = orin.get("open") or []
        if not open_:
            checks.append(Check("Orin", OK, "the caretaker is watching: all calm"))
        for inc in open_[:3]:
            level = BAD if inc.get("severity") == "bad" and inc.get("state") == "escalated" else WARN
            state = {"escalated": "needs you", "verifying": "fixed, checking it holds", "open": "looking into it"}
            what = state.get(str(inc.get("state")), str(inc.get("state")))
            checks.append(Check("Orin", level, f"{inc.get('title')} ({what})", str(inc.get("advice") or "")))

    if not agent_up and kind != "answered":  # both down: one thing to look at, not two
        checks.append(Check("services", BAD, "the agent and the dashboard are both stopped",
                            "systemctl status polymath polymath-dashboard --no-pager   (says why), then: "
                            "sudo systemctl restart polymath polymath-dashboard"))  # fmt: skip

    # drives plugged in (readable by everyone: /mnt/polymath)
    root = config.storage.mount_root
    try:
        mounted = sorted(d.name for d in root.iterdir() if os.path.ismount(d))
    except OSError:
        mounted = []
    if config.storage.sd_only:
        checks.append(Check("drives", OK, "none used: SD-card mode (the SSD is left alone)"))
    from polymath.body.sensors import DEAD_DRIVE_FIX, SLOW_DRIVE_FIX, slow_usb_disks, unresponsive_usb_disks

    for name in unresponsive_usb_disks():
        checks.append(Check("drives", BAD, f"/dev/{name} is connected by USB but reports 0 bytes: the drive behind "
                            "it is not responding", DEAD_DRIVE_FIX))  # fmt: skip
    for name, mbps in slow_usb_disks():
        checks.append(Check("drives", WARN, f"/dev/{name} runs at USB 2 speed ({mbps} Mbit/s), not USB 3",
                            SLOW_DRIVE_FIX))  # fmt: skip
    if config.storage.sd_only:
        pass
    elif mounted:
        checks.append(Check("drives", OK, f"{len(mounted)} mounted: {', '.join(m[:12] for m in mounted)}"))
    else:
        checks.append(Check("drives", WARN, f"no drive mounted under {root}",
                            "if the brain lives on your SSD: is it plugged in, and does `lsblk` show it? "
                            "Then: journalctl -u 'polymath-volume@*' -n 30"))  # fmt: skip

    from polymath.body.heal import read_public

    healed = [a for a in ((read_public(config) or {}).get("actions") or []) if time.time() - float(a.get("at") or 0)
              < 86400]  # fmt: skip
    if healed:
        last = healed[-1]
        when = time.strftime("%H:%M", time.localtime(float(last["at"])))
        detail = f"brought the brain's drive back {len(healed)} time(s) in the last 24 h; last at {when}: "
        fix = "it works, but the drive keeps dropping out: give it a powered USB hub or its own power"
        checks.append(Check("drive helper", WARN, detail + str(last.get("detail")), fix))

    # your other devices' access (interface.access)
    from polymath.interface.access import access_check

    status, detail, fix = access_check(config)
    checks.append(Check("access", {"ok": OK, "warn": WARN, "bad": BAD}[status], detail, fix))
    # disk room
    try:
        free = shutil.disk_usage(data).free / 1e9
    except OSError:
        free = None
    if free is not None:
        low = free < config.body.disk_min_free_gb
        more = ("it frees space by itself (oldest feeds and web pages first)" if config.storage.sd_only
                else "it frees space by itself; plug in a drive for more room")  # fmt: skip
        shown = f"{free:,.1f}" if free < 10 else f"{free:,.0f}"
        checks.append(Check("disk", WARN if low else OK, f"{shown} GB free", more if low else ""))
    return checks


def render(checks: list[Check], *, ascii_only: bool = False) -> list[str]:
    marks = ASCII_MARK if ascii_only else MARK
    lines = []
    for c in checks:
        lines.append(f"{marks[c.level]} {c.name:<10} {c.detail}")
        if c.fix:
            lines.append(f"  {'':<10} → {c.fix}")
    bad = sum(c.level == BAD for c in checks)
    lines.append("everything is working" if not bad else f"{bad} thing{'s' if bad != 1 else ''} to fix (above)")
    return lines
