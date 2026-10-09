"""System tools. Every command is a fixed argv built in code from allowlisted
names; nothing the model provides is ever passed to a shell."""
from __future__ import annotations

import re
import shutil
import subprocess
from collections import deque
from pathlib import Path

from pydantic import BaseModel, Field

from ..observability import metrics
from .registry import Tool, ToolContext, ToolError, ToolResult

_NAME = re.compile(r"^[A-Za-z0-9@._\-]{1,100}$")


class NoArgs(BaseModel):
    pass


class ServiceArgs(BaseModel):
    service: str = Field(max_length=100)


class LogArgs(BaseModel):
    path: str
    lines: int = Field(default=100, ge=1, le=2000)
    grep: str | None = Field(default=None, max_length=200)


class ContainerArgs(BaseModel):
    container: str | None = None


def _run_fixed(argv: list[str], timeout: int = 20) -> subprocess.CompletedProcess:
    exe = shutil.which(argv[0])
    if not exe:
        raise ToolError(f"{argv[0]} is not installed", "unavailable")
    return subprocess.run([exe, *argv[1:]], capture_output=True, text=True, timeout=timeout,
                          env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})


def _metrics(_a: NoArgs, ctx: ToolContext) -> ToolResult:
    snap = metrics.snapshot(ctx.services.settings.data_dir)
    return ToolResult(True, snap.as_dict(), "metrics captured")


def _check_service(name: str, ctx: ToolContext) -> str:
    if not _NAME.match(name) or name not in ctx.services.settings.approved_services:
        raise ToolError(f"service '{name}' is not on APPROVED_SERVICES", "policy")
    return name


def _service_status(a: ServiceArgs, ctx: ToolContext) -> ToolResult:
    name = _check_service(a.service, ctx)
    r = _run_fixed(["systemctl", "show", name, "--no-pager",
                    "--property=ActiveState,SubState,MainPID,NRestarts,ActiveEnterTimestamp,MemoryCurrent"])
    props = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
    return ToolResult(r.returncode == 0, {"service": name, **props}, props.get("ActiveState", "unknown"),
                      exit_code=r.returncode, error=r.stderr[-500:] or None)


def _read_log(a: LogArgs, ctx: ToolContext) -> ToolResult:
    approved = [str(Path(p).resolve()) for p in ctx.services.settings.approved_log_files]
    approved.append(str((ctx.services.settings.logs / "aegis.log").resolve()))
    p = Path(a.path).resolve()
    if str(p) not in approved:
        raise ToolError("log file is not on APPROVED_LOG_FILES", "policy")
    if not p.is_file():
        raise ToolError("log file not found", "not_found")
    pat = re.compile(re.escape(a.grep), re.I) if a.grep else None
    tail: deque = deque(maxlen=a.lines)
    with open(p, encoding="utf-8", errors="replace") as f:
        for line in f:
            if pat is None or pat.search(line):
                tail.append(line.rstrip("\n")[:2000])
    return ToolResult(True, {"path": str(p), "lines": list(tail)}, f"{len(tail)} lines")


def _containers(a: ContainerArgs, ctx: ToolContext) -> ToolResult:
    approved = ctx.services.settings.approved_containers
    if not approved:
        raise ToolError("no containers approved (APPROVED_CONTAINERS)", "policy")
    names = [a.container] if a.container else approved
    if any(n not in approved or not _NAME.match(n) for n in names):
        raise ToolError("container is not on APPROVED_CONTAINERS", "policy")
    r = _run_fixed(["docker", "inspect", "--format",
                    "{{.Name}} {{.State.Status}} {{.State.Health}} restarts={{.RestartCount}}", *names])
    return ToolResult(r.returncode == 0, {"containers": r.stdout.splitlines()}, "docker inspect",
                      exit_code=r.returncode, error=r.stderr[-500:] or None)


def _health(_a: NoArgs, ctx: ToolContext) -> ToolResult:
    from ..observability.health import health_report
    rep = health_report(ctx.services)
    return ToolResult(rep["ok"], rep, "healthy" if rep["ok"] else "degraded")


def _restart(a: ServiceArgs, ctx: ToolContext) -> ToolResult:
    name = _check_service(a.service, ctx)
    # Requires a sudoers rule limited to exactly this command (see docs/SECURITY.md).
    r = _run_fixed(["sudo", "-n", "/usr/bin/systemctl", "restart", name], timeout=60)
    return ToolResult(r.returncode == 0, {"service": name, "stderr": r.stderr[-1000:]},
                      f"restart exit {r.returncode}", exit_code=r.returncode,
                      error=r.stderr[-1000:] or None if r.returncode else None,
                      error_class=None if r.returncode == 0 else "restart_failed")


TOOLS = [
    Tool("system_metrics", "Read CPU, memory, disk and temperature metrics.", NoArgs, _metrics, tier=0,
         permissions=("system:read",)),
    Tool("service_status", "Check the status of an approved systemd service.", ServiceArgs, _service_status,
         tier=0, permissions=("system:read",)),
    Tool("read_log", "Read the tail of an approved log file (optionally filtered).", LogArgs, _read_log, tier=0,
         permissions=("system:read_logs",)),
    Tool("container_status", "Inspect approved Docker containers.", ContainerArgs, _containers, tier=0,
         permissions=("system:read",)),
    Tool("health_check", "Run AEGIS' predefined health checks.", NoArgs, _health, tier=0,
         permissions=("system:read",)),
    Tool("service_restart", "Restart an approved systemd service (human approval required).", ServiceArgs,
         _restart, tier=2, permissions=("system:service_control",), timeout_seconds=60, reversible=False,
         idempotent=False,
         describe=lambda a: (f"systemd service {a.service}", "service briefly unavailable during restart",
                             f"sudo -n /usr/bin/systemctl restart {a.service}")),
]
