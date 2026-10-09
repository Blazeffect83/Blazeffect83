"""Dashboard/API authentication, CSRF, authorisation and page rendering."""
import re

import pytest
from fastapi.testclient import TestClient

from app.api.authentication import hash_password, hash_token, verify_password
from app.main import create_app

PASSWORD = "correct horse battery"
TOKEN = "aegis_test_token_value"


@pytest.fixture
def app(services):
    services.settings.admin_password_hash = hash_password(PASSWORD)
    services.settings.api_token_hash = hash_token(TOKEN)
    return create_app(services=services, start_worker=False)


@pytest.fixture
def client(app):
    return TestClient(app, base_url="http://testserver")


def login(client) -> str:
    r = client.post("/login", data={"username": "admin", "password": PASSWORD}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    page = client.get("/tasks")
    return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)


def test_password_hashing():
    h = hash_password(PASSWORD)
    assert h.startswith("scrypt$") and PASSWORD not in h
    assert verify_password(PASSWORD, h) and not verify_password("wrong password!", h)
    with pytest.raises(ValueError):
        hash_password("short")


API_GETS = ["/api/status", "/api/health", "/api/objectives", "/api/approvals", "/api/topics", "/api/search?q=x",
            "/api/contradictions", "/api/reports/daily", "/api/export/knowledge", "/api/metrics", "/api/usage",
            "/api/budgets", "/api/logs", "/api/audit", "/api/tools", "/api/config", "/api/notifications",
            "/api/backups"]
API_POSTS = ["/api/objectives", "/api/objectives/x/cancel", "/api/approvals/x/approve", "/api/topics",
             "/api/research/pause", "/api/budgets", "/api/backups", "/api/shutdown", "/api/notifications/read"]


@pytest.mark.parametrize("path", API_GETS)
def test_api_get_requires_auth(client, path):
    assert client.get(path).status_code == 401


@pytest.mark.parametrize("path", API_POSTS)
def test_api_post_requires_auth(client, path):
    assert client.post(path, json={}).status_code in (401, 422)
    assert client.post(path, json={"goal": "hello world", "name": "t"}).status_code == 401


