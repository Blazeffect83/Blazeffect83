"""SD-card mode: the whole brain runs on the SD card while its drive cannot be trusted.

The brain normally lives on its drive (the SSD). When that drive keeps freezing, ``scripts/sd-card-mode.sh on``
switches Polymath to the SD card for as long as it takes, with one drop-in (``/etc/polymath/polymath.d/
sd-card.toml``: ``[storage] sd_only = true``) and nothing else changed in ``polymath.toml``:

* the brain lives in ``storage.sd_dir`` (``/var/lib/polymath/brain``), a systemd state directory on the SD card.
  The script starts it from the SD card's copy left behind when the brain moved onto the drive (it is renamed,
  never copied, so it costs no room), or empty;
* everything runs as usual, dumps included, inside what the card can give: the brain's budget is what it uses plus
  what is free, less ``sd_keep_free_gb`` (2 GB) for the system (``config.sd_budget``); the guard and eviction keep it
  there, and the SD card wear guard watches the writes;
* no drive is touched: the storage pool, moving and mounting the brain's drive, healing it and the standby brain are
  all off (``config.apply_sd_only``), and the script switches the SSD's USB device off (like unplugging it).

When the drive works again, the SSD watch (``body.ssdwatch``, v0.10.0) notices, checks it for ten minutes, and does
what ``scripts/sd-card-mode.sh off`` does: it sets the drop-in aside and has the SSD mounted. The agent starts on
its real brain and **folds** this one in (``core.standby.fold_job``): every document it read in
SD-card mode (after ``fold_after``, the last one it was started with), the Wikidata records, the crawler's progress
and the problems it noted since ``started``. Then the SD-card brain is removed.

This module is the agent's side: the marker that says what this brain is, and what the feed and doctor show.
"""

from __future__ import annotations

import contextlib
import json
import time
import uuid
from pathlib import Path
from typing import Any

from polymath.core.config import Config

MARKER = "sd-card.json"  # in the SD-card brain's directory: what this brain is and since when it runs
SEED = "seeded-from.json"  # written by scripts/sd-card-mode.sh when it starts the brain from the SD card's old copy
DROPIN = "sd-card.toml"  # in /etc/polymath/polymath.d: the switch itself (scripts/sd-card-mode.sh)


def active(config: Config) -> bool:
    return bool(config.storage.sd_only)


def marker_path(config: Config) -> Path:
    return Path(config.storage.sd_dir) / MARKER


def read_marker(config: Config) -> dict[str, Any] | None:
    try:
        raw = json.loads(marker_path(config).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) and raw.get("id") else None


def begin(config: Config, db: Any, *, now: float | None = None) -> dict[str, Any]:
    """Mark this brain as the SD-card brain (once; a restart keeps the marker). ``fold_after`` is its newest document
    now: everything up to it is what it was started with, which the real brain has already."""
    found = read_marker(config)
    if found is not None:
        return found
    now = time.time() if now is None else now
    newest = 0
    with contextlib.suppress(Exception):
        newest = int(db.scalar("SELECT COALESCE(MAX(id), 0) FROM documents") or 0)
    seed: dict[str, Any] = {}
    with contextlib.suppress(OSError, ValueError):
        raw = json.loads((Path(config.storage.sd_dir) / SEED).read_text(encoding="utf-8"))
        seed = raw if isinstance(raw, dict) else {}
    mark = {"id": uuid.uuid4().hex, "started": now, "fold_after": newest, "seeded_from": seed.get("from", "")}
    path = marker_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{MARKER}.tmp")
    tmp.write_text(json.dumps(mark), encoding="utf-8")
    tmp.replace(path)
    return mark


def start(config: Config, db: Any) -> dict[str, Any]:
    """At every start in SD-card mode: the marker (once), what the feed and the dashboard show (``kv sd_card``),
    and the caretaker's inbox."""
    mark = begin(config, db)
    with contextlib.suppress(Exception):
        db.kv_set("sd_card", {"since": mark["started"], "budget_gb": round(config.body.disk_budget_gb, 1),
                              "seeded_from": mark.get("seeded_from") or ""})  # fmt: skip
    note(db, config)
    return mark


def status(config: Config, db: Any = None) -> dict[str, Any] | None:
    """For the snapshot (feed, face, doctor): since when the brain runs on the SD card, and how much room it has."""
    if not active(config):
        return None
    import shutil

    mark = read_marker(config) or {}
    out: dict[str, Any] = {
        "since": mark.get("started"),
        "dir": str(config.paths.data_dir),
        "budget_gb": round(config.body.disk_budget_gb, 1),
        "seeded_from": mark.get("seeded_from") or "",
    }
    with contextlib.suppress(OSError):
        out["free_gb"] = round(shutil.disk_usage(config.paths.data_dir).free / 1e9, 1)
    from polymath.body.ssdwatch import read_public

    watch = read_public(config)
    if watch and watch.get("state"):  # what the SSD watch sees (it moves the brain back onto the SSD by itself)
        out["ssd"] = {k: watch.get(k) for k in ("state", "short", "detail", "fix", "since", "streak_since", "next_try")}
    if db is not None:
        with contextlib.suppress(Exception):
            newest = int(db.scalar("SELECT COALESCE(MAX(id), 0) FROM documents") or 0)
            out["documents_new"] = max(0, newest - int(mark.get("fold_after") or 0))
    return out


def note(db: Any, config: Config) -> None:
    """Into the caretaker's inbox (once a day): the brain runs on the SD card, and what that means."""
    from polymath.core import issues

    mark = read_marker(config) or {}
    since = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(mark.get("started") or time.time())))
    room = f"{config.body.disk_budget_gb:.1f} GB"
    text = (f"SD-card mode since {since}: the brain runs on the SD card ({room} of room), the SSD is not used. It "
            "moves back onto the SSD by itself once that is plugged in and healthy; by hand: sudo "
            "./scripts/sd-card-mode.sh off")  # fmt: skip
    with contextlib.suppress(Exception):
        issues.report(db, "storage", "sd_card", text, severity="warn", every=86400.0)
