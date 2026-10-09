"""Shared fixtures. No test makes a real network call or a paid API request:
model providers are scripted mocks and web content comes from an in-process
httpx.MockTransport fake web."""
from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.models.mock_provider import MockProvider  # noqa: E402
from app.services import build_services  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


class FakeWeb:
    """Maps URL → (status, headers, body). Records requested URLs."""

    def __init__(self):
        self.pages: dict[str, tuple[int, dict, bytes]] = {}
        self.routes: list[tuple[str, object]] = []  # (url prefix, fn(request) -> httpx.Response)
        self.requests: list[str] = []

    def add(self, url: str, body: str | bytes, content_type: str = "text/html; charset=utf-8", status: int = 200,
            headers: dict | None = None):
        b = body.encode() if isinstance(body, str) else body
        self.pages[url] = (status, {"content-type": content_type, **(headers or {})}, b)

    def route(self, prefix: str, fn) -> None:
        self.routes.append((prefix, fn))

    def redirect(self, url: str, location: str, status: int = 302):
        self.pages[url] = (status, {"location": location}, b"")

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        for prefix, fn in self.routes:
            if url.startswith(prefix):
                return fn(request)
        if url in self.pages:
            status, headers, body = self.pages[url]
            return httpx.Response(status, headers=headers, content=body)
        if url.endswith("/robots.txt"):
            return httpx.Response(404, content=b"")
        return httpx.Response(404, headers={"content-type": "text/html"}, content=b"not found")

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def make_settings(tmp_path: Path, **kw) -> Settings:
    base = dict(
        data_dir=tmp_path / "data", model_provider="mock", model_name="mock-model",
        sandbox_python=sys.executable, research_per_domain_delay_seconds=0.0, worker_poll_seconds=0.05,
        daily_spend_limit_usd=5.0, max_requests_per_hour=10_000, metrics_interval_seconds=3600,
        min_free_disk_mb=1, max_cpu_temp_c=200, max_memory_percent=101, max_load_per_cpu=1000,
        approval_ttl_minutes=60, max_step_attempts=2, max_replans=3, sandbox_timeout_seconds=60,
    )
    base.update(kw)
    return Settings(**base)


@pytest.fixture
def web() -> FakeWeb:
    return FakeWeb()


@pytest.fixture
def mock_model() -> MockProvider:
    return MockProvider(sleep=lambda s: None)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def services(settings, web, mock_model):
    s = build_services(settings, providers={"mock": mock_model}, http_transport=web.transport,
                       fetch_sleep=lambda s: None)
    yield s
    s.db.close()


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text()
