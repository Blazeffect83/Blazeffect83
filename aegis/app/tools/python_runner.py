"""Sandboxed development tools: run Python, run tests, install packages."""
from __future__ import annotations

import re
import uuid

from pydantic import BaseModel, Field, field_validator

from ..security.sandbox import SandboxUnavailable
from .filesystem import safe_path, workspace_for
from .registry import Tool, ToolContext, ToolError, ToolResult

_PKG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,80}(\[[A-Za-z0-9,_\-]+\])?((==|>=|<=|~=)[A-Za-z0-9.*+!]+)?$")
_PYTEST_SUMMARY = re.compile(r"(\d+) (passed|failed|error|errors|skipped|xfailed|xpassed)")


class PythonArgs(BaseModel):
    code: str | None = Field(default=None, description="Python source to run (written to a temp file)")
    script: str | None = Field(default=None, description="workspace-relative script path to run")
    args: list[str] = Field(default_factory=list)
    stdin: str | None = None
    timeout_seconds: int = Field(default=60, gt=0, le=600)
    network: bool = Field(default=False, description="network access (requires human approval)")

    @field_validator("args")
    @classmethod
    def _args(cls, v):
        if len(v) > 50 or any(len(a) > 1000 or "\x00" in a for a in v):
            raise ValueError("too many / too long arguments")
        return v


class TestArgs(BaseModel):
    path: str = Field(default=".", description="workspace-relative test file or directory")
    timeout_seconds: int = Field(default=180, gt=0, le=900)
    extra_args: list[str] = Field(default_factory=list, description="e.g. ['-k', 'name']")

    @field_validator("extra_args")
    @classmethod
    def _extra(cls, v):
        allowed = re.compile(r"^(-k|-x|-q|-v|--maxfail=\d+|-m|[A-Za-z0-9_ .:\[\]\-]+)$")
        if len(v) > 10 or not all(allowed.match(a) for a in v):
            raise ValueError("unsupported pytest arguments")
        return v


class PipArgs(BaseModel):
    packages: list[str] = Field(min_length=1, max_length=20)

    @field_validator("packages")
    @classmethod
    def _pkgs(cls, v):
        bad = [p for p in v if not _PKG.match(p)]
        if bad:
            raise ValueError(f"invalid package specifiers: {bad}")
        return v


def _sandbox_run(ctx: ToolContext, argv: list[str], *, network=False, timeout=None, stdin=None):
    sb = ctx.services.sandbox
    ws = workspace_for(ctx)
    try:
        return sb.run(argv, ws, network=network, timeout=timeout, stdin=stdin)
    except SandboxUnavailable as exc:
        raise ToolError(str(exc), "sandbox_unavailable") from exc


def _py(ctx: ToolContext) -> str:
    return ctx.services.sandbox.python


def _python_env_prefix() -> list[str]:
    return ["-s", "-X", "utf8"]


def _run_python(args: PythonArgs, ctx: ToolContext) -> ToolResult:
    if bool(args.code) == bool(args.script):
        raise ToolError("provide exactly one of 'code' or 'script'", "invalid_arguments")
    ws = workspace_for(ctx)
    if args.code is not None:
        rel = f".aegis_run_{uuid.uuid4().hex[:8]}.py"
        (ws / rel).write_text(args.code, encoding="utf-8")
    else:
        safe_path(ws, args.script, must_exist=True)
        rel = args.script
    sandbox_path = f"/workspace/{rel}" if ctx.services.sandbox.backend == "bwrap" else str(ws / rel)
    argv = [_py(ctx), *_python_env_prefix(), sandbox_path, *args.args]
    try:
        r = _sandbox_run(ctx, argv, network=args.network, timeout=args.timeout_seconds, stdin=args.stdin)
    finally:
        if args.code is not None:
            (ws / rel).unlink(missing_ok=True)
    ok = r.exit_code == 0 and not r.timed_out
    return ToolResult(
        ok, {"exit_code": r.exit_code, "stdout": r.stdout[-20000:], "stderr": r.stderr[-20000:],
             "timed_out": r.timed_out, "backend": r.backend, "truncated": r.truncated},
        f"exit {r.exit_code}" + (" (timeout)" if r.timed_out else ""), exit_code=r.exit_code,
        error=None if ok else (r.stderr[-2000:] or r.stdout[-2000:] or f"exit {r.exit_code}"),
        error_class=None if ok else ("timeout" if r.timed_out else "nonzero_exit"),
        resource_usage={**r.resource_usage, "duration_ms": r.duration_ms})


