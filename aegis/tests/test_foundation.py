"""Configuration, database, migrations, redaction, audit log, state machine."""
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from app.agent import state_machine as sm
from app.config import load_settings, parse_env_file
from app.db import Database
from app.observability.audit import AuditLog
from app.security.redact import REDACTED, Redactor


def test_env_file_parsing_and_precedence(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# comment\nMODEL_PROVIDER=openai\nexport DAILY_SPEND_LIMIT_USD='1.5'\n"
                   "RESEARCH_ALLOWED_DOMAINS=python.org, docs.python.org\nBAD LINE\n")
    assert parse_env_file(env)["DAILY_SPEND_LIMIT_USD"] == "1.5"
    monkeypatch.setenv("AEGIS_MODEL_PROVIDER", "local")
    s = load_settings(env, data_dir=tmp_path)
    assert s.model_provider == "local"  # real environment beats the file
    assert s.daily_spend_limit_usd == 1.5
    assert s.research_allowed_domains == ["python.org", "docs.python.org"]


def test_inline_comments_and_quotes(tmp_path):
    env = tmp_path / ".env"
    env.write_text("LOCAL_ONLY=true   # paid off\nADMIN_PASSWORD_HASH='scrypt$1$a#b'  # hash\nMODEL_NAME=\nX=a#b\n")
    v = parse_env_file(env)
    assert v["LOCAL_ONLY"] == "true" and v["ADMIN_PASSWORD_HASH"] == "scrypt$1$a#b" and v["MODEL_NAME"] == ""
    assert v["X"] == "a#b"


def test_env_example_is_valid(tmp_path):
    from app.config import APP_ROOT
    s = load_settings(APP_ROOT / ".env.example", data_dir=tmp_path)
    assert s.model_provider == "none" and s.sandbox_backend == "auto" and s.cookie_secure is False
    assert s.bind_host == "127.0.0.1"


def test_secrets_redacted_in_settings_dump(tmp_path):
    s = load_settings(tmp_path / "none", data_dir=tmp_path, anthropic_api_key="sk-ant-secret-value-123456")
    d = s.redacted()
    assert d["anthropic_api_key"] == "***set***"
    assert "sk-ant-secret-value-123456" not in str(d)


def test_migrations_idempotent_and_schema(settings):
    db = Database(settings.db_path, settings.migrations_dir)
    settings.ensure_dirs()
    assert db.migrate() == ["0001_initial"]
    assert db.migrate() == []
    tables = {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("objectives", "tasks", "task_events", "research_sources", "documents", "claims", "claim_evidence",
              "topics", "document_topics", "relationships", "experiments", "tool_runs", "approvals", "model_usage",
              "system_metrics", "audit_events"):
        assert t in tables
    assert db.scalar("PRAGMA foreign_keys") == 1
    assert db.integrity_check()[0]


def test_migrations_ship_inside_package_and_missing_dir_fails(tmp_path):
    import app
    from app.config import Settings
    pkg = Path(app.__file__).resolve().parent
    assert Settings().migrations_dir == pkg / "migrations"
    assert (pkg / "migrations" / "0001_initial.sql").is_file()
    with pytest.raises(RuntimeError, match="no migration scripts"):
        Database(tmp_path / "x.db", tmp_path / "missing").migrate()


def test_foreign_keys_enforced(services):
    with pytest.raises(sqlite3.IntegrityError):
        services.db.insert("tasks", {"id": "t1", "objective_id": "missing", "seq": 0, "plan_version": 1,
                                     "title": "x", "tool": "file_read", "created_at": "x", "updated_at": "x"})


def test_database_lock_retry(settings):
    settings.ensure_dirs()
    db = Database(settings.db_path, settings.migrations_dir)
    db.migrate()
    blocker = sqlite3.connect(str(settings.db_path), timeout=1, check_same_thread=False)
    blocker.execute("BEGIN IMMEDIATE")
    blocker.execute("INSERT INTO kv VALUES ('a', '1', 'x')")
    released = []

    def release():
        time.sleep(0.5)
        blocker.commit()
        released.append(True)

    threading.Thread(target=release).start()
    db.kv_set("b", 2)  # must wait for the lock instead of failing
    assert released and db.kv_get("b") == 2
    blocker.close()


@pytest.mark.parametrize("text", [
    "key sk-ant-api03-abcdefghijklmnopqrstuvwxyz",
    "openai sk-proj-ABCDEFGHIJKLMNOPQRSTUV123",
    "Authorization: Bearer abc.def.ghi-jkl_mnop",
    "ANTHROPIC_API_KEY=supersecretvalue",
    "password: hunter2hunter2",
    "token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123",
])
def test_redaction_patterns(text):
    out = Redactor().text(text)
    assert REDACTED in out
    for secret in ("abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUV123", "abc.def.ghi", "supersecretvalue",
                   "hunter2hunter2", "ghp_ABCDEFGH"):
        assert secret not in out


def test_redaction_known_secret_and_objects():
    r = Redactor(["my-very-specific-secret"])
    assert r.text("x my-very-specific-secret y") == f"x {REDACTED} y"
    obj = r.obj({"api_key": "abc123456", "nested": [{"note": "my-very-specific-secret"}], "n": 3})
    assert obj["api_key"] == REDACTED and REDACTED in obj["nested"][0]["note"] and obj["n"] == 3


def test_audit_append_only_and_chain(services):
    a = AuditLog(services.db)
    a.record("test.one", args={"api_key": "sekrit-value-1"}, result="ok")
    a.record("test.two", result="ok")
    assert a.verify_chain() == (True, None)
    row = services.db.one("SELECT args FROM audit_events WHERE event = 'test.one'")
    assert "sekrit-value-1" not in row["args"]
    with pytest.raises(sqlite3.IntegrityError):
        services.db.execute("UPDATE audit_events SET result = 'tampered'")
    with pytest.raises(sqlite3.IntegrityError):
        services.db.execute("DELETE FROM audit_events")
    # Out-of-band tampering (triggers dropped) is detected by the hash chain.
    services.db.execute("DROP TRIGGER audit_no_update")
    services.db.execute("UPDATE audit_events SET result = 'tampered' WHERE event = 'test.one'")
    ok, bad = a.verify_chain()
    assert not ok and bad is not None


def _objective(services, oid="obj_1"):
    services.db.insert("objectives", {"id": oid, "goal": "g", "status": "QUEUED", "created_at": "t",
                                      "updated_at": "t"})
    return oid


def test_state_transitions_persisted(services):
    oid = _objective(services)
    st = services.state
    assert st.transition_objective(oid, sm.PLANNING, "plan")
    assert st.transition_objective(oid, sm.READY, "ready")
    with pytest.raises(sm.InvalidTransition):
        st.transition_objective(oid, sm.COMPLETED, "skip verification")
    events = st.events(oid)
    assert [e["to_status"] for e in events] == [sm.PLANNING, sm.READY]


def test_compare_and_set_protects_user_actions(services):
    oid = _objective(services)
    st = services.state
    st.transition_objective(oid, sm.PLANNING)
    st.transition_objective(oid, sm.PAUSED, "user paused")
    # The worker expected PLANNING; its transition must not override the pause.
    assert st.transition_objective(oid, sm.READY, "worker", expect=sm.PLANNING) is False
    assert st.objective_status(oid) == sm.PAUSED


def test_terminal_states_are_final(services):
    oid = _objective(services)
    st = services.state
    st.transition_objective(oid, sm.CANCELLED)
    for to in (sm.READY, sm.RUNNING, sm.QUEUED):
        with pytest.raises(sm.InvalidTransition):
            st.transition_objective(oid, to)