def test_dashboard_pages_redirect_to_login(client):
    for p in ("/", "/tasks", "/approvals", "/research", "/system", "/reports"):
        r = client.get(p, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"


def test_dashboard_posts_require_auth(client):
    for p in ("/tasks", "/approvals/x/approve", "/research/pause", "/system/shutdown", "/system/backup"):
        assert client.post(p, data={"goal": "x"}).status_code == 401


def test_health_is_public_but_minimal(client):
    r = client.get("/health")
    assert set(r.json()) == {"ok"}


def test_bearer_token(client, services):
    h = {"authorization": f"Bearer {TOKEN}"}
    assert client.get("/api/status", headers=h).status_code == 200
    r = client.post("/api/objectives", headers=h, json={"goal": "Research local LLM runtimes", "kind": "research"})
    assert r.status_code == 201
    assert client.get("/api/status", headers={"authorization": "Bearer wrong"}).status_code == 401


def test_login_rate_limit(client):
    for _ in range(5):
        client.post("/login", data={"username": "admin", "password": "nope-nope-nope"})
    r = client.post("/login", data={"username": "admin", "password": PASSWORD}, follow_redirects=False)
    assert "too%20many" in r.headers["location"].lower() or "too many" in r.headers["location"].lower()


def test_session_cookie_flags(client):
    r = client.post("/login", data={"username": "admin", "password": PASSWORD}, follow_redirects=False)
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie


def test_csrf_required_for_cookie_posts(client, services):
    csrf = login(client)
    assert client.post("/tasks", data={"goal": "Write a hello world script"}).status_code == 403
    assert client.post("/tasks", data={"goal": "Write a hello world script", "csrf": "forged"}).status_code == 403
    assert client.post("/api/research/pause").status_code == 403
    r = client.post("/tasks", data={"goal": "Write a hello world script", "kind": "coding", "csrf": csrf},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/tasks/obj_")
    assert client.post("/api/research/pause", headers={"x-csrf-token": csrf}).json()["paused"] is True


def test_all_pages_render_after_login(client, services, web, mock_model):
    from tests.conftest import load_fixture
    web.add("https://www.raspberrypi.com/docs/storage", load_fixture("official_docs.html"))
    web.add("https://blog.example.org/pi5-build", load_fixture("blog_corroborating.html"))
    services.research.ingest_url("https://www.raspberrypi.com/docs/storage", query="pi")
    services.research.ingest_url("https://blog.example.org/pi5-build", query="pi")
    from app.scheduler.schedules import create_topic
    create_topic(services.db, name="pi5", seed_urls=["https://www.raspberrypi.com/docs/storage"])
    mock_model.push({"steps": [{"title": "net", "tool": "pip_install", "args": {"packages": ["six"]}}]})
    from app.agent.objectives import ObjectiveManager
    oid = ObjectiveManager(services).create("Install six")
    services.controller.run(oid)
    csrf = login(client)
    for p in ("/", "/tasks", f"/tasks/{oid}", "/approvals", "/research", "/research?kind=claims&q=power",
              "/research?q=nvme", "/documents/1", "/claims/1", "/reports", "/system", "/api/docs"):
        r = client.get(p)
        assert r.status_code == 200, p
    page = client.get("/approvals").text
    assert "pip install --target .deps six" in page and "Approve once" in page
    aid = services.approvals.list("pending")[0]["id"]
    r = client.post(f"/approvals/{aid}/reject", data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303 and services.approvals.get(aid)["status"] == "rejected"
    assert services.state.objective_status(oid) == "READY"
    audit = services.db.one("SELECT * FROM audit_events WHERE event = 'approval.rejected'")
    assert audit["actor"] == "user:admin"


def test_security_headers(client):
    r = client.get("/login")
    for h in ("content-security-policy", "x-frame-options", "x-content-type-options", "referrer-policy"):
        assert h in r.headers
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


def test_body_size_limit(client):
    r = client.post("/api/objectives", content=b"x" * (3 * 1024 * 1024),
                    headers={"content-type": "application/json", "authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 413


def test_input_validation(client):
    h = {"authorization": f"Bearer {TOKEN}"}
    assert client.post("/api/objectives", headers=h, json={"goal": "x"}).status_code == 422
    assert client.post("/api/objectives", headers=h, json={"goal": "valid goal", "kind": "root"}).status_code == 422
    assert client.post("/api/objectives", headers=h, json={"goal": "valid goal", "allowed_tools": ["sudo"]}
                       ).status_code == 400
    assert client.post("/api/topics", headers=h, json={"name": "t", "interval_minutes": 1}).status_code == 422


def test_logout_invalidates_session(client):
    csrf = login(client)
    assert client.get("/api/status").status_code == 200
    client.post("/logout", data={"csrf": csrf})
    assert client.get("/api/status").status_code == 401


def test_expired_session_rejected(client, services):
    login(client)
    services.db.execute("UPDATE sessions SET expires_at = '2000-01-01T00:00:00+00:00'")
    assert client.get("/api/status").status_code == 401


def test_secrets_never_exposed_by_api(client, services):
    services.settings.anthropic_api_key = "sk-ant-api03-SUPERSECRETKEYVALUE0001"
    h = {"authorization": f"Bearer {TOKEN}"}
    body = client.get("/api/config", headers=h).text
    assert "SUPERSECRET" not in body and "scrypt$" not in body


def test_budget_update_via_api(client, services):
    h = {"authorization": f"Bearer {TOKEN}"}
    r = client.post("/api/budgets", headers=h, json={"daily_spend_limit_usd": 0.25, "max_requests_per_hour": 10})
    assert r.json()["daily_spend_limit_usd"] == 0.25 and services.settings.max_requests_per_hour == 10
    assert services.db.kv_get("budget_overrides")["daily_spend_limit_usd"] == 0.25
    assert client.post("/api/budgets", headers=h, json={"daily_spend_limit_usd": -1}).status_code == 422
