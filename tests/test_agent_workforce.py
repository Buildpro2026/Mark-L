from actions import agent_orchestrator as ao

EXPECTED_AGENT_IDS = {
    "buildpro_email_monitor", "buildpro_candidate_intake", "business_research_agent", "opportunity_scout",
    "buildpro_prospecting_agent", "buildpro_candidate_agent",
    "careerrocket_research_agent", "daily_deal_finder_agent",
    "social_content_agent", "market_intelligence_agent",
    "communications_agent", "revenue_tracking_agent",
    "system_monitor_agent", "jarvis_executive_analyst",
}


def test_full_workforce_is_registered():
    orch = ao.AgentOrchestrator()
    ids = {a.id for a in orch.list_agents()}
    assert EXPECTED_AGENT_IDS <= ids


def test_every_agent_has_identity_purpose_permissions_schedule_owner_business():
    orch = ao.AgentOrchestrator()
    for agent in orch.list_agents():
        assert agent.id and agent.name and agent.description
        assert isinstance(agent.permission_level, ao.PermissionLevel)
        assert agent.owner == "Lee"
        assert agent.business in {"buildpro", "careerrocket", "ddf", "general"}
        # schedule is optional (None = on-demand only) but must be present as a field
        assert hasattr(agent, "schedule")


def test_agent_error_state_starts_clear_and_sets_on_failure():
    def boom(task):
        raise RuntimeError("simulated failure")

    flaky = ao.AgentDefinition(
        id="flaky_test_agent", name="Flaky", description="x", nucleus_id="system",
        permission_level=ao.PermissionLevel.OBSERVE, handler=boom,
    )
    orch = ao.AgentOrchestrator(agents={"flaky_test_agent": flaky})
    assert orch.get_agent("flaky_test_agent").last_error is None

    orch.assign_task("flaky_test_agent", "do something")
    assert orch.get_agent("flaky_test_agent").last_error is not None
    assert orch.get_agent("flaky_test_agent").last_run_ts is not None


def test_agent_error_state_clears_after_a_subsequent_success():
    calls = {"n": 0}

    def sometimes_fails(task):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("first call fails")
        return {"summary": "ok"}

    agent = ao.AgentDefinition(
        id="recovering_agent", name="Recovering", description="x", nucleus_id="system",
        permission_level=ao.PermissionLevel.OBSERVE, handler=sometimes_fails,
    )
    orch = ao.AgentOrchestrator(agents={"recovering_agent": agent})
    orch.assign_task("recovering_agent", "attempt 1")
    assert orch.get_agent("recovering_agent").last_error is not None

    orch.assign_task("recovering_agent", "attempt 2")
    assert orch.get_agent("recovering_agent").last_error is None


# ── real-infrastructure handlers ─────────────────────────────────────────

def test_daily_deal_finder_agent_uses_real_get_top_products(monkeypatch):
    orch = ao.AgentOrchestrator()
    import actions.daily_deal_finders as ddf
    monkeypatch.setattr(ddf, "get_top_products", lambda limit=5: [{"name": "Widget", "price": "$19.99"}])

    task = orch.assign_task("daily_deal_finder_agent", "top deals")
    assert task.status == ao.TaskStatus.DONE
    assert task.result["top_products"][0]["name"] == "Widget"


def test_communications_agent_uses_real_twilio_status(monkeypatch, tmp_path):
    orch = ao.AgentOrchestrator()
    import actions.twilio_integration as tw
    monkeypatch.setattr(tw, "DB_PATH", tmp_path / "comm_agent_test.db")
    import json
    cfg_path = tmp_path / "api_keys.json"
    cfg_path.write_text(json.dumps({"twilio": {}}), encoding="utf-8")
    monkeypatch.setattr(tw, "API_CONFIG_PATH", cfg_path)

    task = orch.assign_task("communications_agent", "status check")
    assert task.status == ao.TaskStatus.DONE
    assert task.result["status"]["state"] == "NOT_CONFIGURED"


def test_revenue_tracking_agent_uses_real_strategic_objective(monkeypatch, tmp_path):
    orch = ao.AgentOrchestrator()
    import actions.strategic_objective as so
    monkeypatch.setattr(so, "CONFIG_FILE", tmp_path / "strategic_objective.json")
    import actions.business_intelligence as bi
    monkeypatch.setattr(bi, "DB_PATH", tmp_path / "bi_agent_test.db")

    task = orch.assign_task("revenue_tracking_agent", "progress check")
    assert task.status == ao.TaskStatus.DONE
    assert task.result["objective"]["target_amount_usd"] == 1_000_000


def test_system_monitor_agent_uses_real_get_system_status(monkeypatch):
    orch = ao.AgentOrchestrator()
    import actions.system_monitor as sm
    monkeypatch.setattr(sm, "get_system_status", lambda: {"cpu_percent": 12.3})

    task = orch.assign_task("system_monitor_agent", "status")
    assert task.status == ao.TaskStatus.DONE
    assert task.result["status"]["cpu_percent"] == 12.3


