"""actions/ceo_operating_cycle.py — the central WAKE->REPORT autonomous
loop (Lee's autonomous-CEO/COS spec, Section THIRD). Runs the real
pipeline end-to-end against isolated databases: this is the one thing
that actually proves the pieces (executive_brief, priorities_engine,
agent_orchestrator, ddf_discovery, verification, business_intelligence,
approval_notifier) compose into one working autonomous cycle, not just
that each works in isolation.
"""
import pytest

from actions import agent_orchestrator as ao
from actions import business_intelligence as bi
from actions import ceo_operating_cycle as cycle
from actions import daily_deal_finders as ddf
from actions import opportunity_engine as oe
from actions import strategic_objective as so
from actions import buffer_integration as buf
from core.headless import config as hc


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(ddf, "DB_PATH", tmp_path / "ddf.db")
    monkeypatch.setattr(oe, "DB_PATH", tmp_path / "opp.db")
    monkeypatch.setattr(so, "CONFIG_FILE", tmp_path / "strategic_objective.json")
    monkeypatch.setattr(buf, "DB_PATH", tmp_path / "buffer.db")
    monkeypatch.setattr(buf, "CONFIG_PATH", tmp_path / "api_keys.json")
    monkeypatch.setattr(hc, "BUFFER_TOKEN", None)
    # approval_notifier._connect() and ceo_operating_cycle._connect() both
    # read core.headless.config.DB_PATH dynamically at call time (not a
    # frozen import) — patching it here is enough to isolate both.
    monkeypatch.setattr(hc, "DB_PATH", tmp_path / "shared_config_jarvis2.db")
    monkeypatch.setattr(hc, "JARVIS_OWNER_PHONE", None)
    monkeypatch.setattr(hc, "PRODUCT_DATA_API_KEY", None)


@pytest.fixture(autouse=True)
def _restore_shared_orchestrator_state():
    """Unlike other test files that touch one custom agent, every test in
    this file calls the real run_cycle(), which runs the ENTIRE real
    BUILTIN_AGENTS workforce through the shared agent_orchestrator
    singleton (see the comment in test_cycle_runs_due_and_stale_
    autonomous_agents on why it's shared across the whole test session).
    Without this, a task/event this file's run leaves behind — even from
    a real builtin agent, not just this file's own test-only agents —
    would leak into a later test elsewhere in the suite (e.g. executive_
    brief._operational_risks() or priorities_engine picking up a stray
    failed task and no longer reporting 'nothing is wrong'). Snapshot
    per-agent status/last_run_ts/last_error/last_success_ts plus the
    _tasks/_events collections before each test, restore after."""
    orch = cycle.agent_orchestrator
    agent_snapshot = {
        aid: (a.status, a.last_run_ts, a.last_error, a.last_success_ts)
        for aid, a in orch._agents.items()
    }
    task_ids_before = set(orch._tasks.keys())
    events_len_before = len(orch._events)
    yield
    for task_id in list(orch._tasks.keys()):
        if task_id not in task_ids_before:
            del orch._tasks[task_id]
    del orch._events[events_len_before:]
    for aid, (status, last_run_ts, last_error, last_success_ts) in agent_snapshot.items():
        agent = orch._agents.get(aid)
        if agent is None:
            continue
        agent.status, agent.last_run_ts = status, last_run_ts
        agent.last_error, agent.last_success_ts = last_error, last_success_ts


def test_full_cycle_runs_end_to_end_without_error():
    result = cycle.run_cycle(force=True, dry_run=True)
    assert result["ok"] is True
    assert result["state"] == "RAN"
    assert isinstance(result["priorities"], list)
    assert isinstance(result["summary"], str) and result["summary"]
    assert result["discovery"]["state"] == "NOT_CONFIGURED"


def test_cycle_runs_at_most_once_per_day_unless_forced():
    first = cycle.run_cycle(force=True, dry_run=True)
    assert first["state"] == "RAN"
    second = cycle.run_cycle(force=False, dry_run=True)
    assert second["state"] == "ALREADY_RAN_TODAY"
    third = cycle.run_cycle(force=True, dry_run=True)
    assert third["state"] == "RAN"


def test_already_ran_today_reflects_persisted_state():
    assert cycle.already_ran_today() is False
    cycle.run_cycle(force=True, dry_run=True)
    assert cycle.already_ran_today() is True


