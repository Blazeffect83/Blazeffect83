"""Model providers (mock HTTP), routing, budgets and usage accounting."""
import json

import httpx
import pytest

from app.models.anthropic_provider import AnthropicProvider
from app.models.base import BudgetExceeded, ErrorClass, Message, ModelError, extract_json
from app.models.mock_provider import MockProvider
from app.models.openai_provider import LocalProvider, OpenAIProvider

SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_anthropic_structured_output_and_usage():
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        seen["headers"] = req.headers
        return httpx.Response(200, json={
            "model": "m", "stop_reason": "tool_use", "usage": {"input_tokens": 11, "output_tokens": 7},
            "content": [{"type": "tool_use", "name": "emit_result", "id": "t", "input": {"answer": "42"}}]})

    p = AnthropicProvider("sk-ant-test-key-000000", "m", client=client(handler), sleep=lambda s: None)
    r = p.generate([Message("user", "q")], system="sys", json_schema=SCHEMA)
    assert r.data == {"answer": "42"} and r.input_tokens == 11 and r.output_tokens == 7
    assert seen["body"]["tool_choice"] == {"type": "tool", "name": "emit_result"}
    assert seen["headers"]["x-api-key"] == "sk-ant-test-key-000000"
    assert seen["headers"]["anthropic-version"]


def test_anthropic_tool_calls_parsed():
    def handler(req):
        return httpx.Response(200, json={"model": "m", "usage": {}, "content": [
            {"type": "text", "text": "ok"}, {"type": "tool_use", "name": "file_read", "id": "1", "input": {"path": "a"}}]})
    p = AnthropicProvider("k" * 20, "m", client=client(handler))
    r = p.generate([Message("user", "q")])
    assert r.text == "ok" and r.tool_calls[0].name == "file_read" and r.tool_calls[0].arguments == {"path": "a"}


def test_rate_limit_retry_honours_retry_after():
    calls, sleeps = [], []

    def handler(req):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, headers={"retry-after": "7"}, json={"error": {"message": "slow down"}})
        return httpx.Response(200, json={"model": "m", "usage": {}, "content": [{"type": "text", "text": "hi"}]})

    p = AnthropicProvider("k" * 20, "m", client=client(handler), max_retries=3, sleep=sleeps.append)
    assert p.generate([Message("user", "q")]).text == "hi"
    assert sleeps == [7.0, 7.0] and len(calls) == 3


def test_auth_error_not_retried():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    p = OpenAIProvider("sk-test-0000000000000000", "m", client=client(handler), sleep=lambda s: None)
    with pytest.raises(ModelError) as e:
        p.generate([Message("user", "q")])
    assert e.value.error_class == ErrorClass.AUTH and len(calls) == 1


def test_timeout_classified_and_bounded():
    calls = []

    def handler(req):
        calls.append(1)
        raise httpx.ReadTimeout("slow", request=req)

    p = OpenAIProvider("sk-test-0000000000000000", "m", client=client(handler), max_retries=2, sleep=lambda s: None)
    with pytest.raises(ModelError) as e:
        p.generate([Message("user", "q")])
    assert e.value.error_class == ErrorClass.TIMEOUT and len(calls) == 3


def test_openai_and_local_request_shapes():
    bodies = []

    def handler(req):
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json={"model": "m", "usage": {"prompt_tokens": 3, "completion_tokens": 2},
                                         "choices": [{"message": {"content": '{"answer": "x"}'},
                                                      "finish_reason": "stop"}]})

    r = OpenAIProvider("sk-test-0000000000000000", "m", client=client(handler)).generate(
        [Message("user", "q")], json_schema=SCHEMA)
    assert r.data == {"answer": "x"} and "max_completion_tokens" in bodies[0]
    assert bodies[0]["response_format"]["type"] == "json_schema"
    r = LocalProvider("http://127.0.0.1:8080/v1", "small", client=client(handler)).generate(
        [Message("user", "q")], json_schema=SCHEMA)
    assert r.data == {"answer": "x"} and "max_tokens" in bodies[1]
    assert bodies[1]["response_format"]["type"] == "json_object"


