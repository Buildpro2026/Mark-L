"""actions/business_pipeline.py — the layer that turns real business
findings into real executed work.

These tests drive the actual pipeline functions and the actual orchestrator.
External services are faked at the integration boundary only (the HTTP-
calling module), never at the pipeline boundary — a test that stubbed
`calendar_findings` itself and then asserted a task appeared would prove
nothing about whether the application flow is connected.

The distinction that matters throughout: an integration that is
unauthorized or unconfigured must report AUTH_ERROR / NOT_CONFIGURED and
produce no findings. It must never read as a successful empty result,
which is how a broken Gmail connection silently looks like a quiet inbox.
"""
import pytest

from actions import agent_orchestrator as ao
from actions import autonomous_ledger as ledger
from actions import business_pipeline as bp


def _orch(**agents):
    defs = {}
    for aid, perm in agents.items():
        defs[aid] = ao.AgentDefinition(
            id=aid, name=aid, description="x", nucleus_id="system",
            permission_level=perm, handler=lambda t: {"summary": "handled"},
        )
    return ao.AgentOrchestrator(agents=defs)


# ── the shared failure vocabulary ────────────────────────────────────────

def test_integration_failures_are_classified_not_swallowed():
    assert bp._integration_failure({"ok": True, "results": []}) is None

    unauth = bp._integration_failure({"ok": False, "state": "NOT_AUTHORIZED", "detail": "no token"})
    assert unauth["state"] == bp.AUTH_ERROR
    assert unauth["findings"] == []

    assert bp._integration_failure({"ok": False, "state": "NOT_CONFIGURED"})["state"] == bp.NOT_CONFIGURED
    assert bp._integration_failure({"ok": False, "state": "RATE_LIMITED"})["state"] == bp.RATE_LIMITED


def test_classify_failure_maps_real_error_text():
    assert bp.classify_failure(RuntimeError("invalid_scope: Bad Request")) == bp.AUTH_ERROR
    assert bp.classify_failure(RuntimeError("429 rate limit exceeded")) == bp.RATE_LIMITED
    assert bp.classify_failure(TimeoutError("connection timed out")) == bp.TRANSIENT_FAILURE
    assert bp.classify_failure(ValueError("bad payload")) == bp.FAILED


# ── ITEM 6: CALENDAR ─────────────────────────────────────────────────────

def test_calendar_creates_prep_tasks_only_for_real_meetings(monkeypatch):
    from actions import calendar_integration

    monkeypatch.setattr(calendar_integration, "list_upcoming_events", lambda **k: {
        "ok": True,
        "events": [
            {"id": "ev-real", "summary": "Client review", "start": "2026-09-08T15:00:00Z",
             "attendees": [{"email": "a@x.com"}, {"email": "b@x.com"}]},
            {"id": "ev-solo", "summary": "Focus block", "start": "2026-09-08T09:00:00Z",
             "attendees": []},
        ],
    })

    result = bp.calendar_findings()
    assert result["state"] == bp.SUCCESS
    ids = [f["subject_id"] for f in result["findings"]]
    assert ids == ["ev-real"], "a solo focus block must not generate a preparation task"
    assert result["findings"][0]["kind"] == "calendar_prep"


def test_calendar_auth_failure_is_reported_not_faked(monkeypatch):
    from actions import calendar_integration
    monkeypatch.setattr(calendar_integration, "list_upcoming_events", lambda **k: {
        "ok": False, "state": "NOT_AUTHORIZED", "detail": "token expired", "events": []})

    result = bp.calendar_findings()
    assert result["state"] == bp.AUTH_ERROR
    assert result["findings"] == []


# ── ITEM 5: GMAIL ────────────────────────────────────────────────────────

def _gmail_stub(monkeypatch, messages, categories):
    from actions import gmail_integration
    from actions import email_classification as ec
    monkeypatch.setattr(gmail_integration, "list_messages",
                        lambda **k: {"ok": True, "messages": [{"id": m["id"]} for m in messages]})
    monkeypatch.setattr(gmail_integration, "get_message",
                        lambda mid: next(m for m in messages if m["id"] == mid))
    monkeypatch.setattr(ec, "classify_email",
                        lambda m: {"category": categories[m["id"]], "confidence": 0.9})


