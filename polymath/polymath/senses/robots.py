"""Own robots.txt parser and matcher (RFC 9309 semantics).

* Groups are selected by the most specific matching user-agent product token;
  ``*`` is the fallback. Multiple groups for the same agent are merged.
* Rules: the longest matching pattern wins; on a tie ``Allow`` wins.
  ``*`` matches any sequence and a trailing ``$`` anchors the end.
* ``Crawl-delay`` and ``Sitemap`` are honoured/collected.
* Fetch outcome policy (applied by the crawler): 2xx → parse, 4xx → allow all,
  5xx/unreachable → disallow all until retried.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import quote, unquote, urlsplit

MAX_ROBOTS_BYTES = 500 * 1024


def _normalize_path(path: str) -> str:
    # Decode then re-encode so %7E and ~ compare equal; keep reserved characters.
    return quote(unquote(path), safe="/?=&;:@!$'()*+,-._~%")


@dataclass
class Rule:
    allow: bool
    pattern: str
    regex: re.Pattern[str]

    @property
    def specificity(self) -> int:
        return len(self.pattern)


def _compile(pattern: str) -> re.Pattern[str]:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    parts = [re.escape(p) for p in body.split("*")]
    return re.compile(".*".join(parts) + ("$" if anchored else ""))


@dataclass
class Group:
    agents: list[str] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    crawl_delay: float | None = None


@dataclass
class Robots:
    groups: list[Group] = field(default_factory=list)
    sitemaps: list[str] = field(default_factory=list)
    allow_all: bool = False
    disallow_all: bool = False

    def _group_for(self, agent: str) -> Group | None:
        """Merge every group naming our product token; fall back to ``*`` groups."""
        token = agent.lower().split("/", 1)[0].strip()
        mine = [g for g in self.groups if any(a.split("/", 1)[0].strip() == token for a in g.agents)]
        chosen = mine or [g for g in self.groups if "*" in g.agents]
        if not chosen:
            return None
        return Group(
            agents=[token] if mine else ["*"],
            rules=[r for g in chosen for r in g.rules],
            crawl_delay=next((g.crawl_delay for g in chosen if g.crawl_delay is not None), None),
        )

    def allowed(self, url: str, agent: str) -> bool:
        if self.disallow_all:
            return False
        if self.allow_all:
            return True
        parts = urlsplit(url)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        if path == "/robots.txt":
            return True
        group = self._group_for(agent)
        if group is None:
            return True
        target = _normalize_path(path)
        best: Rule | None = None
        for rule in group.rules:
            if not rule.pattern:
                continue  # an empty Disallow allows everything
            better = (
                best is None
                or rule.specificity > best.specificity
                or (rule.specificity == best.specificity and rule.allow and not best.allow)
            )
            if better and rule.regex.match(target):
                best = rule
        return True if best is None else best.allow

    def crawl_delay(self, agent: str) -> float | None:
        if self.allow_all or self.disallow_all:
            return None
        g = self._group_for(agent)
        return g.crawl_delay if g else None


def parse_robots(text: str) -> Robots:
    robots = Robots()
    current: Group | None = None
    last_was_agent = False
    for raw in text[:MAX_ROBOTS_BYTES].splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key in {"user-agent", "useragent"}:
            if current is None or not last_was_agent:
                current = Group()
                robots.groups.append(current)
            current.agents.append(value.lower())
            last_was_agent = True
            continue
        last_was_agent = False
        if key == "sitemap":
            robots.sitemaps.append(value)
            continue
        if current is None:
            continue
        if key in {"allow", "disallow"}:
            pattern = _normalize_path(value) if value else ""
            if pattern and not pattern.startswith(("/", "*")):
                pattern = "/" + pattern
            current.rules.append(Rule(key == "allow", pattern, _compile(pattern)))
        elif key == "crawl-delay":
            try:
                current.crawl_delay = max(0.0, float(value))
            except ValueError:
                pass
    return robots


def robots_for_status(status: int, text: str) -> Robots:
    if 200 <= status < 300:
        return parse_robots(text)
    if 400 <= status < 500:
        return Robots(allow_all=True)
    return Robots(disallow_all=True)