def test_missing_model_name_never_defaults():
    with pytest.raises(ModelError) as e:
        AnthropicProvider("k" * 20, "")
    assert "MODEL_NAME" in str(e.value)


def test_extract_json_variants():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! {"a": [1, 2]} hope that helps') == {"a": [1, 2]}
    with pytest.raises(ModelError):
        extract_json("no json here")


def test_router_records_usage_and_cost(services, mock_model):
    services.settings.model_prices = ["mock-model=1:2"]
    mock_model.push("hello")
    r = services.router.generate("general", "hi")
    assert r.text == "hello"
    row = services.db.one("SELECT * FROM model_usage")
    assert row["success"] == 1 and row["provider"] == "mock" and row["input_tokens"] >= 1
    assert row["cost_usd"] == 0  # mock/local providers are free


def test_daily_spend_limit_hard_stop(services, mock_model):
    services.db.insert("model_usage", {"purpose": "planning", "provider": "anthropic", "model": "m",
                                       "input_tokens": 1, "output_tokens": 1, "cost_usd": 5.0, "success": 1,
                                       "created_at": __import__("app.db").db.now_iso()})
    mock_model.push("never returned")
    with pytest.raises(BudgetExceeded):
        services.router.generate("general", "hi")
    assert len(mock_model.calls) == 0  # no request was sent


def test_hourly_request_limit(services, mock_model):
    services.settings.max_requests_per_hour = 2
    mock_model.push("a", "b", "c")
    services.router.generate("general", "1")
    services.router.generate("general", "2")
    with pytest.raises(BudgetExceeded):
        services.router.generate("general", "3")


def test_objective_token_budget(services, mock_model):
    services.db.insert("objectives", {"id": "obj_b", "goal": "g", "status": "RUNNING", "created_at": "t",
                                      "updated_at": "t"})
    mock_model.push("x" * 4000, "y")
    services.router.generate("planning", "z" * 4000, objective_id="obj_b", token_budget=500)
    with pytest.raises(BudgetExceeded):
        services.router.generate("planning", "again", objective_id="obj_b", token_budget=500)


def test_unknown_price_uses_conservative_estimate(services):
    cost, estimate = services.router.cost("unpriced-model", 1_000_000, 0, "anthropic")
    assert estimate and cost > 0


def test_local_only_blocks_paid_providers(services):
    services.settings.local_only = True
    services.settings.model_provider = "anthropic"
    services.settings.anthropic_api_key = "sk-ant-should-not-be-used-000"
    with pytest.raises(ModelError) as e:
        services.router.generate("general", "hi")
    assert "local-only" in str(e.value)


def test_no_silent_fallback_to_paid_provider(services):
    services.settings.model_provider = "local"
    services.settings.local_model_base_url = ""  # local provider unavailable
    services.settings.anthropic_api_key = "sk-ant-available-but-not-authorised"
    with pytest.raises(ModelError):
        services.router.generate("general", "hi")
    assert services.db.scalar("SELECT COUNT(*) FROM model_usage WHERE provider = 'anthropic'") == 0


def test_explicit_fallback_is_used(services):
    failing = MockProvider([ModelError("down", ErrorClass.SERVER)] * 5, sleep=lambda s: None)
    backup = MockProvider(["from fallback"], model="backup", sleep=lambda s: None)
    backup.name = "local"
    services.router._providers.update({"mock": failing, "local": backup})
    services.settings.fallback_provider = "local"
    assert services.router.generate("general", "hi").text == "from fallback"


def test_light_roles_use_light_model(services, mock_model):
    services.settings.model_name_light = "mock-small"
    mock_model.push("a", "b")
    services.router.generate("extraction", "x")
    services.router.generate("planning", "y")
    assert [c["model"] for c in mock_model.calls] == ["mock-small", "mock-model"]
