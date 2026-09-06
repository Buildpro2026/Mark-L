"""actions/elevenlabs_tts.py — the real neural-TTS path for the web /ui
(2026-08-31, Lee's spec: JARVIS must sound human, not like browser-default
speechSynthesis). Mocks the actual HTTP call so these tests cover the
module's own contract (honest ok/error dicts, never plays audio itself)
rather than re-testing requests or ElevenLabs' API.
"""
import pytest

from actions import elevenlabs_tts as tts


def test_not_configured_without_a_key(monkeypatch):
    monkeypatch.setattr("core.headless.config.ELEVENLABS_API_KEY", None)
    assert tts.is_configured() is False
    result = tts.synthesize_speech("Good morning.")
    assert result == {"ok": False, "state": "NOT_CONFIGURED", "detail": "ElevenLabs API key is not configured."}


def test_configured_when_key_present(monkeypatch):
    monkeypatch.setattr("core.headless.config.ELEVENLABS_API_KEY", "sk-fake-key")
    assert tts.is_configured() is True


def test_refuses_empty_text(monkeypatch):
    monkeypatch.setattr("core.headless.config.ELEVENLABS_API_KEY", "sk-fake-key")
    result = tts.synthesize_speech("   ")
    assert result["ok"] is False
    assert result["state"] == "ERROR"


class _FakeResponse:
    def __init__(self, status_code, content=b"", text=""):
        self.status_code = status_code
        self.content = content
        self.text = text
        self.ok = 200 <= status_code < 300


def test_successful_synthesis_returns_base64_audio(monkeypatch):
    monkeypatch.setattr("core.headless.config.ELEVENLABS_API_KEY", "sk-fake-key")
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return _FakeResponse(200, content=b"\xff\xfbfake-mp3-bytes")

    monkeypatch.setattr("requests.post", fake_post)
    result = tts.synthesize_speech("Good morning, Lee.")

    assert result["ok"] is True
    assert result["mime_type"] == "audio/mpeg"
    import base64
    assert base64.b64decode(result["audio_base64"]) == b"\xff\xfbfake-mp3-bytes"
    assert tts.DEFAULT_VOICE_ID in captured["url"]
    assert captured["headers"]["xi-api-key"] == "sk-fake-key"
    assert captured["json"]["text"] == "Good morning, Lee."


def test_uses_the_requested_voice_id_when_given(monkeypatch):
    monkeypatch.setattr("core.headless.config.ELEVENLABS_API_KEY", "sk-fake-key")
    captured = {}
    monkeypatch.setattr("requests.post", lambda url, **k: (captured.setdefault("url", url), _FakeResponse(200, content=b"x"))[1])
    tts.synthesize_speech("hi", voice_id="custom-voice-id")
    assert "custom-voice-id" in captured["url"]


def test_rejected_key_returns_unauthorized(monkeypatch):
    monkeypatch.setattr("core.headless.config.ELEVENLABS_API_KEY", "sk-bad-key")
    monkeypatch.setattr("requests.post", lambda *a, **k: _FakeResponse(401, text="unauthorized"))
    result = tts.synthesize_speech("hi")
    assert result == {"ok": False, "state": "UNAUTHORIZED", "detail": "ElevenLabs API key was rejected."}


def test_other_http_errors_are_reported_honestly(monkeypatch):
    monkeypatch.setattr("core.headless.config.ELEVENLABS_API_KEY", "sk-fake-key")
    monkeypatch.setattr("requests.post", lambda *a, **k: _FakeResponse(500, text="server error"))
    result = tts.synthesize_speech("hi")
    assert result["ok"] is False
    assert result["state"] == "ERROR"
    assert "500" in result["detail"]


def test_network_failure_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr("core.headless.config.ELEVENLABS_API_KEY", "sk-fake-key")
    import requests

    def raise_it(*a, **k):
        raise requests.RequestException("connection refused")

    monkeypatch.setattr("requests.post", raise_it)
    result = tts.synthesize_speech("hi")
    assert result["ok"] is False
    assert result["state"] == "ERROR"
    assert "connection refused" in result["detail"]
