"""Cartesia Sonic text-to-speech for the web /ui.

Why this exists alongside actions/elevenlabs_tts.py: the phone line is a
Cartesia Line agent (voice_agent/main.py), and it speaks with a Cartesia
voice. If the browser kept speaking with an ElevenLabs voice, JARVIS would
sound like two different people depending on which surface Lee happened to
be using. This module makes the browser speak with the SAME Cartesia voice
id the phone agent uses (CARTESIA_VOICE_ID), so there is one JARVIS voice
everywhere.

Same contract as elevenlabs_tts.synthesize_speech(): returns base64 audio
bytes for ui.py's /tts/speak route to hand to the browser's <audio>
element. Never plays audio server-side — there is no speaker on a Render
container. Never fabricates success: a missing key, an HTTP error, or a
network failure returns an honest {"ok": False, ...} the caller falls back
from, exactly like every other real integration in this codebase.

Endpoint: POST https://api.cartesia.ai/tts/bytes — the non-streaming REST
form, which returns a complete audio file in one response. The streaming
WebSocket API is lower latency but pointless here: the browser can't start
playing until the <audio> element has bytes anyway, and a WebSocket would
add a dependency and a failure mode for no gain on a one-shot reply.
"""
from __future__ import annotations

import base64
from typing import Any

# Sonic's own default American male voice. Overridden by CARTESIA_VOICE_ID,
# which is the value that should actually be set — pick the voice in the
# Cartesia playground, then set the same id here and in the Line agent.
DEFAULT_VOICE_ID = "a0e99841-438c-4a64-b679-ae501e7d6091"

API_URL = "https://api.cartesia.ai/tts/bytes"


def _cfg():
    from core.headless import config as _hc
    return _hc


def get_api_key() -> str | None:
    return _cfg().CARTESIA_API_KEY or None


def get_voice_id() -> str:
    return _cfg().CARTESIA_VOICE_ID or DEFAULT_VOICE_ID


def is_configured() -> bool:
    return bool(get_api_key())


def synthesize_speech(text: str, voice_id: str | None = None) -> dict[str, Any]:
    """Real Cartesia call — returns base64-encoded MP3 bytes on success."""
    api_key = get_api_key()
    if not api_key:
        return {"ok": False, "state": "NOT_CONFIGURED", "detail": "Cartesia API key is not configured."}
    if not text or not text.strip():
        return {"ok": False, "state": "ERROR", "detail": "No text to speak."}

    import requests

    cfg = _cfg()
    try:
        resp = requests.post(
            API_URL,
            headers={
                "X-API-Key": api_key,
                "Cartesia-Version": cfg.CARTESIA_API_VERSION,
                "Content-Type": "application/json",
            },
            json={
                "model_id": cfg.CARTESIA_TTS_MODEL,
                "transcript": text,
                "voice": {"mode": "id", "id": voice_id or get_voice_id()},
                "output_format": {"container": "mp3", "sample_rate": 44100, "bit_rate": 128000},
                "language": "en",
            },
            timeout=30,
        )
    except requests.RequestException as e:
        return {"ok": False, "state": "ERROR", "detail": f"Cartesia request failed: {e}"}

    if resp.status_code in (401, 403):
        return {"ok": False, "state": "UNAUTHORIZED", "detail": "Cartesia API key was rejected."}
    if not resp.ok:
        return {"ok": False, "state": "ERROR", "detail": f"Cartesia returned HTTP {resp.status_code}: {resp.text[:300]}"}

    return {
        "ok": True,
        "audio_base64": base64.b64encode(resp.content).decode("ascii"),
        "mime_type": "audio/mpeg",
    }


def get_status() -> dict[str, Any]:
    """Non-consequential diagnostic — no API call, no cost."""
    if not is_configured():
        return {
            "state": "NOT_CONFIGURED",
            "detail": "CARTESIA_API_KEY is not set on this server.",
        }
    cfg = _cfg()
    return {
        "state": "CONFIGURED",
        "detail": f"Cartesia TTS ready ({cfg.CARTESIA_TTS_MODEL}).",
        "voice_id": get_voice_id(),
        "voice_id_explicitly_set": bool(cfg.CARTESIA_VOICE_ID),
    }
