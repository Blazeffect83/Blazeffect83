"""Transparent, rule-based source-quality rubric (0–100).

The score is an ordinal heuristic for ranking, *not* a calibrated probability.
Every point awarded or removed is recorded in the breakdown so the dashboard
can show exactly why a document scored as it did.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..security.network_policy import domain_matches

_SECONDARY_HINTS = ("medium.com", "substack.com", "blogspot.", "wordpress.com", "reddit.com", "quora.com",
                    "pinterest.", "facebook.com", "x.com", "twitter.com", "tiktok.com")
_PRIMARY_PATH_HINTS = ("/docs", "/documentation", "/manual", "/reference", "/spec", "/rfc", "/api/",
                       "/release-notes", "/changelog", "/abs/", "/pdf/")
_COI_HINTS = re.compile(r"\b(sponsored|affiliate link|promo code|buy now|paid partnership|advertorial)\b", re.I)
_EVIDENCE_HINTS = re.compile(r"\b(benchmark|measured|we (?:found|observed|tested)|results? show|"
                             r"according to|doi:|arxiv|table \d|figure \d|source:)\b", re.I)


@dataclass
class QualityScore:
    score: float
    breakdown: dict = field(default_factory=dict)
    is_primary: bool = False

    @property
    def label(self) -> str:
        return "high" if self.score >= 70 else "medium" if self.score >= 45 else "low"


def _age_days(published: str | None) -> float | None:
    if not published:
        return None
    for fmt in (None, "%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z", "%Y-%m-%d"):
        try:
            dt = datetime.fromisoformat(published.replace("Z", "+00:00")) if fmt is None \
                else datetime.strptime(published.strip(), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt).total_seconds() / 86400
        except (ValueError, TypeError):
            continue
    return None


def score_source(*, url: str, domain: str, text: str, published_at: str | None, author: str | None,
                 primary_domains: list[str], relevance: float, corroborations: int = 0,
                 injection_flags: list[str] | None = None, freshness_days: int = 365) -> QualityScore:
    b: dict[str, float] = {}
    base = 40.0
    b["base"] = base
    primary = domain_matches(domain, primary_domains) or any(h in url.lower() for h in _PRIMARY_PATH_HINTS) \
        or domain.endswith((".gov", ".edu", ".int"))
    if primary:
        b["primary_source"] = 15
    if any(h in domain for h in _SECONDARY_HINTS):
        b["user_generated_platform"] = -10
    if url.startswith("https://"):
        b["https"] = 3
    else:
        b["no_https"] = -5
    if author:
        b["named_author"] = 4
    evidence = len(_EVIDENCE_HINTS.findall(text))
    if evidence:
        b["evidence_markers"] = min(10, evidence * 2)
    if "```" in text or re.search(r"^\s{4,}\S", text, re.M):
        b["code_or_examples"] = 3
    words = len(text.split())
    if words < 120:
        b["very_short"] = -10
    elif words > 600:
        b["substantive_length"] = 4
    age = _age_days(published_at)
    if age is None:
        b["undated"] = -3
    elif age <= freshness_days:
        b["recent"] = 6
    elif age > freshness_days * 3:
        b["old"] = -6
    if _COI_HINTS.search(text):
        b["conflict_of_interest_markers"] = -10
    if corroborations:
        b["independent_corroboration"] = min(12, corroborations * 4)
    b["relevance"] = round(max(0.0, min(1.0, relevance)) * 15, 1)
    if injection_flags:
        b["prompt_injection_suspected"] = -25
    score = max(0.0, min(100.0, sum(b.values())))
    return QualityScore(score=round(score, 1), breakdown=b, is_primary=primary)
