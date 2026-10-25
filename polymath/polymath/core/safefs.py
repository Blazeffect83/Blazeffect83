"""File writes that never follow a link someone else planted.

The root helpers (``body.volumes``, ``body.heal``) write into directories the ``polymath`` user owns: the brain's
folder on a drive, and ``/run/polymath``. A plain ``write_text`` + ``chown`` there follows a symlink. A compromised
agent, or a crafted USB drive, could plant ``x.tmp -> /etc/passwd`` and have root overwrite or hand over any file.

These helpers open the directory itself with ``O_NOFOLLOW`` and work relative to it (``dir_fd``):
- the temporary file is created with ``O_CREAT | O_EXCL | O_NOFOLLOW``, so it never opens an existing file or link;
- ownership and mode are set on the open descriptor (``fchown``/``fchmod``), never on a path;
- the final rename replaces a link rather than writing through it.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def safe_write(
    directory: Path | str, name: str, data: str | bytes, *, mode: int = 0o644, owner: tuple[int, int] | None = None
) -> None:
    """Atomically write ``directory/name``. Raises ``OSError`` (``ELOOP``) if ``directory`` is a symlink."""
    if "/" in name or name in {"", ".", ".."}:
        raise ValueError(f"not a plain file name: {name!r}")
    raw = data.encode("utf-8") if isinstance(data, str) else data
    dfd = os.open(directory, _DIR)
    try:
        tmp = f".{name}.{os.getpid()}.tmp"
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp, dir_fd=dfd)  # a stale one, or something planted: removed, never followed
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=dfd)
        try:
            view = memoryview(raw)
            while view:
                view = view[os.write(fd, view) :]
            os.fchmod(fd, mode)
            if owner is not None:
                os.fchown(fd, *owner)
        except BaseException:
            os.close(fd)
            with contextlib.suppress(OSError):
                os.unlink(tmp, dir_fd=dfd)
            raise
        os.close(fd)
        os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
    finally:
        os.close(dfd)


def safe_dir(path: Path | str, *, mode: int | None = None, owner: tuple[int, int] | None = None) -> bool:
    """Make ``path`` (one level) if missing, refusing a symlink; set owner/mode through its own descriptor.

    Returns True when it was created. Raises ``OSError`` (``ELOOP``/``ENOTDIR``) for a link or a non-directory."""
    created = False
    try:
        os.mkdir(path, 0o700)  # never follows a link: an existing one is FileExistsError
        created = True
    except FileExistsError:
        pass
    dfd = os.open(path, _DIR)
    try:
        if owner is not None:
            os.fchown(dfd, *owner)
        if mode is not None:
            os.fchmod(dfd, mode)
    finally:
        os.close(dfd)
    return created
