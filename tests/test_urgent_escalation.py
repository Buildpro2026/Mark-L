"""Focused tests for the 2026-09-03 urgent-event escalation system (Lee's
autonomous-CEO/COS spec, Section 16): actions/approval_notifier.py
generalized beyond pending-approval tasks, plus the recurring
urgent_escalation_sweep agent that actually ticks it."""
import pytest

from actions import agent_orchestrator as ao
from actions import approval_notifier


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    from core.headless import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "jarvis2.db")
    monkeypatch.setattr(config, "JARVIS_OWNER_PHONE", "+13125550142")
    return tmp_path


def _task():
    return ao.AgentTask(id="t1", agent_id="test", description="")


# ── notify_urgent_event ─────────────────────────────────────────────────

def test_level_below_2_never_sends(isolated_db):
    result = approval_notifier.notify_urgent_event("evt1", "Something minor", level=1)
    assert result["action"] == "none"


def test_honestly_reports_not_configured_without_owner_phone(monkeypatch, tmp_path):
    from core.headless import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "jarvis2.db")
    monkeypatch.setattr(config, "JARVIS_OWNER_PHONE", "")
    result = approval_notifier.notify_urgent_event("evt2", "Urgent thing", level=2)
    assert result["configured"] is False


def test_honestly_reports_not_configured_without_twilio(isolated_db, monkeypatch):
    from actions import twilio_integration as twilio
    monkeypatch.setattr(twilio, "is_configured", lambda: False)
    result = approval_notifier.notify_urgent_event("evt3", "Urgent thing", level=2)
    assert result["configured"] is False


def test_sends_sms_once_and_is_deduped(isolated_db, monkeypatch):
    from actions import twilio_integration as twilio
    sent = []
    monkeypatch.setattr(twilio, "is_configured", lambda: True)
    monkeypatch.setattr(twilio, "send_sms", lambda to, body: (sent.append((to, body)), {"ok": True, "sid": "SM1"})[1])

    first = approval_notifier.notify_urgent_event("evt4", "Server is down", detail="jarvis-headless-core", level=3)
    assert first["action"] == "sms"
    assert len(sent) == 1
    assert "Server is down" in sent[0][1]

    second = approval_notifier.notify_urgent_event("evt4", "Server is down", level=3)
    assert second["action"] == "already_sent"
    assert len(sent) == 1  # never texted twice for the same event


# ── sweep_urgent_escalations ─────────────────────────────────────────────

def test_sweep_escalates_a_level3_event_past_the_window(isolated_db, monkeypatch):
    from actions import twilio_integration as twilio
    from actions import cartesia_calls
    monkeypatch.setattr(twilio, "is_configured", lambda: True)
    monkeypatch.setattr(twilio, "send_sms", lambda to, body: {"ok": True, "sid": "SM1"})
    monkeypatch.setattr(cartesia_calls, "is_configured", lambda: True)
    calls = []
    monkeypatch.setattr(cartesia_calls, "place_call", lambda to, msg: (calls.append((to, msg)), {"ok": True, "agent_call_id": "c1"})[1])

    approval_notifier.notify_urgent_event("evt5", "Server is down", level=3)
    result = approval_notifier.sweep_urgent_escalations(escalate_after_minutes=-1)  # force "already past window"
    assert len(calls) == 1
    assert result[0]["action"] == "call"
    assert result[0]["ok"] is True


def test_sweep_never_escalates_twice(isolated_db, monkeypatch):
    from actions import twilio_integration as twilio
    from actions import cartesia_calls
    monkeypatch.setattr(twilio, "is_configured", lambda: True)
    monkeypatch.setattr(twilio, "send_sms", lambda to, body: {"ok": True, "sid": "SM1"})
    monkeypatch.setattr(cartesia_calls, "is_configured", lambda: True)
    calls = []
    monkeypatch.setattr(cartesia_calls, "place_call", lambda to, msg: (calls.append(1), {"ok": True, "agent_call_id": "c1"})[1])

    approval_notifier.notify_urgent_event("evt6", "Server is down", level=3)
    approval_notifier.sweep_urgent_escalations(escalate_after_minutes=-1)
    approval_notifier.sweep_urgent_escalations(escalate_after_minutes=-1)
    assert len(calls) == 1


def test_sweep_skips_events_not_yet_past_the_window(isolated_db, monkeypatch):
    from actions import twilio_integration as twilio
    from actions import cartesia_calls
    monkeypatch.setattr(twilio, "is_configured", lambda: True)
    monkeypatch.setattr(twilio, "send_sms", lambda to, body: {"ok": True, "sid": "SM1"})
    monkeypatch.setattr(cartesia_calls, "is_configured", lambda: True)
    calls = []
    monkeypatch.setattr(cartesia_calls, "place_call", lambda to, msg: (calls.append(1), {"ok": True})[1])

    approval_notifier.notify_urgent_event("evt7", "Server is down", level=3)
    approval_notifier.sweep_urgent_escalations(escalate_after_minutes=999)
    assert calls == []


def test_acknowledged_event_is_never_escalated(isolated_db, monkeypatch):
    from actions import twilio_integration as twilio
    from actions import cartesia_calls
    monkeypatch.setattr(twilio, "is_configured", lambda: True)
    monkeypatch.setattr(twilio, "send_sms", lambda to, body: {"ok": True, "sid": "SM1"})
    monkeypatch.setattr(cartesia_calls, "is_configured", lambda: True)
    calls = []
    monkeypatch.setattr(cartesia_calls, "place_call", lambda to, msg: (calls.append(1), {"ok": True})[1])

    approval_notifier.notify_urgent_event("evt8", "Server is down", level=3)
    approval_notifier.acknowledge_urgent_event("evt8")
    approval_notifier.sweep_urgent_escalations(escalate_after_minutes=-1)
    assert calls == []


# ── the recurring sweep agent ─────────────────────────────────────────────

def test_sweep_agent_is_registered_observe_level_and_frequent():
    agent = ao.BUILTIN_AGENTS["urgent_escalation_sweep"]
    assert agent.schedule == "5m"
    assert agent.permission_level == ao.PermissionLevel.OBSERVE
    assert agent.autonomous_ok is True


def test_sweep_agent_handler_reports_honestly_with_nothing_pending(isolated_db):
    result = ao._urgent_escalation_sweep_handler(_task())
    assert result["escalated"] == []
