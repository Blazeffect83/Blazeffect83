"""Crash recovery, worker scheduling, resource limits, backups, retention."""
import gzip
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.agent import state_machine as sm
from app.agent.objectives import ObjectiveManager
from app.maintenance import backups, retention
from app.models.mock_provider import MockProvider
from app.scheduler import schedules
from app.scheduler.worker import Worker, reconcile
from app.services import build_services

from .conftest import load_fixture, make_settings

ROOT = Path(__file__).resolve().parents[1]


def _kill_mid_action(tmp_path, tool):
    proc = subprocess.Popen([sys.executable, str(ROOT / "tests" / "crash_helper.py"), str(tmp_path), tool],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=str(ROOT))
    oid = proc.stdout.readline().strip()
    settings = make_settings(tmp_path)
    import sqlite3
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            c = sqlite3.connect(str(settings.db_path))
            n = c.execute("SELECT COUNT(*) FROM tool_runs WHERE status = 'started'").fetchone()[0]
            c.close()
            if n:
                break
        except sqlite3.Error:
            pass
        time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("child never started the tool: " + proc.stderr.read())
    time.sleep(0.5)
    os.kill(proc.pid, signal.SIGKILL)  # abrupt termination, no cleanup
    proc.wait(10)
    return oid, settings


def test_abrupt_kill_during_non_idempotent_action_pauses(tmp_path):
    oid, settings = _kill_mid_action(tmp_path, "python_run")
    s = build_services(settings, providers={"mock": MockProvider()})
    assert s.db.integrity_check()[0]
    out = reconcile(s)
    assert out["paused"] == 1
    o = ObjectiveManager(s).get(oid)
    assert o["status"] == sm.PAUSED and "interrupted" in o["status_reason"]
    assert o["tasks"][0]["status"] == sm.PAUSED
    run = s.db.one("SELECT * FROM tool_runs")
    assert run["status"] == "failed" and run["error_class"] == "interrupted"
    # The worker does not resume it automatically.
    assert s.controller.run(oid) == sm.PAUSED
    assert s.db.scalar("SELECT COUNT(*) FROM tool_runs") == 1
    s.db.close()


def test_abrupt_kill_during_idempotent_action_resumes(tmp_path):
    oid, settings = _kill_mid_action(tmp_path, "run_tests")
    ws = settings.workspace / oid
    (ws / "test_slow.py").write_text("def test_fast():\n    assert True\n")
    s = build_services(settings, providers={"mock": MockProvider()})
    out = reconcile(s)
    assert out["resumed"] >= 1
    assert s.state.objective_status(oid) == sm.READY
    assert s.controller.run(oid) == sm.COMPLETED
    runs = s.db.query("SELECT status, idempotency_key FROM tool_runs ORDER BY id")
    assert [r["status"] for r in runs] == ["failed", "succeeded"]
    assert runs[0]["idempotency_key"] != runs[1]["idempotency_key"]
    s.db.close()


def test_reconcile_requeues_interrupted_planning(services):
    oid = ObjectiveManager(services).create("Something being planned")
    services.state.transition_objective(oid, sm.PLANNING)
    reconcile(services)
    assert services.state.objective_status(oid) == sm.QUEUED


def test_completed_step_not_repeated_after_crash(services, mock_model):
    """Crash after the tool finished but before the task state update: the
    recorded result is reused (idempotency key), not re-executed."""
    mock_model.push({"steps": [{"title": "w", "tool": "file_write", "args": {"path": "a", "content": "1"}}]})
    m = ObjectiveManager(services)
    oid = m.create("Write a file once")
    services.controller.run(oid, max_iterations=1)  # planning only
    task = services.db.one("SELECT * FROM tasks WHERE objective_id = ?", (oid,))
    from app.agent.executor import idempotency_key
    services.db.insert("tool_runs", {"idempotency_key": idempotency_key(oid, task["id"], 1), "objective_id": oid,
                                     "task_id": task["id"], "tool": "file_write", "args": "{}", "status": "succeeded",
                                     "output": '{"path": "a", "bytes": 1}', "started_at": "t"})
    assert services.controller.run(oid) == sm.COMPLETED
    assert services.db.scalar("SELECT COUNT(*) FROM tool_runs") == 1
    assert not (services.settings.workspace / oid / "a").exists()  # tool really was not run twice


