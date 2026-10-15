"""Minimal sd_notify(3) implementation over ``$NOTIFY_SOCKET`` (no libsystemd).

Supports READY=1, WATCHDOG=1, STOPPING=1 and STATUS=... messages. Abstract
socket addresses (leading ``@``) are translated to a leading NUL byte.
"""

from __future__ import annotations

import os
import socket
import time
from collections.abc import Mapping


def notify(state: str, env: Mapping[str, str] | None = None) -> bool:
    """Send one notification. Returns False when not running under systemd."""
    env = os.environ if env is None else env
    addr = env.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    elif not addr.startswith("/"):
        return False  # vsock and other address families are not used by systemd on the Pi
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC) as sock:
        try:
            sock.connect(addr)
            sock.sendall(state.encode("utf-8"))
        except OSError:
            return False
    return True


class Notifier:
    """Rate-limited watchdog pings plus lifecycle notifications."""

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        self.env = dict(os.environ if env is None else env)
        self._last_ping = 0.0
        self.sent: list[str] = []
        usec = self.env.get("WATCHDOG_USEC")
        pid = self.env.get("WATCHDOG_PID")
        enabled = bool(usec) and (not pid or pid == str(os.getpid()))
        self.watchdog_interval = int(usec) / 1e6 if enabled and usec else 0.0
        # Ping at a quarter of the interval (systemd recommends at least half).
        self.ping_every = self.watchdog_interval / 4 if self.watchdog_interval else 5.0

    @property
    def active(self) -> bool:
        return bool(self.env.get("NOTIFY_SOCKET"))

    def _send(self, state: str) -> bool:
        ok = notify(state, self.env)
        if ok:
            self.sent.append(state.split("=", 1)[0])
        return ok

    def ready(self, status: str = "") -> bool:
        msg = "READY=1" + (f"\nSTATUS={status}" if status else "")
        return self._send(msg)

    def watchdog(self, force: bool = False) -> bool:
        now = time.monotonic()
        if not force and now - self._last_ping < self.ping_every:
            return False
        self._last_ping = now
        return self._send("WATCHDOG=1")

    def status(self, text: str) -> bool:
        return self._send(f"STATUS={text[:200]}")

    def stopping(self) -> bool:
        return self._send("STOPPING=1")