def test_prospecting_agent_reports_honestly_when_hubspot_not_configured(monkeypatch, tmp_path):
    """buildpro_prospecting_agent now has a real HubSpot-backed handler
    (see actions/agent_orchestrator.py::_buildpro_prospecting_agent_handler)
    — isolated here from both the real HubSpot token and the real local DB
    so this test can never touch either. With no token configured it must
    still report honestly rather than fabricating prospects."""
    import json
    import actions.hubspot_integration as hubspot
    import actions.buildpro_data as bd
    from core.headless import config as _hc
    cfg_path = tmp_path / "api_keys.json"
    cfg_path.write_text(json.dumps({"hubspot_token": ""}), encoding="utf-8")
    monkeypatch.setattr(hubspot, "CONFIG_PATH", cfg_path)
    # get_token() checks core.headless.config.HUBSPOT_TOKEN (the real env
    # var) FIRST — on a dev machine with a real .env this silently ignored
    # the empty-token config file above.
    monkeypatch.setattr(_hc, "HUBSPOT_TOKEN", None)
    monkeypatch.setattr(bd, "DB_PATH", tmp_path / "agent_test.db")

    orch = ao.AgentOrchestrator()
    task = orch.assign_task("buildpro_prospecting_agent", "find new clients")
    assert task.status == ao.TaskStatus.DONE
    assert task.result["configured"] is False
    assert task.result["prospects"] == []


def test_executive_analyst_synthesizes_objective_and_opportunities(monkeypatch, tmp_path):
    orch = ao.AgentOrchestrator()
    import actions.strategic_objective as so
    monkeypatch.setattr(so, "CONFIG_FILE", tmp_path / "strategic_objective.json")
    import actions.business_intelligence as bi
    monkeypatch.setattr(bi, "DB_PATH", tmp_path / "bi_exec_test.db")
    import actions.opportunity_engine as oe
    monkeypatch.setattr(oe, "DB_PATH", tmp_path / "opp_exec_test.db")
    oe.add_opportunity("ddf", "quick_cash", "Top Quick Idea", revenue_potential=5, probability=5)

    task = orch.assign_task("jarvis_executive_analyst", "executive brief")
    assert task.status == ao.TaskStatus.DONE
    assert "Top Quick Idea" in task.result["summary"]


def test_candidate_intake_agent_reports_honestly_when_gmail_not_authorized(monkeypatch):
    import actions.google_auth as google_auth
    monkeypatch.setattr(google_auth, "get_credential_status", lambda: {"authorized": False})

    orch = ao.AgentOrchestrator()
    task = orch.assign_task("buildpro_candidate_intake", "check for new candidates")
    assert task.status == ao.TaskStatus.DONE
    assert task.result["configured"] is False


def test_candidate_intake_agent_processes_only_candidate_reply_messages(monkeypatch):
    import actions.google_auth as google_auth
    import actions.gmail_integration as gmail
    import actions.candidate_intake as intake

    monkeypatch.setattr(google_auth, "get_credential_status", lambda: {"authorized": True})
    messages = [
        {"id": "m1", "sender": "jane@example.com", "subject": "Application - Electrician"},
        {"id": "m2", "sender": "vendor@service.com", "subject": "Your invoice is ready"},
    ]
    monkeypatch.setattr(gmail, "list_messages", lambda query, max_results: {"ok": True, "messages": messages})
    monkeypatch.setattr(gmail, "classify_message",
                         lambda m: "candidate_reply" if m["id"] == "m1" else "notification")

    processed_ids = []
    monkeypatch.setattr(intake, "process_candidate_email",
                         lambda msg, auto_send_welcome: (
                             processed_ids.append(msg["id"]),
                             {"ok": True, "welcome_email_drafted": True, "resume_attached_to_hubspot": False},
                         )[1])

    orch = ao.AgentOrchestrator()
    task = orch.assign_task("buildpro_candidate_intake", "check for new candidates")

    assert task.status == ao.TaskStatus.DONE
    assert processed_ids == ["m1"]  # the notification email (m2) was never processed
    assert task.result["configured"] is True
    assert len(task.result["processed"]) == 1


def test_candidate_intake_agent_reports_gmail_scan_failure_honestly(monkeypatch):
    import actions.google_auth as google_auth
    import actions.gmail_integration as gmail

    monkeypatch.setattr(google_auth, "get_credential_status", lambda: {"authorized": True})
    monkeypatch.setattr(gmail, "list_messages",
                         lambda query, max_results: {"ok": False, "state": "ERROR", "detail": "network down"})

    orch = ao.AgentOrchestrator()
    task = orch.assign_task("buildpro_candidate_intake", "check for new candidates")
    assert task.status == ao.TaskStatus.DONE
    assert task.result["error"] == "network down"
