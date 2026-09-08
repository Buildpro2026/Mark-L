"""POST /ui/api/tts/speak — the web UI's real neural-TTS endpoint
(2026-08-31). Covers session gating and the honest not-configured/error
responses.

Rewritten 2026-09-08: these three cases used to drive ElevenLabs, because
the endpoint's provider chain ended there. The chain was removed on Lee's
explicit instruction — the /ui voice is free Gemini only, and a transient
failure must not silently start spending money — so the same three
behaviours are now asserted against Gemini. Coverage is unchanged in
shape: not configured, real audio, honest failure. The added assertion is
that no paid provider is reachable even when one is configured.
"""
from fastapi.testclient import TestClient

from core.headless import config


def _client(monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", "test-ui-token-not-a-real-secret")
    from core.headless.app import create_app
    app = create_app(start_background_worker=False)
    return TestClient(app, base_url="https://testserver")


def _logged_in_client(monkeypatch):
    client = _client(monkeypatch)
    r = client.post("/ui/login", json={"token": "test-ui-token-not-a-real-secret"})
    assert r.status_code == 200
    return client


def test_requires_a_session(monkeypatch):
    client = _client(monkeypatch)
    r = client.post("/ui/api/tts/speak", json={"text": "hello"})
    assert r.status_code == 401


def test_reports_not_configured_when_no_key_set(monkeypatch):
    from actions import gemini_tts
    monkeypatch.setattr(gemini_tts, "is_configured", lambda: False)
    client = _logged_in_client(monkeypatch)
    r = client.post("/ui/api/tts/speak", json={"text": "Good morning."})
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is False
    assert "GEMINI_API_KEY" in body["detail"]


def test_returns_real_audio_when_configured(monkeypatch):
    from actions import gemini_tts
    monkeypatch.setattr(gemini_tts, "is_configured", lambda: True)
    monkeypatch.setattr(
        gemini_tts, "synthesize_speech",
        lambda text, **k: {"ok": True, "audio_base64": "ZmFrZQ==", "mime_type": "audio/wav"},
    )
    client = _logged_in_client(monkeypatch)
    r = client.post("/ui/api/tts/speak", json={"text": "Good morning."})
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is True
    assert body["ok"] is True
    assert body["audio_base64"] == "ZmFrZQ=="
    assert body["mime_type"] == "audio/wav"
    assert body["provider"] == "gemini"


def test_surfaces_a_failed_synthesis_honestly(monkeypatch):
    from actions import gemini_tts
    monkeypatch.setattr(gemini_tts, "is_configured", lambda: True)
    monkeypatch.setattr(
        gemini_tts, "synthesize_speech",
        lambda text, **k: {"ok": False, "state": "UNAUTHORIZED",
                           "detail": "Gemini API key was rejected."},
    )
    client = _logged_in_client(monkeypatch)
    r = client.post("/ui/api/tts/speak", json={"text": "Good morning."})
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is True
    assert body["ok"] is False
    assert "rejected" in body["detail"]


def test_a_failing_gemini_does_not_fall_through_to_a_paid_provider(monkeypatch):
    from actions import gemini_tts, cartesia_tts, elevenlabs_tts
    monkeypatch.setattr(gemini_tts, "is_configured", lambda: True)
    monkeypatch.setattr(gemini_tts, "synthesize_speech",
                        lambda text, **k: {"ok": False, "detail": "rate limited"})
    # Both paid providers fully available — and still never reached.
    for paid in (cartesia_tts, elevenlabs_tts):
        monkeypatch.setattr(paid, "is_configured", lambda: True)
        monkeypatch.setattr(paid, "synthesize_speech", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("a paid voice provider was billed")))

    client = _logged_in_client(monkeypatch)
    body = client.post("/ui/api/tts/speak", json={"text": "hello"}).json()
    assert body["ok"] is False
    assert body["provider"] == "gemini"
    assert body["paid_fallback_suppressed"] is True
