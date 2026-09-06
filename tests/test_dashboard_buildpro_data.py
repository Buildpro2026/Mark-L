from fastapi.testclient import TestClient

from dashboard.server import DashboardServer
from actions import buildpro_data as bd

# Matches conftest.py's _dashboard_api_token autouse fixture — see that
# fixture's docstring for why /3d/api/* needs it now.
_AUTH_HEADERS = {"Authorization": "Bearer test-dashboard-token-not-a-real-secret"}


def _client(monkeypatch, tmp_path):
    monkeypatch.setattr(bd, "DB_PATH", tmp_path / "buildpro.db")
    server = DashboardServer()
    return TestClient(server.app, headers=_AUTH_HEADERS)


def test_buildpro_module_includes_recruiting_summary(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    bd.add_candidate("Jane Doe")
    bd.add_client("Acme Co")
    bd.add_job("Superintendent", status="open")

    data = client.get("/3d/api/module/buildpro").json()["data"]
    assert "buildpro_recruiting" in data
    rec = data["buildpro_recruiting"]
    assert rec["candidate_count"] == 1
    assert rec["client_count"] == 1
    assert rec["active_jobs"] == 1
    # Phase 9 fields must still be present — this addition shouldn't replace them
    assert "business_intelligence" in data


def test_candidates_module_returns_real_candidate_list(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    bd.add_candidate("Alice", email="alice@example.com")
    bd.add_candidate("Bob", email="bob@example.com")

    data = client.get("/3d/api/module/candidates").json()["data"]
    assert len(data["results"]) == 2
    names = {r["name"] for r in data["results"]}
    assert names == {"Alice", "Bob"}
    assert "2 candidate" in data["summary"]


def test_clients_module_returns_real_client_list(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    bd.add_client("BuilderCo")

    data = client.get("/3d/api/module/clients").json()["data"]
    assert len(data["results"]) == 1
    assert data["results"][0]["name"] == "BuilderCo"


def test_jobs_module_returns_real_job_list(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    bd.add_job("Site Superintendent", location="Houston, TX")

    data = client.get("/3d/api/module/jobs").json()["data"]
    assert len(data["results"]) == 1
    assert data["results"][0]["title"] == "Site Superintendent"


def test_candidates_module_empty_state_is_honest_not_fabricated(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    data = client.get("/3d/api/module/candidates").json()["data"]
    assert data["results"] == []
    assert "0 candidate" in data["summary"]


def test_buildpro_module_reflects_qualified_matches(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    cand = bd.add_candidate("Strong Candidate")
    job = bd.add_job("Great Job")
    bd.add_match(cand, job, match_score=95, match_rationale="Perfect fit")

    data = client.get("/3d/api/module/buildpro").json()["data"]
    rec = data["buildpro_recruiting"]
    assert rec["qualified_matches"] == 1
    assert rec["highest_match_scores"][0]["candidate_name"] == "Strong Candidate"


# ── recruiting-engine additions: prospects, matches, followups ───────────

def test_prospects_module_returns_only_prospect_status_clients(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    bd.add_client("Active Client", status="active")
    bd.add_client("A Prospect", status="prospect")

    data = client.get("/3d/api/module/prospects").json()["data"]
    assert len(data["results"]) == 1
    assert data["results"][0]["name"] == "A Prospect"


def test_clients_module_still_returns_all_statuses_unchanged(monkeypatch, tmp_path):
    """Pre-existing behavior preserved — the 'clients' endpoint contract
    from the previous session isn't changed by adding 'prospects'."""
    client = _client(monkeypatch, tmp_path)
    bd.add_client("Active Client", status="active")
    bd.add_client("A Prospect", status="prospect")

    data = client.get("/3d/api/module/clients").json()["data"]
    assert len(data["results"]) == 2


def test_matches_module_returns_scored_matches_with_names(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    cand = bd.add_candidate("Match Candidate")
    job = bd.add_job("Match Job")
    bd.add_match(cand, job, match_score=77)

    data = client.get("/3d/api/module/matches").json()["data"]
    assert len(data["results"]) == 1
    assert data["results"][0]["candidate_name"] == "Match Candidate"
    assert data["results"][0]["job_title"] == "Match Job"


def test_matches_module_empty_state_is_honest(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    data = client.get("/3d/api/module/matches").json()["data"]
    assert data["results"] == []
    assert "0 candidate/job match" in data["summary"]


def test_buildpro_module_includes_prospect_count(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    bd.add_client("A Prospect", status="prospect")

    data = client.get("/3d/api/module/buildpro").json()["data"]
    assert data["buildpro_recruiting"]["prospect_count"] == 1


def test_buildpro_module_includes_followup_lists(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    stale = bd.add_candidate("Stale Candidate", status="new")
    import time
    conn = bd._connect()
    conn.execute("UPDATE buildpro_candidates SET updated_ts = ? WHERE id = ?", (time.time() - 30 * 86400, stale))
    conn.commit()
    conn.close()

    data = client.get("/3d/api/module/buildpro").json()["data"]
    assert "buildpro_followups" in data
    assert len(data["buildpro_followups"]["candidates"]) == 1


def test_buildpro_nucleus_hierarchy_includes_prospects_and_matches_children():
    """The 3D scene renders child nuclei from the hierarchy config — new
    Prospects/Matches nodes must be reachable the same way Clients/
    Candidates/Jobs already are, without any frontend changes."""
    from actions.nucleus_hierarchy import get_hierarchy_children
    children = {c["id"] for c in get_hierarchy_children("buildpro")}
    assert {"clients", "candidates", "jobs", "prospects", "matches"} <= children
