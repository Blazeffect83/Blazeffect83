"""``polymath doctor``: is every part of Polymath working, and if not, the one thing to do about it.

It runs as you (no sudo): it asks the dashboard over HTTP, reads the agent's snapshot in ``/run/polymath``, and
opens the database read-only only if you may. Each check prints one line: ✓ fine, ! worth knowing, ✗ broken,
with the fix after it. Nothing is changed.
"""

from __future__ import annotations

import json
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


def _exists(path: Path) -> bool | None:
    """Whether ``path`` exists; None when you may not look (``Path.exists`` raises for that)."""
    try:
        path.stat()
    except FileNotFoundError:
        return False
    except OSError:
        return None
    return True


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
    moved, home = _exists(data / MOVED_FILE), _exists(data / HOME_FILE)
    if moved is None or home is None:  # the brain belongs to the polymath user: you may not look inside
        checks.append(Check("brain", OK, f"at {data} (its files belong to the polymath user)"))
    elif moved and not home:
        try:
            name = json.loads((data / MOVED_FILE).read_text(encoding="utf-8")).get("name") or "its drive"
        except (OSError, ValueError):
            name = "its drive"
        checks.append(Check("brain", BAD, f"the brain lives on {name}, which is not mounted",
                            "plug the drive in (or replug it): Polymath mounts it and starts by itself"))  # fmt: skip
    elif home:
        checks.append(Check("brain", OK, f"on its drive, mounted at {data}"))
    else:
        checks.append(Check("brain", OK, f"at {data}"))

    # the agent, through its own snapshot (needs nothing but /run)
    st, age = snapshot.read_status(snapshot.path_for(config))
    if st is None:
        checks.append(Check("agent", WARN, "no live snapshot in /run/polymath (written by v0.5.1 and later)",
                            "reinstall to get it: sudo ./install.sh"))  # fmt: skip
    elif age is not None and age > snapshot.STALE_S:
        checks.append(Check("agent", BAD, f"not running: its snapshot is {int(age // 60)} min old", restart_agent))
    else:
        doing = st.get("activity") or st.get("state") or "running"
        checks.append(Check("agent", OK, f"running: {doing}"))
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

    # disk room
    try:
        free = shutil.disk_usage(data).free / 1e9
    except OSError:
        free = None
    if free is not None:
        low = free < config.body.disk_min_free_gb
        checks.append(Check("disk", WARN if low else OK, f"{free:,.0f} GB free",
                            "it frees space by itself; plug in a drive for more room" if low else ""))  # fmt: skip
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
