"""Phase 4 executive main-screen synthesis: Today's Priorities, Active
Agents, and the Calendar snapshot. Every item traces back to real data;
nothing here fabricates urgency, activity, or a calendar conflict that
isn't actually there.
"""
from datetime import datetime, timedelta, timezone

import pytest

from actions import priorities_engine
from actions import agent_orchestrator as ao
from actions import business_intelligence as bi
from actions import buildpro_data as bd
from actions import buffer_integration as buf
from core.headless import config as _hc


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(bi, "DB_PATH", tmp_path / "bi.db")
    monkeypatch.setattr(bd, "DB_PATH", tmp_path / "buildpro.db")
    monkeypatch.setattr(buf, "CONFIG_PATH", tmp_path / "api_keys.json")
    monkeypatch.setattr(_hc, "BUFFER_TOKEN", None)


# ── Today's Priorities ───────────────────────────────────────────────

def test_priorities_is_empty_when_nothing_is_flagged(gmail_not_authorized):
    assert priorities_engine.get_todays_priorities() == []


def test_a_pending_approval_becomes_a_priority_item():
    orch = ao.orchestrator
    orch._agents["test_priority_execute"] = ao.AgentDefinition(
        id="test_priority_execute", name="Test Priority Execute", description="x",
        nucleus_id="system", permission_level=ao.PermissionLevel.EXECUTE,
        handler=lambda task: {"ok": True},
    )
    try:
        task = orch.assign_task("test_priority_execute", "send the thing")
        items = priorities_engine.get_todays_priorities()
        kinds = [i["kind"] for i in items]
        assert "approval" in kinds
    finally:
        del orch._agents["test_priority_execute"]
        del orch._tasks[task.id]


def test_risks_outrank_recommendations():
    orch = ao.orchestrator
    orch._agents["test_priority_risk"] = ao.AgentDefinition(
        id="test_priority_risk", name="Test Priority Risk", description="x",
        nucleus_id="system", permission_level=ao.PermissionLevel.EXECUTE,
        handler=lambda task: {"ok": True},
    )
    try:
        task = orch.assign_task("test_priority_risk", "do it")
        task.updated_ts -= 25 * 3600  # stalled -> a real risk
        orch._tasks[task.id] = task

        items = priorities_engine.get_todays_priorities()
        assert items[0]["kind"] == "risk"
    finally:
        del orch._agents["test_priority_risk"]
        del orch._tasks[task.id]


def test_priorities_respects_the_limit():
    orch = ao.orchestrator
    ids = []
    try:
        for i in range(5):
            aid = f"test_priority_bulk_{i}"
            orch._agents[aid] = ao.AgentDefinition(
                id=aid, name=f"Bulk {i}", description="x", nucleus_id="system",
                permission_level=ao.PermissionLevel.EXECUTE, handler=lambda task: {"ok": True},
            )
            task = orch.assign_task(aid, "do it")
            ids.append((aid, task.id))
        items = priorities_engine.get_todays_priorities(limit=2)
        assert len(items) == 2
    finally:
        for aid, tid in ids:
            del orch._agents[aid]
            del orch._tasks[tid]


# ── alert sensitivity filtering ──────────────────────────────────────

def test_quiet_sensitivity_hides_everything_but_real_risks():
    orch = ao.orchestrator
    orch._agents["test_quiet_execute"] = ao.AgentDefinition(
        id="test_quiet_execute", name="Test Quiet Execute", description="x",
        nucleus_id="system", permission_level=ao.PermissionLevel.EXECUTE,
        handler=lambda task: {"ok": True},
    )
    try:
        task = orch.assign_task("test_quiet_execute", "do it")  # a pending approval, severity 3
        items = priorities_engine.get_todays_priorities(
            min_severity=priorities_engine.ALERT_SENSITIVITY_MIN_SEVERITY["quiet"]
        )
        assert all(i["kind"] == "risk" for i in items)
    finally:
        del orch._agents["test_quiet_execute"]
        del orch._tasks[task.id]


def test_high_alert_sensitivity_surfaces_standing_recommendations(monkeypatch):
    from actions import buildpro_intelligence
    monkeypatch.setattr(
        buildpro_intelligence, "generate_morning_report_data",
        lambda: {"recommended_actions": ["3 new job(s) opened in the last 7 days."]},
    )
    items = priorities_engine.get_todays_priorities(
        min_severity=priorities_engine.ALERT_SENSITIVITY_MIN_SEVERITY["high_alert"]
    )
    assert any(i["kind"] == "recommendation" for i in items)


