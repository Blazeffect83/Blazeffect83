"""Small typing helpers for tests."""

from __future__ import annotations

from typing import TypeVar

T = TypeVar("T")


def some(value: T | None) -> T:
    """Narrow an optional lookup the test expects to succeed (fails loudly when it does not)."""
    assert value is not None
    return value
