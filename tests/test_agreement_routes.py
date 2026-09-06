"""core/headless/agreement_routes.py — the public, unauthenticated
representation-agreement signing page. Covers the real HTTP path through
the full app (no auth header at all — the whole point is a candidate can
reach this from a plain email link), not just the underlying signing logic
(see tests/test_agreement_signing.py for that).
"""
from fastapi.testclient import TestClient

from actions import agreement_signing as sig
from core.headless import config


def _client(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "API_TOKEN", "test-token-not-a-real-secret")
    monkeypatch.setattr(sig, "DB_PATH", tmp_path / "test_agreements.db")
    from core.headless.app import create_app
    app = create_app(start_background_worker=False)
    return TestClient(app)


def test_view_unknown_token_returns_404(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    r = client.get("/agreement/does-not-exist")
    assert r.status_code == 404
    assert "not found" in r.text.lower()


def test_view_pending_agreement_shows_the_form_and_text(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    token = sig.create_pending_agreement(1, "Jane Doe", "jane@example.com")

    r = client.get(f"/agreement/{token}")
    assert r.status_code == 200
    assert "Jane Doe" in r.text
    assert "Sign Agreement" in r.text
    assert "REPRESENTATION AGREEMENT" in r.text
    assert f'/agreement/{token}/sign' in r.text


def test_no_auth_required_at_all(monkeypatch, tmp_path):
    # The defining requirement: NO Authorization header, NO session cookie.
    client = _client(monkeypatch, tmp_path)
    token = sig.create_pending_agreement(1, "Jane Doe", "jane@example.com")
    r = client.get(f"/agreement/{token}")
    assert r.status_code == 200  # not 401/403


def test_signing_persists_and_shows_thank_you(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    token = sig.create_pending_agreement(1, "Jane Doe", "jane@example.com")

    r = client.post(f"/agreement/{token}/sign", data={"signed_name": "Jane M. Doe"})
    assert r.status_code == 200
    assert "Thank you, Jane M. Doe" in r.text

    agreement = sig.get_agreement(token)
    assert agreement["signed_name"] == "Jane M. Doe"
    assert agreement["signer_ip"]  # TestClient sets a client host


def test_signing_with_blank_name_shows_an_error_and_does_not_sign(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    token = sig.create_pending_agreement(1, "Jane Doe", "jane@example.com")

    r = client.post(f"/agreement/{token}/sign", data={"signed_name": "  "})
    assert r.status_code == 400
    assert sig.get_agreement(token)["signed_ts"] is None


def test_viewing_an_already_signed_agreement_shows_confirmation_not_the_form(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    token = sig.create_pending_agreement(1, "Jane Doe", "jane@example.com")
    sig.sign_agreement(token, "Jane Doe", "203.0.113.5")

    r = client.get(f"/agreement/{token}")
    assert r.status_code == 200
    assert "Already signed" in r.text
    assert "Sign Agreement" not in r.text
