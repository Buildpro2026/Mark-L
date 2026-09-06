"""Focused tests for the 2026-09-03 email intelligence router (Lee's
autonomous-CEO/COS spec, Sections 4 & 8): the cross-business half of email
classification that routes DDF/CareerRocket/JARVIS/REVIEW_REQUIRED mail to
real business-intelligence entries, while BuildPro stays fully owned by
the existing candidate/client intake agents and PERSONAL/IRRELEVANT are
never persisted here."""
import pytest

from actions import agent_orchestrator as ao
from actions import business_intelligence as biz_intel
from actions import buildpro_data as bd
from actions import google_auth


def _task():
    return ao.AgentTask(id="t1", agent_id="test", description="")


def _msgs(*msgs):
    return {"ok": True, "messages": list(msgs)}


@pytest.fixture(autouse=True)
def _authorized(monkeypatch):
    monkeypatch.setattr(google_auth, "get_credential_status", lambda: {"authorized": True})


def test_agent_is_registered_with_a_real_schedule_and_autonomous_ok():
    agent = ao.BUILTIN_AGENTS["email_intelligence_router"]
    assert agent.schedule == "30m"
    assert agent.autonomous_ok is True
    assert agent.permission_level == ao.PermissionLevel.SUGGEST


def test_reports_honestly_when_gmail_not_authorized(monkeypatch):
    monkeypatch.setattr(google_auth, "get_credential_status", lambda: {"authorized": False})
    result = ao._email_intelligence_router_handler(_task())
    assert result["configured"] is False


def test_ddf_email_creates_a_real_research_entry(monkeypatch):
    from actions import gmail_integration
    msg = {
        "id": "m1", "thread_id": "th1", "sender": "creators@tiktok.com",
        "sender_domain": "tiktok.com", "subject": "Creator Fund opportunity",
        "snippet": "Join our Creator Fund", "body": "Join our Creator Fund and earn commission.",
        "permalink": "https://mail.google.com/mail/u/0/#all/th1",
    }
    monkeypatch.setattr(gmail_integration, "list_messages", lambda query, max_results: _msgs(msg))

    result = ao._email_intelligence_router_handler(_task())
    assert result["configured"] is True
    assert len(result["routed"]) == 1
    assert result["routed"][0]["category"] == "DAILY_DEAL_FINDERS"

    entries = biz_intel.list_entries(business="ddf")
    assert len(entries) == 1
    assert entries[0]["data"]["gmail_message_id"] == "m1"
    assert entries[0]["data"]["source_url"] == "https://mail.google.com/mail/u/0/#all/th1"
    assert entries[0]["data"]["company_id"] == "daily_deal_finders"


def test_jarvis_infra_alert_creates_a_risks_entry(monkeypatch):
    from actions import gmail_integration
    msg = {
        "id": "m2", "sender": "notifications@render.com", "sender_domain": "render.com",
        "subject": "Your deploy failed", "body": "Build failed for jarvis-headless-core.",
    }
    monkeypatch.setattr(gmail_integration, "list_messages", lambda query, max_results: _msgs(msg))

    result = ao._email_intelligence_router_handler(_task())
    entries = biz_intel.list_entries(category="risks")
    assert len(entries) == 1
    assert "System Alert" in entries[0]["title"]


def test_irrelevant_email_never_creates_any_record(monkeypatch):
    from actions import gmail_integration
    msg = {
        "id": "m3", "sender": "hello@render.com", "sender_domain": "render.com",
        "subject": "New feature!", "body": "Cloud application platform. Unsubscribe here.",
    }
    monkeypatch.setattr(gmail_integration, "list_messages", lambda query, max_results: _msgs(msg))

    result = ao._email_intelligence_router_handler(_task())
    assert result["routed"] == []
    assert biz_intel.list_entries() == []
    # still marked processed so it isn't rescanned forever
    assert bd.is_message_processed("m3", "email_router") is True


def test_buildpro_email_is_not_touched_by_this_router(monkeypatch):
    from actions import gmail_integration
    msg = {"id": "m4", "sender": "jane@example.com", "subject": "Re: Application for Electrician role"}
    monkeypatch.setattr(gmail_integration, "list_messages", lambda query, max_results: _msgs(msg))

    result = ao._email_intelligence_router_handler(_task())
    assert result["routed"] == []
    assert bd.list_candidates() == []  # this agent never writes BuildPro CRM records
    assert bd.is_message_processed("m4", "email_router") is True


def test_personal_email_is_not_persisted(monkeypatch):
    from actions import gmail_integration
    msg = {"id": "m5", "sender": "mom@example.com", "subject": "Dinner Friday?", "body": "Want to grab dinner?"}
    monkeypatch.setattr(gmail_integration, "list_messages", lambda query, max_results: _msgs(msg))

    result = ao._email_intelligence_router_handler(_task())
    assert result["routed"] == []
    assert biz_intel.list_entries() == []


def test_already_routed_message_is_skipped_on_a_later_run(monkeypatch):
    from actions import gmail_integration
    msg = {
        "id": "m6", "sender": "creators@tiktok.com", "sender_domain": "tiktok.com",
        "subject": "Creator Fund opportunity", "body": "Join our Creator Fund and earn commission.",
    }
    monkeypatch.setattr(gmail_integration, "list_messages", lambda query, max_results: _msgs(msg))

    ao._email_intelligence_router_handler(_task())
    second = ao._email_intelligence_router_handler(_task())
    assert second["routed"] == []
    assert len(biz_intel.list_entries(business="ddf")) == 1  # not duplicated


def test_router_agent_runs_immediately_through_the_orchestrator(monkeypatch):
    from actions import gmail_integration
    monkeypatch.setattr(gmail_integration, "list_messages", lambda query, max_results: _msgs())
    orch = ao.AgentOrchestrator()
    task = orch.assign_task("email_intelligence_router", "Manual scan")
    assert task.status == ao.TaskStatus.DONE
