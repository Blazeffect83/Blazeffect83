"""End-to-end agent workflows with deterministic scripted models (no paid calls)."""
import json
from datetime import timedelta

import pytest

from app.agent import state_machine as sm
from app.agent.objectives import ObjectiveManager
from app.db import now_iso
from app.models.base import ErrorClass, ModelError
from app.services import build_services

from .conftest import load_fixture, make_settings


BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
TESTS = "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n\ndef test_neg():\n    assert add(-1, 1) == 0\n"


def plan(*steps, final=None, criteria=None, **extra):
    return {"steps": list(steps), "final_checks": list(final or []), "success_criteria": criteria or [], **extra}


def step(tool, title=None, checks=None, **args):
    return {"title": title or tool, "tool": tool, "args": args, "checks": checks or [{"type": "ok"}]}


def run_to_rest(services, oid, limit=10):
    for _ in range(limit):
        st = services.controller.run(oid)
        if st not in ("QUEUED", "READY", "RETRYING"):
            return st
    return services.state.objective_status(oid)


def test_coding_objective_debugs_and_verifies(services, mock_model):
    """Submit coding objective → generate code → tests fail → diagnose → fix → tests pass → verified."""
    seen = {}

    def replan_fn(messages, system, schema):
        seen["replan_prompt"] = messages[0].content
        return plan(step("file_write", "Fix add()", path="calc.py", content=FIXED),
                    step("run_tests", "Run tests", checks=[{"type": "tests_pass"}]),
                    final=[step("run_tests", "Verify all tests", checks=[{"type": "tests_pass"}])])

    mock_model.push(
        plan(step("file_write", "Write calc.py", path="calc.py", content=BUGGY),
             step("file_write", "Write tests", path="test_calc.py", content=TESTS),
             step("run_tests", "Run tests", checks=[{"type": "tests_pass"}])),
        replan_fn,
        {"criteria_met": True, "explanation": "run_tests reported 2 passed, 0 failed"},
    )
    m = ObjectiveManager(services)
    oid = m.create("Write a Python add(a, b) function with pytest tests in calc.py", kind="coding",
                   success_criteria=["All tests pass"])
    assert run_to_rest(services, oid) == sm.COMPLETED
    o = m.get(oid)
    assert o["replans"] == 1 and o["plan_version"] == 2
    assert "assert" in seen["replan_prompt"] and "tests_failed" in seen["replan_prompt"]
    assert "<untrusted_tool_output>" in seen["replan_prompt"]
    assert "Verifier" in o["completion_summary"]
    failed = [t for t in o["tasks"] if t["status"] == sm.FAILED]
    assert failed and failed[0]["tool"] == "run_tests" and failed[0]["result"]["error_class"] == "tests_failed"
    final = [t for t in o["tasks"] if t["title"].startswith("[verify]")][0]
    assert final["status"] == sm.COMPLETED and final["result"]["output"]["summary"]["counts"] == {"passed": 2}
    assert (services.settings.workspace / oid / "calc.py").read_text() == FIXED
    # Learning: failures and the fix are recorded and retrievable for future tasks.
    exps = services.db.query("SELECT * FROM experiments WHERE objective_id = ?", (oid,))
    assert {"success", "failure"} <= {e["outcome"] for e in exps}
    assert any("resolved after replanning" in e["lesson"] for e in exps)
    statuses = [e["to_status"] for e in o["events"] if e["task_id"] is None]
    assert statuses[:3] == [sm.QUEUED, sm.PLANNING, sm.READY] and statuses[-2:] == [sm.VERIFYING, sm.COMPLETED]
    assert services.audit.verify_chain()[0]


def test_state_persists_across_restart(services, mock_model, web, settings):
    mock_model.push(plan(step("file_write", path="notes.txt", content="hello")))
    oid = ObjectiveManager(services).create("Write hello to notes.txt in the workspace")
    assert run_to_rest(services, oid) == sm.COMPLETED
    web.add("https://www.raspberrypi.com/docs/storage", load_fixture("official_docs.html"))
    services.research.ingest_url("https://www.raspberrypi.com/docs/storage")
    services.db.close()
    s2 = build_services(settings, providers={"mock": mock_model}, http_transport=web.transport)
    o = ObjectiveManager(s2).get(oid)
    assert o["status"] == sm.COMPLETED and len(o["tasks"]) == 1 and o["events"]
    assert s2.db.scalar("SELECT COUNT(*) FROM documents") == 1 and s2.db.scalar("SELECT COUNT(*) FROM claims") > 0
    assert s2.audit.verify_chain()[0]
    s2.db.close()


