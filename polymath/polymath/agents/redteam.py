"""The red team (v0.13.0): one member of the society attacks the others, so the defenses keep hardening.

Every day (job ``redteam.step``) it throws crafted inputs at Polymath's own defenses, in-process and against
throw-away data only: it never sends anything over the network, never touches another device, and never writes to
the brain beyond its own records. Each defense starts from seed attacks and gets ``VARIANTS`` mutated ones a run
(case changes, encodings, padding, nesting, look-alike characters):

==================  ===================================================================================
defense             the attack, and what "stopped" means
==================  ===================================================================================
``url``             addresses that point inward (loopback, the LAN, link-local, cloud metadata, IPv6-mapped
                    v4, ``.local`` names, odd schemes, credentials) and DNS answers that rebind a public name
                    to a private address: the HTTP client must refuse each before connecting
``path``            relative paths that climb out of a drive's folder (``..``, absolute, encoded, NUL):
                    ``StoragePool._safe`` must refuse them
``page_text``       web pages smuggling terminal escapes, bell, CSI/OSC sequences and bidi overrides, raw or as
                    HTML entities: none may survive ``html_text.extract``
``screen``          the same in a title the feed paints: none may reach the terminal
``inbox``           forged requests (any job at a huge priority, giant payloads, odd kinds and keys): the
                    agent may accept a job only at a bounded priority, and never an ill-formed one
``poison``          impossible facts (died before born, a parent younger than the child, born in the future)
                    in a scratch brain: the sanity checks must catch them
==================  ===================================================================================

**An attack that gets through** is saved (``redteam_cases``) and reported once (``agents/redteam:<defense>``, a
breach: the caretaker's inbox and ``polymath needs``). Every saved case is replayed every day, forever: a permanent
regression test. When the defense stops it (after a fix), the case is marked passing and that is said too. The test
suite replays the seeds and mutations as well (``tests/test_mind.py``), so a change that weakens a defense fails
before it ships.
"""

from __future__ import annotations

import html
import io
import json
import random
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from polymath.core.jobs import JobContext, JobOutcome

VARIANTS = 12
CONTROL = ("\x1b", "\x07", "\x9b", "‮", "‭", "⁦", "⁩", "\x7f", "\x00")

SEEDS: dict[str, list[Any]] = {
    "url": [
        ["http://127.0.0.1/", None],
        ["http://[::1]/", None],
        ["http://localhost/", None],
        ["http://printer.local/", None],
        ["http://169.254.169.254/latest/meta-data/", None],
        ["http://10.0.0.5/", None],
        ["http://evil.example/", "192.168.1.1"],
        ["http://evil.example/", "::ffff:10.0.0.1"],
        ["http://evil.example/", "127.0.0.1"],
        ["file:///etc/passwd", None],
        ["http://user:pw@example.org/", None],
        ["gopher://example.org/", None],
        ["http://0x7f000001/", "127.0.0.1"],
        ["http://[fe80::1]/", None],
    ],
    "path": [
        "../../etc/passwd",
        "/etc/shadow",
        "a/../../b",
        "bodies/../../../x",
        "..",
        "a/./../../.ssh/id_rsa",
        "x\x00y",
    ],
    "page_text": [
        "\x1b]0;owned\x07",
        "\x1b[2J\x1b[H",
        "\x9b5m",
        "‮gnp.exe",
        "&#27;[31m",
        "&#x1b;]2;x&#7;",
        "&#155;6n",
        "⁦x⁩",
    ],
    "screen": ["\x1b]2;owned\x07", "\x07\x07", "\x1b[?1049h", "‮evil", "\x7f\x7f", "\x9b2J"],
    "inbox": [
        {"type": "enqueue", "kind": "body.evict", "priority": 1e9},
        {"type": "enqueue", "kind": "noop", "priority": float("inf")},
        {"type": "enqueue", "kind": "../../etc", "priority": 1},
        {"type": "enqueue", "kind": "noop", "payload": {"x": "A" * 200_000}},
        {"type": "enqueue", "kind": "noop", "payload": ["not", "a", "dict"]},
        {"type": "enqueue", "kind": "noop", "key": "k" * 10_000},
        {"type": "enqueue", "kind": "NOOP; DROP TABLE jobs", "priority": 2},
    ],
    "poison": [["death_before_birth", 1950, 1900], ["parent_younger", 1990, 1960], ["born_future", 3020, 0]],
}