def test_gmail_routes_actionable_mail_and_ignores_the_rest(monkeypatch):
    from actions import email_classification as ec
    messages = [
        {"id": "m1", "ok": True, "subject": "Deal enquiry"},
        {"id": "m2", "ok": True, "subject": "Newsletter"},
        {"id": "m3", "ok": True, "subject": "Candidate CV"},
    ]
    _gmail_stub(monkeypatch, messages, {
        "m1": ec.CATEGORY_DDF,
        "m2": ec.CATEGORY_IRRELEVANT,
        "m3": ec.CATEGORY_BUILDPRO,
    })

    result = bp.gmail_findings()
    assert result["state"] == bp.SUCCESS
    ids = {f["subject_id"] for f in result["findings"]}
    assert ids == {"m1"}, "only the actionable, non-BuildPro message should become work"
    assert result["findings"][0]["agent_id"] == "daily_deal_finder_agent"


def test_gmail_buildpro_mail_is_left_to_the_buildpro_intake_agents(monkeypatch):
    """Two systems creating BuildPro CRM records is exactly the duplicate
    the router was built to avoid — the pipeline must not become a second."""
    from actions import email_classification as ec
    messages = [{"id": "b1", "ok": True, "subject": "New candidate"}]
    _gmail_stub(monkeypatch, messages, {"b1": ec.CATEGORY_BUILDPRO})
    assert bp.gmail_findings()["findings"] == []


def test_gmail_unauthorized_is_an_auth_error_not_an_empty_inbox(monkeypatch):
    from actions import gmail_integration
    monkeypatch.setattr(gmail_integration, "list_messages", lambda **k: {
        "ok": False, "state": "NOT_AUTHORIZED", "detail": "invalid_scope: Bad Request",
        "messages": []})

    result = bp.gmail_findings()
    assert result["state"] == bp.AUTH_ERROR, (
        "a broken Gmail connection must never read as a successful quiet inbox"
    )
    assert result["findings"] == []


# ── ITEM 8: HUBSPOT ──────────────────────────────────────────────────────

def test_hubspot_uses_real_record_ids_as_identity(monkeypatch):
    from actions import hubspot_integration as hs
    monkeypatch.setattr(hs, "is_configured", lambda: True)
    monkeypatch.setattr(hs, "get_companies", lambda **k: {"ok": True, "results": [
        {"id": "12345", "properties": {"name": "Acme Construction", "domain": "acme.com"}},
        {"id": "67890", "properties": {}},   # no name — not actionable
    ]})

    result = bp.hubspot_findings()
    assert result["state"] == bp.SUCCESS
    assert [f["subject_id"] for f in result["findings"]] == ["12345"]
    assert result["findings"][0]["agent_id"] == "buildpro_prospecting_agent"


def test_hubspot_without_a_token_is_not_configured(monkeypatch):
    from actions import hubspot_integration as hs
    monkeypatch.setattr(hs, "is_configured", lambda: False)
    result = bp.hubspot_findings()
    assert result["state"] == bp.NOT_CONFIGURED
    assert result["findings"] == []


# ── ITEM 9: BUILDPRO ─────────────────────────────────────────────────────

def test_buildpro_only_promotes_strong_matches(monkeypatch):
    from actions import buildpro_intelligence
    monkeypatch.setattr(buildpro_intelligence, "generate_morning_report_data", lambda: {
        "top_matches": [
            {"candidate_name": "Dana Reyes", "job_title": "Superintendent", "score": 88},
            {"candidate_name": "Sam Lee", "job_title": "Estimator", "score": 41},
        ]})

    result = bp.buildpro_findings(min_score=70)
    assert len(result["findings"]) == 1
    assert "Dana Reyes" in result["findings"][0]["title"]


def test_buildpro_match_identity_is_stable_across_runs(monkeypatch):
    """The same pairing must produce the same key every sweep, or the
    ledger cannot stop duplicate outreach."""
    from actions import buildpro_intelligence
    data = {"top_matches": [{"candidate_name": "Dana Reyes", "job_title": "Superintendent", "score": 88}]}
    monkeypatch.setattr(buildpro_intelligence, "generate_morning_report_data", lambda: data)

    first = bp.buildpro_findings()["findings"][0]["subject_id"]
    second = bp.buildpro_findings()["findings"][0]["subject_id"]
    assert first == second and len(first) == 32


# ── ITEMS 10 + 11: DDF -> SOCIAL ─────────────────────────────────────────

def test_high_ticket_products_become_content_opportunities(monkeypatch):
    from actions import daily_deal_finders as ddf
    monkeypatch.setattr(ddf, "select_daily_high_ticket_picks", lambda **k: [
        {"id": "prod-1", "name": "Laser Level Kit", "price": 349.0}])

    result = bp.ddf_content_findings()
    assert result["state"] == bp.SUCCESS
    f = result["findings"][0]
    assert f["agent_id"] == "social_content_agent"
    assert "[product:prod-1]" in f["description"], (
        "the social agent parses this marker to know which product to prepare"
    )