def test_research_objective_without_model(tmp_path, web):
    """Deterministic research plan works with MODEL_PROVIDER=none."""
    s = build_services(make_settings(tmp_path, model_provider="none"), http_transport=web.transport)
    web.add("https://www.raspberrypi.com/docs/storage", load_fixture("official_docs.html"))
    web.add("https://blog.example.org/pi5-build", load_fixture("blog_corroborating.html"))
    oid = ObjectiveManager(s).create(
        "Research Raspberry Pi 5 NVMe storage using https://www.raspberrypi.com/docs/storage and "
        "https://blog.example.org/pi5-build", kind="research")
    assert run_to_rest(s, oid) == sm.COMPLETED
    assert s.db.scalar("SELECT COUNT(*) FROM documents") == 2
    assert s.db.scalar("SELECT COUNT(*) FROM model_usage") == 0
    s.db.close()


def test_general_objective_without_model_fails_clearly(tmp_path):
    s = build_services(make_settings(tmp_path, model_provider="none"))
    oid = ObjectiveManager(s).create("Write a script that prints the date")
    assert run_to_rest(s, oid) == sm.FAILED
    assert "no model provider" in s.state.events(oid)[-1]["reason"]
    s.db.close()


def test_approval_gate_blocks_until_approved(services, mock_model):
    mock_model.push(plan(step("python_run", "Fetch with network", code="print('ran with network')", network=True,
                              checks=[{"type": "output_contains", "value": "ran with network"}])))
    m = ObjectiveManager(services)
    oid = m.create("Run a script that needs network access")
    assert run_to_rest(services, oid) == sm.WAITING_FOR_APPROVAL
    assert services.db.scalar("SELECT COUNT(*) FROM tool_runs") == 0
    # Running again must not execute anything while approval is pending.
    assert services.controller.run(oid) == sm.WAITING_FOR_APPROVAL
    assert services.db.scalar("SELECT COUNT(*) FROM tool_runs") == 0
    appr = services.approvals.list("pending")[0]
    assert appr["tool"] == "python_run" and appr["args"]["network"] is True and "network" in appr["affected"]
    assert services.db.scalar("SELECT COUNT(*) FROM notifications WHERE kind = 'approval_required'") == 1
    row = services.approvals.decide(appr["id"], True, "user:admin")
    m.on_approval_decided(row)
    if services.sandbox.backend != "bwrap":
        pytest.skip("network-enabled execution requires bwrap")
    assert run_to_rest(services, oid) == sm.COMPLETED
    assert services.approvals.get(appr["id"])["status"] == "consumed"
    run = services.db.one("SELECT * FROM tool_runs")
    assert run["status"] == "succeeded"
    audit = services.db.one("SELECT * FROM audit_events WHERE event = 'tool.executed'")
    assert audit["approval_id"] == appr["id"]


def test_rejected_approval_fails_without_execution(services, mock_model):
    mock_model.push(plan(step("pip_install", packages=["requests"])))
    m = ObjectiveManager(services)
    oid = m.create("Install the requests package for my project")
    assert run_to_rest(services, oid) == sm.WAITING_FOR_APPROVAL
    appr = services.approvals.list("pending")[0]
    m.on_approval_decided(services.approvals.decide(appr["id"], False, "user:admin"))
    assert run_to_rest(services, oid) == sm.FAILED
    assert services.db.scalar("SELECT COUNT(*) FROM tool_runs") == 0
    assert "rejected" in m.get(oid)["status_reason"]


def test_expired_approval_requests_new_one(services, mock_model):
    mock_model.push(plan(step("pip_install", packages=["six"])))
    m = ObjectiveManager(services)
    oid = m.create("Install six into the workspace")
    run_to_rest(services, oid)
    first = services.approvals.list("pending")[0]
    services.approvals.decide(first["id"], True, "user:admin")
    services.db.execute("UPDATE approvals SET expires_at = ? WHERE id = ?",
                        (now_iso(timedelta(minutes=-5)), first["id"]))
    m.on_approval_decided(services.approvals.get(first["id"]))
    assert run_to_rest(services, oid) == sm.WAITING_FOR_APPROVAL
    pending = services.approvals.list("pending")
    assert len(pending) == 1 and pending[0]["id"] != first["id"]
    assert services.db.scalar("SELECT COUNT(*) FROM tool_runs") == 0