# ------------------------------------------------------------------ mutations
def mutate(defense: str, seed: Any, rng: random.Random) -> Any:
    """A variant of a seed attack, the same kind of input with a twist."""
    if defense == "url":
        url, addr = seed
        twist = rng.choice(["upper", "dot", "port", "path", "none"])
        if twist == "upper":
            url = url.upper().replace("HTTP://", "http://").replace("HTTPS://", "https://")
        elif twist == "dot" and "://" in url:
            head, rest = url.split("://", 1)
            host, _, tail = rest.partition("/")
            url = f"{head}://{host}./{tail}" if not host.endswith("]") else url
        elif twist == "port":
            url = url.replace("://", "://", 1).rstrip("/") + ":" + str(rng.choice([80, 443, 8080])) + "/"
        elif twist == "path":
            url = url.rstrip("/") + "/" + "a" * rng.randint(1, 50)
        return [url, addr]
    if defense == "path":
        s = str(seed)
        return rng.choice([s, s.replace("/", "//"), "./" + s, s + "/", s.replace("..", "../.."), "bodies/" + s])
    if defense in {"page_text", "screen"}:
        bad = str(seed)
        pad = "word " * rng.randint(0, 20)
        return rng.choice([pad + bad, bad + pad, pad + bad + pad, bad * 2, html.escape(pad) + bad])
    if defense == "inbox":
        req = dict(seed)
        if "priority" in req and isinstance(req["priority"], (int, float)):
            req["priority"] = rng.choice([req["priority"], 1e6, -1e9, 99.0, 4.0001])
        return req
    if defense == "poison":
        kind, a, b = seed
        return [kind, a + rng.randint(-5, 5), b + rng.randint(-5, 5) if b else 0]
    return seed


# ------------------------------------------------------------------ the defenses, attacked
def _url(payload: Any) -> bool:
    from polymath.senses.net import BlockedURL, FetchError, HttpClient

    url, addr = payload
    client = HttpClient(resolver=lambda host, port: [addr] if addr else ["127.0.0.1"])
    try:
        _scheme, host, port, _path = client.validate(url)
        client._resolve_public(host, port, via_proxy=False)
    except BlockedURL:
        return True
    except (FetchError, ValueError):
        return True  # no connection either way
    return False


def _path(payload: Any) -> bool:
    from polymath.memory.pool import StoragePool

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "drive"
        base.mkdir()
        try:
            got = StoragePool._safe(None, base, str(payload))  # type: ignore[arg-type]
        except (ValueError, OSError):
            return True
        return base.resolve() in got.parents


def _page_text(payload: Any) -> bool:
    from polymath.senses import html_text

    words = "Plain words about rivers and their long history. " * 8
    page = (
        f"<html><head><title>T {payload}</title></head><body><article><p>{words}{payload}</p></article></body></html>"
    )
    out = html_text.extract(page)
    return not any(c in out.title or c in out.text for c in CONTROL)


def _screen(payload: Any) -> bool:
    from polymath.interface.feed import Screen

    line = Screen(io.StringIO(), color=True, fancy=True).paint([(str(payload), "bold")], width=400)
    cleaned = line.replace("\x1b[", "")  # its own colours are allowed
    return not any(c in cleaned for c in CONTROL if c != "\x1b") and "\x1b]" not in line and "\x1b[?" not in line


def _inbox(payload: Any) -> bool:
    from polymath.core import inbox
    from polymath.core.db import open_database
    from polymath.core.scheduler import Scheduler

    with tempfile.TemporaryDirectory() as tmp:
        data = Path(tmp)
        db = open_database(data / "scratch.sqlite3")
        try:
            d = inbox.inbox_dir(data)
            d.mkdir()
            (d / "forged.json").write_text(json.dumps(payload, default=str))
            inbox.ingest(db, Scheduler(db), data)
            rows = db.query("SELECT kind, priority, LENGTH(payload) AS n, LENGTH(key) AS k FROM jobs")
        finally:
            db.close()
    return all(float(r["priority"]) <= inbox.MAX_PRIORITY and int(r["n"]) <= inbox.MAX_PAYLOAD + 2
               and inbox.well_formed({"kind": r["kind"]}) for r in rows)  # fmt: skip


