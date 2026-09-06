"""Phase 4 Part 16 — completing the /ui console's data connections for
BuildPro, DDF, and Intelligence. Verifies these hit real actions/*
functions (not mock data) through the actual authenticated HTTP path.
"""
import pytest
from fastapi.testclient import TestClient

from core.headless import config


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    from actions import buildpro_data, daily_deal_finders, business_intelligence, opportunity_engine
    monkeypatch.setattr(buildpro_data, "DB_PATH", tmp_path / "buildpro.db")
    monkeypatch.setattr(daily_deal_finders, "DB_PATH", tmp_path / "ddf.db")
    monkeypatch.setattr(business_intelligence, "DB_PATH", tmp_path / "bi.db")
    monkeypatch.setattr(opportunity_engine, "DB_PATH", tmp_path / "opp.db")


def _client(monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", "test-ui-token-not-a-real-secret")
    from core.headless.app import create_app
    app = create_app(start_background_worker=False)
    client = TestClient(app, base_url="https://testserver")
    client.post("/ui/login", json={"token": "test-ui-token-not-a-real-secret"})
    return client


def test_buildpro_overview_reflects_real_data(monkeypatch, gmail_not_authorized):
    from actions import buildpro_data
    buildpro_data.add_candidate("Jane Welder", skills="welding")
    client = _client(monkeypatch)
    r = client.get("/ui/api/buildpro-overview")
    assert r.status_code == 200
    body = r.json()
    assert len(body["candidates"]) == 1
    assert body["candidates"][0]["name"] == "Jane Welder"


def test_buildpro_overview_honest_when_empty(monkeypatch, gmail_not_authorized):
    client = _client(monkeypatch)
    body = client.get("/ui/api/buildpro-overview").json()
    assert body["candidates"] == []
    assert body["clients"] == []


def test_ddf_overview_reflects_real_data(monkeypatch):
    from actions import daily_deal_finders as ddf
    ddf.save_product({
        "name": "Test Product", "source": "amazon", "category": "gadgets",
        "price": 49.99, "current_price": 49.99, "url": "https://x.com",
        "retailer": "amazon", "product_id": "t1", "approved": True,
    })
    client = _client(monkeypatch)
    body = client.get("/ui/api/ddf-overview").json()
    assert len(body["todays_deals"]) == 1
    assert body["todays_deals"][0]["product_id"] == "t1"


def test_ddf_overview_honest_when_empty(monkeypatch):
    client = _client(monkeypatch)
    body = client.get("/ui/api/ddf-overview").json()
    assert body["high_ticket_picks"] == []
    assert body["todays_deals"] == []


def test_intelligence_reflects_real_data(monkeypatch):
    from actions import business_intelligence as bi
    bi.add_entry("research", "buildpro", "Test finding", content="details")
    client = _client(monkeypatch)
    body = client.get("/ui/api/intelligence").json()
    assert len(body["recent_entries"]) == 1
    assert body["recent_entries"][0]["title"] == "Test finding"


def test_all_new_endpoints_require_a_session(monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", "test-ui-token-not-a-real-secret")
    from core.headless.app import create_app
    app = create_app(start_background_worker=False)
    client = TestClient(app, base_url="https://testserver")
    for path in ("/ui/api/buildpro-overview", "/ui/api/ddf-overview", "/ui/api/intelligence", "/ui/api/priorities", "/ui/api/active-agents", "/ui/api/opportunities", "/ui/api/calendar-snapshot", "/ui/api/settings"):
        r = client.get(path)
        assert r.status_code == 401, f"{path} should require a session"
