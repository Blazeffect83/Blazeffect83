"""The caretaker's knowledge (``faults.toml``): known problems, what Polymath does about them, and the steps for
Christian; plus what is true on the Pi now and what happened on it before.

* ``knowledge()`` loads and validates it once (it ships inside the package);
* ``for_issue(code)`` and ``for_text(text)`` find the faults a problem matches. ``core.issues.report`` tags
  every problem with them, so the caretaker's inbox says *which* known problem it is;
* ``briefing(...)`` is the current situation in a few lines, for ``polymath caretaker`` and, later, Orin.

Nothing here acts. Steps marked ``approval`` change the system and are only ever shown to Christian.
"""

from __future__ import annotations

import fnmatch
import functools
import re
import textwrap
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

NEEDS = ("auto", "caretaker", "you", "sudo", "approval")


class FaultsError(ValueError):
    """faults.toml is malformed (a test keeps this from ever shipping)."""


@dataclass(frozen=True)
class Step:
    needs: str
    do: str
    command: str = ""


@dataclass(frozen=True)
class Cause:
    cause: str
    prior: float


@dataclass(frozen=True)
class Fault:
    id: str
    title: str
    area: str
    codes: tuple[str, ...]
    patterns: tuple[str, ...]
    symptoms: tuple[str, ...]
    causes: tuple[Cause, ...]
    checks: tuple[str, ...]
    self_healing: tuple[str, ...]
    steps: tuple[Step, ...]
    since: str
    updated: str

    def matches_code(self, code: str) -> bool:
        return any(fnmatch.fnmatchcase(code, g) for g in self.codes)

    def matches_text(self, text: str) -> bool:
        low = text.lower()
        return any(re.search(p.lower(), low) if any(c in p for c in ".*[") else p.lower() in low
                   for p in self.patterns)  # fmt: skip

    def next_step(self) -> Step | None:
        """What to do: the first step that is not Polymath's own (``auto``) work."""
        return next((s for s in self.steps if s.needs != "auto"), self.steps[0] if self.steps else None)


@dataclass(frozen=True)
class Incident:
    date: str
    version: str
    what: str
    cause: str
    fault: str
    fixed_in: str
    open: bool = False


@dataclass(frozen=True)
class Knowledge:
    meta: dict[str, Any]
    situation: dict[str, Any]
    updates: tuple[dict[str, Any], ...]
    faults: tuple[Fault, ...]
    history: tuple[Incident, ...]
    by_id: dict[str, Fault] = field(default_factory=dict)

    @property
    def updated_for(self) -> str:
        return str(self.meta.get("updated_for", ""))


def _fault(raw: dict[str, Any]) -> Fault:
    try:
        steps = tuple(Step(str(s["needs"]), str(s["do"]), str(s.get("command", ""))) for s in raw.get("steps", []))
        causes = tuple(Cause(str(c["cause"]), float(c["prior"])) for c in raw.get("causes", []))
        f = Fault(
            id=str(raw["id"]), title=str(raw["title"]), area=str(raw["area"]),
            codes=tuple(raw.get("codes", [])), patterns=tuple(raw.get("patterns", [])),
            symptoms=tuple(raw.get("symptoms", [])), causes=causes, checks=tuple(raw.get("checks", [])),
            self_healing=tuple(raw.get("self_healing", [])), steps=steps,
            since=str(raw["since"]), updated=str(raw["updated"]),
        )  # fmt: skip
    except (KeyError, TypeError, ValueError) as exc:
        raise FaultsError(f"fault {raw.get('id', '?')}: {exc!r}") from exc
    bad = [s.needs for s in f.steps if s.needs not in NEEDS]
    if bad:
        raise FaultsError(f"fault {f.id}: unknown step needs {bad} (one of {NEEDS})")
    if not f.steps or not f.symptoms:
        raise FaultsError(f"fault {f.id}: needs symptoms and steps")
    if f.causes and abs(sum(c.prior for c in f.causes) - 1.0) > 0.01:
        raise FaultsError(f"fault {f.id}: cause priors must add up to 1")
    return f