def test_social_agent_prepares_content_but_never_publishes(monkeypatch):
    """Publishing is a real external side effect and stays behind the
    approval gate — an OBSERVE sweep may draft, never post."""
    from actions import daily_deal_finders as ddf
    from actions import buffer_integration

    published = {"n": 0}
    monkeypatch.setattr(buffer_integration, "verify_buffer",
                        lambda: {"status": "ok", "configured": True})
    monkeypatch.setattr(buffer_integration, "publish_to_buffer",
                        lambda *a, **k: published.__setitem__("n", published["n"] + 1))
    monkeypatch.setattr(ddf, "get_product", lambda pid: {"id": pid, "name": "Laser Level Kit"})
    monkeypatch.setattr(ddf, "prepare_post", lambda p: {"text": f"Check out {p['name']}"})

    orch = ao.AgentOrchestrator(agents={"social_content_agent": ao.BUILTIN_AGENTS["social_content_agent"]})
    task = orch.assign_task("social_content_agent", "Prepare content [product:prod-1]")

    assert task.status == ao.TaskStatus.DONE
    assert task.result["prepared_post"]["text"] == "Check out Laser Level Kit"
    assert task.result["requires_approval_to_publish"] is True
    assert published["n"] == 0, "content was published without an approval gate"


def test_social_agent_falls_back_to_connectivity_when_no_product(monkeypatch):
    from actions import buffer_integration
    monkeypatch.setattr(buffer_integration, "verify_buffer",
                        lambda: {"status": "not_configured", "configured": False})
    orch = ao.AgentOrchestrator(agents={"social_content_agent": ao.BUILTIN_AGENTS["social_content_agent"]})
    task = orch.assign_task("social_content_agent", "Routine check")
    assert "Buffer" in task.result["summary"]
    assert "prepared_post" not in task.result


# ── ITEM 12: RESEARCH ────────────────────────────────────────────────────

def test_research_opportunities_become_tasks(monkeypatch):
    from actions import opportunity_engine
    monkeypatch.setattr(opportunity_engine, "rank_opportunities", lambda **k: [
        {"id": "opp-1", "title": "Contractors hiring supers in DFW", "score": 82}])
    result = bp.research_findings()
    assert result["findings"][0]["agent_id"] == "business_research_agent"
    assert result["findings"][0]["subject_id"] == "opp-1"


# ── DISPATCH, DEDUP, ISOLATION, APPROVAL ─────────────────────────────────

def test_a_finding_creates_a_task_exactly_once_across_sweeps():
    orch = _orch(business_research_agent=ao.PermissionLevel.OBSERVE)
    finding = bp._finding("research_opportunity", "opp-stable", "Research: X",
                          "business_research_agent", "do research")

    first = bp.dispatch_findings([finding], orchestrator=orch)
    second = bp.dispatch_findings([finding], orchestrator=orch)

    assert len(first) == 1 and first[0]["task_id"]
    assert second == [], "the same subject generated duplicate work on a second sweep"


def test_a_failed_dispatch_releases_its_claim_so_the_work_is_not_lost():
    orch = _orch(business_research_agent=ao.PermissionLevel.OBSERVE)
    finding = bp._finding("research_opportunity", "opp-retry", "Research: Y",
                          "unknown_agent_id", "do research")

    assert bp.dispatch_findings([finding], orchestrator=orch) == []
    assert ledger.already_handled("research_opportunity", "opp-retry") is False, (
        "a subject whose dispatch failed must remain claimable, not be silently swallowed"
    )


def test_dispatch_preserves_the_approval_gate():
    orch = _orch(risky_agent=ao.PermissionLevel.EXECUTE)
    finding = bp._finding("hubspot_opportunity", "co-1", "Outreach", "risky_agent", "send outreach")
    created = bp.dispatch_findings([finding], orchestrator=orch)
    assert created[0]["task_status"] == ao.TaskStatus.PENDING_APPROVAL.value, (
        "EXECUTE-level work must not auto-run just because the pipeline created it"
    )


