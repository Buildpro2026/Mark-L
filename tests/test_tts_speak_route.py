"""POST /ui/api/tts/speak — the web UI's real neural-TTS endpoint
(2026-08-31). Covers session gating and the honest not-configured/error
responses; actions/elevenlabs_tts.py's own HTTP-call behavior is covered
in tests/test_elevenlabs_tts.py.
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
    monkeypatch.setattr(config, "ELEVENLABS_API_KEY", None)
    client = _logged_in_client(monkeypatch)
    r = client.post("/ui/api/tts/speak", json={"text": "Good morning."})
    assert r.status_code == 200
    assert r.json() == {"configured": False}


def test_returns_real_audio_when_configured(monkeypatch):
    monkeypatch.setattr(config, "ELEVENLABS_API_KEY", "sk-fake-key")
    client = _logged_in_client(monkeypatch)
    from actions import elevenlabs_tts
    monkeypatch.setattr(
        elevenlabs_tts, "synthesize_speech",
        lambda text, voice_id=None: {"ok": True, "audio_base64": "ZmFrZQ==", "mime_type": "audio/mpeg"},
    )
    r = client.post("/ui/api/tts/speak", json={"text": "Good morning."})
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is True
    assert body["ok"] is True
    assert body["audio_base64"] == "ZmFrZQ=="
    assert body["mime_type"] == "audio/mpeg"


def test_surfaces_a_failed_synthesis_honestly(monkeypatch):
    monkeypatch.setattr(config, "ELEVENLABS_API_KEY", "sk-fake-key")
    client = _logged_in_client(monkeypatch)
    from actions import elevenlabs_tts
    monkeypatch.setattr(
        elevenlabs_tts, "synthesize_speech",
        lambda text, voice_id=None: {"ok": False, "state": "UNAUTHORIZED", "detail": "ElevenLabs API key was rejected."},
    )
    r = client.post("/ui/api/tts/speak", json={"text": "Good morning."})
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is True
    assert body["ok"] is False
    assert "rejected" in body["detail"]
