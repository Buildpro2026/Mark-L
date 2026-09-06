"""BuildPro Email Responder (J4) — the EXECUTE-capable half of the email
workflow: sends an already-created Gmail draft, gated by the real
approval flow (PENDING_APPROVAL -> approve_task()), never on its own.
"""
import pytest

from actions import agent_orchestrator as ao
from actions import gmail_integration
from actions import audit_log


def test_buildpro_email_responder_is_execute_level():
    agent = ao.BUILTIN_AGENTS["buildpro_email_responder"]
    assert agent.permission_level == ao.PermissionLevel.EXECUTE
    assert agent.nucleus_id == "buildpro"


def test_assigning_a_send_task_never_sends_without_approval(monkeypatch, tmp_path):
    monkeypatch.setattr(audit_log, "DB_PATH", tmp_path / "audit.db")
    called = {"n": 0}
    monkeypatch.setattr(gmail_integration, "send_draft", lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    orch = ao.AgentOrchestrator()
    task = orch.assign_task("buildpro_email_responder", "draft-abc123")

    assert task.status == ao.TaskStatus.PENDING_APPROVAL
    assert called["n"] == 0

    with pytest.raises(PermissionError):
        orch.run_task(task.id)
    assert called["n"] == 0


def test_approval_sends_the_exact_draft_and_records_audit(monkeypatch, tmp_path):
    monkeypatch.setattr(audit_log, "DB_PATH", tmp_path / "audit2.db")
    captured = {}

    def _fake_send_draft(draft_id, approved=False):
        captured.update(draft_id=draft_id, approved=approved)
        return {"ok": True, "message_id": "m-1"}

    monkeypatch.setattr(gmail_integration, "send_draft", _fake_send_draft)

    orch = ao.AgentOrchestrator()
    task = orch.assign_task("buildpro_email_responder", "draft-abc123")
    approved_task = orch.approve_task(task.id)

    assert approved_task.status == ao.TaskStatus.DONE
    assert captured == {"draft_id": "draft-abc123", "approved": True}
    assert approved_task.result["sent"] is True

    rows = audit_log.list_recent(limit=5)
    assert any(r["action"] == "gmail_send_draft" and r["execution_status"] == "succeeded" for r in rows)


def test_send_failure_is_reported_and_audited_not_fabricated(monkeypatch, tmp_path):
    monkeypatch.setattr(audit_log, "DB_PATH", tmp_path / "audit3.db")
    monkeypatch.setattr(gmail_integration, "send_draft",
                         lambda draft_id, approved=False: {"ok": False, "state": "ERROR", "detail": "draft not found"})

    orch = ao.AgentOrchestrator()
    task = orch.assign_task("buildpro_email_responder", "draft-missing")
    approved_task = orch.approve_task(task.id)

    assert approved_task.status == ao.TaskStatus.DONE   # handler completed; the failure is in the result, not an exception
    assert approved_task.result["sent"] is False
    rows = audit_log.list_recent(limit=5)
    assert any(r["action"] == "gmail_send_draft" and r["execution_status"] == "failed" for r in rows)