def test_one_broken_source_does_not_stop_the_others():
    orch = _orch(business_research_agent=ao.PermissionLevel.OBSERVE)

    def _broken():
        raise RuntimeError("invalid_scope: Bad Request")

    def _working():
        return {"state": bp.SUCCESS, "findings": [bp._finding(
            "research_opportunity", "opp-iso", "Research: Z",
            "business_research_agent", "do research")], "scanned": 1}

    report = bp.gather_and_dispatch(
        sources={"broken": _broken, "working": _working}, orchestrator=orch)

    assert report["states"]["broken"] == bp.AUTH_ERROR
    assert report["states"]["working"] == bp.SUCCESS
    assert report["total_dispatched"] == 1, "a broken source blocked a healthy one"


# ── END TO END ───────────────────────────────────────────────────────────

def test_discover_to_task_to_agent_to_execute_to_verify_to_record(monkeypatch):
    """DISCOVER -> TASK -> AGENT -> EXECUTE -> VERIFY -> RECORD, with no
    manual prompt anywhere: a real product goes in, and a completed,
    recorded, verified agent task comes out."""
    from actions import daily_deal_finders as ddf
    from actions import buffer_integration

    monkeypatch.setattr(ddf, "select_daily_high_ticket_picks", lambda **k: [
        {"id": "prod-e2e", "name": "Rotary Hammer", "price": 429.0}])
    monkeypatch.setattr(ddf, "get_product", lambda pid: {"id": pid, "name": "Rotary Hammer"})
    monkeypatch.setattr(ddf, "prepare_post", lambda p: {"text": f"Deal: {p['name']}"})
    monkeypatch.setattr(buffer_integration, "verify_buffer",
                        lambda: {"status": "ok", "configured": True})

    orch = ao.AgentOrchestrator(agents={
        "social_content_agent": ao.BUILTIN_AGENTS["social_content_agent"]})

    # DISCOVER
    found = bp.ddf_content_findings()
    assert found["state"] == bp.SUCCESS and found["findings"]

    # TASK -> AGENT -> EXECUTE
    dispatched = bp.dispatch_findings(found["findings"], orchestrator=orch)
    assert len(dispatched) == 1
    task = orch.get_task(dispatched[0]["task_id"])

    assert task.status == ao.TaskStatus.DONE
    assert task.attempt == 1 and task.started_ts and task.completed_ts
    # the agent did the real work, not a stub
    assert task.result["prepared_post"]["text"] == "Deal: Rotary Hammer"
    assert task.result["requires_approval_to_publish"] is True
    # RECORD — the product is claimed, so tomorrow's sweep will not redo it
    assert ledger.already_handled("ddf_content", "prod-e2e") is True
    assert orch._agents["social_content_agent"].status == ao.AgentStatus.IDLE


def test_ceo_cycle_actually_runs_the_business_pipeline(monkeypatch):
    """The pipeline is only useful if the WAKE path calls it. This proves
    the CEO cycle dispatches business findings and reports degraded sources
    in the morning brief rather than hiding them."""
    from actions import ceo_operating_cycle as cycle

    def _fake_gather_and_dispatch():
        return {
            "sources": {"gmail": {"state": bp.AUTH_ERROR, "found": 0}},
            "states": {"gmail": bp.AUTH_ERROR, "ddf_content": bp.SUCCESS},
            "dispatched": [{"kind": "ddf_content", "subject_id": "p1", "title": "t",
                            "agent_id": "social_content_agent", "task_id": "x",
                            "task_status": "done"}],
            "total_dispatched": 1, "healthy_sources": 1,
        }

    monkeypatch.setattr(cycle.business_pipeline, "gather_and_dispatch", _fake_gather_and_dispatch)
    monkeypatch.setattr(cycle, "_deliver_report",
                        lambda run_date, summary: {"action": "none", "configured": False})

    result = cycle.run_cycle(force=True)

    assert result["state"] == "RAN"
    assert "Created 1 new task(s) from business findings" in result["summary"]
    assert "gmail=AUTH_ERROR" in result["summary"], (
        "a degraded business source must be named in the brief, not quietly omitted"
    )


def test_ceo_cycle_survives_a_business_pipeline_failure(monkeypatch):
    from actions import ceo_operating_cycle as cycle
    monkeypatch.setattr(cycle.business_pipeline, "gather_and_dispatch",
                        lambda: (_ for _ in ()).throw(RuntimeError("pipeline exploded")))
    monkeypatch.setattr(cycle, "_deliver_report",
                        lambda run_date, summary: {"action": "none", "configured": False})

    result = cycle.run_cycle(force=True)
    assert result["state"] == "RAN", "a pipeline failure must not abort the whole cycle"