def test_worker_runs_objectives_and_research_without_dashboard(services, mock_model, web):
    web.add("https://www.raspberrypi.com/docs/storage", load_fixture("official_docs.html"))
    schedules.create_topic(services.db, name="pi", seed_urls=["https://www.raspberrypi.com/docs/storage"],
                           interval_minutes=60)
    mock_model.push({"steps": [{"title": "w", "tool": "file_write", "args": {"path": "a", "content": "1"}}]})
    oid = ObjectiveManager(services).create("Write a file from the worker")
    w = Worker(services)
    w.tick(wait=True)
    w.tick(wait=True)
    assert services.state.objective_status(oid) == sm.COMPLETED
    assert services.db.scalar("SELECT COUNT(*) FROM documents") == 1
    topic = services.db.one("SELECT * FROM research_topics")
    assert topic["last_run_at"] and topic["next_run_at"] > topic["last_run_at"]
    assert services.db.kv_get("worker_heartbeat")
    # Not due again yet → no new fetches.
    n = len(web.requests)
    w.tick(wait=True)
    assert len(web.requests) == n
    w.stop()


def test_worker_background_thread_lifecycle(services, mock_model):
    mock_model.push({"steps": [{"title": "w", "tool": "file_write", "args": {"path": "a", "content": "1"}}]})
    oid = ObjectiveManager(services).create("Write a file in the background")
    w = Worker(services)
    w.start()
    deadline = time.time() + 20
    while time.time() < deadline and services.state.objective_status(oid) != sm.COMPLETED:
        time.sleep(0.05)
    w.stop()
    assert services.state.objective_status(oid) == sm.COMPLETED
    assert w.status()["stopping"]


def test_research_pause_and_resource_pressure(services, web, mock_model):
    web.add("https://www.raspberrypi.com/docs/storage", load_fixture("official_docs.html"))
    schedules.create_topic(services.db, name="pi", seed_urls=["https://www.raspberrypi.com/docs/storage"])
    services.db.kv_set("research_paused", True)
    w = Worker(services)
    w.tick(wait=True)
    assert services.db.scalar("SELECT COUNT(*) FROM documents") == 0
    services.db.kv_set("research_paused", False)
    services.settings.max_memory_percent = 0  # simulate memory pressure
    services.settings.metrics_interval_seconds = 0
    mock_model.push({"steps": [{"title": "w", "tool": "file_write", "args": {"path": "a", "content": "1"}}]})
    oid = ObjectiveManager(services).create("Should wait for resources")
    w.tick(wait=True)
    assert not w.resource_ok and services.state.objective_status(oid) == sm.QUEUED
    assert services.db.scalar("SELECT COUNT(*) FROM documents") == 0
    assert services.db.scalar("SELECT COUNT(*) FROM notifications WHERE kind = 'resource_pressure'") == 1
    services.settings.max_memory_percent = 101
    w.tick(wait=True)
    assert w.resource_ok and services.db.scalar("SELECT COUNT(*) FROM documents") == 1
    w.stop()


def test_failing_topic_backs_off(services, web):
    tid = schedules.create_topic(services.db, name="broken", seed_urls=["https://down.example.com/"],
                                 interval_minutes=60)
    w = Worker(services)
    topic = services.db.one("SELECT * FROM research_topics WHERE id = ?", (tid,))
    schedules.mark_run(services.db, topic, False, "error")
    t2 = services.db.one("SELECT * FROM research_topics WHERE id = ?", (tid,))
    assert t2["consecutive_failures"] == 1
    from app.db import parse_iso
    gap = parse_iso(t2["next_run_at"]) - parse_iso(t2["last_run_at"])
    assert gap.total_seconds() >= 2 * 60 * 60 - 5
    w.stop()


