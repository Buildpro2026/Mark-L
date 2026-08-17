"""dashboard/server.py's Twilio webhook signature verification
(_verify_twilio_signature) — added because the 5 /twilio/* webhook
endpoints previously accepted any unsigned POST from anyone who found the
URL, letting an attacker inject fake "incoming call"/"SMS" notifications
and fake history rows. These are the only routes in this file meant to
receive traffic from the open internet (a real Twilio number's webhooks),
unlike the rest of /3d/* which is local-trust by design — see
docs/DASHBOARD_SECURITY.md.

_generate_signature() below independently implements Twilio's documented
algorithm (HMAC-SHA1 over the URL + sorted form params, base64-encoded) —
the same algorithm the production code implements — to produce/verify
real signatures rather than just asserting the function was called.
"""
import base64
import hashlib
import hmac
import json

from fastapi.testclient import TestClient

import dashboard.server as server_mod
from dashboard.server import DashboardServer
from actions import twilio_integration as tw

AUTH_TOKEN = "test-auth-token-abc123"


def _generate_signature(url: str, params: dict, auth_token: str = AUTH_TOKEN) -> str:
    data = url
    for key in sorted(params.keys()):
        data += key + str(params[key])
    return base64.b64encode(
        hmac.new(auth_token.encode("utf-8"), data.encode("utf-8"), hashlib.sha1).digest()
    ).decode("utf-8")


def _configure_twilio(monkeypatch, tmp_path):
    cfg_path = tmp_path / "api_keys.json"
    cfg_path.write_text(json.dumps({"twilio": {
        "account_sid": "AC_test", "auth_token": AUTH_TOKEN, "from_number": "+15550000000",
    }}), encoding="utf-8")
    monkeypatch.setattr(tw, "API_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(tw, "DB_PATH", tmp_path / "sig_test.db")


# ── _verify_twilio_signature — direct unit tests ─────────────────────────

class _FakeRequest:
    def __init__(self, url: str, signature: str | None):
        self.url = url
        self.headers = {"x-twilio-signature": signature} if signature is not None else {}


def test_verify_rejects_when_twilio_not_configured(monkeypatch, tmp_path):
    monkeypatch.setattr(tw, "API_CONFIG_PATH", tmp_path / "nope.json")
    form = {"From": "+15551111111"}
    sig = _generate_signature("http://x/twilio/voice", form)
    req = _FakeRequest("http://x/twilio/voice", sig)
    assert server_mod._verify_twilio_signature(req, form) is False


def test_verify_rejects_missing_signature_header(monkeypatch, tmp_path):
    _configure_twilio(monkeypatch, tmp_path)
    form = {"From": "+15551111111"}
    req = _FakeRequest("http://x/twilio/voice", None)
    assert server_mod._verify_twilio_signature(req, form) is False


def test_verify_accepts_a_correctly_computed_signature(monkeypatch, tmp_path):
    _configure_twilio(monkeypatch, tmp_path)
    form = {"From": "+15551111111", "To": "+15550000000", "CallSid": "CA1"}
    url = "http://testserver/twilio/voice"
    sig = _generate_signature(url, form)
    req = _FakeRequest(url, sig)
    assert server_mod._verify_twilio_signature(req, form) is True


def test_verify_rejects_a_tampered_form_value(monkeypatch, tmp_path):
    _configure_twilio(monkeypatch, tmp_path)
    url = "http://testserver/twilio/voice"
    real_form = {"From": "+15551111111", "To": "+15550000000", "CallSid": "CA1"}
    sig = _generate_signature(url, real_form)
    # Attacker changes a value after the signature was computed for the original.
    tampered_form = {**real_form, "From": "+19995551234"}
    req = _FakeRequest(url, sig)
    assert server_mod._verify_twilio_signature(req, tampered_form) is False


def test_verify_rejects_a_signature_computed_with_the_wrong_auth_token(monkeypatch, tmp_path):
    _configure_twilio(monkeypatch, tmp_path)
    url = "http://testserver/twilio/voice"
    form = {"From": "+15551111111"}
    sig = _generate_signature(url, form, auth_token="wrong-token")
    req = _FakeRequest(url, sig)
    assert server_mod._verify_twilio_signature(req, form) is False


def test_verify_rejects_a_signature_computed_for_a_different_url(monkeypatch, tmp_path):
    _configure_twilio(monkeypatch, tmp_path)
    form = {"From": "+15551111111"}
    sig = _generate_signature("http://testserver/twilio/sms", form)   # wrong path
    req = _FakeRequest("http://testserver/twilio/voice", sig)
    assert server_mod._verify_twilio_signature(req, form) is False


def test_verify_never_raises_on_malformed_url_or_form():
    req = _FakeRequest(None, "some-signature")   # malformed on purpose
    assert server_mod._verify_twilio_signature(req, {"a": object()}) is False


# ── End-to-end over real HTTP (TestClient) ────────────────────────────────

def test_webhook_rejects_an_unsigned_request_end_to_end(monkeypatch, tmp_path):
    _configure_twilio(monkeypatch, tmp_path)
    server = DashboardServer()
    client = TestClient(server.app)

    response = client.post("/twilio/voice", data={
        "From": "+15551111111", "To": "+15550000000", "CallSid": "CA_unsigned", "CallStatus": "ringing",
    })
    assert response.status_code == 403
    assert tw.get_history(kind="call") == []   # never recorded — the forged request was rejected


def test_webhook_accepts_a_correctly_signed_request_end_to_end(monkeypatch, tmp_path):
    _configure_twilio(monkeypatch, tmp_path)
    server = DashboardServer()
    client = TestClient(server.app)

    form = {"From": "+15551111111", "To": "+15550000000", "CallSid": "CA_signed", "CallStatus": "ringing"}
    # TestClient's base URL for a relative path like this:
    url = "http://testserver/twilio/voice"
    sig = _generate_signature(url, form)

    response = client.post("/twilio/voice", data=form, headers={"X-Twilio-Signature": sig})
    assert response.status_code == 200
    history = tw.get_history(kind="call")
    assert history[0]["sid"] == "CA_signed"


def test_webhook_rejects_when_twilio_is_not_configured_even_without_attacker(tmp_path, monkeypatch):
    # No credentials at all — nothing legitimate could ever hit this in
    # this state, so it must refuse outright rather than silently no-op.
    monkeypatch.setattr(tw, "API_CONFIG_PATH", tmp_path / "nope.json")
    monkeypatch.setattr(tw, "DB_PATH", tmp_path / "sig_test2.db")
    server = DashboardServer()
    client = TestClient(server.app)

    response = client.post("/twilio/sms", data={"From": "+1555", "Body": "hi", "MessageSid": "SM1"})
    assert response.status_code == 403