def parse_pytest(output: str) -> dict:
    counts: dict[str, int] = {}
    tail = "\n".join(output.strip().splitlines()[-5:])
    for n, kind in _PYTEST_SUMMARY.findall(tail):
        kind = "errors" if kind in ("error", "errors") else kind
        counts[kind] = counts.get(kind, 0) + int(n)
    failed_tests = re.findall(r"^FAILED (\S+)", output, re.M)
    return {"counts": counts, "failed_tests": failed_tests[:50]}


def _run_tests(args: TestArgs, ctx: ToolContext) -> ToolResult:
    ws = workspace_for(ctx)
    if args.path not in (".", ""):
        safe_path(ws, args.path, must_exist=True)
    target = args.path or "."
    argv = [_py(ctx), *_python_env_prefix(), "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider",
            "-rf", target, *args.extra_args]
    r = _sandbox_run(ctx, argv, timeout=args.timeout_seconds)
    summary = parse_pytest(r.stdout)
    c = summary["counts"]
    ok = r.exit_code == 0 and c.get("passed", 0) > 0 and not c.get("failed") and not c.get("errors")
    desc = ", ".join(f"{v} {k}" for k, v in c.items()) or f"exit {r.exit_code}"
    return ToolResult(
        ok, {"exit_code": r.exit_code, "summary": summary, "stdout": r.stdout[-20000:], "stderr": r.stderr[-5000:],
             "timed_out": r.timed_out, "backend": r.backend},
        desc, exit_code=r.exit_code,
        error=None if ok else (r.stdout[-4000:] + "\n" + r.stderr[-1000:]).strip(),
        error_class=None if ok else ("timeout" if r.timed_out else "tests_failed" if c.get("failed") or
                                     c.get("errors") else "no_tests" if r.exit_code == 5 else "nonzero_exit"),
        resource_usage={**r.resource_usage, "duration_ms": r.duration_ms})


def _pip_install(args: PipArgs, ctx: ToolContext) -> ToolResult:
    index = ctx.services.settings.package_index_url
    argv = [_py(ctx), "-m", "pip", "install", "--no-input", "--disable-pip-version-check", "--no-cache-dir",
            "--target", "/workspace/.deps" if ctx.services.sandbox.backend == "bwrap"
            else str(workspace_for(ctx) / ".deps"), "--index-url", index, *args.packages]
    r = _sandbox_run(ctx, argv, network=True, timeout=600)
    ok = r.exit_code == 0
    return ToolResult(ok, {"exit_code": r.exit_code, "stdout": r.stdout[-8000:], "stderr": r.stderr[-8000:]},
                      f"pip exit {r.exit_code}", exit_code=r.exit_code,
                      error=None if ok else r.stderr[-2000:], error_class=None if ok else "install_failed",
                      resource_usage={**r.resource_usage, "duration_ms": r.duration_ms})


def _describe_python(a: PythonArgs):
    return ("objective workspace; outbound network" if a.network else "objective workspace",
            "sandboxed code gains outbound network access for this run" if a.network else "sandboxed execution",
            (a.code or f"python {a.script} {' '.join(a.args)}")[:4000])


def _describe_pip(a: PipArgs):
    return ("objective workspace .deps directory; package index (network)",
            "downloads and installs third-party packages (may execute package build code inside the sandbox)",
            "pip install --target .deps " + " ".join(a.packages))


TOOLS = [
    Tool("python_run", "Run Python code or a workspace script inside the sandbox.", PythonArgs, _run_python,
         tier=1, permissions=("sandbox:exec",), timeout_seconds=600,
         resource_limits="sandbox memory/CPU/process/time limits; network off unless approved",
         reversible=False, idempotent=False,
         tier_for=lambda a: 2 if a.network else 1, describe=_describe_python),
    Tool("run_tests", "Run pytest on workspace tests inside the sandbox and summarise results.", TestArgs,
         _run_tests, tier=1, permissions=("sandbox:exec",), timeout_seconds=900,
         resource_limits="sandbox limits; no network", reversible=True, idempotent=True),
    Tool("pip_install", "Install Python packages from the approved index into the workspace (.deps).", PipArgs,
         _pip_install, tier=2, permissions=("sandbox:exec", "network:package_index"), timeout_seconds=600,
         reversible=True, idempotent=True, describe=_describe_pip),
]