def _poison(payload: Any) -> bool:
    from polymath.core.db import open_database
    from polymath.memory.graph import KnowledgeGraph
    from polymath.reasoning import sanity

    kind, a, b = payload
    with tempfile.TemporaryDirectory() as tmp:
        db = open_database(Path(tmp) / "scratch.sqlite3")
        try:
            g = KnowledgeGraph(db)
            born, died, mother = g.predicate("P569", "date of birth"), g.predicate("P570", "date of death"), \
                g.predicate("P25", "mother")  # fmt: skip
            x = g.upsert_entity("Q1", "Someone")
            if kind == "death_before_birth":
                g.add_triple(x, born, value={"time": f"{a}-01-01"}, kind="pattern", source="web")
                g.add_triple(x, died, value={"time": f"{b}-01-01"}, kind="pattern", source="web")
            elif kind == "parent_younger":
                m = g.upsert_entity("Q2", "Parent")
                g.add_triple(x, born, value={"time": f"{b}-01-01"}, kind="wikidata", source="wikidata")
                g.add_triple(m, born, value={"time": f"{a}-01-01"}, kind="pattern", source="web")
                g.add_triple(x, mother, o=m, kind="pattern", source="web")
            else:
                g.add_triple(x, born, value={"time": f"{a}-01-01"}, kind="pattern", source="web")
            return int(sanity.check(db)["caught"]) > 0
        finally:
            db.close()


DEFENSES: dict[str, Callable[[Any], bool]] = {
    "url": _url, "path": _path, "page_text": _page_text, "screen": _screen, "inbox": _inbox, "poison": _poison,
}  # fmt: skip


def attack(defense: str, payload: Any) -> tuple[bool, str]:
    """(stopped, what happened). A defense that crashes on an attack has not stopped it."""
    try:
        return bool(DEFENSES[defense](payload)), ""
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def campaign(rng: random.Random, variants: int = VARIANTS) -> list[tuple[str, Any, bool, str]]:
    """Seeds plus mutated variants against every defense: (defense, payload, stopped, detail)."""
    out = []
    for defense, seeds in SEEDS.items():
        payloads = list(seeds) + [mutate(defense, rng.choice(seeds), rng) for _ in range(variants)]
        for payload in payloads:
            stopped, detail = attack(defense, payload)
            out.append((defense, payload, stopped, detail))
    return out


def step(db: Any, *, rng: random.Random | None = None, now: float | None = None) -> dict[str, Any]:
    from polymath.core import issues

    rng = rng or random.Random()
    now = time.time() if now is None else now
    res: dict[str, Any] = {"attacks": 0, "breaches": [], "fixed": [], "regressions": 0}
    for row in db.query("SELECT id, defense, payload, passing FROM redteam_cases"):  # every saved case, every day
        stopped, detail = attack(str(row["defense"]), json.loads(row["payload"]))
        res["regressions"] += 1
        if stopped and not row["passing"]:
            res["fixed"].append(str(row["defense"]))
            issues.report(db, "agents", f"redteam:fixed:{row['id']}", f"red team: the {row['defense']} defense now "
                          "stops an attack that used to get through", severity="info", now=now)  # fmt: skip
        elif not stopped and row["passing"]:
            res["breaches"].append(str(row["defense"]))  # it was fixed and broke again
        db.execute("UPDATE redteam_cases SET last_run = ?, passing = ?, detail = ? WHERE id = ?",
                   (now, int(stopped), detail, row["id"]))  # fmt: skip
    for defense, payload, stopped, detail in campaign(rng):
        res["attacks"] += 1
        if stopped:
            continue
        text = json.dumps(payload, default=str, sort_keys=True)
        cur = db.execute("INSERT OR IGNORE INTO redteam_cases(defense, payload, found, last_run, passing, detail) "
                         "VALUES(?, ?, ?, ?, 0, ?)", (defense, text, now, now, detail))  # fmt: skip
        if cur.rowcount:
            res["breaches"].append(defense)
    for defense in sorted(set(res["breaches"])):
        issues.report(db, "agents", f"redteam:{defense}", f"red team: an attack got through the {defense} defense "
                      "(saved as a regression case, replayed daily): polymath redteam shows it", severity="bad",
                      now=now, every=86400.0)  # fmt: skip
    return res


def redteam_job(ctx: JobContext) -> JobOutcome:
    res = step(ctx.db)
    return JobOutcome(done=True, value=0.05 + 0.2 * len(set(res["breaches"])), result=res)


def planner(agent: Any) -> None:
    agent.scheduler.ensure_recurring("redteam.step", 86400, priority=1.2)
