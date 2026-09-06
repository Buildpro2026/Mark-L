"""ElevenLabs text-to-speech for the web /ui — the real, human-sounding
voice path (2026-08-31, Lee's spec).

Why this exists separately from core/tts.py's ElevenLabsTTSEngine: that
class calls sd.play()/miniaudio to play audio through the machine's own
speakers, which only makes sense for the old desktop app — there is no
speaker on a Render container, and the browser is where the CEO actually
needs to hear JARVIS. This module does the same ElevenLabs REST call but
returns the raw audio bytes for the caller (ui.py's /tts/speak route) to
hand back to the browser, which plays them through a real <audio>
element instead of browser speechSynthesis. That's the actual fix for
"JARVIS sounds robotic": speechSynthesis uses whatever low-quality
OS/browser voice happens to be installed on Lee's machine and was never
actually connected to the voice_provider Settings choice at all (see
ui_static/index.html's speakReply — it hardcodes an OS voice search,
completely independent of ElevenLabs/Gemini/local selection); ElevenLabs
is real neural TTS, chosen because it's already the provider this
codebase's own docstrings call "best quality" and needs no new SDK.

Voice: "Adam" (pNInz6obpgDQGcFmaJgB, ElevenLabs' own premade voice,
already the default in core/tts.py) — a deep, natural, general-American
male voice that matches "mature, confident, calm, articulate" without
needing to guess at an unfamiliar voice id.

Never fabricates success: any missing key, HTTP error, or network
failure returns an honest {"ok": False, ...} the caller can fall back
from, exactly like every other real integration in this codebase.
"""
from __future__ import annotations

import base64
from typing import Any

DEFAULT_VOICE_ID = "pNInz6obpgDQGcFmaJgB"  # "Adam" — deep, natural male voice


def get_api_key() -> str | None:
    from core.headless import config as _hc
    return _hc.ELEVENLABS_API_KEY or None


def is_configured() -> bool:
    return bool(get_api_key())


def synthesize_speech(text: str, voice_id: str | None = None) -> dict[str, Any]:
    """Real ElevenLabs call — returns base64-encoded MP3 bytes on success.
    Never plays audio itself (no sounddevice/PortAudio dependency here at
    all), so this is safe to import and call from the headless container."""
    api_key = get_api_key()
    if not api_key:
        return {"ok": False, "state": "NOT_CONFIGURED", "detail": "ElevenLabs API key is not configured."}
    if not text or not text.strip():
        return {"ok": False, "state": "ERROR", "detail": "No text to speak."}

    import requests

    vid = voice_id or DEFAULT_VOICE_ID
    try:
        resp = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{vid}",
            headers={"xi-api-key": api_key, "Content-Type": "application/json"},
            json={
                "text": text,
                "model_id": "eleven_multilingual_v2",
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
            },
            timeout=30,
        )
    except requests.RequestException as e:
        return {"ok": False, "state": "ERROR", "detail": f"ElevenLabs request failed: {e}"}

    if resp.status_code == 401:
        return {"ok": False, "state": "UNAUTHORIZED", "detail": "ElevenLabs API key was rejected."}
    if not resp.ok:
        return {"ok": False, "state": "ERROR", "detail": f"ElevenLabs returned HTTP {resp.status_code}: {resp.text[:300]}"}

    return {
        "ok": True,
        "audio_base64": base64.b64encode(resp.content).decode("ascii"),
        "mime_type": "audio/mpeg",
    }
