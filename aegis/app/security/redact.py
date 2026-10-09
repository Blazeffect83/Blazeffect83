"""Secret redaction applied to logs, audit records, tool output and notifications."""
from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

_PATTERNS = [
    # Provider-style API keys.
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bxox[abpr]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{30,}\b"),  # Telegram bot token
    # Authorization headers / bearer tokens.
    re.compile(r"(?i)(authorization\s*[:=]\s*)(bearer|basic)\s+[A-Za-z0-9._~+/=\-]+"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{12,}"),
    # key=value pairs with sensitive names.
    re.compile(
        r"(?i)\b([A-Z0-9_]*(?:api[_-]?key|secret|password|passwd|token|private[_-]?key)[A-Z0-9_]*)"
        r"(\s*[:=]\s*)(['\"]?)([^\s'\",;]{4,})"
    ),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
]

SENSITIVE_KEYS = re.compile(r"(?i)(api[_-]?key|secret|password|passwd|token|authorization|cookie|private[_-]?key)")


class Redactor:
    def __init__(self, known_secrets: list[str] | None = None):
        self.known = sorted({s for s in (known_secrets or []) if s and len(s) >= 6}, key=len, reverse=True)

    def add(self, secret: str) -> None:
        if secret and len(secret) >= 6 and secret not in self.known:
            self.known.append(secret)
            self.known.sort(key=len, reverse=True)

    def text(self, value: str | None) -> str | None:
        if value is None:
            return None
        out = str(value)
        for s in self.known:
            out = out.replace(s, REDACTED)
        for pat in _PATTERNS:
            if pat.groups >= 4:
                out = pat.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{REDACTED}", out)
            elif pat.groups == 2:
                out = pat.sub(lambda m: f"{m.group(1)}{REDACTED}", out)
            else:
                out = pat.sub(REDACTED, out)
        return out

    def obj(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                k: (REDACTED if isinstance(k, str) and SENSITIVE_KEYS.search(k) and v else self.obj(v))
                for k, v in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self.obj(v) for v in value]
        if isinstance(value, str):
            return self.text(value)
        return value


_global = Redactor()


def get_redactor() -> Redactor:
    return _global


def configure_redactor(secrets: list[str]) -> Redactor:
    for s in secrets:
        _global.add(s)
    return _global
