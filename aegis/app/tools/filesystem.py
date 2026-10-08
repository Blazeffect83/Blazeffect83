"""Workspace-confined file tools. Every path is resolved and must stay inside
the objective's workspace; symlinks are refused anywhere along the path."""
from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field

from .registry import Tool, ToolContext, ToolError, ToolResult

MAX_WRITE_BYTES = 1_000_000
MAX_READ_BYTES = 200_000


def workspace_for(ctx: ToolContext) -> Path:
    root = ctx.services.settings.workspace
    ws = (root / (ctx.objective_id or "_scratch")).resolve()
    if not ws.is_relative_to(root):
        raise ToolError("workspace escapes root", "path_violation")
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def safe_path(ws: Path, rel: str, *, must_exist: bool = False) -> Path:
    if not rel or "\x00" in rel:
        raise ToolError("empty or invalid path", "path_violation")
    p = Path(rel)
    if p.is_absolute():
        raise ToolError("absolute paths are not allowed; use a path relative to the workspace", "path_violation")
    if any(part == ".." for part in p.parts):
        raise ToolError("'..' is not allowed in paths", "path_violation")
    target = ws / p
    # Refuse symlinks in any existing component (prevents link-based escapes).
    cur = ws
    for part in p.parts:
        cur = cur / part
        if cur.is_symlink():
            raise ToolError(f"symlinks are not allowed ({part})", "path_violation")
    resolved = target.resolve()
    if not resolved.is_relative_to(ws):
        raise ToolError("path escapes the workspace", "path_violation")
    if must_exist and not resolved.exists():
        raise ToolError(f"{rel} does not exist", "not_found")
    return resolved


class WriteArgs(BaseModel):
    path: str = Field(description="path relative to the objective workspace")
    content: str
    append: bool = False


class ReadArgs(BaseModel):
    path: str
    max_bytes: int = Field(default=MAX_READ_BYTES, le=MAX_READ_BYTES, gt=0)


class ListArgs(BaseModel):
    path: str = "."


def _write(args: WriteArgs, ctx: ToolContext) -> ToolResult:
    data = args.content.encode()
    if len(data) > MAX_WRITE_BYTES:
        raise ToolError(f"content exceeds {MAX_WRITE_BYTES} bytes", "too_large")
    ws = workspace_for(ctx)
    p = safe_path(ws, args.path)
    p.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if args.append else os.O_TRUNC) | os.O_NOFOLLOW
    fd = os.open(p, flags, 0o644)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return ToolResult(True, {"path": args.path, "bytes": len(data)}, f"wrote {len(data)} bytes to {args.path}")


def _read(args: ReadArgs, ctx: ToolContext) -> ToolResult:
    ws = workspace_for(ctx)
    p = safe_path(ws, args.path, must_exist=True)
    if not p.is_file():
        raise ToolError(f"{args.path} is not a file", "not_found")
    fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as f:
        data = f.read(args.max_bytes + 1)
    text = data[: args.max_bytes].decode(errors="replace")
    return ToolResult(True, {"path": args.path, "content": text, "truncated": len(data) > args.max_bytes},
                      f"read {min(len(data), args.max_bytes)} bytes from {args.path}")


def _list(args: ListArgs, ctx: ToolContext) -> ToolResult:
    ws = workspace_for(ctx)
    base = ws if args.path in (".", "") else safe_path(ws, args.path, must_exist=True)
    entries = []
    for root, dirs, files in os.walk(base, followlinks=False):
        dirs[:] = [d for d in dirs if not d.startswith((".deps", "__pycache__", ".pytest_cache"))]
        for f in files:
            fp = Path(root) / f
            entries.append({"path": str(fp.relative_to(ws)), "bytes": fp.lstat().st_size})
            if len(entries) >= 500:
                break
    return ToolResult(True, {"files": entries}, f"{len(entries)} files")


TOOLS = [
    Tool("file_write", "Create or overwrite a text file inside the objective workspace.", WriteArgs, _write,
         tier=1, permissions=("workspace:write",), timeout_seconds=10,
         resource_limits=f"max {MAX_WRITE_BYTES} bytes", reversible=False, idempotent=True),
    Tool("file_read", "Read a text file from the objective workspace.", ReadArgs, _read, tier=0,
         permissions=("workspace:read",), timeout_seconds=10, resource_limits=f"max {MAX_READ_BYTES} bytes"),
    Tool("file_list", "List files in the objective workspace.", ListArgs, _list, tier=0,
         permissions=("workspace:read",), timeout_seconds=10),
]
