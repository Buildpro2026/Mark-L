"""BuildPro -> HubSpot writeback: a strong match with no existing HubSpot
record moves toward a real company through the existing approval gate,
never around it.
"""
import pytest


@pytest.fixture(autouse=True)
def _isolate_ledger(monkeypatch, tmp_path):
    from actions import autonomous_ledger as ledger
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "ledger.db")


def test_an_existing_hubspot_company_needs_no_task(monkeypatch):
    from actions import cross_system as cs, hubspot_integration as hs
    monkeypatch.setattr(hs, "is_configured", lambda: True)
    monkeypatch.setattr(hs, "search_companies", lambda *a, **k: {"ok": True, "results": [{"id": "9"}]})

    result = cs.prepare_hubspot_writeback("Turner")
    assert result["state"] == cs.RECORD_EXISTS
    assert result["task_id"] is None


def test_a_new_employer_proposes_an_approval_gated_task(monkeypatch):
    from actions import cross_system as cs, hubspot_integration as hs
    from actions.agent_orchestrator import orchestrator, TaskStatus
    monkeypatch.setattr(hs, "is_configured", lambda: True)
    monkeypatch.setattr(hs, "search_companies", lambda *a, **k: {"ok": True, "results": []})

    result = cs.prepare_hubspot_writeback("Brand New GC")
    assert result["state"] == cs.APPROVAL_REQUIRED
    assert result["task_id"] is not None

    task = orchestrator._require_task(result["task_id"])
    assert task.status == TaskStatus.PENDING_APPROVAL
    assert task.agent_id == "buildpro_hubspot_writeback_agent"
    assert task.description == "Brand New GC"


def test_the_same_employer_is_not_proposed_twice(monkeypatch):
    from actions import cross_system as cs, hubspot_integration as hs
    monkeypatch.setattr(hs, "is_configured", lambda: True)
    monkeypatch.setattr(hs, "search_companies", lambda *a, **k: {"ok": True, "results": []})

    first = cs.prepare_hubspot_writeback("Repeat GC")
    second = cs.prepare_hubspot_writeback("Repeat GC")
    assert first["state"] == cs.APPROVAL_REQUIRED
    assert second["state"] == cs.RECORD_EXISTS
    assert second["task_id"] is None


def test_hubspot_not_configured_proposes_nothing(monkeypatch):
    from actions import cross_system as cs, hubspot_integration as hs
    monkeypatch.setattr(hs, "is_configured", lambda: False)
    result = cs.prepare_hubspot_writeback("Anyone")
    assert result["state"] == "NOT_CONFIGURED"
    assert result["task_id"] is None


def test_approving_the_task_actually_writes_and_a_rejection_never_does(monkeypatch):
    from actions import cross_system as cs, hubspot_integration as hs
    from actions.agent_orchestrator import orchestrator, TaskStatus
    monkeypatch.setattr(hs, "is_configured", lambda: True)
    monkeypatch.setattr(hs, "search_companies", lambda *a, **k: {"ok": True, "results": []})

    writes = []
    monkeypatch.setattr(hs, "upsert_company",
                        lambda name, props, approved=False: writes.append((name, approved)) or
                        {"ok": True, "id": "123"})

    result = cs.prepare_hubspot_writeback("Approve Me LLC")
    task = orchestrator.approve_task(result["task_id"])
    assert task.status == TaskStatus.DONE
    assert writes == [("Approve Me LLC", True)]


def test_rejecting_the_task_never_writes_to_hubspot(monkeypatch):
    from actions import cross_system as cs, hubspot_integration as hs
    from actions.agent_orchestrator import orchestrator, TaskStatus
    monkeypatch.setattr(hs, "is_configured", lambda: True)
    monkeypatch.setattr(hs, "search_companies", lambda *a, **k: {"ok": True, "results": []})

    def _must_not_write(*a, **k):
        raise AssertionError("a rejected task wrote to HubSpot")
    monkeypatch.setattr(hs, "upsert_company", _must_not_write)

    result = cs.prepare_hubspot_writeback("Reject Me LLC")
    task = orchestrator.reject_task(result["task_id"])
    assert task.status == TaskStatus.REJECTED


def test_the_handler_never_calls_upsert_with_approved_false():
    import inspect
    from actions import agent_orchestrator as ao
    source = inspect.getsource(ao._buildpro_hubspot_writeback_handler)
    assert "approved=True" in source
    assert "approved=False" not in source


def test_a_failed_write_is_reported_not_silently_swallowed(monkeypatch):
    from actions import cross_system as cs, hubspot_integration as hs
    from actions.agent_orchestrator import orchestrator, TaskStatus
    monkeypatch.setattr(hs, "is_configured", lambda: True)
    monkeypatch.setattr(hs, "search_companies", lambda *a, **k: {"ok": True, "results": []})
    monkeypatch.setattr(hs, "upsert_company",
                        lambda *a, **k: {"ok": False, "detail": "rate limited"})

    result = cs.prepare_hubspot_writeback("Fails GC")
    task = orchestrator.approve_task(result["task_id"])
    assert task.result["created"] is False
    assert "rate limited" in task.result["summary"]


# ══ SELF-MAINTENANCE: EXPIRED APPROVALS ARE ACTUALLY EXPIRED ═════════════
# AgentOrchestrator.expire_stale_approvals() has existed and worked since
# it was written, but nothing in the sweep ever called it — an approval
# could sit unanswered forever and still run the moment someone clicked
# an old dashboard link.

def test_run_sweep_expires_unanswered_approvals(monkeypatch):
    from actions import self_healing as heal
    from actions.agent_orchestrator import orchestrator, TaskStatus

    task = orchestrator.assign_task("buildpro_hubspot_writeback_agent", "Stale Co")
    assert task.status == TaskStatus.PENDING_APPROVAL

    old_ttl = orchestrator.expire_stale_approvals.__defaults__
    monkeypatch.setattr(task, "created_ts", 0.0)   # far in the past

    report = heal.run_sweep()
    assert "expired_approvals" in report["checks"]
    refreshed = orchestrator._require_task(task.id)
    assert refreshed.status == TaskStatus.EXPIRED


def test_an_expired_approval_can_never_run(monkeypatch):
    from actions.agent_orchestrator import orchestrator, TaskStatus

    task = orchestrator.assign_task("buildpro_hubspot_writeback_agent", "Old Co")
    monkeypatch.setattr(task, "created_ts", 0.0)
    orchestrator.expire_stale_approvals()

    result = orchestrator.approve_task(task.id)   # approving an EXPIRED task is a no-op
    assert result.status == TaskStatus.EXPIRED