def test_normal_sensitivity_excludes_standing_recommendations(monkeypatch):
    from actions import buildpro_intelligence
    monkeypatch.setattr(
        buildpro_intelligence, "generate_morning_report_data",
        lambda: {"recommended_actions": ["3 new job(s) opened in the last 7 days."]},
    )
    items = priorities_engine.get_todays_priorities(
        min_severity=priorities_engine.ALERT_SENSITIVITY_MIN_SEVERITY["normal"]
    )
    assert not any(i["kind"] == "recommendation" for i in items)


# ── Active Agents ─────────────────────────────────────────────────────

def test_idle_never_run_agent_is_not_shown_as_active():
    # A brand-new agent registered fresh in this test, not one of the
    # 13 built-ins — those load their real last_run_ts from this
    # machine's actual database at process import time (the orchestrator
    # singleton is created once, before any test's DB_PATH isolation
    # takes effect), so asserting on a built-in's history here would be
    # asserting on real, uncontrolled machine state instead of behavior.
    orch = ao.orchestrator
    orch._agents["test_never_run"] = ao.AgentDefinition(
        id="test_never_run", name="Test Never Run", description="x",
        nucleus_id="system", permission_level=ao.PermissionLevel.OBSERVE,
        handler=lambda task: {"ok": True},
    )
    try:
        summary = priorities_engine.get_active_agents_summary()
        names = [a["name"] for a in summary]
        assert "Test Never Run" not in names
    finally:
        del orch._agents["test_never_run"]


def test_agent_with_pending_approval_is_shown_as_needing_attention():
    orch = ao.orchestrator
    orch._agents["test_active_execute"] = ao.AgentDefinition(
        id="test_active_execute", name="Test Active Execute", description="x",
        nucleus_id="system", permission_level=ao.PermissionLevel.EXECUTE,
        handler=lambda task: {"ok": True},
    )
    try:
        task = orch.assign_task("test_active_execute", "do it")
        summary = priorities_engine.get_active_agents_summary()
        entry = next(a for a in summary if a["agent_id"] == "test_active_execute")
        assert entry["needs_attention"] is True
        assert "approval" in entry["what"].lower()
    finally:
        del orch._agents["test_active_execute"]
        del orch._tasks[task.id]


def test_recently_run_agent_shows_up_as_active():
    orch = ao.orchestrator
    agent = orch.get_agent("system_monitor_agent")
    agent.last_run_ts = __import__("time").time() - 60
    try:
        summary = priorities_engine.get_active_agents_summary()
        entry = next((a for a in summary if a["agent_id"] == "system_monitor_agent"), None)
        assert entry is not None
        assert "ran" in entry["what"].lower()
    finally:
        agent.last_run_ts = None


# ── Calendar snapshot ─────────────────────────────────────────────────

def test_calendar_snapshot_reports_not_available_when_unauthorized(gmail_not_authorized):
    snap = priorities_engine.get_calendar_snapshot()
    assert snap["available"] is False
    assert snap["events"] == []


def test_calendar_snapshot_detects_a_real_overlap(monkeypatch):
    from actions import google_auth
    monkeypatch.setattr(google_auth, "get_credential_status", lambda: {"authorized": True})

    today = datetime.now(timezone.utc)
    from actions import calendar_integration
    monkeypatch.setattr(calendar_integration, "list_upcoming_events", lambda *a, **k: {
        "ok": True,
        "events": [
            {"summary": "Standup", "start": today.replace(hour=9, minute=0).isoformat(), "end": today.replace(hour=9, minute=30).isoformat()},
            {"summary": "Client call", "start": today.replace(hour=9, minute=15).isoformat(), "end": today.replace(hour=10, minute=0).isoformat()},
        ],
    })
    snap = priorities_engine.get_calendar_snapshot()
    assert snap["available"] is True
    assert len(snap["conflicts"]) == 1


def test_calendar_snapshot_no_false_positive_conflict_for_back_to_back_events(monkeypatch):
    from actions import google_auth
    monkeypatch.setattr(google_auth, "get_credential_status", lambda: {"authorized": True})

    today = datetime.now(timezone.utc)
    from actions import calendar_integration
    monkeypatch.setattr(calendar_integration, "list_upcoming_events", lambda *a, **k: {
        "ok": True,
        "events": [
            {"summary": "Standup", "start": today.replace(hour=9, minute=0).isoformat(), "end": today.replace(hour=9, minute=30).isoformat()},
            {"summary": "Planning", "start": today.replace(hour=9, minute=30).isoformat(), "end": today.replace(hour=10, minute=0).isoformat()},
        ],
    })
    snap = priorities_engine.get_calendar_snapshot()
    assert snap["conflicts"] == []
