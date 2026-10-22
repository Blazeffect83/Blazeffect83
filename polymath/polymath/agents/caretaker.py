"""Orin the Caretaker: keeps Polymath and the Pi running smoothly and safely (docs/CARETAKER.md).

Orin is a system agent: not counted among the society's agents, never retired, and it runs whether the society
is paused or not. It is the job ``caretaker.step``, every ``EVERY`` seconds at an urgent priority (ahead of the
bandit's choices, and still during a pause). One turn takes well under a second and only reads, except for the
bounded Tier 1 remedies below.

Every turn is an **incident loop** (C.3), deterministic given the database:

1. **Detect.** It reads its senses (``SIGNALS``: temperature, free space, memory, the database's write-ahead log,
   backups, the internet, how much it reads, quiz accuracy, safety lists) and keeps one sample per signal every
   ``SAMPLE_EVERY`` for a week. Each learned signal gets a normal band, median ± k·MAD of its own week (k starts at
   4 and is tuned by its false alarms). Hard limits come from the fundamentals. A reading outside its band twice in
   a row, or past a hard limit once, opens an **incident** (one per root symptom). It also reads the problems
   inbox (``core/issues``), failing and dead jobs, the Pi's power flags, the brain drive's USB speed, and watches
   the install (build, configuration, the data directory's permissions).
2. **Hypothesize.** Each incident model (``MODELS``) has causes with priors, nudged by what was the cause before
   (``fault_stats``). Bayes ranks them.
3. **Check.** It runs the read-only check worth the most (expected drop in uncertainty per unit of cost), at most
   ``MAX_CHECKS`` a turn.
4. **Act.** When one cause is at least ``ACT_AT`` likely, it applies the remedy most likely to work for it
   (Beta(a, b) per cause and remedy, learned from verified outcomes). **Tier 1** remedies are its own: slow the
   agent down, run eviction or a backup now, checkpoint the write-ahead log, probe the internet, requeue jobs that
   died of a passing problem, quarantine a crashing job kind for 6 hours, refresh the safety lists, roll back a
   self-tuned setting. Each is bounded and logged. **Tier 2** is telling Christian.
5. **Verify.** After the remedy's window the symptom must be gone: then the incident is resolved and the remedy
   gets the credit; otherwise it gets a failure and the next remedy is tried.
6. **Escalate.** Tier 2 causes, two failed remedies, or an old incident that keeps getting worse: one clear
   message, in the feed, ``polymath doctor`` and ``polymath caretaker``. It says what is wrong, the evidence, the
   most likely cause and how sure it is, and the exact step, quoted from the known fault
   (``agents/faults.toml``), which says who may do it. The message repeats at most once a day.

What Orin never does (``tests/test_caretaker.py`` scans this file): run commands, open sockets, change
permissions, mount, format, delete files, write configuration or code, or widen what Polymath may visit.
Everything it changes goes through the database: jobs, a few kv switches the loop reads, its own tables.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from polymath.core.logging import get_logger

log = get_logger("caretaker")

KIND = "caretaker.step"
NAME = "Orin"
EVERY = 120.0  # a turn every two minutes
PRIORITY = 3.6  # urgent: ahead of the bandit's choices, behind nothing but eviction's own urgency
SAMPLE_EVERY = 600.0  # one sample per signal every 10 minutes
KEEP_SAMPLES_S = 7 * 86400.0
MIN_BAND_SAMPLES = 36  # six hours of samples before a learned band is trusted
K_DEFAULT, K_MIN, K_MAX = 4.0, 2.0, 8.0
MAX_CHECKS = 3
ACT_AT = 0.6
QUARANTINE_S = 6 * 3600.0
MAX_QUARANTINED = 3
SLOWDOWN = 0.6  # intensity while Orin slows the agent down
SLOWDOWN_HOLD_S = 3600.0  # it expires by itself; Orin renews it while it is needed
REQUEUE_MAX = 50
ESCALATE_AGAIN_S = 86400.0
INBOX_QUIET_CLOSE_S = 86400.0  # an inbox incident with no new report for a day is closed
KEEP_INCIDENTS_S = 365 * 86400.0
SECURITY_EVERY = 3600.0
CORE_KINDS = frozenset({KIND, "net.probe", "eval.holdout", "body.backup", "body.evict", "body.housekeeping"})
STATUS_KEY = "caretaker"  # kv: what the feed, the dashboard and doctor show

# Messages that are not for Christian to act on today. Faults he put on hold (faults.toml [situation] on_hold) are
# noted, never escalated.


# ---------------------------------------------------------------------------------------------- the senses
@dataclass(frozen=True)
class Signal:
    name: str
    model: str  # the incident model it opens
    direction: str  # "high": too high is bad; "low": too low is bad
    learned: bool  # judged against its learned band too (not only its hard limit)
    floor: float  # band deviations smaller than this are noise (absolute units)
    unit: str = ""
    label: str = ""


SIGNALS: tuple[Signal, ...] = (
    Signal("temp_c", "heat", "high", True, 5.0, "°C", "CPU temperature"),
    Signal("free_gb", "disk_low", "low", False, 0.5, "GB", "free space"),
    Signal("rss_mb", "memory_high", "high", False, 100.0, "MB", "agent memory"),
    Signal("wal_mb", "wal_big", "high", False, 16.0, "MB", "write-ahead log"),
    Signal("backup_age_h", "backup_stale", "high", False, 6.0, "h", "last good backup"),
    Signal("offline_min", "offline_long", "high", False, 5.0, "min", "without internet"),
    Signal("docs_per_h", "ingest_stall", "low", True, 3.0, "documents/h", "reading"),
    Signal("quiz_acc", "quiz_drop", "low", True, 0.05, "", "self-test accuracy"),
    Signal("blocklists_age_d", "blocklists_old", "high", False, 1.0, "days", "safety lists' age"),
)
BY_SIGNAL = {s.name: s for s in SIGNALS}


@dataclass
class Env:
    """Where Orin looks outside the database (paths, so tests can point them at fixtures)."""

    throttled: Path = Path("/sys/devices/platform/soc/soc:firmware/get_throttled")
    hwmon: Path = Path("/sys/class/hwmon")
    proc_status: Path = Path("/proc/self/status")
    sys_block: Path = Path("/sys/block")
    config_file: Path = field(
        default_factory=lambda: Path(os.environ.get("POLYMATH_CONFIG", "/etc/polymath/polymath.toml"))
    )


# ---------------------------------------------------------------------------------------------- the models
@dataclass(frozen=True)
class Check:
    cost: float
    likes: dict[str, float]  # P(check is true | cause)
    describe: str


@dataclass(frozen=True)
class Model:
    title: str
    area: str
    fault: str  # the known fault in agents/faults.toml (its steps are quoted when Orin escalates)
    causes: dict[str, float]  # cause → prior
    remedies: dict[str, tuple[str, ...]]  # cause → remedies in Orin's order of preference
    checks: dict[str, Check] = field(default_factory=dict)
    window_s: float = 1800.0  # how long a remedy gets before Orin verifies it
    severity: str = "warn"
    signal: str = ""  # the signal whose return to normal resolves it ("" when the model checks itself)
    cause_words: dict[str, str] = field(default_factory=dict)


MODELS: dict[str, Model] = {
    "heat": Model(
        "the Pi runs hot", "body", "body-thermal",
        {"load": 0.45, "cooling": 0.55},
        {"load": ("slowdown", "tell"), "cooling": ("slowdown", "tell")},
        {"load_high": Check(1.0, {"load": 0.85, "cooling": 0.35}, "the load is high (above 3)")},
        window_s=1800.0, signal="temp_c",
        cause_words={"load": "too much work at once", "cooling": "poor cooling or a warm room"},
    ),
    "disk_low": Model(
        "the disk is filling up", "body", "body-disk",
        {"brain": 0.6, "other": 0.4},
        {"brain": ("evict", "tell"), "other": ("tell",)},
        {"brain_share": Check(1.0, {"brain": 0.9, "other": 0.2}, "the brain is most of what is on the disk")},
        window_s=3600.0, signal="free_gb",
        cause_words={"brain": "the brain grew", "other": "other files filled the disk"},
    ),
    "memory_high": Model(
        "the agent uses a lot of memory", "body", "agent-memory-high",
        {"load": 1.0}, {"load": ("slowdown", "tell")}, window_s=1800.0, signal="rss_mb",
        cause_words={"load": "big jobs at once"},
    ),
    "wal_big": Model(
        "the database's write-ahead log keeps growing", "database", "wal-growing",
        {"reader": 1.0}, {"reader": ("checkpoint", "tell")}, window_s=900.0, signal="wal_mb",
        cause_words={"reader": "a long read held the checkpoint back"},
    ),
    "backup_stale": Model(
        "no good backup for a day and a half", "database", "backup-stale",
        {"missed": 1.0}, {"missed": ("backup", "tell")}, window_s=3 * 3600.0, signal="backup_age_h",
        cause_words={"missed": "the nightly backup was skipped or failed"},
    ),
    "offline_long": Model(
        "no internet for a while", "internet", "offline",
        {"outage": 1.0}, {"outage": ("probe", "tell")}, window_s=6 * 3600.0, signal="offline_min",
        severity="info", cause_words={"outage": "the router or the line is down"},
    ),
    "blocklists_old": Model(
        "the safety lists are old", "security", "blocklists-stale",
        {"missed": 1.0}, {"missed": ("blocklists", "tell")}, window_s=6 * 3600.0, signal="blocklists_age_d",
        cause_words={"missed": "the weekly refresh did not happen"},
    ),
    "ingest_stall": Model(
        "it reads much less than usual", "learning", "ingest-stall",
        {"offline": 0.25, "disk": 0.2, "body": 0.2, "dead": 0.15, "idle": 0.2},
        {"offline": ("probe", "tell"), "disk": ("evict", "tell"), "body": ("note",), "dead": ("requeue", "tell"),
         "idle": ("replan", "tell")},
        {
            "offline_now": Check(0.5, {"offline": 0.95, "disk": 0.05, "body": 0.05, "dead": 0.1, "idle": 0.05},
                                 "the internet is down"),
            "body_slowed": Check(0.5, {"offline": 0.1, "disk": 0.7, "body": 0.9, "dead": 0.1, "idle": 0.1},
                                 "the body guard slowed or paused it"),
            "dead_recent": Check(1.0, {"offline": 0.3, "disk": 0.2, "body": 0.1, "dead": 0.95, "idle": 0.1},
                                 "jobs died in the last day"),
            "queue_empty": Check(1.0, {"offline": 0.3, "disk": 0.3, "body": 0.2, "dead": 0.4, "idle": 0.9},
                                 "nothing to read is queued"),
        },
        window_s=3 * 3600.0, signal="docs_per_h",
        cause_words={"offline": "the internet is down", "disk": "low disk space", "body": "heat or a full disk",
                     "dead": "reading jobs died", "idle": "nothing left to read in its plan"},
    ),
    "quiz_drop": Model(
        "its self-test scores dropped", "learning", "quiz-accuracy-drop",
        {"tuning": 0.4, "data": 0.6},
        {"tuning": ("reset_knob", "tell"), "data": ("note",)},
        {"recent_tune": Check(1.0, {"tuning": 0.9, "data": 0.2}, "a setting was self-tuned in the last 3 days")},
        window_s=2 * 86400.0, signal="quiz_acc", severity="info",
        cause_words={"tuning": "a self-tuned setting", "data": "new kinds of facts to be tested on"},
    ),
    "dead_jobs": Model(
        "jobs gave up", "jobs", "job-dead",
        {"transient": 0.5, "bug": 0.5},
        {"transient": ("requeue", "tell"), "bug": ("tell",)},
        {
            "passing_error": Check(1.0, {"transient": 0.85, "bug": 0.15},
                                   "they died of a passing problem (network, disk, a locked database)"),
            "online_now": Check(0.5, {"transient": 0.9, "bug": 0.75}, "the internet works now"),
        },
        window_s=3600.0, cause_words={"transient": "a passing problem", "bug": "a fault in the job"},
    ),
    "crash_loop": Model(
        "a kind of job keeps failing", "jobs", "job-dead",
        {"bug": 1.0}, {"bug": ("quarantine", "tell")}, window_s=QUARANTINE_S,
        cause_words={"bug": "a fault in that job, hit again and again"},
    ),
    "power": Model(
        "the Pi reports under-voltage", "body", "pi-under-voltage", {"supply": 1.0}, {"supply": ("tell",)},
        severity="bad", cause_words={"supply": "a weak power supply or cable"},
    ),
    "usb2": Model(
        "the brain's drive runs at USB 2 speed", "storage", "brain-drive-usb2", {"port": 1.0}, {"port": ("tell",)},
        cause_words={"port": "a black USB 2 port, a USB 2 cable or a hub"},
    ),
    "build_mismatch": Model(
        "an update is installed but not running", "install", "update-not-running", {"restart": 1.0},
        {"restart": ("tell",)}, cause_words={"restart": "the agent was not restarted after the install"},
    ),
    "config_changed": Model(
        "the configuration changed", "security", "config-changed", {"edit": 1.0}, {"edit": ("tell",)},
        severity="info", cause_words={"edit": "someone with sudo edited it (usually you)"},
    ),
    "permissions": Model(
        "the brain's folder changed permissions", "security", "data-dir-permissions", {"edit": 1.0},
        {"edit": ("tell",)}, severity="bad", cause_words={"edit": "a chmod or chown on it"},
    ),
}  # fmt: skip

# Remedies: cost, how often at most, and whether Orin does it itself (tier 1) or tells Christian (tier 2).
REMEDY_COST = {"slowdown": 1.0, "evict": 1.0, "backup": 2.0, "checkpoint": 0.5, "probe": 0.5, "requeue": 1.0,
               "quarantine": 2.0, "blocklists": 1.0, "reset_knob": 2.0, "replan": 1.0, "note": 0.1,
               "tell": 3.0}  # fmt: skip
REMEDY_EVERY = {"evict": 6 * 3600.0, "backup": 6 * 3600.0, "checkpoint": 600.0, "probe": 300.0,
                "blocklists": 7 * 86400.0, "replan": 6 * 3600.0, "reset_knob": 86400.0}  # fmt: skip
TIER1 = frozenset(REMEDY_COST) - {"tell", "note"}


# ---------------------------------------------------------------------------------------------- the maths
def bands(values: list[float]) -> tuple[float, float]:
    """(median, MAD) of a list of values."""
    xs = sorted(values)
    n = len(xs)
    med = xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2
    devs = sorted(abs(x - med) for x in xs)
    mad = devs[n // 2] if n % 2 else (devs[n // 2 - 1] + devs[n // 2]) / 2
    return med, mad


def outside(sig: Signal, value: float, median: float, mad: float, k: float) -> bool:
    """Is ``value`` outside the learned band (median ± k·1.4826·MAD, never narrower than the signal's floor)?"""
    width = max(sig.floor, k * 1.4826 * mad)
    return value > median + width if sig.direction == "high" else value < median - width


def posterior(priors: dict[str, float], observed: dict[str, bool], checks: dict[str, Check]) -> dict[str, float]:
    """P(cause | what the checks showed) ∝ prior × Π P(observation | cause)."""
    post = dict(priors)
    for name, seen in observed.items():
        chk = checks.get(name)
        if chk is None:
            continue
        for c in post:
            p = min(max(chk.likes.get(c, 0.5), 1e-3), 1 - 1e-3)
            post[c] *= p if seen else 1 - p
    total = sum(post.values()) or 1.0
    return {c: v / total for c, v in post.items()}


def entropy(p: dict[str, float]) -> float:
    return -sum(v * math.log2(v) for v in p.values() if v > 0)


def best_check(post: dict[str, float], checks: dict[str, Check], done: set[str]) -> tuple[str, float] | None:
    """The check worth the most: expected drop in entropy per unit of cost (value of information)."""
    h = entropy(post)
    best: tuple[str, float] | None = None
    for name, chk in checks.items():
        if name in done:
            continue
        p_true = sum(post[c] * chk.likes.get(c, 0.5) for c in post)
        if not 0 < p_true < 1:
            continue
        expected = p_true * entropy(posterior(post, {name: True}, checks)) + (1 - p_true) * entropy(
            posterior(post, {name: False}, checks)
        )
        score = (h - expected) / max(chk.cost, 1e-6)
        if score > 0.01 and (best is None or score > best[1]):
            best = (name, score)
    return best


# ---------------------------------------------------------------------------------------------- Orin


def docs_since(db: Any, since: float) -> int:
    """Documents fetched since ``since``. Ids grow with time, so a binary search over ids finds the first one in a
    handful of point lookups: no scan of the whole table (``fetched`` has no index, and on a brain of several GB a
    scan every 2 minutes reads hundreds of MB from the SD card)."""
    top = db.one("SELECT id, fetched FROM documents ORDER BY id DESC LIMIT 1")
    if top is None or float(top["fetched"]) < since:
        return 0
    best = int(top["id"])  # the first id whose document was fetched at or after `since` (ids may have gaps)
    lo, hi = int(db.scalar("SELECT MIN(id) FROM documents", default=0) or 0), best
    while lo <= hi:  # each step raises lo or lowers hi: it ends
        mid = (lo + hi) // 2
        r = db.one("SELECT id, fetched FROM documents WHERE id >= ? ORDER BY id LIMIT 1", (mid,))
        if r is None:  # pragma: no cover - the newest id is >= mid
            break
        if float(r["fetched"]) >= since:  # no ids between mid and r.id: the first is r.id or below mid
            best, hi = min(best, int(r["id"])), mid - 1
        else:
            lo = int(r["id"]) + 1
    return int(db.scalar("SELECT COUNT(*) FROM documents WHERE id >= ?", (best,), default=0) or 0)


class Orin:
    def __init__(self, config: Any, db: Any, scheduler: Any = None, *, env: Env | None = None,
                 clock: Callable[[], float] = time.time) -> None:  # fmt: skip
        from polymath.core.scheduler import Scheduler

        self.config = config
        self.db = db
        self.scheduler = scheduler or Scheduler(db)
        self.env = env or Env()
        self.clock = clock
        self.now = clock()
        self.checks_left = MAX_CHECKS
        self.said: list[str] = []  # why lines written this turn

    # ------------------------------------------------------------------ the turn
    def turn(self) -> dict[str, Any]:
        """One turn of the incident loop. Returns what it saw and did (the job's result)."""
        self.now = self.clock()
        self.checks_left = MAX_CHECKS
        readings = self.read()
        self.sample(readings)
        opened = self.detect(readings)
        opened += self.watch_jobs()
        opened += self.watch_body_and_install()
        opened += self.triage_inbox()
        for inc in self.open_incidents():
            self.work(inc, readings)
        self.restore(readings)
        self.prune()
        status = self.publish()
        return {"opened": opened, "open": len(status["open"]), "said": self.said[-5:]}

    # ------------------------------------------------------------------ 1. the senses
    def read(self) -> dict[str, float]:
        """Every signal it can read now (missing ones are left out)."""
        out: dict[str, float] = {}
        db, now = self.db, self.now
        vit = db.one("SELECT temp_c, load1, disk_free_gb, data_gb, mode, at FROM vitals ORDER BY at DESC LIMIT 1")
        if vit is not None and now - float(vit["at"]) < 3600:
            if vit["temp_c"] is not None:
                out["temp_c"] = float(vit["temp_c"])
            if vit["disk_free_gb"] is not None:
                out["free_gb"] = float(vit["disk_free_gb"])
        rss = self._rss_mb()
        if rss is not None:
            out["rss_mb"] = rss
        wal = Path(str(self.config.paths.db_path) + "-wal")
        try:
            out["wal_mb"] = wal.stat().st_size / 1e6
        except OSError:
            pass
        out["backup_age_h"] = self._backup_age_h()
        from polymath.core import netstate

        st = netstate.get(db)
        out["offline_min"] = 0.0 if st.online else max(0.0, (now - float(st.since or now)) / 60)
        out["docs_per_h"] = float(docs_since(db, now - 3600))
        q = db.one("SELECT accuracy, created FROM quizzes ORDER BY id DESC LIMIT 1")
        if q is not None:
            out["quiz_acc"] = float(q["accuracy"])
        if self.config.senses.blocklists:
            last = db.scalar("SELECT MAX(updated) FROM jobs WHERE kind = 'web.blocklists' AND state = 'done'")
            first = db.scalar("SELECT MIN(created) FROM jobs") or now
            out["blocklists_age_d"] = (now - float(last if last is not None else first)) / 86400
        return out

    def _rss_mb(self) -> float | None:
        try:
            for line in self.env.proc_status.read_text(encoding="utf-8").splitlines():
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
        except (OSError, ValueError, IndexError):
            return None
        return None

    def _backup_age_h(self) -> float:
        last = self.db.scalar("SELECT MAX(created) FROM backups WHERE ok = 1")
        if last is None:  # never backed up: count from when the brain started
            last = self.db.scalar("SELECT MIN(created) FROM jobs") or self.now
        return max(0.0, (self.now - float(last)) / 3600)

    def hard(self, name: str, value: float) -> bool:
        """Past a hard limit (the fundamentals: docs/CARETAKER.md C.6)."""
        b = self.config.body
        limits: dict[str, bool] = {
            "temp_c": value >= b.throttle_celsius - 2.0,
            "free_gb": value < b.disk_min_free_gb * 1.5,
            "rss_mb": value > 2400.0,  # MemoryHigh is 2600 MB
            "wal_mb": value > 256.0,
            "backup_age_h": value > 36.0 and not getattr(self.config, "standby", False),
            "offline_min": value > 30.0,
            "blocklists_age_d": value > 14.0,
        }
        return limits.get(name, False)

    def sample(self, readings: dict[str, float]) -> None:
        """One sample per signal every SAMPLE_EVERY; bands recomputed from the last week."""
        last = float(self.db.kv_get("caretaker_sampled") or 0.0)
        if self.now - last < SAMPLE_EVERY:
            return
        self.db.kv_set("caretaker_sampled", self.now)
        for name, value in readings.items():
            self.db.execute("INSERT OR REPLACE INTO signal_samples(signal, at, value) VALUES(?,?,?)",
                            (name, self.now, value))  # fmt: skip
            sig = BY_SIGNAL.get(name)
            if sig is None or not sig.learned:
                continue
            vals = [
                float(r["value"])
                for r in self.db.query(
                    "SELECT value FROM signal_samples WHERE signal = ? AND at >= ?", (name, self.now - KEEP_SAMPLES_S)
                )
            ]
            med, mad = bands(vals)
            self.db.execute("INSERT OR REPLACE INTO signal_bands(signal, median, mad, n, updated) VALUES(?,?,?,?,?)",
                            (name, med, mad, len(vals), self.now))  # fmt: skip
        self.db.execute("DELETE FROM signal_samples WHERE at < ?", (self.now - KEEP_SAMPLES_S,))

    def k(self) -> float:
        return float(self.db.kv_get("caretaker_k") or K_DEFAULT)

    def abnormal(self, name: str, value: float) -> str:
        """'' when normal; else why: 'hard' (past a hard limit) or 'band' (outside its learned band)."""
        if self.hard(name, value):
            return "hard"
        sig = BY_SIGNAL.get(name)
        if sig is None or not sig.learned:
            return ""
        row = self.db.one("SELECT median, mad, n FROM signal_bands WHERE signal = ?", (name,))
        if row is None or int(row["n"]) < MIN_BAND_SAMPLES:
            return ""
        if name == "docs_per_h" and float(row["median"]) < 5:
            return ""  # too little reading to judge a drop
        return "band" if outside(sig, value, float(row["median"]), float(row["mad"]), self.k()) else ""

    # ------------------------------------------------------------------ 2. incidents
    def detect(self, readings: dict[str, float]) -> int:
        streaks: dict[str, int] = dict(self.db.kv_get("caretaker_streaks") or {})
        opened = 0
        for name, value in readings.items():
            why = self.abnormal(name, value)
            sig = BY_SIGNAL.get(name)
            if not why or sig is None:
                streaks.pop(name, None)
                continue
            streaks[name] = streaks.get(name, 0) + 1
            if why == "band" and streaks[name] < 2:
                continue  # a single blip is not an incident
            band = self.db.one("SELECT median FROM signal_bands WHERE signal = ?", (name,))
            usual = f" (usually {float(band['median']):.1f})" if band is not None and why == "band" else ""
            symptom = {"signal": name, "value": round(value, 2), "why": why, "at": self.now,
                       "text": f"{sig.label or name} {value:.1f}{sig.unit and ' ' + sig.unit}{usual}"}  # fmt: skip
            if self.open_incident(sig.model, sig.model, symptom):
                opened += 1
        self.db.kv_set("caretaker_streaks", streaks)
        return opened

    def open_incident(self, key: str, model: str, symptom: dict[str, Any], *, title: str = "", fault: str = "",
                      severity: str = "", issue_id: int | None = None) -> bool:  # fmt: skip
        """Open an incident for ``key``, or add the symptom to the one already open. True when it is new."""
        row = self.db.one("SELECT * FROM incidents WHERE key = ? AND state NOT IN ('resolved', 'closed')", (key,))
        if row is not None:
            syms = [*json.loads(row["symptoms"])[-9:], symptom]
            ids = json.loads(row["issue_ids"]) + ([issue_id] if issue_id else [])
            self.db.execute("UPDATE incidents SET symptoms = ?, issue_ids = ?, updated = ? WHERE id = ?",
                            (json.dumps(syms), json.dumps(ids[-50:]), self.now, row["id"]))  # fmt: skip
            return False
        m = MODELS.get(model)
        title = title or (m.title if m else model)
        fault = fault or (m.fault if m else "")
        severity = severity or (m.severity if m else "warn")
        prior = self.priors(m) if m else {}
        self.db.execute(
            "INSERT INTO incidents(key, kind, area, fault, title, opened, updated, state, severity, symptoms, "
            "hypotheses, issue_ids) VALUES(?,?,?,?,?,?,?,'open',?,?,?,?)",
            (key, model, m.area if m else str(symptom.get("area", "inbox")), fault, title, self.now, self.now,
             severity, json.dumps([symptom]), json.dumps(prior), json.dumps([issue_id] if issue_id else [])),
        )  # fmt: skip
        self.why(f"{title}: {symptom.get('text', '')} → looking into it", status="open")
        return True

    def open_incidents(self) -> list[Any]:
        return list(self.db.query("SELECT * FROM incidents WHERE state NOT IN ('resolved', 'closed') ORDER BY id"))

    def priors(self, m: Model) -> dict[str, float]:
        """The model's priors, nudged by how often each cause was the cause before (2 pseudo-observations)."""
        counts = {str(r["cause"]): float(r["a"]) - 1 for r in self.db.query(
            "SELECT cause, a FROM fault_stats WHERE remedy = '' AND cause LIKE ?", (f"{self._key(m)}:%",))}  # fmt: skip
        n = sum(counts.values())
        out = {c: (2 * p + counts.get(f"{self._key(m)}:{c}", 0.0)) / (2 + n) for c, p in m.causes.items()}
        total = sum(out.values()) or 1.0
        return {c: v / total for c, v in out.items()}

    @staticmethod
    def _key(m: Model) -> str:
        return next(k for k, v in MODELS.items() if v is m)

    # ------------------------------------------------------------------ 3-6. working an incident
    def work(self, inc: Any, readings: dict[str, float]) -> None:
        if inc["kind"] == "inbox":
            self.work_inbox(inc)
            return
        m = MODELS.get(inc["kind"])
        if m is None:
            return
        actions = json.loads(inc["actions"])
        pending = [a for a in actions if a.get("verify_at") and a.get("verified") is None]
        if pending:
            if self.now >= float(pending[-1]["verify_at"]):
                self.verify(inc, m, actions, readings)
            return
        if inc["state"] == "escalated":
            self.maybe_resolve_escalated(inc, m, readings)
            self.escalate(inc, m, again=True)
            return
        if self.gone(inc, m, readings) and not actions:
            if self.now - float(inc["opened"]) >= min(m.window_s, 1800.0):
                self.close(inc, "fixed itself", "back to normal by itself")
                self.adjust_k(+0.25)  # nothing needed doing: a little less sensitive
            return
        observed: dict[str, bool] = {c["name"]: bool(c["seen"]) for c in json.loads(inc["checks"])}
        post = posterior(self.priors(m), observed, m.checks)
        while self.checks_left > 0:
            choice = best_check(post, m.checks, set(observed))
            if choice is None:
                break
            name = choice[0]
            seen = self.check(name, inc)
            self.checks_left -= 1
            observed[name] = seen
            post = posterior(self.priors(m), observed, m.checks)
            checks = [*json.loads(inc["checks"]), {"name": name, "seen": seen, "at": self.now}]
            self.db.execute("UPDATE incidents SET checks = ?, hypotheses = ?, updated = ? WHERE id = ?",
                            (json.dumps(checks), json.dumps(post), self.now, inc["id"]))  # fmt: skip
            inc = self.db.one("SELECT * FROM incidents WHERE id = ?", (inc["id"],))
            self.why(f"{m.title}: checked whether {m.checks[name].describe}: {'yes' if seen else 'no'} → "
                     + self._ranked(m, post))  # fmt: skip
        cause, p = max(post.items(), key=lambda kv: kv[1])
        self.db.execute("UPDATE incidents SET hypotheses = ? WHERE id = ?", (json.dumps(post), inc["id"]))
        if p < ACT_AT and best_check(post, m.checks, set(observed)) is not None:
            return  # more checks next turn
        tried = {a["remedy"] for a in actions}
        failed = sum(1 for a in actions if a.get("verified") is False)
        options = [r for r in m.remedies.get(cause, ("tell",)) if r not in tried]
        if failed >= 2 or not options:
            options = ["tell"]
        remedy = max(options, key=lambda r: self.p_fix(m, cause, r) / REMEDY_COST[r])
        self.apply(inc, m, cause, p, remedy, readings)

    def _ranked(self, m: Model, post: dict[str, float]) -> str:
        top = sorted(post.items(), key=lambda kv: -kv[1])[:2]
        return " or ".join(f"{m.cause_words.get(c, c)} (p {v:.2f})" for c, v in top)

    def check(self, name: str, inc: Any) -> bool:
        """The read-only checks (C.3 step 3)."""
        db, now = self.db, self.now
        if name == "load_high":
            v = db.one("SELECT load1 FROM vitals ORDER BY at DESC LIMIT 1")
            return v is not None and v["load1"] is not None and float(v["load1"]) > 3.0
        if name == "brain_share":
            v = db.one("SELECT disk_free_gb, data_gb FROM vitals ORDER BY at DESC LIMIT 1")
            if v is None or v["data_gb"] is None:
                return True
            try:
                import shutil

                used = shutil.disk_usage(self.config.paths.data_dir).used / 1e9
            except OSError:
                return True
            return float(v["data_gb"]) >= 0.6 * used
        if name == "offline_now" or name == "online_now":
            from polymath.core import netstate

            online = netstate.get(db).online
            return online if name == "online_now" else not online
        if name == "body_slowed":
            v = db.one("SELECT mode FROM vitals ORDER BY at DESC LIMIT 1")
            return v is not None and str(v["mode"]) != "normal"
        if name == "dead_recent":
            return bool(db.scalar("SELECT 1 FROM jobs WHERE state = 'dead' AND updated >= ? LIMIT 1", (now - 86400,)))
        if name == "queue_empty":
            return not db.scalar("SELECT 1 FROM jobs WHERE state = 'queued' AND kind IN ('wikipedia.part', "
                                 "'feeds.poll', 'crawl.step', 'dump.download', 'wikidata.dump') LIMIT 1")  # fmt: skip
        if name == "recent_tune":
            return bool(db.scalar("SELECT 1 FROM self_changes WHERE area = 'tuning' AND action = 'adjusted' AND "
                                  "at >= ? LIMIT 1", (now - 3 * 86400,)))  # fmt: skip
        if name == "passing_error":
            ids = [s.get("job") for s in json.loads(inc["symptoms"]) if s.get("job")]
            if not ids:
                return False
            marks = ",".join("?" * len(ids))
            errs = [
                str(r["last_error"] or "") for r in db.query(f"SELECT last_error FROM jobs WHERE id IN ({marks})", ids)
            ]
            return bool(errs) and all(passing(e) for e in errs)
        return False

    def p_fix(self, m: Model, cause: str, remedy: str) -> float:
        row = self.db.one("SELECT a, b FROM fault_stats WHERE cause = ? AND remedy = ?",
                          (f"{self._key(m)}:{cause}", remedy))  # fmt: skip
        a, b = (float(row["a"]), float(row["b"])) if row is not None else (1.0, 1.0)
        return a / (a + b)

    def learn(self, m: Model, cause: str, remedy: str, worked: bool) -> None:
        key = f"{self._key(m)}:{cause}"
        for r, ok in ((remedy, worked), ("", True)):  # remedy '' counts how often the cause was it
            if r == "" and not worked:
                continue
            self.db.execute(
                "INSERT INTO fault_stats(cause, remedy, a, b) VALUES(?,?,?,?) ON CONFLICT(cause, remedy) DO UPDATE "
                "SET a = a + excluded.a - 1, b = b + excluded.b - 1",
                (key, r, 2.0 if ok else 1.0, 1.0 if ok else 2.0),
            )

    def apply(self, inc: Any, m: Model, cause: str, p: float, remedy: str, readings: dict[str, float]) -> None:
        if remedy == "tell":
            self.db.execute("UPDATE incidents SET hypotheses = ? WHERE id = ?", (json.dumps({cause: p}), inc["id"]))
            self.escalate(self.db.one("SELECT * FROM incidents WHERE id = ?", (inc["id"],)), m, cause=cause, p=p)
            return
        if remedy == "note":
            self.close(inc, "noted", f"{m.cause_words.get(cause, cause)} (p {p:.2f}): nothing to fix")
            return
        last = float((self.db.kv_get("caretaker_last") or {}).get(remedy, 0.0))
        recently = remedy in REMEDY_EVERY and self.now - last < REMEDY_EVERY[remedy]  # then: the next remedy
        did = None if recently else self.remedy(remedy, inc)
        actions = json.loads(inc["actions"])
        actions.append({"remedy": remedy, "cause": cause, "p": round(p, 3), "at": self.now, "did": did or "",
                        "verify_at": self.now + m.window_s if did else self.now,
                        "verified": None if did else False})  # fmt: skip
        self.db.execute("UPDATE incidents SET actions = ?, state = ?, updated = ? WHERE id = ?",
                        (json.dumps(actions), "verifying" if did else "open", self.now, inc["id"]))  # fmt: skip
        if did:
            stamps = dict(self.db.kv_get("caretaker_last") or {})
            stamps[remedy] = self.now
            self.db.kv_set("caretaker_last", stamps)
            self.why(f"{m.title} → likely {m.cause_words.get(cause, cause)} (p {p:.2f}) → {did}", status="acting")
            from polymath.drive import changelog

            changelog.record(self.db, "caretaker", remedy, "adjusted", f"{NAME}: {did} ({m.title})",
                             detail={"incident": int(inc["id"]), "cause": cause, "p": round(p, 3)})  # fmt: skip

    def verify(self, inc: Any, m: Model, actions: list[dict[str, Any]], readings: dict[str, float]) -> None:
        last = actions[-1]
        ok = self.gone(inc, m, readings)
        last["verified"] = ok
        self.learn(m, str(last["cause"]), str(last["remedy"]), ok)
        self.db.execute("UPDATE incidents SET actions = ?, updated = ? WHERE id = ?",
                        (json.dumps(actions), self.now, inc["id"]))  # fmt: skip
        if ok:
            self.close(inc, "fixed", f"{last['did']}: it worked", resolved=True)
            return
        self.why(f"{m.title}: {last['did']} did not fix it → trying the next step", status="open")
        self.db.execute("UPDATE incidents SET state = 'open' WHERE id = ?", (inc["id"],))

    def gone(self, inc: Any, m: Model, readings: dict[str, float]) -> bool:
        """Is the incident's symptom back to normal?"""
        kind = inc["kind"]
        if m.signal:
            value = readings.get(m.signal)
            return value is not None and not self.abnormal(m.signal, value)
        if kind == "dead_jobs":
            ids = [s.get("job") for s in json.loads(inc["symptoms"]) if s.get("job")]
            marks = ",".join("?" * len(ids))
            return not ids or not self.db.scalar(f"SELECT 1 FROM jobs WHERE state = 'dead' AND id IN ({marks}) LIMIT 1",
                                                 ids)  # fmt: skip
        if kind == "crash_loop":
            job_kind = inc["key"].split(":", 1)[1]
            return self.failures(job_kind) < 3
        if kind == "power":
            return not self.undervoltage()[0]
        if kind == "usb2":
            return not self.slow_drives()
        if kind == "build_mismatch":
            return not self.build_mismatch()
        return False  # config and permission changes stay until Christian has seen them (closed after a day)

    def maybe_resolve_escalated(self, inc: Any, m: Model, readings: dict[str, float]) -> None:
        if self.gone(inc, m, readings) or (inc["kind"] in {"config_changed", "permissions"}
                                           and self.now - float(inc["opened"]) > INBOX_QUIET_CLOSE_S):  # fmt: skip
            self.close(inc, "fixed", "back to normal after Christian's step", resolved=True)

    def escalate(self, inc: Any, m: Model | None, *, cause: str = "", p: float = 0.0, again: bool = False) -> None:
        """Tier 2: one clear message (what, evidence, likely cause with probability, the exact step)."""
        if inc is None or inc["state"] in {"resolved", "closed"}:
            return
        if again and inc["escalated_at"] and self.now - float(inc["escalated_at"]) < ESCALATE_AGAIN_S:
            return
        if not again:
            hyp = json.loads(inc["hypotheses"] or "{}")
            if not cause and hyp:
                cause, p = max(hyp.items(), key=lambda kv: kv[1])
        advice = inc["advice"] if again and inc["advice"] else self.message(inc, m, cause, p)
        self.db.execute("UPDATE incidents SET state = 'escalated', advice = ?, escalated_at = ?, updated = ? "
                        "WHERE id = ?", (advice, self.now, self.now, inc["id"]))  # fmt: skip
        self.why(advice, status="tell", severity=str(inc["severity"]))

    def message(self, inc: Any, m: Model | None, cause: str, p: float) -> str:
        from polymath.agents import faults

        syms = json.loads(inc["symptoms"] or "[]")
        evidence = "; ".join(dict.fromkeys(str(s.get("text", "")) for s in syms[-3:] if s.get("text"))) or "seen"
        k = faults.knowledge()
        f = k.by_id.get(str(inc["fault"]))
        step = next((s for s in f.steps if s.needs in {"you", "sudo", "approval"}), None) if f else None
        do = f"{faults.describe_step(step)}" if step else "nothing for you to do yet: I keep watching"
        likely = f"{m.cause_words.get(cause, cause)} (p {p:.2f})" if m and cause else "not known yet: new to me"
        return f"{NAME}: {inc['title']}. Evidence: {evidence}. Most likely: {likely}. Do: {do}"

    def close(self, inc: Any, outcome: str, why: str, *, resolved: bool = False) -> None:
        self.db.execute("UPDATE incidents SET state = ?, outcome = ?, updated = ? WHERE id = ?",
                        ("resolved" if resolved else "closed", outcome, self.now, inc["id"]))  # fmt: skip
        self.why(f"{inc['title']}: {why}", status="resolved" if resolved else "closed")

    def adjust_k(self, delta: float) -> None:
        self.db.kv_set("caretaker_k", round(min(K_MAX, max(K_MIN, self.k() + delta)), 2))

    # ------------------------------------------------------------------ Tier 1 remedies (bounded, logged)
    def remedy(self, name: str, inc: Any) -> str | None:
        """Apply a Tier 1 remedy; returns what it did, or None when it could not."""
        s, now = self.scheduler, self.now
        if name == "slowdown":
            self.db.kv_set("caretaker_slowdown", {"factor": SLOWDOWN, "until": now + SLOWDOWN_HOLD_S,
                                                  "why": str(inc["title"])})  # fmt: skip
            return f"slowed the agent down to {SLOWDOWN:.0%} for now"
        if name in {"evict", "backup", "probe", "blocklists", "replan"}:
            kind = {"evict": "body.evict", "backup": "body.backup", "probe": "net.probe",
                    "blocklists": "web.blocklists", "replan": "sources.plan"}[name]  # fmt: skip
            if self.db.scalar("SELECT 1 FROM jobs WHERE kind = ? AND (state = 'running' OR (state = 'queued' AND "
                              "not_before <= ?)) LIMIT 1", (kind, now + 3600)):  # fmt: skip
                # already due within the hour (the nightly backup, say): a second copy would run the same work twice
                return {"evict": "eviction is already on its way", "backup": "a backup is already on its way",
                        "probe": "an internet check is already on its way",
                        "blocklists": "fresh safety lists are already on their way",
                        "replan": "the source planner is already on its way"}[name]  # fmt: skip
            s.enqueue(kind, {"by": "caretaker"}, key=f"{kind}:caretaker:{int(now)}", priority=3.0)
            if name == "evict":
                s.enqueue("body.housekeeping", {}, key=f"body.housekeeping:caretaker:{int(now)}", priority=3.0)
            return {"evict": "asked for eviction and housekeeping now", "backup": "asked for a backup now",
                    "probe": "asked whether the internet is back", "blocklists": "asked for fresh safety lists",
                    "replan": "asked the source planner to plan more reading"}[name]  # fmt: skip
        if name == "checkpoint":
            self.db.kv_set("caretaker_checkpoint", now)  # the loop runs it between slices (outside a transaction)
            return "asked for a write-ahead log checkpoint"
        if name == "requeue":
            done: list[int] = list(self.db.kv_get("caretaker_requeued") or [])
            rows = self.db.query("SELECT id, last_error FROM jobs WHERE state = 'dead' AND updated >= ? ORDER BY id "
                                 "DESC LIMIT 200", (now - 86400,))  # fmt: skip
            ids = [int(r["id"]) for r in rows if passing(str(r["last_error"] or "")) and int(r["id"]) not in done]
            ids = ids[:REQUEUE_MAX]
            if not ids:
                return None
            self.db.execute(
                f"UPDATE jobs SET state = 'queued', attempts = 0, not_before = ?, updated = ?, "
                f"last_error = 'requeued by {NAME}: ' || COALESCE(last_error, '') "
                f"WHERE id IN ({','.join('?' * len(ids))})", [now, now, *ids])  # fmt: skip
            self.db.kv_set("caretaker_requeued", (done + ids)[-2000:])
            return f"requeued {len(ids)} job(s) that died of a passing problem"
        if name == "quarantine":
            job_kind = str(inc["key"]).split(":", 1)[1]
            if job_kind in CORE_KINDS or job_kind.startswith("body."):
                return None  # core work is never stopped
            raw = self.db.kv_get("caretaker_quarantine") or {}
            q = {k: raw[k] for k in quarantined(self.db, now)} if isinstance(raw, dict) else {}
            if len(q) >= MAX_QUARANTINED and job_kind not in q:
                return None
            q[job_kind] = now + QUARANTINE_S
            self.db.kv_set("caretaker_quarantine", q)
            return f"paused {job_kind} for 6 hours (it kept failing the same way)"
        if name == "reset_knob":
            from polymath.drive import selftune

            row = self.db.one("SELECT subject FROM self_changes WHERE area = 'tuning' AND action = 'adjusted' AND "
                              "at >= ? ORDER BY at DESC LIMIT 1", (now - 3 * 86400,))  # fmt: skip
            if row is None or str(row["subject"]) not in selftune.KNOBS:
                return None
            done_names = selftune.reset(self.db, [str(row["subject"])], by=NAME)
            return f"rolled back the self-tuned setting {', '.join(done_names)} and pinned it" if done_names else None
        return None

    def restore(self, readings: dict[str, float]) -> None:
        """Undo the slowdown once heat and memory are back to normal (it would also expire by itself)."""
        slow = self.db.kv_get("caretaker_slowdown")
        if not isinstance(slow, dict):
            return
        busy = self.db.scalar("SELECT 1 FROM incidents WHERE kind IN ('heat', 'memory_high') AND state NOT IN "
                              "('resolved', 'closed') LIMIT 1")  # fmt: skip
        if busy and float(slow.get("until", 0)) > self.now:
            slow["until"] = self.now + SLOWDOWN_HOLD_S  # still needed: renewed
            self.db.kv_set("caretaker_slowdown", slow)
            return
        if not busy:
            self.db.kv_set("caretaker_slowdown", None)
            self.why("back to full speed: heat and memory are normal again", status="resolved")

    # ------------------------------------------------------------------ jobs, the body, the install
    def failures(self, job_kind: str) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM cycles WHERE action = ? AND status = 'failed' AND "
                                  "started >= ?", (job_kind, self.now - 3600), 0) or 0)  # fmt: skip

    def watch_jobs(self) -> int:
        opened = 0
        for r in self.db.query(
            "SELECT action AS kind, COUNT(*) AS n FROM cycles WHERE status = 'failed' AND started >= ? "
            "GROUP BY action HAVING n >= 3",
            (self.now - 3600,),
        ):
            err = self.db.scalar("SELECT last_error FROM jobs WHERE kind = ? AND last_error IS NOT NULL "
                                 "ORDER BY updated DESC LIMIT 1", (r["kind"],)) or ""  # fmt: skip
            sym = {"text": f"{r['kind']} failed {r['n']} times in an hour: {str(err)[:120]}", "at": self.now}
            opened += self.open_incident(f"crash_loop:{r['kind']}", "crash_loop", sym,
                                         title=f"{r['kind']} keeps failing")  # fmt: skip
        seen = int(self.db.kv_get("caretaker_dead_seen") or 0)
        rows = self.db.query("SELECT id, kind, last_error FROM jobs WHERE state = 'dead' AND id > ? AND updated >= ? "
                             "ORDER BY id LIMIT 200", (seen, self.now - 86400))  # fmt: skip
        for r in rows:
            sym = {"job": int(r["id"]), "text": f"{r['kind']}: {str(r['last_error'] or '')[:120]}", "at": self.now}
            opened += self.open_incident("dead_jobs", "dead_jobs", sym)
        if rows:
            self.db.kv_set("caretaker_dead_seen", max(int(r["id"]) for r in rows))
        return opened

    def undervoltage(self) -> tuple[bool, str]:
        try:
            flags = int(self.env.throttled.read_text(encoding="utf-8").strip(), 16)
            if flags & 0x1:
                return True, f"under-voltage now (firmware flags {flags:#x})"
            if flags & 0x10000:
                return True, f"under-voltage since boot (firmware flags {flags:#x})"
        except (OSError, ValueError):
            pass
        for alarm in sorted(self.env.hwmon.glob("hwmon*/in0_lcrit_alarm")) if self.env.hwmon.is_dir() else []:
            try:
                if alarm.read_text(encoding="utf-8").strip() == "1":
                    return True, "under-voltage alarm (hwmon in0_lcrit_alarm)"
            except OSError:
                continue
        return False, ""

    def slow_drives(self) -> list[tuple[str, int]]:
        if self.config.storage.sd_only:
            return []  # SD-card mode: the SSD is left alone
        from polymath.body.sensors import slow_usb_disks

        return slow_usb_disks(self.env.sys_block)

    def build_mismatch(self) -> str:
        from polymath import __version__
        from polymath.version import read_build

        installed = read_build().version
        return f"v{installed} is installed, the agent runs v{__version__}" if installed != __version__ else ""

    def config_hash(self) -> tuple[str, list[str]]:
        from polymath.core.config import dropin_dir

        files = [self.env.config_file] + (sorted(dropin_dir(self.env.config_file).glob("*.toml"))
                                         if dropin_dir(self.env.config_file).is_dir() else [])  # fmt: skip
        h = hashlib.sha256()
        names = []
        for f in files:
            try:
                h.update(f.name.encode() + b"\0" + f.read_bytes())
                names.append(str(f))
            except OSError:
                continue
        return (h.hexdigest() if names else ""), names

    def watch_body_and_install(self) -> int:
        opened = 0
        low, text = self.undervoltage()
        if low:
            opened += self.open_incident("power", "power", {"text": text, "at": self.now})
        for name, mbps in self.slow_drives():
            opened += self.open_incident("usb2", "usb2", {"text": f"/dev/{name} at {mbps} Mbit/s", "at": self.now})
        last = float(self.db.kv_get("caretaker_security_at") or 0.0)
        if self.now - last < SECURITY_EVERY:
            return opened
        self.db.kv_set("caretaker_security_at", self.now)
        mismatch = self.build_mismatch()
        if mismatch:
            opened += self.open_incident("build_mismatch", "build_mismatch", {"text": mismatch, "at": self.now})
        digest, names = self.config_hash()
        known = self.db.kv_get("caretaker_config_hash")
        if digest and known and digest != known:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(self.now))
            opened += self.open_incident("config_changed", "config_changed",
                                         {"text": f"the configuration changed (seen {when}): {', '.join(names)}",
                                          "at": self.now})  # fmt: skip
        if digest:
            self.db.kv_set("caretaker_config_hash", digest)
        try:
            st = os.stat(self.config.paths.data_dir)
            mode = f"{st.st_mode & 0o777:o} {st.st_uid}:{st.st_gid}"
        except OSError:
            mode = ""
        seen = self.db.kv_get("caretaker_data_mode")
        if mode and seen and mode != seen:
            opened += self.open_incident("permissions", "permissions",
                                         {"text": f"{self.config.paths.data_dir}: was {seen}, now {mode}",
                                          "at": self.now})  # fmt: skip
        if mode:
            self.db.kv_set("caretaker_data_mode", mode)
        return opened

    # ------------------------------------------------------------------ the inbox (core/issues)
    def triage_inbox(self) -> int:
        """Every problem reported since the last turn: attached to an incident that covers it, or one of its own."""
        from polymath.agents import faults

        seen = int(self.db.kv_get("caretaker_issue_seen") or 0)
        rows = self.db.query("SELECT id, at, text, detail FROM events WHERE kind = 'issue' AND id > ? ORDER BY id "
                             "LIMIT 200", (seen,))  # fmt: skip
        if not rows:
            return 0
        self.db.kv_set("caretaker_issue_seen", int(rows[-1]["id"]))
        hold = set(faults.knowledge().situation.get("on_hold", []))
        opened = 0
        for r in rows:
            try:
                detail = json.loads(r["detail"] or "{}")
            except ValueError:
                detail = {}
            area, code = str(detail.get("area", "")), str(detail.get("code", ""))
            known = [str(k) for k in detail.get("known") or []]
            fault = known[0] if known else ""
            if code.startswith("dead:") or code.startswith("permanent:"):
                continue  # the dead-jobs model takes these from the jobs table itself
            if code.startswith("thermal") and self.db.scalar(
                    "SELECT 1 FROM incidents WHERE kind = 'heat' AND state NOT IN ('resolved', 'closed')"):  # fmt: skip
                continue
            key = f"issue:{fault or area + '/' + code.split(':')[0]}"
            sym = {"text": str(r["text"])[:200], "at": float(r["at"]), "area": area, "code": code}
            known_fault = faults.knowledge().by_id.get(fault)
            title = known_fault.title if known_fault is not None else fault or str(r["text"])[:80]
            severity = str(detail.get("status", "warn"))
            if fault in hold:  # Christian put it on hold: noted, never escalated
                self.note_on_hold(key, fault, title, sym, int(r["id"]))
                continue
            opened += self.open_incident(key, "inbox", sym, title=title, fault=fault, severity=severity,
                                         issue_id=int(r["id"]))  # fmt: skip
        return opened

    def note_on_hold(self, key: str, fault: str, title: str, symptom: dict[str, Any], issue_id: int) -> None:
        """A problem Christian put on hold: recorded (a closed incident), said at most once a day, never raised."""
        advice = f"{NAME}: noted ({title}); on hold at Christian's request"
        self.db.execute(
            "INSERT INTO incidents(key, kind, area, fault, title, opened, updated, state, severity, symptoms, "
            "issue_ids, advice, outcome) VALUES(?, 'inbox', ?, ?, ?, ?, ?, 'closed', 'info', ?, ?, ?, 'on hold')",
            (key, str(symptom.get("area", "")), fault, title, self.now, self.now, json.dumps([symptom]),
             json.dumps([issue_id]), advice),
        )  # fmt: skip
        said: dict[str, str] = dict(self.db.kv_get("caretaker_hold_said") or {})
        day = time.strftime("%Y-%m-%d", time.localtime(self.now))
        if said.get(fault) != day:
            said[fault] = day
            self.db.kv_set("caretaker_hold_said", said)
            self.why(advice, status="closed")

    def work_inbox(self, inc: Any) -> None:
        from polymath.agents import faults

        last = max([float(s.get("at", 0)) for s in json.loads(inc["symptoms"])] or [float(inc["opened"])])
        if self.now - last > INBOX_QUIET_CLOSE_S:
            self.close(inc, "gone", "no new report for a day")
            return
        k = faults.knowledge()
        f = k.by_id.get(str(inc["fault"]))
        if f is not None and f.id in set(k.situation.get("on_hold", [])):
            return
        step = next((s for s in f.steps if s.needs in {"you", "sudo", "approval"}), None) if f else None
        if f is not None and step is None:
            return  # Polymath (or Orin) handles it by itself: watched, not escalated
        self.escalate(inc, None, again=inc["state"] == "escalated")

    # ------------------------------------------------------------------ what it says and shows
    def why(self, text: str, *, status: str = "info", severity: str = "") -> None:
        self.said.append(text)
        detail = {"status": status, **({"severity": severity} if severity else {})}
        self.db.execute("INSERT INTO events(at, kind, text, detail) VALUES(?, 'caretaker', ?, ?)",
                        (self.now, text[:400], json.dumps(detail)))  # fmt: skip
        log.info("caretaker", extra={"text": text[:200], "status": status})

    def publish(self) -> dict[str, Any]:
        """What the feed, the dashboard, doctor and ``polymath caretaker`` show (kv ``caretaker``)."""
        open_ = [{"id": int(r["id"]), "title": str(r["title"]), "state": str(r["state"]), "fault": str(r["fault"]),
                  "advice": str(r["advice"] or ""), "since": float(r["opened"]), "severity": str(r["severity"])}
                 for r in self.open_incidents()]  # fmt: skip
        resolved_recently = self.db.scalar("SELECT 1 FROM incidents WHERE state = 'resolved' AND updated >= ? LIMIT 1",
                                           (self.now - 1800,))  # fmt: skip
        working = any(i["state"] in {"open", "verifying"} for i in open_)  # escalated ones wait on Christian
        mood = "mending" if working else "proud" if resolved_recently else "calm"
        status = {"name": NAME, "at": self.now, "open": open_, "mood": mood,
                  "slowdown": self.db.kv_get("caretaker_slowdown"),
                  "quarantine": {k: (self.db.kv_get("caretaker_quarantine") or {})[k]
                                 for k in sorted(quarantined(self.db, self.now))}}  # fmt: skip
        self.db.kv_set(STATUS_KEY, status)
        return status

    def prune(self) -> None:
        self.db.execute("DELETE FROM incidents WHERE state IN ('resolved', 'closed') AND updated < ?",
                        (self.now - KEEP_INCIDENTS_S,))  # fmt: skip
        self.db.execute("DELETE FROM events WHERE kind = 'caretaker' AND at < ?", (self.now - 30 * 86400,))


