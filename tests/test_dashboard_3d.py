from fastapi.testclient import TestClient

from dashboard.server import DashboardServer

# Matches conftest.py's _dashboard_api_token autouse fixture, which points
# core.headless.config.API_TOKEN at this same value for the whole test
# session — see that fixture's docstring for why /3d/api/*/ /3d/ws need it
# now (J2 closed the J1 finding that they had no auth at all).
_TEST_TOKEN = "test-dashboard-token-not-a-real-secret"
_AUTH_HEADERS = {"Authorization": f"Bearer {_TEST_TOKEN}"}


def test_three_d_page_serves_spatial_shell_markup():
    server = DashboardServer()
    client = TestClient(server.app, headers=_AUTH_HEADERS)

    response = client.get("/3d")

    assert response.status_code == 200
    html = response.text
    assert "jarvis-orb" in html
    assert "spatial-stage" in html
    # Real WebGL app, not another CSS/DOM prototype — must reference three.js
    # via an import map and load the actual scene module.
    assert "importmap" in html
    assert "/3d/assets/vendor/three.module.js" in html
    assert "/3d/assets/app.js" in html


def test_three_d_assets_are_served_including_vendored_threejs():
    server = DashboardServer()
    client = TestClient(server.app, headers=_AUTH_HEADERS)

    app_js = client.get("/3d/assets/app.js")
    assert app_js.status_code == 200
    assert "THREE" in app_js.text

    three_js = client.get("/3d/assets/vendor/three.module.js")
    assert three_js.status_code == 200
    assert three_js.headers["content-type"].startswith(("text/javascript", "application/javascript"))

    orbit_controls = client.get("/3d/assets/vendor/OrbitControls.js")
    assert orbit_controls.status_code == 200


def test_three_d_overview_endpoint_returns_module_data():
    server = DashboardServer()
    client = TestClient(server.app, headers=_AUTH_HEADERS)

    response = client.get("/3d/api/overview")

    assert response.status_code == 200
    payload = response.json()
    assert payload["focus"] == "core"
    assert any(module["id"] == "deals" for module in payload["modules"])
    assert payload["summary"]["module_count"] >= 7


def test_three_d_module_and_command_endpoints_dispatch_existing_actions():
    server = DashboardServer()
    client = TestClient(server.app, headers=_AUTH_HEADERS)

    module_response = client.get("/3d/api/module/deals")
    assert module_response.status_code == 200
    module_payload = module_response.json()
    assert module_payload["module"]["id"] == "deals"
    assert "top_products" in module_payload["data"]

    command_response = client.post(
        "/3d/api/command",
        json={"action": "system_status"},
    )
    assert command_response.status_code == 200
    command_payload = command_response.json()
    assert command_payload["ok"] is True
    assert "cpu_percent" in command_payload["result"]


def test_apply_navigation_open_back_home_and_status():
    server = DashboardServer()

    opened = server.apply_navigation("open", "buildpro")
    assert opened["nucleus_id"] == "buildpro"
    assert opened["name"] == "BuildPro"

    opened_again = server.apply_navigation("open", "email")
    assert opened_again["nucleus_id"] == "email"

    status = server.apply_navigation("status")
    assert status["nucleus_id"] == "email"   # status must not change state

    back = server.apply_navigation("back")
    assert back["nucleus_id"] == "buildpro"   # returns to the previous nucleus

    home = server.apply_navigation("home")
    assert home["nucleus_id"] == "jarvis"

    # home clears the back-stack — a further back() has nothing to return to
    still_home = server.apply_navigation("back")
    assert still_home["nucleus_id"] == "jarvis"


