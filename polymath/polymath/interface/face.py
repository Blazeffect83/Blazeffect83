"""Polymath's face: a tiny animated character at the top of the live feed that shows what it is doing.

There is no language model and nothing random here. The expression comes from three things:

* what the agent is doing right now (the job it is running, its mode, whether it is online);
* reactions to what just happened (a right or wrong self-test answer, a fixed mistake, a contradiction, an error,
  a milestone), each lasting a few seconds;
* the clock, which drives the animation: eyes scanning while it reads, thought dots while it reasons, a spinner
  while it trains or downloads, a blink every few seconds, ``zZ`` while it waits for work, and the spark on its head
  pulsing while the agent is alive.

It looks like this, followed by the rest of the header::

    ✦[◑‿◑]    reading         ✧[◔_◔]... reasoning       ✦[^‿^]✧   a right answer      ○[×_×]    offline

Every glyph is single-width and present in DejaVu Sans Mono (the Raspberry Pi OS terminal font); the feed's
ASCII fallback covers terminals without Unicode.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

FRAME_S = 0.5  # one animation step
WIDTH = 10  # spark + "[" + up to 4 face characters + "]" + up to 3 trailing characters: the header never jumps
BLINK_EVERY = 13  # frames between blinks (about every 6.5 s)
SPINNER = ("◜", "◝", "◞", "◟")

Seg = tuple[str, str]


@dataclass(frozen=True)
class Mood:
    frames: tuple[str, ...]  # face per animation step, cycled
    style: str
    trail: tuple[str, ...] = ("",)  # small decoration after the face, cycled
    blink: bool = True


MOODS: dict[str, Mood] = {
    # what it is doing
    "reading": Mood(("◐‿◐", "◐‿◐", "◑‿◑", "◑‿◑"), "cyan"),  # eyes run along the line
    "downloading": Mood(("◕‿◕",), "blue", SPINNER),
    "vetting": Mood(("≖_≖", "≖_≖", "¬_¬", "¬_¬"), "blue", ("?", "?", " ", " ")),  # squinting at a new site
    "thinking": Mood(("◔_◔",), "magenta", ("", ".", "..", "...")),
    "training": Mood(("◕‿◕",), "bcyan", SPINNER),
    "quizzing": Mood(("•_•",), "yellow", ("?", "?", "", "")),
    "agents": Mood(("◕ω◕",), "yellow", ("·", "··", "···", "")),
    "writing": Mood(("•‿•",), "", ("✎", "✎", "", "")),
    "tidying": Mood(("ᵔ‿ᵔ",), "dim", SPINNER),
    "idle": Mood(("-‿-",), "dim", ("z", "zZ", "zZz", "zZ"), blink=False),
    "sleepy": Mood(("˘‿˘",), "dim", ("z", "zZ", "zZz", "zZ"), blink=False),  # idle at night
    "hot": Mood((">_<",), "yellow", ("≈", "")),
    "paused": Mood(("-_-",), "yellow", blink=False),
    "offline": Mood(("×_×",), "red", blink=False),
    "connecting": Mood(("·_·", "·_·", "•_•", "•_•"), "dim", blink=False),
    # reactions
    "happy": Mood(("^‿^",), "bgreen", ("✧", "", "✦", ""), blink=False),
    "aha": Mood(("◕‿◕",), "bcyan", ("!", "")),
    "proud": Mood(("⌐■_■",), "bgreen", blink=False),
    "oops": Mood(("ᵒ_ᵒ",), "red", ("!", "")),
    "doubt": Mood(("¬_¬",), "yellow", ("?",)),
    "error": Mood(("×_×",), "red", ("!", ""), blink=False),
    "celebrate": Mood(("^‿^", "^ω^"), "bmagenta", ("★", "☆"), blink=False),
}

# job kind (or its prefix before the dot) → mood while that job runs
ACTION_MOOD: dict[str, str] = {
    "dump.download": "downloading",
    "web.blocklists": "downloading",
    "web.blocklist": "downloading",
    "web.vet": "vetting",
    "web.trust": "vetting",
    "perception.read": "reading",
    "perception.anchors": "reading",
    "perception.automaton": "training",
    "perception.relations": "training",
    "memory.index": "training",
    "memory.graph": "thinking",
    "memory.topics": "thinking",
    "eval.quiz": "quizzing",
    "eval.holdout": "quizzing",
    "eval.remedy": "thinking",
    "eval.digest": "writing",
    "eval.report": "writing",
    "agents.verify": "quizzing",
    "drive.learn": "agents",
    "body.recall": "reading",
    "noop": "idle",
}
PREFIX_MOOD: dict[str, str] = {
    "wikipedia": "reading",
    "wikidata": "reading",
    "openalex": "reading",
    "pubmed": "reading",
    "gutenberg": "reading",
    "stackexchange": "reading",
    "feeds": "reading",
    "crawl": "reading",
    "perception": "training",
    "embed": "training",
    "reason": "thinking",
    "drive": "thinking",
    "agents": "agents",
    "body": "tidying",
    "sources": "thinking",
}


def mood_for_action(action: str | None, *, hour: int = 12) -> str:
    if not action or action == "noop":
        return "sleepy" if 0 <= hour < 6 else "idle"
    if action in ACTION_MOOD:
        return ACTION_MOOD[action]
    return PREFIX_MOOD.get(action.split(".", 1)[0], "thinking")


@dataclass
class _Reaction:
    mood: str
    until: float
    priority: int


class Face:
    """Picks and animates the expression. Feed it the status (``update``) and events (``see``); draw ``segments``."""

    def __init__(self) -> None:
        self.status: dict[str, Any] | None = None
        self.offline = ""
        self.reaction: _Reaction | None = None

    # ------------------------------------------------------------------ input
    def update(self, status: dict[str, Any] | None, *, offline: str = "") -> None:
        self.status, self.offline = status, offline

    def react(self, mood: str, now: float, seconds: float, priority: int) -> None:
        r = self.reaction
        if r is None or now >= r.until or priority >= r.priority:
            self.reaction = _Reaction(mood, now + seconds, priority)

    def see(self, event: dict[str, Any], now: float | None = None) -> None:
        """React to one feed event."""
        now = time.time() if now is None else now
        k = event.get("kind")
        if k == "quiz":
            self.react("happy" if event.get("correct") else "oops", now, 3.0, 1)
        elif k == "quizscore":
            better = float(event.get("accuracy", 0)) > float(event.get("chance", 0))
            self.react("happy" if better else "oops", now, 5.0, 2)
        elif k == "inferred":
            self.react("aha", now, 2.0, 0)
        elif k == "disputed":
            self.react("doubt", now, 3.0, 1)
        elif k == "error":
            self.react("error", now, 4.0, 2)
        elif k == "note":
            what, status = event.get("what"), event.get("status")
            if what == "fixed":
                self.react("proud", now, 8.0, 3)
            elif what == "home" or (what == "prediction" and status == "confirmed"):
                self.react("celebrate" if what == "home" else "proud", now, 8.0, 3)
            elif what == "didyouknow":
                self.react("aha", now, 4.0, 1)
            elif what == "wear" and status in {"read-only", "critical"}:
                self.react("error", now, 6.0, 2)
            elif what == "recap":
                self.react("happy", now, 6.0, 2)
            elif what == "site" and status in {"approved", "probation"}:
                self.react("happy", now, 3.0, 1)
            elif what == "site" and status in {"refused", "dropped"}:
                self.react("doubt", now, 3.0, 1)
        elif k in {"report", "newagent"} or (k == "digest" and event.get("head")):
            self.react("happy", now, 6.0, 2)
        elif k == "storage" and event.get("event") == "added":
            self.react("celebrate", now, 8.0, 3)
        elif k == "milestone":
            self.react("celebrate", now, 10.0, 4)

    # ------------------------------------------------------------------ output
    def mood(self, now: float | None = None) -> str:
        now = time.time() if now is None else now
        st = self.status
        if st is None or self.offline:
            return "connecting"
        if not st.get("online"):
            return "offline"
        if self.reaction is not None and now < self.reaction.until:
            return self.reaction.mood
        mode = st.get("mode")
        if st.get("state") == "paused" or mode == "pause":
            return "paused"
        if mode in {"throttle", "yield"}:
            return "hot"
        if st.get("activity") == "waiting for work":
            return mood_for_action(None, hour=time.localtime(now).tm_hour)
        return mood_for_action(st.get("action") or None, hour=time.localtime(now).tm_hour)

    def segments(self, now: float | None = None) -> list[Seg]:
        """The face as header segments, always ``WIDTH`` characters wide."""
        now = time.time() if now is None else now
        name = self.mood(now)
        m = MOODS[name]
        n = int(now / FRAME_S)
        face = m.frames[n % len(m.frames)]
        if m.blink and n % BLINK_EVERY == 0 and len(face) == 3:
            face = f"-{face[1]}-"
        trail = m.trail[n % len(m.trail)]
        if name in {"offline"}:
            spark: Seg = ("○", "red")
        elif name == "connecting":
            spark = ("✧" if n % 2 else " ", "dim")
        else:
            spark = ("✦", "byellow") if n % 4 == 0 else ("✧", "yellow")  # a heartbeat on its antenna
        body = f"[{face}]{trail}"
        return [spark, (body.ljust(WIDTH - 1), m.style)]