PASSING = ("timed out", "timeout", "connection", "network", "temporary failure", "name or service", "unreachable",
           "database is locked", "disk i/o", "no space left", "errno 28", "errno 5", "read-only file system",
           "ssl", "http 5", "503", "502", "504", "429", "reset by peer", "broken pipe")  # fmt: skip


def passing(error: str) -> bool:
    """Was this a passing problem (network, disk, a lock), which a retry can get past?"""
    low = error.lower()
    return any(w in low for w in PASSING)


# ---------------------------------------------------------------------------------------------- what people read
def describe(status: dict[str, Any] | None) -> list[str]:
    """Orin's part of ``polymath caretaker``: what it is working on, and what it asks of you."""
    if not status:
        return ["", f"{NAME}: not reporting (the agent is not running, or runs a version without {NAME})"]
    open_ = status.get("open") or []
    out = ["", f"{NAME} the Caretaker ({'working on ' + str(len(open_)) + ' incident(s)' if open_ else 'all calm'}):"]
    for i in open_:
        since = time.strftime("%m-%d %H:%M", time.localtime(float(i.get("since") or 0)))
        out.append(f"  - {i.get('title')} [{i.get('state')}, since {since}]")
        if i.get("advice"):
            out.append(f"    {i['advice']}")
    slow = status.get("slowdown")
    if isinstance(slow, dict) and slow.get("until"):
        out.append(f"  slowed the agent down: {slow.get('why', '')}")
    for kind, until in (status.get("quarantine") or {}).items():
        out.append(f"  paused {kind} until {time.strftime('%H:%M', time.localtime(float(until)))}")
    return out