def test_three_d_command_navigate_action_updates_shared_state():
    server = DashboardServer()
    client = TestClient(server.app, headers=_AUTH_HEADERS)

    response = client.post(
        "/3d/api/command",
        json={"action": "navigate", "nav_action": "open", "nucleus_id": "ddf"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["result"]["nucleus_id"] == "ddf"
    assert server._nucleus_id == "ddf"   # server-side state actually moved


def test_three_d_ws_pushes_current_state_on_connect_and_applies_client_navigate():
    server = DashboardServer()
    server.apply_navigation("open", "buildpro")
    client = TestClient(server.app, headers=_AUTH_HEADERS)

    with client.websocket_connect(f"/3d/ws?token={_TEST_TOKEN}") as ws:
        initial = ws.receive_json()
        assert initial["type"] == "navigate"
        assert initial["nucleus_id"] == "buildpro"

        ws.send_json({"type": "navigate", "action": "open", "nucleus_id": "email"})
        pushed = ws.receive_json()
        assert pushed["type"] == "navigate"
        assert pushed["nucleus_id"] == "email"

    assert server._nucleus_id == "email"


def test_three_d_module_communications_is_a_clearly_labeled_placeholder():
    # config/api_keys.json ships with empty Twilio credentials, so this must
    # honestly report NOT_CONFIGURED — not fabricate readiness — even though
    # the Twilio integration module itself is real (see test_twilio_webhooks.py
    # and test_twilio_integration.py for the fully-configured/live-call paths).
    server = DashboardServer()
    client = TestClient(server.app, headers=_AUTH_HEADERS)

    response = client.get("/3d/api/module/communications")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["configured"] is False
    assert data["status"] == "NOT_CONFIGURED"
    assert "channels" in data
    assert data["channels"]["contacts"]["status"] == "placeholder"   # no contacts DB exists regardless of Twilio config


def test_three_d_module_ddf_includes_both_hierarchy_context_and_live_deal_data():
    # "ddf" is the real hierarchy id (used everywhere else); it must return
    # the same live deal data "deals" does, plus hierarchy node/children so
    # the spatial UI can render Products/Drafts as child objects.
    server = DashboardServer()
    client = TestClient(server.app, headers=_AUTH_HEADERS)

    response = client.get("/3d/api/module/ddf")
    assert response.status_code == 200
    data = response.json()["data"]
    assert "top_products" in data
    assert data["node"]["id"] == "ddf"
    assert any(child["id"] == "products" for child in data["children"])


def test_three_d_module_system_keeps_hierarchy_context_alongside_status():
    # Regression test: the "system" branch used to do `data = get_system_status()`,
    # silently discarding the hierarchy node/children/path attached earlier.
    server = DashboardServer()
    client = TestClient(server.app, headers=_AUTH_HEADERS)

    response = client.get("/3d/api/module/system")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["node"]["id"] == "system"
    assert "cpu_percent" in data


def test_three_d_module_system_surfaces_agent_orchestrator_status():
    # Task 3's Nucleus integration: agent status is visible through the
    # existing /3d/api/module/system endpoint, no new route needed.
    server = DashboardServer()
    client = TestClient(server.app, headers=_AUTH_HEADERS)

    response = client.get("/3d/api/module/system")
    assert response.status_code == 200
    agents_summary = response.json()["data"]["agents"]
    assert any(a["id"] == "buildpro_email_monitor" for a in agents_summary["agents"])


def test_three_d_module_endpoints_surface_files_reports_email_and_calendar_data():
    server = DashboardServer()
    client = TestClient(server.app, headers=_AUTH_HEADERS)

    files_response = client.get("/3d/api/module/files", params={"query": "py"})
    assert files_response.status_code == 200
    files_payload = files_response.json()
    assert files_payload["module"]["id"] == "files"
    assert "results" in files_payload["data"]
    assert "recent_files" in files_payload["data"]

    reports_response = client.get("/3d/api/module/reports")
    assert reports_response.status_code == 200
    reports_payload = reports_response.json()
    assert reports_payload["module"]["id"] == "reports"
    assert "system_status" in reports_payload["data"]
    assert "report_files" in reports_payload["data"]

    email_response = client.get("/3d/api/module/email")
    assert email_response.status_code == 200
    email_payload = email_response.json()
    assert email_payload["module"]["id"] == "email"
    assert "status" in email_payload["data"]
    assert "configured" in email_payload["data"]

    calendar_response = client.get("/3d/api/module/calendar")
    assert calendar_response.status_code == 200
    calendar_payload = calendar_response.json()
    assert calendar_payload["module"]["id"] == "calendar"
    assert "status" in calendar_payload["data"]
    assert "configured" in calendar_payload["data"]