def test_cycle_never_calls_approve_task_or_publishes_anything():
    # The hard guardrail from the module docstring, enforced here rather
    # than only asserted in a comment: patch approve_task to blow up if
    # called, and advance_to_published(approved=True) the same way.
    # Uses cycle.agent_orchestrator (the exact object reference
    # ceo_operating_cycle.py itself calls into) rather than ao.orchestrator
    # — see the comment on test_cycle_runs_due_and_stale_autonomous_agents
    # for why those two names can diverge.
    def _boom(*a, **kw):
        raise AssertionError("ceo_operating_cycle must never approve a pending task itself")
    orig = cycle.agent_orchestrator.approve_task
    cycle.agent_orchestrator.approve_task = _boom
    try:
        result = cycle.run_cycle(force=True, dry_run=True)
        assert result["ok"] is True
    finally:
        cycle.agent_orchestrator.approve_task = orig


def test_cycle_runs_due_and_stale_autonomous_agents():
    # The real BUILTIN_AGENTS workforce has several IDLE-by-default
    # scheduled agents that are due the first time any test in this
    # process runs them, but the singleton is shared across every test —
    # an earlier test's run_cycle() call can leave them not-yet-due again
    # by wall-clock time. Add one guaranteed-fresh always-due agent so
    # this test doesn't depend on execution order.
    #
    # Mutate cycle.agent_orchestrator, NOT ao.orchestrator: some other
    # test files (e.g. test_buildpro_hubspot_sync_agent.py) replace the
    # actions.agent_orchestrator.orchestrator module attribute with a
    # brand-new AgentOrchestrator() instance via a raw assignment (not
    # monkeypatch, so it isn't auto-reverted in the usual way either) —
    # after that runs, `ao.orchestrator` and the object
    # ceo_operating_cycle.py actually calls (captured at ITS OWN import
    # time as `agent_orchestrator`) can be two different objects. Mutating
    # cycle.agent_orchestrator directly is what guarantees this test
    # affects the same instance run_cycle() will actually use, regardless
    # of what any other test file has done to the module-level name.
    orch = cycle.agent_orchestrator
    orch._agents["test_cycle_always_due_agent"] = ao.AgentDefinition(
        id="test_cycle_always_due_agent", name="Test Cycle Always Due", description="x",
        nucleus_id="system", permission_level=ao.PermissionLevel.OBSERVE,
        status=ao.AgentStatus.IDLE, schedule="1m", last_run_ts=None,
        handler=lambda task: {"summary": "ran"},
    )
    try:
        result = cycle.run_cycle(force=True, dry_run=True)
        assert result["agents_run"] > 0
    finally:
        del orch._agents["test_cycle_always_due_agent"]


def test_verification_records_are_produced_for_every_executed_item():
    result = cycle.run_cycle(force=True, dry_run=True)
    assert len(result["verifications"]) >= result["agents_run"]
    for v in result["verifications"]:
        assert "verification_status" in v
        assert v["verification_status"] in ("verified_success", "verified_failure")


def test_failed_agent_task_produces_a_followup_risk_entry():
    orch = cycle.agent_orchestrator  # see comment above — must match what run_cycle() itself uses
    orch._agents["test_cycle_failing_agent"] = ao.AgentDefinition(
        id="test_cycle_failing_agent", name="Test Cycle Failing Agent", description="x",
        nucleus_id="system", permission_level=ao.PermissionLevel.OBSERVE,
        status=ao.AgentStatus.IDLE, schedule="1m",
        handler=lambda task: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    try:
        before = len(bi.list_entries(category="risks", limit=200))
        result = cycle.run_cycle(force=True, dry_run=True)
        after = bi.list_entries(category="risks", limit=200)
        assert len(after) > before
        failing = [v for v in result["verifications"] if not v["success"]]
        assert any(
            "test_cycle_failing_agent" in ((v.get("intended_action") or "") + (v.get("actual_action") or ""))
            for v in failing
        )
    finally:
        del orch._agents["test_cycle_failing_agent"]


def test_report_notification_is_skipped_but_recorded_in_dry_run():
    result = cycle.run_cycle(force=True, dry_run=True)
    assert result["notification"] == {"action": "skipped_dry_run"}


def test_report_notification_is_sent_when_not_dry_run(monkeypatch):
    sent = {}

    def _fake_notify(event_id, title, detail, level=2, priority="normal"):
        sent["event_id"] = event_id
        sent["title"] = title
        return {"event_id": event_id, "action": "none", "configured": False}

    from actions import approval_notifier
    monkeypatch.setattr(approval_notifier, "notify_urgent_event", _fake_notify)

    result = cycle.run_cycle(force=True, dry_run=False)
    assert result["notification"]["action"] == "none"  # honestly not configured, but the real path ran
    assert sent["event_id"].startswith("ceo_cycle-")
    assert sent["title"] == "JARVIS Morning Brief"
