"""Provider-neutral model interface, error classification and retry logic."""
from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)


class ErrorClass:
    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    SERVER = "server"
    BAD_REQUEST = "bad_request"
    NETWORK = "network"
    PARSE = "parse"
    BUDGET = "budget"
    UNAVAILABLE = "unavailable"
    REFUSAL = "refusal"

    TRANSIENT = {RATE_LIMIT, TIMEOUT, SERVER, NETWORK}


class ModelError(Exception):
    def __init__(self, message: str, error_class: str, retry_after: float | None = None,
                 status: int | None = None):
        super().__init__(message)
        self.error_class = error_class
        self.retry_after = retry_after
        self.status = status

    @property
    def transient(self) -> bool:
        return self.error_class in ErrorClass.TRANSIENT


class BudgetExceeded(ModelError):
    def __init__(self, message: str):
        super().__init__(message, ErrorClass.BUDGET)


@dataclass
class Message:
    role: str  # "user" | "assistant"
    content: str


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict


@dataclass
class ToolCall:
    name: str
    arguments: dict
    id: str = ""


@dataclass
class ModelResponse:
    text: str
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    data: Any = None  # parsed structured output
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str | None = None
    latency_ms: int = 0


def classify_http(status: int) -> str:
    if status in (401, 403):
        return ErrorClass.AUTH
    if status == 429:
        return ErrorClass.RATE_LIMIT
    if status in (408, 504):
        return ErrorClass.TIMEOUT
    if status >= 500 or status == 529:
        return ErrorClass.SERVER
    return ErrorClass.BAD_REQUEST


def parse_retry_after(headers: httpx.Headers) -> float | None:
    v = headers.get("retry-after")
    if not v:
        return None
    try:
        return max(0.0, float(v))
    except ValueError:
        return None


_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```")


def extract_json(text: str) -> Any:
    """Parse JSON from model text, tolerating code fences and surrounding prose."""
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    m = _FENCE.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except ValueError:
            pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        start, end = text.find(open_c), text.rfind(close_c)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except ValueError:
                continue
    raise ModelError("model did not return valid JSON", ErrorClass.PARSE)


class ModelProvider:
    """Base class. Subclasses implement ``_generate_once``."""

    name = "base"

    def __init__(self, model: str, timeout: float = 120.0, max_retries: int = 3,
                 client: httpx.Client | None = None, sleep=time.sleep):
        if not model:
            raise ModelError(
                f"{self.name}: no model name configured (set MODEL_NAME; check the provider's "
                "current model list — AEGIS never hardcodes model identifiers)",
                ErrorClass.UNAVAILABLE,
            )
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self._client = client
        self._sleep = sleep

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=httpx.Timeout(self.timeout, connect=15.0))
        return self._client

    def generate(
        self,
        messages: list[Message],
        *,
        system: str = "",
        max_tokens: int = 2048,
        json_schema: dict | None = None,
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.2,
        model: str | None = None,
    ) -> ModelResponse:
        attempt = 0
        while True:
            start = time.monotonic()
            try:
                resp = self._generate_once(
                    messages, system=system, max_tokens=max_tokens, json_schema=json_schema,
                    tools=tools, temperature=temperature, model=model or self.model,
                )
                resp.latency_ms = int((time.monotonic() - start) * 1000)
                if json_schema is not None and resp.data is None:
                    resp.data = extract_json(resp.text)
                return resp
            except httpx.TimeoutException as exc:
                err = ModelError(f"{self.name}: timeout: {exc}", ErrorClass.TIMEOUT)
            except httpx.TransportError as exc:
                err = ModelError(f"{self.name}: network error: {exc}", ErrorClass.NETWORK)
            except ModelError as exc:
                err = exc
            if not err.transient or attempt >= self.max_retries:
                raise err
            delay = err.retry_after if err.retry_after is not None else min(30.0, 2 ** attempt + random.random())
            log.warning("%s transient error (%s); retry %d in %.1fs", self.name, err.error_class, attempt + 1, delay)
            self._sleep(delay)
            attempt += 1

    def _generate_once(self, messages, *, system, max_tokens, json_schema, tools, temperature, model) -> ModelResponse:
        raise NotImplementedError

    def _raise_for_status(self, resp: httpx.Response) -> None:
        if resp.status_code < 400:
            return
        try:
            body = resp.json()
            detail = body.get("error", body)
            if isinstance(detail, dict):
                detail = detail.get("message") or json.dumps(detail)[:300]
        except ValueError:
            detail = resp.text[:300]
        raise ModelError(
            f"{self.name}: HTTP {resp.status_code}: {detail}",
            classify_http(resp.status_code),
            retry_after=parse_retry_after(resp.headers),
            status=resp.status_code,
        )
