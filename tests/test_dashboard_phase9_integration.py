from fastapi.testclient import TestClient

from dashboard.server import DashboardServer

# Matches conftest.py's _dashboard_api_token autouse fixture — see that
# fixture's docstring for why /3d/api/* needs it now.
_AUTH_HEADERS = {"Authorization": "Bearer test-dashboard-token-not-a-real-secret"}


def test_overview_includes_strategic_objective():
    server = DashboardServer()
    client = TestClient(server.app, headers=_AUTH_HEADERS)
    payload = client.get("/3d/api/overview").json()
    assert "strategic_objective" in payload
    assert payload["strategic_objective"]["target_amount_usd"] == 1_000_000


def test_system_module_includes_objective_and_business_intelligence(monkeypatch, tmp_path):
    from actions import business_intelligence as bi
    from actions import strategic_objective as so
    monkeypatch.setattr(bi, "DB_PATH", tmp_path / "bi.db")
    monkeypatch.setattr(so, "CONFIG_FILE", tmp_path / "objective.json")

    server = DashboardServer()
    client = TestClient(server.app, headers=_AUTH_HEADERS)
    data = client.get("/3d/api/module/system").json()["data"]
    assert data["strategic_objective"]["target_amount_usd"] == 1_000_000
    assert "counts" in data["business_intelligence"]


def test_buildpro_module_includes_business_intelligence_and_opportunities(monkeypatch, tmp_path):
    from actions import business_intelligence as bi
    from actions import opportunity_engine as oe
    monkeypatch.setattr(bi, "DB_PATH", tmp_path / "bi.db")
    monkeypatch.setattr(oe, "DB_PATH", tmp_path / "opp.db")
    bi.add_entry("research", "buildpro", "Competitor scan")
    oe.add_opportunity("buildpro", "long_term", "Retainer program", revenue_potential=5, probability=5)

    server = DashboardServer()
    client = TestClient(server.app, headers=_AUTH_HEADERS)
    data = client.get("/3d/api/module/buildpro").json()["data"]
    assert data["business_intelligence"]["counts"]["research"] == 1
    assert data["top_opportunities"][0]["title"] == "Retainer program"


def test_ddf_module_still_has_top_products_alongside_business_intelligence(monkeypatch, tmp_path):
    from actions import business_intelligence as bi
    from actions import opportunity_engine as oe
    monkeypatch.setattr(bi, "DB_PATH", tmp_path / "bi.db")
    monkeypatch.setattr(oe, "DB_PATH", tmp_path / "opp.db")

    server = DashboardServer()
    client = TestClient(server.app, headers=_AUTH_HEADERS)
    data = client.get("/3d/api/module/ddf").json()["data"]
    assert "top_products" in data          # pre-existing behavior preserved
    assert "business_intelligence" in data  # Phase 9 addition
