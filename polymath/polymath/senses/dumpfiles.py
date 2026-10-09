"""Shared machinery for file-based dumps: download job, resumable record iterators."""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, TypeVar

from polymath.core.jobs import JobContext, JobOutcome, PermanentError
from polymath.senses.download import download_step, remove_download
from polymath.senses.net import FetchError, HttpClient

T = TypeVar("T")
MAX_OPEN_ITERATORS = 4


def raw_path(ctx: JobContext, rel: str) -> Path:
    base = ctx.config.paths.raw_dir.resolve()
    path = (base / rel).resolve()
    if base not in path.parents:
        raise PermanentError(f"refusing path outside raw dir: {rel}")
    return path


def download_job(ctx: JobContext) -> JobOutcome:
    """Job ``dump.download``: payload {url, dest, limit?, then?: {kind, payload, key, priority}}.

    Runs in slices; when finished it enqueues the follow-up ingest job.
    """
    p = ctx.job.payload
    client: HttpClient = ctx.services["http"]
    dest = raw_path(ctx, p["dest"])
    try:
        state = download_step(
            client,
            p["url"],
            dest,
            time_budget=max(0.0, ctx.time_left() - 0.5),
            limit=p.get("limit"),
            chunk=ctx.config.senses.download_chunk,
        )
    except FetchError as exc:
        if exc.status in (403, 404, 410):
            raise PermanentError(str(exc)) from exc
        raise
    progress = {"bytes": state.verified, "total": state.total}
    if not state.complete:
        return JobOutcome(done=False, checkpoint=progress, value=0.0, result=progress)
    then = p.get("then")
    if then:
        ctx.scheduler.enqueue(
            then["kind"], then.get("payload", {}), key=then.get("key"), priority=float(then.get("priority", 0.0))
        )
    return JobOutcome(done=True, value=0.1, result=progress)


def resume_records(ctx: JobContext, make: Callable[[], Iterator[T]], position: int) -> Iterator[T]:
    """An iterator positioned after ``position`` records.

    Live iterators are cached between slices of the same job (cheap); after a
    restart the source is re-read and the first ``position`` records skipped
    (correct, merely slower).
    """
    cache: dict[str, tuple[int, Iterator[Any]]] = ctx.services.setdefault("iterators", {})
    entry = cache.pop(ctx.job.key, None)
    if entry is not None and entry[0] == position:
        return entry[1]
    it = make()
    if position:
        next(itertools.islice(it, position, position), None)
    return it


def park_records(ctx: JobContext, it: Iterator[Any], position: int) -> None:
    cache: dict[str, tuple[int, Iterator[Any]]] = ctx.services.setdefault("iterators", {})
    cache[ctx.job.key] = (position, it)
    while len(cache) > MAX_OPEN_ITERATORS:
        cache.pop(next(iter(cache)))


def finish_file(ctx: JobContext, rel: str) -> int:
    """Delete a consumed raw file (the knowledge is in the database now)."""
    ctx.services.setdefault("iterators", {}).pop(ctx.job.key, None)
    if ctx.job.payload.get("keep_raw"):
        return 0
    return remove_download(raw_path(ctx, rel))


def run_records(
    ctx: JobContext,
    make: Callable[[], Iterator[T]],
    handle: Callable[[T], float],
    *,
    rel: str,
    limit_key: str = "stored",
) -> JobOutcome:
    """Generic sliced loop over records: checkpoint = records consumed."""
    cp = ctx.job.checkpoint or {"position": 0, limit_key: 0}
    position = int(cp["position"])
    limit = ctx.job.payload.get("limit")
    it = resume_records(ctx, make, position)
    value = 0.0
    exhausted = True
    for record in it:
        position += 1
        v = handle(record)
        if v > 0:
            cp[limit_key] = int(cp.get(limit_key, 0)) + 1
            value += v
        if limit is not None and cp[limit_key] >= limit:
            break
        if ctx.should_stop():
            exhausted = False
            break
        if position % 100 == 0:
            ctx.tick()
    cp["position"] = position
    done = exhausted or (limit is not None and cp[limit_key] >= limit)
    if done:
        cp["freed_bytes"] = finish_file(ctx, rel)
    else:
        park_records(ctx, it, position)
    return JobOutcome(done=done, checkpoint=cp, value=value, result=dict(cp))