def parse(text: str) -> Knowledge:
    data = tomllib.loads(text)
    faults = tuple(_fault(r) for r in data.get("fault", []))
    by_id: dict[str, Fault] = {}
    for f in faults:
        if f.id in by_id:
            raise FaultsError(f"fault id {f.id} appears twice")
        by_id[f.id] = f
    try:
        history = tuple(Incident(**h) for h in data.get("history", []))
    except TypeError as exc:
        raise FaultsError(f"history: {exc}") from exc
    unknown = sorted({h.fault for h in history} - set(by_id)) + sorted(
        set(data.get("situation", {}).get("recommended", [])) - set(by_id)
    )
    if unknown:
        raise FaultsError(f"history or situation names unknown faults: {unknown}")
    return Knowledge(meta=dict(data.get("meta", {})), situation=dict(data.get("situation", {})),
                     updates=tuple(data.get("update", [])), faults=faults, history=history,
                     by_id=by_id)  # fmt: skip


@functools.lru_cache(maxsize=1)
def knowledge() -> Knowledge:
    return parse(resources.files("polymath.agents").joinpath("faults.toml").read_text(encoding="utf-8"))


def for_issue(code: str, text: str = "") -> list[Fault]:
    """The known faults an issue (``area/code``, and its text) matches: by code first, else by its words."""
    k = knowledge()
    hits = [f for f in k.faults if f.matches_code(code)]
    if not hits and text:
        hits = [f for f in k.faults if f.matches_text(text)]
    return hits


def for_text(text: str) -> list[Fault]:
    return [f for f in knowledge().faults if f.matches_text(text)]


def describe_step(step: Step) -> str:
    tag = {"auto": "Polymath does this itself", "caretaker": "the caretaker may do this itself",
           "you": "you", "sudo": "you, with sudo", "approval": "needs your approval"}[step.needs]  # fmt: skip
    return f"{step.do}{f': {step.command}' if step.command else ''}  [{tag}]"


def fault_lines(f: Fault) -> list[str]:
    out = [f"{f.title}  ({f.id}; known since v{f.since}, updated v{f.updated})", "  what you see:"]
    out += [f"    - {s}" for s in f.symptoms]
    if f.causes:
        out.append("  likely causes:")
        out += [f"    - {c.cause} ({c.prior:.0%})" for c in sorted(f.causes, key=lambda c: -c.prior)]
    if f.checks:
        out.append("  how to check:")
        out += [f"    - {c}" for c in f.checks]
    if f.self_healing:
        out.append("  what Polymath does by itself:")
        out += [f"    - {s}" for s in f.self_healing]
    out.append("  what to do:")
    out += [f"    {i}. {describe_step(s)}" for i, s in enumerate(f.steps, 1)]
    return out


def briefing(issues: list[dict[str, Any]] | None = None, *, version: str = "") -> list[str]:
    """The current situation: what is true on the Pi, what is open, and the next steps (for people and Orin)."""
    k = knowledge()
    st = k.situation
    out = [f"caretaker knowledge for v{k.updated_for} (updated {k.meta.get('updated', '?')})"]
    if version and version != k.updated_for:
        out.append(f"! this build is v{version}: the knowledge is for v{k.updated_for}")
    out += ["", f"situation ({st.get('as_of', '?')}):"]
    out += textwrap.wrap(" ".join(str(st.get("summary", "")).split()), 110, initial_indent="  ", subsequent_indent="  ")
    if st.get("waiting_on"):
        out.append("  waiting on:")
        out += [f"    - {w}" for w in st["waiting_on"]]
    if st.get("suspected_root_cause"):
        out.append(f"  suspected root cause: {st['suspected_root_cause']}")
    for fid in st.get("recommended", []):
        f = k.by_id[fid]
        step = f.next_step()
        out.append(f"  recommended: {f.title}" + (f" → {describe_step(step)}" if step else ""))
    if issues:
        out += ["", "recent problems (newest first):"]
        for i in issues[:8]:
            code = f"{i.get('area', '')}/{i.get('code', '')}"
            hits = for_issue(code, str(i.get("text", "")))
            known = f"  → known: {hits[0].id}" if hits else "  → not a known problem yet"
            out.append(f"  - {str(i.get('text', ''))[:140]}{known}")
    still = [h for h in k.history if h.open]
    if still:
        out += ["", "open from before:"]
        out += [f"  - {h.date} (v{h.version}): {h.what} → {h.fault}" for h in still]
    return out
