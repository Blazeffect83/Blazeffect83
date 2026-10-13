"""Is the internet there? The agent's own answer, kept in kv ``net`` and shown in the feed.

The loop learns it passively, from how network jobs end: a connection-level failure (no DNS, no route, refused,
timed out) counts against the internet, any network job that succeeds proves it is there. ``OFFLINE_AFTER``
failures in a row mean offline. While offline the loop holds network jobs back (they keep their place, attempts
and checkpoints) and studies what it already has; ``senses.connectivity`` probes every so often and the first
answer brings everything back. An HTTP error from a server (404, 503) says nothing about the internet itself, so
it never counts.

This module only reads and writes the database; the probe itself lives in ``senses`` (the only network code).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from polymath.core.db import Database

KEY = "net"
OFFLINE_AFTER = 2  # connection failures in a row before the agent calls itself offline (one can be a blip)
# error text of connection-level failures, as raised by senses.net (FetchError, status None) or the OS
_NETWORK_HINTS = (
    "dns failure",
    "no address for",
    "name or service not known",
    "temporary failure in name resolution",
    "network is unreachable",
    "no route to host",
    "connection refused",
    "connection reset",
    "connection aborted",
    "timed out",
    "timeout",
    "remotedisconnected",
)
_NETWORK_TYPES = {
    "gaierror",
    "timeout",
    "TimeoutError",
    "ConnectionError",
    "ConnectionRefusedError",
    "ConnectionResetError",
    "ConnectionAbortedError",
    "BrokenPipeError",
    "RemoteDisconnected",
}


@dataclass
class NetState:
    online: bool = True
    since: float = 0.0  # when the current state began
    fails: int = 0  # connection failures in a row
    last_ok: float = 0.0
    last_fail: float = 0.0
    reason: str = ""  # the last failure, short
    probes: int = 0  # failed probes since going offline
    last_probe: float = 0.0


def get(db: Database) -> NetState:
    raw = db.kv_get(KEY)
    if not isinstance(raw, dict):
        return NetState()
    fields = NetState.__dataclass_fields__
    return NetState(**{k: v for k, v in raw.items() if k in fields})


def _save(db: Database, st: NetState) -> None:
    db.kv_set(KEY, asdict(st))


def is_network_error(exc: BaseException) -> bool:
    """A failure that says the internet (or DNS) is not reachable, as opposed to a server answering badly."""
    if getattr(exc, "status", None) is not None:  # the server answered: the internet works
        return False
    if type(exc).__name__ in _NETWORK_TYPES:
        return True
    transient = bool(getattr(exc, "transient", False))
    text = str(exc).lower()
    return (transient or isinstance(exc, OSError)) and any(h in text for h in _NETWORK_HINTS)


def _event(db: Database, now: float, text: str, detail: dict[str, Any]) -> None:
    db.execute("INSERT INTO events(at, kind, text, detail) VALUES(?, 'net', ?, ?)", (now, text, json.dumps(detail)))


def failed(db: Database, now: float, reason: str) -> bool:
    """Record a connection failure. Returns True when this one took the agent offline."""
    st = get(db)
    st.fails += 1
    st.last_fail = now
    st.reason = reason[:160]
    went = st.online and st.fails >= OFFLINE_AFTER
    if went:
        st.online, st.since, st.probes = False, now, 0
        _event(db, now, "the internet is gone: studying what I already have until it is back",
               {"status": "offline", "reason": st.reason})  # fmt: skip
    _save(db, st)
    return went


def succeeded(db: Database, now: float) -> bool:
    """Record that the internet answered. Returns True when this brought the agent back online."""
    st = get(db)
    back = not st.online
    if back:
        away = now - st.since if st.since else 0.0
        _event(db, now, f"back online after {_duration(away)}: reading again", {"status": "online", "away_s": away})
        st.online, st.since = True, now
    elif st.fails == 0 and now - st.last_ok < 60:
        return False  # nothing new: spare the write (network jobs succeed many times a minute)
    st.fails, st.last_ok, st.probes = 0, now, 0
    _save(db, st)
    return back


def probed(db: Database, now: float) -> None:
    """A probe found no internet."""
    st = get(db)
    st.probes += 1
    st.last_probe = now
    _save(db, st)


def _duration(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 90:
        return f"{s} s"
    if s < 5400:
        return f"{s // 60} min"
    if s < 2 * 86400:
        return f"{s / 3600:.1f} h".replace(".0 h", " h")
    return f"{s / 86400:.1f} days".replace(".0 days", " days")
