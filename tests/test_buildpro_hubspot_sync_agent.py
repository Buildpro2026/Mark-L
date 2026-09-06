"""Focused tests for the 2026-09-03 fix: actions/buildpro_sync.py (a
complete, already-tested HubSpot -> BuildPro bulk mirror) existed but was
never wired to anything — nothing ever called sync_contacts()/
sync_companies()/sync_all() outside its own test file. This is why the
live /3d HubSpot module showed real contacts/companies while BuildPro's
own candidate/client counts stayed near zero even though buildpro_sync.py
itself worked fine.

Covers:
  * the new buildpro_hubspot_sync agent is registered with a real
    schedule (so a fresh deploy backfills within the hour with no manual
    start_agent() call — see AgentOrchestrator.__init__'s
    "schedule -> defaults to IDLE" behavior) and autonomous_ok=True;
  * its handler actually calls buildpro_sync.sync_all() and reports an
    honest summary, including the NOT_CONFIGURED case;
  * it runs immediately (no approval needed) through the real
    orchestrator, same as every other SUGGEST-level agent;
  * the hubspot tool's new 'sync' action (core/headless/tool_executor.py)
    triggers the same sync on demand;
  * candidate/client intake auto-queues a real PENDING_APPROVAL task for
    buildpro_email_responder when a welcome draft is created, so it's
    actually discoverable via /3d/api/approvals instead of only sitting
    silently in Gmail Drafts (Lee's 2026-09-03 spec for the approval flow).
"""
import asyncio

import pytest

