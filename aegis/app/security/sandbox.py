"""Sandboxed process execution.

Backends:

``bwrap`` (strong, default when available)
    bubblewrap with all namespaces unshared (network off unless explicitly
    approved), read-only system dirs, only the objective workspace writable,
    a private /tmp, cleared environment, unprivileged uid 65534, no access to
    the AEGIS data dir, ``.env``, Docker socket or host home directories.
    Plus rlimits (memory, CPU time, processes, file size) and a wall-clock
    timeout that kills the whole process group.

``rlimit`` (weak, opt-in only)
    rlimits + cleared environment + timeout, but **no filesystem or network
    isolation**. Refused unless ``SANDBOX_ALLOW_WEAK=true``.
"""
from __future__ import annotations

import logging
import os
import resource
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

OUTPUT_CAP = 256 * 1024
SAFE_PATH = "/usr/local/bin:/usr/bin:/bin"


class SandboxUnavailable(Exception):
    pass


@dataclass
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    backend: str
    truncated: bool = False
    resource_usage: dict = field(default_factory=dict)


@dataclass
class SandboxLimits:
    timeout_seconds: int = 120
    memory_mb: int = 512
    max_processes: int = 64
    max_file_mb: int = 50


def _drain(stream, buf: list, cap: int, flag: list):
    total = 0
    while True:
        chunk = stream.read(8192)
        if not chunk:
            break
        if total < cap:
            buf.append(chunk[: cap - total])
        else:
            flag[0] = True
        total += len(chunk)
    stream.close()


def _preexec(limits: SandboxLimits, cpu_seconds: int):
    def fn():
        os.setsid()
        mem = limits.memory_mb * 1024 * 1024
        for res, val in (
            (resource.RLIMIT_AS, mem),
            (resource.RLIMIT_CPU, cpu_seconds),
            (resource.RLIMIT_FSIZE, limits.max_file_mb * 1024 * 1024),
            (resource.RLIMIT_CORE, 0),
            (resource.RLIMIT_NOFILE, 256),
        ):
            try:
                resource.setrlimit(res, (val, val))
            except (ValueError, OSError):
                pass
        if os.getuid() != 0:
            try:
                resource.setrlimit(resource.RLIMIT_NPROC, (limits.max_processes, limits.max_processes))
            except (ValueError, OSError):
                pass
    return fn


