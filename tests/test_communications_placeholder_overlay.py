"""dashboard/server.py: the 3D UI's sphere/list rendering for the
Communications nucleus (Phone/SMS/Calls/Contacts/Notifications) used to read
a hardcoded "placeholder": true from config/nucleus_config.json that never
changed — even after real Twilio credentials were added, those nodes would
stay permanently styled as dim/"coming soon" wireframes, contradicting the
module-detail panel (which already computed this correctly and dynamically).

These tests confirm the fix: placeholder state is now computed live from
actions/twilio_integration.get_status(), in both /3d/api/overview (the
hierarchy tree driving the 3D scene) and /3d/api/module/communications (the
detail-panel sidebar children list) — not read from the static config file.
"""
from fastapi.testclient import TestClient

from dashboard.server import DashboardServer

# Matches conftest.py's _dashboard_api_token autouse fixture — see that
# fixture's docstring for why /3d/api/* needs it now.
_AUTH_HEADERS = {"Authorization": "Bearer test-dashboard-token-not-a-real-secret"}


def _comm_children_from_overview(payload: dict) -> list[dict]:
    for domain in payload["hierarchy"]["children"]:
        if domain["id"] == "communications":
            return domain["children"]
    raise AssertionError("communications domain missing from hierarchy")


def test_overview_and_module_children_are_placeholders_when_not_configured():
    # config/api_keys.json in this repo ships with no "twilio" key —
    # NOT_CONFIGURED is the real, honest state right now.
    server = DashboardServer()
    client = TestClient(server.app, headers=_AUTH_HEADERS)

    overview = client.get("/3d/api/overview").json()
    comm_children = _comm_children_from_overview(overview)
    assert comm_children, "expected 5 communications category nodes"
    assert all(c["placeholder"] is True for c in comm_children)

    module = client.get("/3d/api/module/communications").json()
    assert all(c["placeholder"] is True for c in module["data"]["children"])


def test_overview_and_module_children_flip_to_live_when_twilio_is_configured(monkeypatch):
    import dashboard.server as server_mod

    monkeypatch.setattr(
        server_mod.twilio, "get_status",
        lambda: {"state": "CONFIGURED", "detail": "ok", "from_number": "+15551234567"},
    )

    server = DashboardServer()
    client = TestClient(server.app, headers=_AUTH_HEADERS)

    overview = client.get("/3d/api/overview").json()
    comm_children = _comm_children_from_overview(overview)
    assert all(c["placeholder"] is False for c in comm_children)

    module = client.get("/3d/api/module/communications").json()
    assert all(c["placeholder"] is False for c in module["data"]["children"])
    assert module["data"]["configured"] is True


def test_overlay_is_false_closed_when_twilio_module_failed_to_import(monkeypatch):
    import dashboard.server as server_mod

    monkeypatch.setattr(server_mod, "twilio", None)

    server = DashboardServer()
    client = TestClient(server.app, headers=_AUTH_HEADERS)

    overview = client.get("/3d/api/overview").json()
    comm_children = _comm_children_from_overview(overview)
    assert all(c["placeholder"] is True for c in comm_children)
