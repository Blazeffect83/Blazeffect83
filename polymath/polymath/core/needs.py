"""What only Christian can do (v0.12.0): one short list, the same everywhere.

Polymath fixes what it can by itself; what is left needs hands (a plug, a cable, a hub) or a decision. This module
gathers those from where they are known and words each as one thing to do:

* the drive roles (``/run/polymath/drive-roles.json``): a drive the brain cannot use because of its USB port, a
  failing drive, too little USB power, backups with nowhere else to go;
* Orin the caretaker: incidents it escalated (it could not fix them; its advice says what to do);
* an update that was rolled back, a backup that failed its test restore, a mirror that keeps failing (the last
  two days of the inbox).

Shown by ``polymath needs``, at the top of ``polymath doctor``, on the dashboard (``/api/needs``) and as a count in
the feed's NOW line. Read-only.
"""

from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path
from typing import Any

RECENT_S = 2 * 86400.0
INBOX = {  # inbox code (at the start of the issue's detail) → what to do
    "rolled_back": "an update kept failing and the previous version was put back: send journalctl -u polymath -n 80 "
    "so it can be fixed",
    "backup_broken": "a backup failed its test restore (a new one was taken): if it happens again, check the backup "
    "drive (polymath storage roles shows its health)",
    "mirror_failed": "the hourly mirror keeps failing: polymath storage roles shows the backup drive's state",
}


def _roles(run_dir: Path | None) -> list[dict[str, str]]:
    rec: dict[str, Any] = {}
    if run_dir is not None:
        with contextlib.suppress(OSError, ValueError):
            raw = json.loads((Path(run_dir) / "drive-roles.json").read_text(encoding="utf-8"))
            rec = raw if isinstance(raw, dict) else {}
    out: list[dict[str, str]] = []
    for d in rec.get("drives") or []:
        why = str(d.get("why_not") or "")
        if d.get("role") != "brain" and "on USB 2" in why and rec.get("brain") == "sd":
            out.append({"what": f"move {d.get('name')} to a blue USB 3 port (USB 3 cable, no USB 2 hub): the brain "
                        "can then move onto it", "source": "drive roles"})  # fmt: skip
    for w in rec.get("warnings") or []:
        text = str(w)
        if "is failing" in text or "mA, more than" in text or "on USB 2" in text or "second drive" in text:
            out.append({"what": text, "source": "drive roles"})
    return out


def _escalated(db: Any) -> list[dict[str, str]]:
    try:
        rows = db.query("SELECT title, advice FROM incidents WHERE state = 'escalated' ORDER BY escalated_at DESC")
    except Exception:  # an older brain without the table
        return []
    return [{"what": str(r["advice"] or r["title"]).split("\n")[0][:300], "source": "Orin the caretaker"}
            for r in rows]  # fmt: skip


def _inbox(db: Any, now: float) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    try:
        rows = db.query("SELECT detail FROM events WHERE kind = 'issue' AND at > ? ORDER BY at DESC", (now - RECENT_S,))
    except Exception:
        return []
    seen: set[str] = set()
    for r in rows:
        try:
            code = str((json.loads(r["detail"] or "{}") or {}).get("code") or "")
        except ValueError:
            continue
        key = code.split(":")[0]
        if key in INBOX and key not in seen:
            seen.add(key)
            out.append({"what": INBOX[key], "source": "inbox"})
    return out


OFFLINE_SAY_S = 6 * 3600.0  # no internet this long: worth a look at the router


def _offline(db: Any, now: float) -> list[dict[str, str]]:
    try:
        raw = db.kv_get("net")
    except Exception:
        return []
    if not isinstance(raw, dict) or raw.get("online", True) or not raw.get("since"):
        return []
    hours = (now - float(raw["since"])) / 3600
    if hours * 3600 < OFFLINE_SAY_S:
        return []
    return [{"what": f"no internet for {hours:.0f} h: check the router and the Pi's Wi-Fi or cable (Polymath keeps "
             "studying what it has meanwhile)", "source": "network"}]  # fmt: skip


def needs(db: Any, run_dir: Path | None, *, now: float | None = None) -> list[dict[str, str]]:
    """Everything that waits on Christian, most pressing first, without repeats (``run_dir``: /run/polymath)."""
    now = time.time() if now is None else now
    items = _roles(run_dir) + (_escalated(db) + _inbox(db, now) + _offline(db, now) if db is not None else [])
    seen: set[str] = set()
    out = []
    for it in items:
        if it["what"] not in seen:
            seen.add(it["what"])
            out.append(it)
    return out


def lines(items: list[dict[str, str]]) -> list[str]:
    if not items:
        return ["nothing needs you: Polymath handles everything by itself right now"]
    return [f"{i}. {it['what']}  ({it['source']})" for i, it in enumerate(items, 1)]
