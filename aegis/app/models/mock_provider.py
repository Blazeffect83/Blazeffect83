"""Deterministic scripted provider for tests and offline demos.

Responses are consumed in order from a script. Each entry is either a string
(returned as text), a dict/list (returned as structured data), a callable
``fn(messages, system, json_schema) -> str | dict``, or a ``ModelError`` to raise.
"""
from __future__ import annotations

import json
from collections import deque
from typing import Any

from .base import ErrorClass, ModelError, ModelProvider, ModelResponse


class MockProvider(ModelProvider):
    name = "mock"

    def __init__(self, script: list[Any] | None = None, model: str = "mock-model", **kw):
        super().__init__(model, **kw)
        self.script: deque = deque(script or [])
        self.calls: list[dict] = []

    def push(self, *items: Any) -> None:
        self.script.extend(items)

    def _generate_once(self, messages, *, system, max_tokens, json_schema, tools, temperature, model):
        self.calls.append({"messages": messages, "system": system, "json_schema": json_schema, "model": model})
        if not self.script:
            raise ModelError("mock: script exhausted", ErrorClass.UNAVAILABLE)
        item = self.script.popleft()
        if callable(item) and not isinstance(item, ModelError):
            item = item(messages, system, json_schema)
        if isinstance(item, ModelError):
            raise item
        prompt_chars = sum(len(m.content) for m in messages) + len(system)
        if isinstance(item, (dict, list)):
            text, data = json.dumps(item), item
        else:
            text, data = str(item), None
        return ModelResponse(
            text=text, provider=self.name, model=model,
            input_tokens=max(1, prompt_chars // 4), output_tokens=max(1, len(text) // 4),
            data=data if json_schema is not None else None,
        )
