"""Ask the filesystem a question without ever hanging on the answer.

A USB drive that froze mid-operation can leave its mount in a state where ``stat`` or ``read`` neither succeeds
nor fails: the calling thread sleeps in the kernel, uninterruptibly, for minutes. Anything that checks the brain's
storage directly then freezes too (the agent at start, the dashboard, the self-heal), which is what the Pi showed
("Job for polymath.service failed because a timeout was exceeded").

``probe(path)`` asks in a separate, tiny process and waits at most ``timeout`` seconds. A process (not a thread):
a task stuck in the kernel cannot be killed, and a stuck *thread* would keep the whole program from exiting, so
``polymath init`` would still hang systemd's start. A stuck child is simply left behind; it ends by itself once
the drive is reset (``storage heal``). A probe still stuck from earlier makes the next one for the same path answer
``hang`` at once, so a frozen mount costs one stuck child per path, not one per check. If no process can be
started (out of memory, a sandbox), a side thread asks instead.
"""

from __future__ import annotations

import errno
import subprocess
import sys
import threading
from pathlib import Path

DEAD_ERRNOS = {errno.EIO, errno.ENOTCONN, errno.ESTALE, errno.ENODEV, errno.ENXIO}
OK, MISSING, DENIED, DEAD, HANG, ERROR = "ok", "missing", "denied", "dead", "hang", "error"
ISOLATE = True  # ask in a child process (tests that fake errors inside this process switch it off)

_CODES = {0: OK, 1: MISSING, 2: DENIED, 3: DEAD, 4: ERROR}
_CHILD = (
    "import os,sys\n"
    "p,r=sys.argv[1],sys.argv[2]=='1'\n"
    "try:\n"
    "    if r:\n"
    "        with open(p,'rb') as f: f.read(4096)\n"
    "    else: os.stat(p)\n"
    "except FileNotFoundError: sys.exit(1)\n"
    "except PermissionError: sys.exit(2)\n"
    f"except OSError as e: sys.exit(3 if e.errno in {sorted(DEAD_ERRNOS)} else 4)\n"
)

_inflight: dict[str, subprocess.Popen[bytes] | threading.Thread] = {}
_lock = threading.Lock()


def _still_running(w: subprocess.Popen[bytes] | threading.Thread) -> bool:
    return w.is_alive() if isinstance(w, threading.Thread) else w.poll() is None


def _check(path: Path, read: bool) -> str:
    """The question itself, asked in this process."""
    try:
        if read:
            with path.open("rb") as f:
                f.read(4096)
        else:
            path.stat()
    except FileNotFoundError:
        return MISSING
    except PermissionError:
        return DENIED
    except OSError as exc:
        return DEAD if exc.errno in DEAD_ERRNOS else ERROR
    return OK


def _in_child(key: str, path: Path, read: bool, timeout: float) -> str | None:
    """The answer from a child process; None when no child could be started."""
    try:
        child = subprocess.Popen(
            [sys.executable, "-I", "-S", "-c", _CHILD, str(path), "1" if read else "0"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            cwd="/", close_fds=True,
        )  # fmt: skip
    except (OSError, ValueError):
        return None
    try:
        code = child.wait(timeout)
    except subprocess.TimeoutExpired:
        with _lock:
            _inflight[key] = child  # left behind: it ends once the drive answers or is reset
        return HANG
    return _CODES.get(code, ERROR)


def _in_thread(key: str, path: Path, read: bool, timeout: float) -> str:
    result: dict[str, str] = {}
    t = threading.Thread(target=lambda: result.setdefault("v", _check(path, read)), name=f"fsprobe {path}",
                         daemon=True)  # fmt: skip
    t.start()
    t.join(timeout)
    if t.is_alive():
        with _lock:
            _inflight[key] = t
        return HANG
    return result.get("v", ERROR)


def probe(path: Path, *, read: bool = False, timeout: float = 5.0) -> str:
    """``ok`` | ``missing`` | ``denied`` | ``dead`` (an I/O error: a dead mount) | ``hang`` (no answer in time) |
    ``error``. Never takes much longer than ``timeout``."""
    key = f"{path}|{read}"
    with _lock:
        stuck = _inflight.get(key)
        if stuck is not None:
            if _still_running(stuck):
                return HANG
            del _inflight[key]  # it finished at last (and is reaped): ask afresh
    if ISOLATE:
        state = _in_child(key, path, read, timeout)
        if state is not None:
            return state
    return _in_thread(key, path, read, timeout)


def stuck() -> int:
    """How many probes are still waiting on a drive that does not answer."""
    with _lock:
        return sum(_still_running(w) for w in _inflight.values())


def unhealthy(state: str) -> bool:
    return state in {DEAD, HANG}


def describe(state: str) -> str:
    return {DEAD: "Input/output error", HANG: "it does not answer at all"}.get(state, state)
