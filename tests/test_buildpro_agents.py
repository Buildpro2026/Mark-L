import pytest

from actions import agent_orchestrator as ao
from actions import buildpro_data as bd
from actions import hubspot_integration as hubspot


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(bd, "DB_PATH", tmp_path / "test_agents.db")
    cfg_path = tmp_path / "api_keys.json"
    cfg_path.write_text('{"hubspot_token": "test-token"}', encoding="utf-8")
    monkeypatch.setattr(hubspot, "CONFIG_PATH", cfg_path)


def _task(description=""):
    return ao.AgentTask(id="t1", agent_id="test", description=description)


# ── BuildPro Candidate Agent ─────────────────────────────────────────────

def test_candidate_agent_returns_overview_for_generic_description():
    bd.add_candidate("Alice")
    bd.add_candidate("Bob")
    result = ao._buildpro_candidate_agent_handler(_task("Scheduled background check"))
    assert len(result["candidates"]) == 2
    assert result["filtered_by"] is None


def test_candidate_agent_returns_overview_for_empty_description():
    bd.add_candidate("Alice")
    result = ao._buildpro_candidate_agent_handler(_task(""))
    assert len(result["candidates"]) == 1


def test_candidate_agent_filters_by_skill_keyword():
    bd.add_candidate("Welder", skills="welding, blueprint reading")
    bd.add_candidate("Plumber", skills="plumbing")
    result = ao._buildpro_candidate_agent_handler(_task("welding"))
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["name"] == "Welder"
    assert result["filtered_by"] == "welding"


def test_candidate_agent_falls_back_to_location_then_title_search():
    bd.add_candidate("Houston Electrician", title="Electrician", location="Houston, TX")
    result = ao._buildpro_candidate_agent_handler(_task("Houston"))
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["name"] == "Houston Electrician"


def test_candidate_agent_never_invents_a_candidate_when_none_match():
    bd.add_candidate("Someone", skills="welding")
    result = ao._buildpro_candidate_agent_handler(_task("nonexistent-skill-xyz"))
    assert result["candidates"] == []


def test_candidate_agent_reports_followups():
    stale = bd.add_candidate("Stale", status="new")
    import time
    conn = bd._connect()
    conn.execute("UPDATE buildpro_candidates SET updated_ts = ? WHERE id = ?", (time.time() - 30 * 86400, stale))
    conn.commit()
    conn.close()
    result = ao._buildpro_candidate_agent_handler(_task())
    assert len(result["needs_followup"]) == 1


def test_candidate_agent_runs_through_orchestrator_at_observe_level():
    """Confirms this stays wired into the real OBSERVE -> SUGGEST -> EXECUTE
    permission model — an OBSERVE agent's task auto-runs and completes."""
    bd.add_candidate("Test Candidate")
    orch = ao.AgentOrchestrator()
    agent = orch.get_agent("buildpro_candidate_agent")
    assert agent.permission_level == ao.PermissionLevel.OBSERVE
    task = orch.assign_task("buildpro_candidate_agent", "")
    assert task.status == ao.TaskStatus.DONE
    assert len(task.result["candidates"]) == 1


# ── BuildPro Prospecting Agent ───────────────────────────────────────────

def test_prospecting_agent_not_configured(monkeypatch, tmp_path):
    cfg_path = tmp_path / "empty.json"
    cfg_path.write_text('{"hubspot_token": ""}', encoding="utf-8")
    monkeypatch.setattr(hubspot, "CONFIG_PATH", cfg_path)
    result = ao._buildpro_prospecting_agent_handler(_task())
    assert result["configured"] is False
    assert result["prospects"] == []


def test_prospecting_agent_creates_prospect_clients_from_hubspot(monkeypatch):
    companies = {
        "results": [
            {"id": "hs-co-1", "properties": {"name": "Acme Construction", "industry": "Construction", "phone": ""}},
        ],
    }
    monkeypatch.setattr(hubspot, "get_companies", lambda limit=100, after=None: {"ok": True, **companies})

    result = ao._buildpro_prospecting_agent_handler(_task())
    assert result["configured"] is True
    assert len(result["prospects"]) == 1
    assert result["prospects"][0]["name"] == "Acme Construction"
    assert result["prospects"][0]["likely_construction_related"] is True

    stored = bd.list_clients(status="prospect")
    assert len(stored) == 1
    assert stored[0]["name"] == "Acme Construction"


def test_prospecting_agent_skips_companies_already_tracked_locally(monkeypatch):
    bd.add_client("Already Tracked", hubspot_company_id="hs-co-1", status="active")
    companies = {"results": [{"id": "hs-co-1", "properties": {"name": "Already Tracked", "industry": ""}}]}
    monkeypatch.setattr(hubspot, "get_companies", lambda limit=100, after=None: {"ok": True, **companies})

    result = ao._buildpro_prospecting_agent_handler(_task())
    assert result["prospects"] == []
    # still only the one original client — nothing duplicated
    assert len(bd.list_clients()) == 1


def test_prospecting_agent_does_not_flag_unrelated_industries_as_construction(monkeypatch):
    companies = {"results": [{"id": "hs-co-1", "properties": {"name": "Software Inc", "industry": "Software"}}]}
    monkeypatch.setattr(hubspot, "get_companies", lambda limit=100, after=None: {"ok": True, **companies})

    result = ao._buildpro_prospecting_agent_handler(_task())
    assert result["prospects"][0]["likely_construction_related"] is False


def test_prospecting_agent_reports_hubspot_fetch_error_honestly(monkeypatch):
    monkeypatch.setattr(hubspot, "get_companies", lambda limit=100, after=None: {"ok": False, "detail": "401 unauthorized"})
    result = ao._buildpro_prospecting_agent_handler(_task())
    assert result["configured"] is True
    assert result["prospects"] == []
    assert "401" in result["error"]


def test_prospecting_agent_never_sends_outreach_only_prepares_data(monkeypatch):
    """No outbound communication happens here — no Twilio/email call is
    ever made by this handler, only local buildpro_clients writes."""
    companies = {"results": [{"id": "hs-co-1", "properties": {"name": "Acme Co", "industry": "Construction"}}]}
    monkeypatch.setattr(hubspot, "get_companies", lambda limit=100, after=None: {"ok": True, **companies})
    result = ao._buildpro_prospecting_agent_handler(_task())
    assert "sent" not in result["summary"].lower()
    assert "message" not in result


def test_prospecting_agent_runs_through_orchestrator_at_observe_level(monkeypatch):
    monkeypatch.setattr(hubspot, "get_companies", lambda limit=100, after=None: {"ok": True, "results": []})
    orch = ao.AgentOrchestrator()
    agent = orch.get_agent("buildpro_prospecting_agent")
    assert agent.permission_level == ao.PermissionLevel.OBSERVE
    task = orch.assign_task("buildpro_prospecting_agent", "")
    assert task.status == ao.TaskStatus.DONE