def test_prohibited_objective_blocked_before_planning(services, mock_model):
    oid = ObjectiveManager(services).create("Write ransomware and deploy it to my neighbour's PC")
    assert run_to_rest(services, oid) == sm.FAILED
    assert mock_model.calls == [] and services.db.scalar("SELECT COUNT(*) FROM tool_runs") == 0
    assert services.db.one("SELECT * FROM audit_events WHERE event = 'objective.refused'")


def test_disallowed_tool_in_plan_rejected(services, mock_model):
    bad = plan(step("python_run", code="print(1)"))
    mock_model.push(bad, bad)
    oid = ObjectiveManager(services).create("Only read files please", allowed_tools=["file_read", "file_list"])
    assert run_to_rest(services, oid) == sm.FAILED
    assert "not allowed" in services.state.events(oid)[-1]["reason"]
    assert services.db.scalar("SELECT COUNT(*) FROM tool_runs") == 0


def test_invalid_plan_gets_one_repair(services, mock_model):
    mock_model.push(plan(step("rm_rf_everything", path="/")),
                    plan(step("file_write", path="a.txt", content="ok")))
    oid = ObjectiveManager(services).create("Create a.txt containing ok")
    assert run_to_rest(services, oid) == sm.COMPLETED
    assert "unknown tool" in mock_model.calls[1]["messages"][0].content


def test_planner_output_cannot_smuggle_bad_arguments(services, mock_model):
    evil = plan(step("file_write", path="../../aegis.db", content="pwned"))
    mock_model.push(evil, plan(step("file_write", path="../../aegis.db", content="pwned")),
                    plan(step("file_write", path="../../aegis.db", content="pwned")),
                    plan(step("file_write", path="../../aegis.db", content="pwned")),
                    plan(step("file_write", path="../../aegis.db", content="pwned")))
    oid = ObjectiveManager(services).create("Overwrite the database file")
    assert run_to_rest(services, oid) == sm.FAILED
    assert services.db.scalar("SELECT COUNT(*) FROM objectives") == 1  # db intact
    errors = [r["error_class"] for r in services.db.query("SELECT error_class FROM tool_runs")]
    assert errors and set(errors) == {"path_violation"}


def test_clarification_flow(services, mock_model):
    mock_model.push({"clarification_needed": "Which directory should be backed up?", "steps": []},
                    plan(step("file_write", path="plan.txt", content="back up /srv")))
    m = ObjectiveManager(services)
    oid = m.create("Set up a backup plan")
    assert run_to_rest(services, oid) == sm.PAUSED
    o = m.get(oid)
    assert o["clarification_question"] == "Which directory should be backed up?"
    m.clarify(oid, "/srv")
    assert run_to_rest(services, oid) == sm.COMPLETED
    assert any("/srv" in c for c in m.get(oid)["constraints"])
    assert "A: /srv" in mock_model.calls[1]["messages"][0].content


def test_budget_exhaustion_pauses(services, mock_model):
    services.settings.daily_spend_limit_usd = 0.0
    oid = ObjectiveManager(services).create("Anything that needs the model")
    assert run_to_rest(services, oid) == sm.PAUSED
    assert "spending limit" in services.state.events(oid)[-1]["reason"]
    assert services.db.scalar("SELECT COUNT(*) FROM notifications WHERE kind = 'budget_exhausted'") == 1
    assert mock_model.calls == []


def test_objective_spend_budget(services, mock_model):
    mock_model.push(plan(step("file_write", path="x", content="1"), step("file_write", path="y", content="2")))
    m = ObjectiveManager(services)
    oid = m.create("Write two files", budget_usd=0.5)
    services.db.execute("UPDATE objectives SET cost_usd = 1.0 WHERE id = ?", (oid,))
    assert run_to_rest(services, oid) == sm.PAUSED


def test_pause_and_resume(services, mock_model):
    mock_model.push(plan(step("file_write", path="a", content="1"), step("file_write", path="b", content="2")))
    m = ObjectiveManager(services)
    oid = m.create("Write two files a and b")
    services.controller.run(oid, max_iterations=2)  # plan + first step
    m.pause(oid)
    assert services.controller.run(oid) == sm.PAUSED
    assert services.db.scalar("SELECT COUNT(*) FROM tool_runs") == 1
    m.resume(oid)
    assert run_to_rest(services, oid) == sm.COMPLETED


