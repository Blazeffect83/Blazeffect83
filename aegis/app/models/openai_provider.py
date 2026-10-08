"""OpenAI Chat Completions provider, also used for OpenAI-compatible local servers
(llama.cpp ``llama-server``, Ollama ``/v1``, vLLM, LM Studio)."""
from __future__ import annotations

import json

from .base import ErrorClass, ModelError, ModelProvider, ModelResponse, ToolCall


class OpenAICompatibleProvider(ModelProvider):
    name = "openai"
    # Official OpenAI models use max_completion_tokens and some reject a
    # non-default temperature; local servers expect the classic parameters.
    token_param = "max_completion_tokens"
    send_temperature = False

    def __init__(self, api_key: str, model: str, base_url: str = "https://api.openai.com/v1",
                 strict_schema: bool = True, require_key: bool = True, **kw):
        if require_key and not api_key:
            raise ModelError(f"{self.name}: API key is not set", ErrorClass.AUTH)
        super().__init__(model, **kw)
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.strict_schema = strict_schema

    def _generate_once(self, messages, *, system, max_tokens, json_schema, tools, temperature, model):
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs += [{"role": m.role, "content": m.content} for m in messages]
        body: dict = {"model": model, "messages": msgs, self.token_param: max_tokens}
        if self.send_temperature:
            body["temperature"] = temperature
        if json_schema is not None:
            if self.strict_schema:
                body["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": "result", "schema": json_schema},
                }
            else:
                body["response_format"] = {"type": "json_object"}
                msgs[0:0] = [{"role": "system", "content": "Respond with JSON matching this schema: "
                              + json.dumps(json_schema)}]
        if tools:
            body["tools"] = [
                {"type": "function", "function": {"name": t.name, "description": t.description,
                                                  "parameters": t.input_schema}}
                for t in tools
            ]
        headers = {"content-type": "application/json"}
        if self._api_key:
            headers["authorization"] = f"Bearer {self._api_key}"
        resp = self.client.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
        self._raise_for_status(resp)
        data = resp.json()
        try:
            choice = data["choices"][0]
            msg = choice["message"]
        except (KeyError, IndexError) as exc:
            raise ModelError(f"{self.name}: malformed response", ErrorClass.PARSE) from exc
        if msg.get("refusal"):
            raise ModelError(f"{self.name}: model refused: {msg['refusal'][:200]}", ErrorClass.REFUSAL)
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            calls.append(ToolCall(fn.get("name", ""), args, tc.get("id", "")))
        usage = data.get("usage") or {}
        return ModelResponse(
            text=msg.get("content") or "",
            provider=self.name,
            model=data.get("model", model),
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
            tool_calls=calls,
            stop_reason=choice.get("finish_reason"),
        )


class OpenAIProvider(OpenAICompatibleProvider):
    name = "openai"


class LocalProvider(OpenAICompatibleProvider):
    """Local OpenAI-compatible endpoint. No key required; json_object mode for
    broad server compatibility."""

    name = "local"
    token_param = "max_tokens"
    send_temperature = True

    def __init__(self, base_url: str, model: str, api_key: str = "", **kw):
        if not base_url:
            raise ModelError("local: LOCAL_MODEL_BASE_URL is not set", ErrorClass.UNAVAILABLE)
        super().__init__(api_key, model, base_url=base_url, strict_schema=False, require_key=False, **kw)
