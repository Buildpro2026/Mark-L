from fastapi.testclient import TestClient

import dashboard.server as server_mod
from dashboard.server import DashboardServer
from actions import twilio_integration as tw

# Matches conftest.py's _dashboard_api_token autouse fixture — see that
# fixture's docstring for why /3d/api/* needs it now.
_AUTH_HEADERS = {"Authorization": "Bearer test-dashboard-token-not-a-real-secret"}


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(tw, "DB_PATH", tmp_path / "webhook_test.db")
    # These tests exercise the DB-recording behavior of each webhook, not
    # the signature mechanism itself (see test_twilio_webhook_signature.py
    # for that) — bypass it here so they stay focused on what they're
    # actually testing.
    monkeypatch.setattr(server_mod, "_verify_twilio_signature", lambda req, form: True)


def test_twilio_voice_webhook_returns_twiml_and_logs_inbound_call(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    server = DashboardServer()
    client = TestClient(server.app, headers=_AUTH_HEADERS)

    response = client.post("/twilio/voice", data={
        "From": "+15551111111", "To": "+15550000000", "CallSid": "CA_test_1", "CallStatus": "ringing",
    })
    assert response.status_code == 200
    assert "application/xml" in response.headers["content-type"]
    assert "<Record" in response.text

    history = tw.get_history(kind="call")
    assert history[0]["sid"] == "CA_test_1"
    assert history[0]["direction"] == "inbound"


def test_twilio_sms_webhook_logs_inbound_message(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    server = DashboardServer()
    client = TestClient(server.app, headers=_AUTH_HEADERS)

    response = client.post("/twilio/sms", data={
        "From": "+15552222222", "To": "+15550000000", "Body": "Are you available tomorrow?", "MessageSid": "SM_test_1",
    })
    assert response.status_code == 200
    history = tw.get_history(kind="sms")
    assert history[0]["sid"] == "SM_test_1"
    assert history[0]["body"] == "Are you available tomorrow?"


def test_twilio_status_webhook_updates_existing_row(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    tw._log(direction="outbound", kind="sms", sid="SM_status_test", status="queued")
    server = DashboardServer()
    client = TestClient(server.app, headers=_AUTH_HEADERS)

    response = client.post("/twilio/status", data={"MessageSid": "SM_status_test", "MessageStatus": "delivered"})
    assert response.status_code == 200
    history = tw.get_history(kind="sms")
    assert history[0]["status"] == "delivered"


def test_twilio_transcription_webhook_updates_the_call(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    tw._log(direction="inbound", kind="call", sid="CA_transcribe_test", status="completed")
    server = DashboardServer()
    client = TestClient(server.app, headers=_AUTH_HEADERS)

    response = client.post("/twilio/transcription", data={
        "CallSid": "CA_transcribe_test", "TranscriptionText": "Please call me back about the invoice.",
    })
    assert response.status_code == 200
    history = tw.get_history(kind="call")
    assert history[0]["transcription"] == "Please call me back about the invoice."


def test_communications_module_reflects_not_configured_by_default():
    # config/api_keys.json ships with empty twilio credentials — this proves
    # the Nucleus honestly reports NOT_CONFIGURED rather than faking readiness.
    server = DashboardServer()
    client = TestClient(server.app, headers=_AUTH_HEADERS)

    response = client.get("/3d/api/module/communications")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["configured"] is False
    assert data["status"] == "NOT_CONFIGURED"


def test_communications_module_reflects_configured_state(monkeypatch, tmp_path):
    import json
    cfg_path = tmp_path / "api_keys.json"
    cfg_path.write_text(json.dumps({"twilio": {
        "account_sid": "AC1", "auth_token": "t", "from_number": "+15551234567",
    }}), encoding="utf-8")
    monkeypatch.setattr(tw, "API_CONFIG_PATH", cfg_path)
    _isolate(monkeypatch, tmp_path)

    server = DashboardServer()
    client = TestClient(server.app, headers=_AUTH_HEADERS)
    response = client.get("/3d/api/module/communications")
    data = response.json()["data"]
    assert data["configured"] is True
    assert data["status"] == "CONFIGURED"
