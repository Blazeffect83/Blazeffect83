"""Which build of Polymath is this: version number, git commit and commit date.

``install.sh`` writes ``build.json`` next to the virtual environment (``/opt/polymath/build.json``) on every
install or update. A running process reads it once at start (its own build); the live feed reads it again every
so often (what is installed now), so it can tell "running the newest code" from "an update is waiting for a
restart". Without ``build.json`` (a development checkout) the commit is read straight from ``.git``.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from polymath import __version__

BUILD_FILE = "build.json"


@dataclass(frozen=True)
class Build:
    version: str
    commit: str = ""  # short git hash
    date: str = ""  # commit date, YYYY-MM-DD
    installed: bool = False  # True when read from an installer-written build.json

    @property
    def key(self) -> str:
        """Identity used to compare builds: two processes run the same code when their keys match."""
        return f"{self.version}+{self.commit}" if self.commit else self.version

    def label(self) -> str:
        return f"v{self.version} · {self.commit}" if self.commit else f"v{self.version}"


def build_file() -> Path:
    env = os.environ.get("POLYMATH_BUILD_FILE")
    return Path(env) if env else Path(sys.prefix).parent / BUILD_FILE


def git_commit(start: Path) -> str:
    """Short commit hash of the git checkout containing ``start``, read from ``.git`` files ("" if none)."""
    for d in [start, *start.parents]:
        git = d / ".git"
        try:
            if git.is_file():  # worktree / submodule: "gitdir: <path>"
                text = git.read_text().strip()
                git = (d / text.split(":", 1)[1].strip()).resolve() if text.startswith("gitdir:") else git
            if not git.is_dir():
                continue
            head = (git / "HEAD").read_text().strip()
            if not head.startswith("ref:"):
                return head[:7]
            ref = head.split(":", 1)[1].strip()
            common = git
            if (git / "commondir").is_file():
                common = (git / (git / "commondir").read_text().strip()).resolve()
            for base in (git, common):
                if (base / ref).is_file():
                    return (base / ref).read_text().strip()[:7]
            packed = common / "packed-refs"
            if packed.is_file():
                for line in packed.read_text().splitlines():
                    if line.endswith(" " + ref):
                        return line.split(" ", 1)[0][:7]
            return ""
        except OSError:
            return ""
    return ""


def read_build(path: Path | None = None) -> Build:
    """The installed build (``build.json``), else this checkout's version and commit."""
    path = build_file() if path is None else path
    try:
        data = json.loads(path.read_text())
        if isinstance(data, dict) and data.get("version"):
            return Build(
                version=str(data["version"]),
                commit=str(data.get("commit") or "")[:12],
                date=str(data.get("date") or ""),
                installed=True,
            )
    except (OSError, ValueError):
        pass
    return Build(version=__version__, commit=git_commit(Path(__file__).resolve().parent))