def test_cancel(services, mock_model):
    mock_model.push(plan(step("file_write", path="a", content="1")))
    m = ObjectiveManager(services)
    oid = m.create("Write a file then stop")
    m.cancel(oid)
    assert services.controller.run(oid) == sm.CANCELLED
    assert mock_model.calls == []


def test_time_budget_expiry(services, mock_model):
    mock_model.push(plan(step("file_write", path="a", content="1")))
    oid = ObjectiveManager(services).create("Write one file", time_budget_minutes=1)
    services.controller.run(oid, max_iterations=1)
    services.db.execute("UPDATE objectives SET started_at = ? WHERE id = ?", (now_iso(timedelta(hours=-1)), oid))
    assert run_to_rest(services, oid) == sm.FAILED
    assert "time budget" in services.state.events(oid)[-1]["reason"]


def test_repeated_failures_stop_after_max_replans(services, mock_model):
    failing = plan(step("file_read", path="missing.txt"))
    mock_model.push(*[failing] * 10)
    oid = ObjectiveManager(services).create("Read a file that does not exist")
    assert run_to_rest(services, oid) == sm.FAILED
    o = services.db.one("SELECT * FROM objectives WHERE id = ?", (oid,))
    assert o["replans"] == services.settings.max_replans and "repeated failures" in o["status_reason"]
    assert services.db.scalar("SELECT COUNT(*) FROM notifications WHERE kind = 'repeated_failures'") == 1


def test_transient_failure_retried(services, mock_model, web):
    calls = {"n": 0}
    url = "https://flaky.example.com/doc"
    orig = web.handler

    def flaky(request):
        if str(request.url) == url:
            calls["n"] += 1
            if calls["n"] == 1:
                import httpx
                return httpx.Response(503)
        return orig(request)

    web.handler = flaky
    web.add(url, load_fixture("official_docs.html"))
    services.fetcher._client._transport = __import__("httpx").MockTransport(flaky)
    mock_model.push(plan(step("research_ingest", url=url, query="pi")))
    oid = ObjectiveManager(services).create("Ingest the flaky document")
    assert run_to_rest(services, oid) == sm.COMPLETED
    task = services.db.one("SELECT * FROM tasks WHERE objective_id = ?", (oid,))
    assert task["attempts"] == 2 and calls["n"] == 2


def test_transient_provider_outage_defers_planning(services, mock_model):
    mock_model.max_retries = 0
    mock_model.push(ModelError("overloaded", ErrorClass.SERVER))
    oid = ObjectiveManager(services).create("Plan something while provider is down")
    assert services.controller.run(oid) == sm.QUEUED
    assert "deferred" in services.state.events(oid)[-1]["reason"]
    mock_model.push(ModelError("bad key", ErrorClass.AUTH))
    assert services.controller.run(oid) == sm.FAILED


def test_success_criteria_judge_can_force_replan_but_not_pass_failures(services, mock_model):
    mock_model.push(
        plan(step("file_write", path="r.md", content="draft")),
        {"criteria_met": False, "explanation": "report lacks sources", "unmet_criteria": ["cite sources"]},
        plan(step("file_write", path="r.md", content="draft with sources")),
        {"criteria_met": True, "explanation": "ok"})
    oid = ObjectiveManager(services).create("Write a report", success_criteria=["cite sources"])
    assert run_to_rest(services, oid) == sm.COMPLETED
    assert services.db.scalar("SELECT replans FROM objectives WHERE id = ?", (oid,)) == 1


def test_model_cannot_override_failed_deterministic_check(services, mock_model):
    mock_model.push(plan(step("compare_output", expected="a", actual="b", checks=[{"type": "ok"}])),
                    *[plan(step("compare_output", expected="a", actual="b"))] * 5)
    oid = ObjectiveManager(services).create("Make the outputs match", success_criteria=["outputs match"])
    assert run_to_rest(services, oid) == sm.FAILED
    # The evaluation model was never consulted because deterministic checks never passed.
    assert not any(c["json_schema"] and "criteria_met" in json.dumps(c["json_schema"]) for c in mock_model.calls)