class Sandbox:
    def __init__(self, backend: str = "auto", allow_weak: bool = False, python: str = "",
                 limits: SandboxLimits | None = None, workspace_root: Path | None = None):
        self.requested = backend
        self.allow_weak = allow_weak
        self.python = python or sys.executable
        self.limits = limits or SandboxLimits()
        self.workspace_root = Path(workspace_root).resolve() if workspace_root else None
        self._backend: str | None = None
        self._probe_detail = ""

    # -- backend selection --------------------------------------------------
    def bwrap_available(self) -> bool:
        exe = shutil.which("bwrap")
        if not exe:
            self._probe_detail = "bwrap not installed (apt install bubblewrap)"
            return False
        try:
            cmd = self._bwrap_argv(Path("/tmp"), "/", network=False) + ["/bin/true"]
            cmd = [c for c in cmd]
            r = subprocess.run(cmd, capture_output=True, timeout=15)
        except (OSError, subprocess.SubprocessError) as exc:
            self._probe_detail = f"bwrap probe failed: {exc}"
            return False
        if r.returncode != 0:
            self._probe_detail = f"bwrap probe failed: {r.stderr.decode(errors='replace')[:300]}"
            return False
        self._probe_detail = "bwrap ok"
        return True

    @property
    def backend(self) -> str:
        if self._backend is None:
            self._backend = self._select()
        return self._backend

    def _select(self) -> str:
        req = self.requested
        if req == "disabled":
            return "disabled"
        if req in ("auto", "bwrap") and self.bwrap_available():
            return "bwrap"
        if req == "bwrap":
            return "unavailable"
        if req == "rlimit" or (req == "auto" and self.allow_weak):
            return "rlimit" if self.allow_weak else "unavailable"
        return "unavailable"

    def status(self) -> dict:
        b = self.backend
        return {
            "backend": b,
            "strong_isolation": b == "bwrap",
            "detail": self._probe_detail or ("weak sandbox: no filesystem/network isolation" if b == "rlimit" else ""),
            "python": self.python,
        }

    # -- bwrap argv ---------------------------------------------------------
    def _bwrap_argv(self, workdir: Path, chdir: str, network: bool) -> list[str]:
        argv = ["bwrap", "--unshare-all", "--die-with-parent", "--new-session", "--clearenv",
                "--uid", "65534", "--gid", "65534", "--hostname", "aegis-sandbox"]
        if network:
            argv.append("--share-net")
        # /proc, /dev and a private /tmp first so later binds can sit beneath them.
        argv += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--ro-bind", "/usr", "/usr"]
        for d in ("bin", "sbin", "lib", "lib32", "lib64"):
            p = Path("/") / d
            if p.is_symlink():
                argv += ["--symlink", os.readlink(p), f"/{d}"]
            elif p.is_dir():
                argv += ["--ro-bind", str(p), f"/{d}"]
        for etc in ("/etc/ld.so.cache", "/etc/ld.so.conf", "/etc/ld.so.conf.d", "/etc/localtime",
                    "/etc/alternatives", "/etc/ssl", "/etc/ca-certificates", "/etc/passwd", "/etc/group"):
            argv += ["--ro-bind-try", etc, etc]
        if network:
            for etc in ("/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf", "/etc/gai.conf"):
                argv += ["--ro-bind-try", etc, etc]
        # Python runtime (venv + base install) read-only.
        prefixes = {Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(),
                    Path(self.python).parent.parent.resolve(), Path(self.python).resolve().parent.parent}
        for pfx in sorted(prefixes):
            if not str(pfx).startswith("/usr") and pfx.exists() and str(pfx) != "/":
                argv += ["--ro-bind", str(pfx), str(pfx)]
        argv += ["--bind", str(workdir), "/workspace", "--remount-ro", "/", "--chdir", chdir,
                 "--setenv", "PATH", f"{Path(self.python).parent}:{SAFE_PATH}",
                 "--setenv", "HOME", "/workspace", "--setenv", "TMPDIR", "/tmp",
                 "--setenv", "LANG", "C.UTF-8", "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
                 "--setenv", "PYTHONNOUSERSITE", "1", "--setenv", "PYTHONPATH", "/workspace/.deps"]
        return argv

    # -- execution ----------------------------------------------------------
    def run(self, argv: list[str], workdir: Path, *, network: bool = False,
            timeout: int | None = None, stdin: str | None = None) -> SandboxResult:
        backend = self.backend
        if backend in ("unavailable", "disabled"):
            raise SandboxUnavailable(
                "no sandbox available: install bubblewrap, or set SANDBOX_ALLOW_WEAK=true to accept a "
                f"weak rlimit-only sandbox ({self._probe_detail or backend})")
        workdir = Path(workdir).resolve()
        if self.workspace_root and not workdir.is_relative_to(self.workspace_root):
            raise SandboxUnavailable("workdir is outside the workspace root")
        workdir.mkdir(parents=True, exist_ok=True)
        timeout = timeout or self.limits.timeout_seconds
        if backend == "bwrap":
            os.chmod(workdir, 0o777)  # uid 65534 inside the userns must be able to write
            full = self._bwrap_argv(workdir, "/workspace", network) + list(argv)
            env = {"PATH": SAFE_PATH}
            cwd = "/"
        else:
            if network:
                raise SandboxUnavailable("network access requires the bwrap sandbox backend")
            full = list(argv)
            env = {"PATH": f"{Path(self.python).parent}:{SAFE_PATH}", "HOME": str(workdir),
                   "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
                   "PYTHONPATH": str(workdir / ".deps")}
            cwd = str(workdir)
        return self._spawn(full, cwd, env, timeout, stdin, backend)

    def _spawn(self, argv, cwd, env, timeout, stdin, backend) -> SandboxResult:
        start = time.monotonic()
        proc = subprocess.Popen(
            argv, cwd=cwd, env=env, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            preexec_fn=_preexec(self.limits, timeout + 5), close_fds=True,
        )
        out, err, trunc = [], [], [False]
        threads = [threading.Thread(target=_drain, args=(proc.stdout, out, OUTPUT_CAP, trunc), daemon=True),
                   threading.Thread(target=_drain, args=(proc.stderr, err, OUTPUT_CAP, trunc), daemon=True)]
        for t in threads:
            t.start()
        if stdin is not None:
            try:
                proc.stdin.write(stdin.encode())
                proc.stdin.close()
            except BrokenPipeError:
                pass
        timed_out = False
        rusage = None
        status = 0
        deadline = start + timeout
        while True:
            pid, status, rusage = os.wait4(proc.pid, os.WNOHANG)
            if pid:
                break
            if time.monotonic() > deadline:
                timed_out = True
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                _, status, rusage = os.wait4(proc.pid, 0)
                break
            time.sleep(0.02)
        proc.returncode = os.waitstatus_to_exitcode(status)
        for t in threads:
            t.join(timeout=5)
        # Kill any stragglers left in the process group.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        usage = {}
        if rusage is not None:
            usage = {"cpu_user_s": round(rusage.ru_utime, 3), "cpu_sys_s": round(rusage.ru_stime, 3),
                     "max_rss_mb": round(rusage.ru_maxrss / 1024, 1)}
        return SandboxResult(
            exit_code=-9 if timed_out else proc.returncode,
            stdout=b"".join(out).decode(errors="replace"),
            stderr=b"".join(err).decode(errors="replace"),
            duration_ms=int((time.monotonic() - start) * 1000),
            timed_out=timed_out, backend=backend, truncated=trunc[0], resource_usage=usage,
        )