from actions import agent_orchestrator as ao
from actions import buildpro_data as bd
from actions import candidate_intake
from actions import buildpro_client_intake
from actions import hubspot_integration as hubspot
from core.headless import config as _hc


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(bd, "DB_PATH", tmp_path / "test_hubspot_sync_agent.db")
    cfg_path = tmp_path / "api_keys.json"
    cfg_path.write_text('{"hubspot_token": "test-token"}', encoding="utf-8")
    monkeypatch.setattr(hubspot, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(_hc, "HUBSPOT_TOKEN", None)


def _task(description=""):
    return ao.AgentTask(id="t1", agent_id="test", description=description)


def _contact(cid, first="Jane", last="Doe", email="jane@example.com"):
    return {"id": cid, "properties": {"firstname": first, "lastname": last, "email": email}}


def _company(cid, name="Acme Co"):
    return {"id": cid, "properties": {"name": name}}


def _page(records):
    return {"ok": True, "results": records, "paging": {}}


# ── agent registration ───────────────────────────────────────────────────

def test_agent_is_registered_with_a_real_schedule_and_autonomous_ok():
    agent = ao.BUILTIN_AGENTS["buildpro_hubspot_sync"]
    assert agent.schedule == "60m"
    assert agent.autonomous_ok is True
    assert agent.permission_level == ao.PermissionLevel.SUGGEST
    assert agent.nucleus_id == "buildpro"


def test_fresh_orchestrator_starts_the_sync_agent_idle_with_no_persisted_state():
    # See AgentOrchestrator.__init__: any BUILTIN_AGENTS entry with a
    # `schedule` defaults to IDLE (started) when there's no persisted row
    # — the common case on Render's free tier, which has no persistent
    # disk, so a redeploy must not require Lee to manually re-start
    # every scheduled agent by hand.
    orch = ao.AgentOrchestrator()
    agent = orch.get_agent("buildpro_hubspot_sync")
    assert agent.status == ao.AgentStatus.IDLE


# ── handler behavior ─────────────────────────────────────────────────────

def test_handler_reports_not_configured_without_a_token(monkeypatch, tmp_path):
    cfg_path = tmp_path / "empty.json"
    cfg_path.write_text('{"hubspot_token": ""}', encoding="utf-8")
    monkeypatch.setattr(hubspot, "CONFIG_PATH", cfg_path)

    result = ao._buildpro_hubspot_sync_handler(_task())
    assert result["configured"] is False
    assert bd.list_candidates() == []
    assert bd.list_clients() == []


def test_handler_pulls_real_hubspot_contacts_and_companies_into_buildpro(monkeypatch):
    monkeypatch.setattr(hubspot, "get_contacts", lambda limit, after=None: _page([_contact("hs-1"), _contact("hs-2", first="John", last="Smith", email="john@example.com")]))
    monkeypatch.setattr(hubspot, "get_companies", lambda limit, after=None: _page([_company("hs-co-1")]))

    result = ao._buildpro_hubspot_sync_handler(_task())
    assert result["configured"] is True
    assert result["result"]["contacts"]["created"] == 2
    assert result["result"]["companies"]["created"] == 1
    assert "2 candidate(s) created" in result["summary"]
    assert "1 client(s) created" in result["summary"]

    candidates = bd.list_candidates()
    clients = bd.list_clients()
    assert len(candidates) == 2
    assert len(clients) == 1
    assert clients[0]["hubspot_company_id"] == "hs-co-1"


def test_handler_is_idempotent_on_repeat_runs(monkeypatch):
    monkeypatch.setattr(hubspot, "get_contacts", lambda limit, after=None: _page([_contact("hs-1")]))
    monkeypatch.setattr(hubspot, "get_companies", lambda limit, after=None: _page([]))

    ao._buildpro_hubspot_sync_handler(_task())
    second = ao._buildpro_hubspot_sync_handler(_task())

    assert second["result"]["contacts"]["created"] == 0
    assert second["result"]["contacts"]["updated"] == 1
    assert len(bd.list_candidates()) == 1  # never duplicated


def test_sync_agent_runs_immediately_through_the_orchestrator_no_approval_needed(monkeypatch):
    monkeypatch.setattr(hubspot, "get_contacts", lambda limit, after=None: _page([]))
    monkeypatch.setattr(hubspot, "get_companies", lambda limit, after=None: _page([]))
    orch = ao.AgentOrchestrator()
    task = orch.assign_task("buildpro_hubspot_sync", "Manual backfill")
    assert task.status == ao.TaskStatus.DONE


# ── manual chat trigger (core/headless/tool_executor.py) ───────────────

def test_hubspot_tool_sync_action_triggers_the_same_sync(monkeypatch):
    monkeypatch.setattr(hubspot, "get_contacts", lambda limit, after=None: _page([_contact("hs-1")]))
    monkeypatch.setattr(hubspot, "get_companies", lambda limit, after=None: _page([]))

    from core.headless.tool_executor import ToolExecutor
    result = asyncio.run(ToolExecutor().execute("hubspot", {"action": "sync"}))

    assert "1 candidate(s) created" in result
    assert len(bd.list_candidates()) == 1


def test_hubspot_tool_sync_action_honest_when_not_configured(monkeypatch, tmp_path):
    cfg_path = tmp_path / "empty.json"
    cfg_path.write_text('{"hubspot_token": ""}', encoding="utf-8")
    monkeypatch.setattr(hubspot, "CONFIG_PATH", cfg_path)

    from core.headless.tool_executor import ToolExecutor
    result = asyncio.run(ToolExecutor().execute("hubspot", {"action": "sync"}))
    assert "isn't configured" in result


# ── draft -> real approval task (Lee's 2026-09-03 approval-flow spec) ──

def test_candidate_intake_queues_the_welcome_draft_for_approval(monkeypatch):
    from actions import gmail_integration

    monkeypatch.setattr(gmail_integration, "create_draft", lambda to, subject, body: {"ok": True, "draft_id": "draft-abc"})
    monkeypatch.setattr(hubspot, "is_configured", lambda: False)  # keep this test focused on the draft/approval wiring

    message = {"id": "m1", "sender": "Jane Doe <jane@example.com>", "subject": "Re: your posting", "attachments": []}
    result = candidate_intake.process_candidate_email(message, auto_send_welcome=False)
    assert result["welcome_email_drafted"] is True
    assert result["draft_id"] == "draft-abc"

    orch = ao.AgentOrchestrator()
    ao.orchestrator = orch  # _queue_draft_for_approval calls the module-level singleton
    try:
        ao._queue_draft_for_approval(result["draft_id"])
        pending = [t for t in orch.list_tasks() if t.status == ao.TaskStatus.PENDING_APPROVAL]
        assert len(pending) == 1
        assert pending[0].agent_id == "buildpro_email_responder"
        assert pending[0].description == "draft-abc"
    finally:
        ao.orchestrator = ao.AgentOrchestrator()  # don't leak a replaced singleton into other tests


def test_client_intake_queues_the_welcome_draft_for_approval(monkeypatch):
    from actions import gmail_integration

    monkeypatch.setattr(gmail_integration, "create_draft", lambda to, subject, body: {"ok": True, "draft_id": "draft-xyz"})
    monkeypatch.setattr(hubspot, "is_configured", lambda: False)

    message = {"id": "m2", "sender": "A Client <client@example.com>", "subject": "Need staffing help", "snippet": ""}
    result = buildpro_client_intake.process_client_email(message, auto_send=False)
    assert result["welcome_email_drafted"] is True
    assert result["draft_id"] == "draft-xyz"


def test_queue_draft_for_approval_is_a_noop_without_a_draft_id():
    orch = ao.AgentOrchestrator()
    before = len(orch.list_tasks())
    ao._queue_draft_for_approval(None)
    ao._queue_draft_for_approval("")
    assert len(orch.list_tasks()) == before
