"""Anthropic Messages API provider (raw HTTP; no SDK dependency)."""
from __future__ import annotations

from .base import ErrorClass, ModelError, ModelProvider, ModelResponse, ToolCall

API_VERSION = "2023-06-01"


class AnthropicProvider(ModelProvider):
    name = "anthropic"

    def __init__(self, api_key: str, model: str, base_url: str = "https://api.anthropic.com", **kw):
        if not api_key:
            raise ModelError("anthropic: ANTHROPIC_API_KEY is not set", ErrorClass.AUTH)
        super().__init__(model, **kw)
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")

    def _generate_once(self, messages, *, system, max_tokens, json_schema, tools, temperature, model):
        body: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }
        # temperature is intentionally left at the provider default: not every
        # current model accepts a custom value.
        if system:
            body["system"] = system
        api_tools = [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in (tools or [])
        ]
        if json_schema is not None:
            # Structured output via a forced tool call: the tool input *is* the result.
            api_tools.append({
                "name": "emit_result",
                "description": "Return the final structured result.",
                "input_schema": json_schema,
            })
            body["tool_choice"] = {"type": "tool", "name": "emit_result"}
        if api_tools:
            body["tools"] = api_tools
        resp = self.client.post(
            f"{self.base_url}/v1/messages",
            json=body,
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": API_VERSION,
                "content-type": "application/json",
            },
        )
        self._raise_for_status(resp)
        data = resp.json()
        text_parts, calls, structured = [], [], None
        for block in data.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                if block.get("name") == "emit_result" and json_schema is not None:
                    structured = block.get("input")
                else:
                    calls.append(ToolCall(block.get("name", ""), block.get("input") or {}, block.get("id", "")))
        if data.get("stop_reason") == "refusal":
            raise ModelError("anthropic: model refused the request", ErrorClass.REFUSAL)
        usage = data.get("usage", {})
        return ModelResponse(
            text="".join(text_parts),
            provider=self.name,
            model=data.get("model", model),
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            data=structured,
            tool_calls=calls,
            stop_reason=data.get("stop_reason"),
        )
