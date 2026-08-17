from fastapi.testclient import TestClient

from dashboard.server import DashboardServer

# Matches conftest.py's _dashboard_api_token autouse fixture — see that
# fixture's docstring for why /3d/ws needs it now.
_TEST_TOKEN = "test-dashboard-token-not-a-real-secret"
_AUTH_HEADERS = {"Authorization": f"Bearer {_TEST_TOKEN}"}


def test_three_d_page_references_pwa_manifest_and_apple_meta():
    server = DashboardServer()
    client = TestClient(server.app, headers=_AUTH_HEADERS)
    html = client.get("/3d").text
    assert '<link rel="manifest" href="/3d/assets/manifest.json" />' in html
    assert 'apple-mobile-web-app-capable' in html
    assert 'name="theme-color"' in html
    assert 'conn-status' in html
    assert 'toast-stack' in html


def test_service_worker_served_at_top_level_scope():
    # Must be served at /3d/sw.js (not /3d/assets/sw.js) so its default scope
    # covers /3d/* rather than only /3d/assets/*.
    server = DashboardServer()
    client = TestClient(server.app, headers=_AUTH_HEADERS)
    response = client.get("/3d/sw.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert "CACHE_NAME" in response.text


def test_manifest_and_icon_are_served_via_the_assets_mount():
    server = DashboardServer()
    client = TestClient(server.app, headers=_AUTH_HEADERS)

    manifest = client.get("/3d/assets/manifest.json")
    assert manifest.status_code == 200
    payload = manifest.json()
    assert payload["start_url"] == "/3d"
    assert payload["display"] == "standalone"

    icon = client.get("/3d/assets/icon.svg")
    assert icon.status_code == 200
    assert "svg" in icon.headers["content-type"]


def test_sms_webhook_pushes_a_toast_notification_to_the_3d_channel(monkeypatch, tmp_path):
    from actions import twilio_integration as tw
    import dashboard.server as server_mod
    monkeypatch.setattr(tw, "DB_PATH", tmp_path / "notif_test.db")
    # This test is about the notification broadcast, not signature
    # validation (see test_twilio_webhook_signature.py for that) — bypass
    # it here so an unsigned test POST doesn't get rejected before ever
    # reaching the broadcast, which would otherwise hang the websocket
    # receive below waiting for a notification that never arrives.
    monkeypatch.setattr(server_mod, "_verify_twilio_signature", lambda req, form: True)

    server = DashboardServer()
    client = TestClient(server.app, headers=_AUTH_HEADERS)

    with client.websocket_connect(f"/3d/ws?token={_TEST_TOKEN}") as ws:
        ws.receive_json()  # initial state push on connect
        client.post("/twilio/sms", data={
            "From": "+15551234567", "To": "+15550000000", "Body": "Confirmed for 3pm", "MessageSid": "SM_notif_1",
        })
        notif = ws.receive_json()
        assert notif["type"] == "notification"
        assert "+15551234567" in notif["text"]