def incident_rows(config: Any, *, every: bool = False) -> list[dict[str, Any]] | None:
    """Incidents from the database (open ones, or the last 30 with ``every``); None when it cannot be read."""
    from polymath.core.db import Database

    try:
        db = Database(config.paths.db_path, readonly=True)
    except Exception:  # not an issue: no permission or no database; the caller falls back to the snapshot
        return None
    try:
        where = "" if every else "WHERE state NOT IN ('resolved', 'closed')"
        return [{"id": int(r["id"]), "title": str(r["title"]), "state": str(r["state"]), "fault": str(r["fault"]),
                 "since": float(r["opened"]), "advice": str(r["advice"] or ""), "outcome": str(r["outcome"] or ""),
                 "severity": str(r["severity"])}
                for r in db.query(f"SELECT * FROM incidents {where} ORDER BY id DESC LIMIT 30")]  # fmt: skip
    except Exception:  # not an issue: an older brain without the incidents table
        return None
    finally:
        db.close()


def incident_lines(rows: list[dict[str, Any]]) -> list[str]:
    out = []
    for r in rows:
        since = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(r.get("since") or 0)))
        done = f" → {r['outcome']}" if r.get("outcome") else ""
        out.append(f"#{r.get('id')} {since}  {r.get('title')} [{r.get('state')}]{done}")
        if r.get("advice"):
            out.append(f"    {r['advice']}")
    return out