def test_backup_create_verify_restore(services, mock_model, tmp_path):
    mock_model.push({"steps": [{"title": "w", "tool": "file_write", "args": {"path": "a", "content": "1"}}]})
    oid = ObjectiveManager(services).create("Objective that must survive restore")
    res = backups.create_backup(services.db, services.settings.backups)
    assert res["verified"] and Path(res["path"]).stat().st_mode & 0o777 == 0o600
    assert backups.verify_backup(Path(res["path"]))[0]
    ObjectiveManager(services).create("Created after the backup")
    services.db.close()
    saved = backups.restore_backup(Path(res["path"]), services.settings.db_path)
    assert saved.exists()
    s2 = build_services(services.settings, providers={"mock": MockProvider()})
    goals = [o["goal"] for o in s2.db.query("SELECT goal FROM objectives")]
    assert goals == ["Objective that must survive restore"] and s2.audit.verify_chain()[0]
    assert ObjectiveManager(s2).get(oid)
    s2.db.close()


def test_corrupt_backup_detected_and_not_restored(services, tmp_path):
    res = backups.create_backup(services.db, services.settings.backups)
    p = Path(res["path"])
    data = bytearray(p.read_bytes())
    data[len(data) // 2] ^= 0xFF
    p.write_bytes(bytes(data))
    ok, detail = backups.verify_backup(p)
    assert not ok and "checksum" in detail
    with pytest.raises(backups.BackupError):
        backups.restore_backup(p, tmp_path / "target.db")
    bogus = tmp_path / "bogus.db.gz"
    with gzip.open(bogus, "wb") as f:
        f.write(b"not a database at all" * 100)
    assert not backups.verify_backup(bogus)[0]


def test_backup_rotation(services):
    for _ in range(4):
        backups.create_backup(services.db, services.settings.backups, keep=2)
        time.sleep(1.05)
    assert len(list(services.settings.backups.glob("aegis-*.db.gz"))) == 2


def test_retention_keeps_knowledge(services, web):
    web.add("https://www.raspberrypi.com/docs/storage", load_fixture("official_docs.html"))
    services.research.ingest_url("https://www.raspberrypi.com/docs/storage")
    services.db.execute("UPDATE documents SET retrieved_at = '2000-01-01T00:00:00+00:00'")
    services.db.insert("system_metrics", {"cpu_percent": 1, "created_at": "2000-01-01T00:00:00+00:00"})
    out = retention.apply_retention(services.db, 30, 90)
    assert out["system_metrics"] == 1
    retention.cleanup_orphans(services.db)
    retention.vacuum(services.db)
    assert services.db.scalar("SELECT COUNT(*) FROM documents") == 1
    assert services.db.scalar("SELECT COUNT(*) FROM claims") > 0
    assert services.db.integrity_check()[0]


def test_maintenance_tick_creates_backup_and_report(services):
    w = Worker(services)
    w._maintenance()
    assert services.db.scalar("SELECT COUNT(*) FROM backups WHERE verified = 1") == 1
    assert services.db.kv_get("last_daily_report")["generated_at"]
    assert "AEGIS research report" in services.db.kv_get("last_daily_report_md")
    w.stop()


def test_knowledge_export_and_delete(services, web):
    web.add("https://www.raspberrypi.com/docs/storage", load_fixture("official_docs.html"))
    o = services.research.ingest_url("https://www.raspberrypi.com/docs/storage")
    data = services.knowledge.export()
    assert len(data["documents"]) == 1 and data["claims"]
    services.knowledge.delete_document(o.document_id)
    assert services.db.scalar("SELECT COUNT(*) FROM documents") == 0
    assert services.db.scalar("SELECT COUNT(*) FROM claims WHERE origin = 'source'") == 0
