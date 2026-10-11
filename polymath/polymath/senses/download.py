"""Resumable, time-sliced file downloader.

State lives next to the file: ``name.part`` (bytes so far) and
``name.meta.json`` (URL, validators, the fsync-verified length). A slice
appends until its time budget runs out, then fsyncs and records the verified
length. After a power cut the part file is truncated back to the verified
length, so no unverified tail is ever trusted. Resumption uses HTTP ``Range``
with ``If-Range`` so a file that changed on the server restarts cleanly.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from polymath.senses.net import FetchError, HttpClient


@dataclass
class DownloadState:
    url: str
    path: str
    verified: int = 0
    total: int | None = None
    etag: str | None = None
    last_modified: str | None = None
    complete: bool = False
    limit: int | None = None  # sample mode: stop after this many bytes

    @property
    def validator(self) -> str | None:
        return self.etag if self.etag and not self.etag.startswith("W/") else self.last_modified


def _meta_path(dest: Path) -> Path:
    return dest.with_name(dest.name + ".meta.json")


def _part_path(dest: Path) -> Path:
    return dest.with_name(dest.name + ".part")


def _write_meta(dest: Path, state: DownloadState) -> None:
    tmp = dest.with_name(dest.name + ".meta.tmp")
    tmp.write_text(json.dumps(asdict(state)))
    with open(tmp, "rb") as fh:
        os.fsync(fh.fileno())
    os.replace(tmp, _meta_path(dest))


def load_state(dest: Path) -> DownloadState | None:
    try:
        return DownloadState(**json.loads(_meta_path(dest).read_text()))
    except (OSError, ValueError, TypeError):
        return None


def available_bytes(dest: Path) -> int:
    """Bytes safe to read now (the verified prefix, or the whole finished file)."""
    st = load_state(dest)
    if st is None:
        return dest.stat().st_size if dest.exists() else 0
    return st.verified


def readable_path(dest: Path) -> Path:
    return dest if dest.exists() else _part_path(dest)


def download_step(
    client: HttpClient,
    url: str,
    dest: Path,
    *,
    time_budget: float = 15.0,
    limit: int | None = None,
    chunk: int = 1 << 20,
) -> DownloadState:
    """Advance the download of ``url`` into ``dest`` for at most ``time_budget`` seconds."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    state = load_state(dest)
    if state is None or state.url != url:
        state = DownloadState(url=url, path=str(dest), limit=limit)
    state.limit = limit
    if state.complete and dest.exists():
        return state
    part = _part_path(dest)
    if part.exists() and part.stat().st_size != state.verified:
        with open(part, "r+b") as fh:  # drop any tail written after the last fsync
            fh.truncate(min(state.verified, part.stat().st_size))
        state.verified = part.stat().st_size
    if not part.exists():
        state.verified = 0
    deadline = time.monotonic() + time_budget
    headers: dict[str, str] = {}
    if state.verified:
        headers["Range"] = f"bytes={state.verified}-"
        if state.validator:
            headers["If-Range"] = state.validator
    with client.stream(url, headers, decode=False) as resp:
        if resp.status == 416:  # nothing left to fetch
            state.complete = state.total is None or state.verified >= state.total
            return _finish(dest, state)
        if resp.status not in (200, 206):
            raise FetchError(
                f"HTTP {resp.status} downloading {url}",
                status=resp.status,
                transient=resp.status >= 500 or resp.status == 429,
            )
        mode = "ab"
        if resp.status == 200:
            mode = "wb"  # range ignored or file changed: start over
            state.verified = 0
            length = resp.headers.get("content-length")
            state.total = int(length) if length and length.isdigit() else None
        else:
            crange = resp.headers.get("content-range", "")
            total = crange.rsplit("/", 1)[-1]
            state.total = int(total) if total.isdigit() else state.total
            start = crange.split(" ", 1)[-1].split("-", 1)[0]
            if not start.isdigit() or int(start) != state.verified:
                raise FetchError(f"server returned unexpected range {crange!r}", transient=True)
        state.etag = resp.headers.get("etag", state.etag)
        state.last_modified = resp.headers.get("last-modified", state.last_modified)
        written = state.verified
        with open(part, mode) as fh:
            for data in resp.iter_chunks(chunk):
                if limit is not None and written + len(data) > limit:
                    data = data[: max(0, limit - written)]
                fh.write(data)
                written += len(data)
                if (limit is not None and written >= limit) or time.monotonic() >= deadline:
                    break
            fh.flush()
            os.fsync(fh.fileno())
        state.verified = written
    finished = state.total is not None and state.verified >= state.total
    state.complete = finished or (limit is not None and state.verified >= limit)
    return _finish(dest, state)


def _finish(dest: Path, state: DownloadState) -> DownloadState:
    if state.complete:
        part = _part_path(dest)
        if part.exists():
            os.replace(part, dest)
    _write_meta(dest, state)
    return state


def remove_download(dest: Path) -> int:
    """Delete a download and its state; returns bytes freed."""
    freed = 0
    for p in (dest, _part_path(dest), _meta_path(dest)):
        if p.exists():
            freed += p.stat().st_size
            p.unlink()
    return freed
