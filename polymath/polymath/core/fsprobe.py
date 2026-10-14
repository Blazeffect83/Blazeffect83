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
# ``direct``: read past the page cache (O_DIRECT), so a drive that froze is caught even while its data is still
# cached in memory (the Pi, 2026-10-14: the probes passed from cache, then opening the database hung)
_CHILD = (
    "import os,sys\n"
    "p,r,d=sys.argv[1],sys.argv[2]=='1',sys.argv[3]=='1'\n"
    "try:\n"
    "    done=False\n"
    "    if r and d and hasattr(os,'O_DIRECT'):\n"
    "        import mmap\n"
    "        try:\n"
    "            fd=os.open(p,os.O_RDONLY|os.O_DIRECT)\n"
    "            try: os.preadv(fd,[mmap.mmap(-1,4096)],0); done=True\n"
    "            finally: os.close(fd)\n"
    "        except OSError as e:\n"
    "            if e.errno!=22: raise\n"
    "    if r and not done:\n"
    "        with open(p,'rb') as f: f.read(4096)\n"
    "    if not r: os.stat(p)\n"
    "except IsADirectoryError: sys.exit(4)\n"
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


def _in_child(key: str, path: Path, read: bool, timeout: float, direct: bool = False) -> str | None:
    """The answer from a child process; None when no child could be started."""
    try:
        child = subprocess.Popen(
            [sys.executable, "-I", "-S", "-c", _CHILD, str(path), "1" if read else "0", "1" if direct else "0"],
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


def probe(path: Path, *, read: bool = False, direct: bool = False, timeout: float = 5.0) -> str:
    """``ok`` | ``missing`` | ``denied`` | ``dead`` (an I/O error: a dead mount) | ``hang`` (no answer in time) |
    ``error``. Never takes much longer than ``timeout``. With ``direct`` a read goes to the drive itself, not to
    what the kernel still has cached (where the filesystem cannot do that, an ordinary read)."""
    key = f"{path}|{read}|{direct}"
    with _lock:
        stuck = _inflight.get(key)
        if stuck is not None:
            if _still_running(stuck):
                return HANG
            del _inflight[key]  # it finished at last (and is reaped): ask afresh
    if ISOLATE:
        state = _in_child(key, path, read, timeout, direct)
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