# ---------------------------------------------------------------------------------------------- job, planner, loop
def step_job(ctx: Any) -> Any:
    """Job ``caretaker.step``: one turn of Orin's incident loop."""
    from polymath.core.jobs import JobOutcome

    out = Orin(ctx.config, ctx.db, ctx.scheduler).turn()
    return JobOutcome(done=True, value=0.0, result=out)


def planner(agent: Any) -> None:
    """Keep Orin's turn coming every EVERY seconds (urgent: it runs ahead of everything, and during a pause)."""
    if not getattr(agent.config.agents, "caretaker", True) or getattr(agent.config, "standby", False):
        return
    agent.scheduler.ensure_recurring(KIND, EVERY, priority=PRIORITY)


def quarantined(db: Any, now: float | None = None) -> set[str]:
    """Job kinds Orin has paused for now (the loop holds them back)."""
    now = time.time() if now is None else now
    try:
        q = db.kv_get("caretaker_quarantine") or {}
    except Exception:  # not an issue: the loop must never fail on Orin's switch
        return set()
    out = set()
    for kind, until in q.items() if isinstance(q, dict) else ():
        try:
            if float(until) > now and kind not in CORE_KINDS:
                out.add(str(kind))
        except (TypeError, ValueError):  # not an issue: a malformed entry is simply not a quarantine
            continue
    return out


def slowdown(db: Any, now: float | None = None) -> tuple[float, str]:
    """(intensity factor, why) while Orin slows the agent down; (1.0, '') otherwise. It expires by itself."""
    now = time.time() if now is None else now
    try:
        s = db.kv_get("caretaker_slowdown")
    except Exception:  # not an issue: the body guard must never fail on Orin's switch
        return 1.0, ""
    try:
        if not isinstance(s, dict) or float(s.get("until", 0)) <= now:
            return 1.0, ""
        return min(1.0, max(0.5, float(s.get("factor", 1.0)))), str(s.get("why", ""))
    except (TypeError, ValueError):  # not an issue: a malformed switch is no slowdown
        return 1.0, ""
