"""Free Gemini TTS — the voice JARVIS actually speaks with in the browser.

Why this exists: the browser UI asked core/headless/ui.py's
synthesize_reply_audio() for audio, that chain offered only Cartesia and
ElevenLabs, neither is configured on this deployment, so it honestly
returned {"configured": false} and the page fell back to the browser's own
speechSynthesis — the operating system's default robotic voice. That
fallback is what "JARVIS sounds robotic" actually was. Nothing was
misconfigured; there was simply no free provider in the chain.

Gemini already powers the desktop app's live voice (main.py) with the
prebuilt voice "Charon", and actions/voice_manager.py has carried Charon as
the default the whole time. This brings that same voice to the browser and
the phone-adjacent surfaces, using the GEMINI_API_KEY that is already
configured — no new provider account, no new environment variable, nothing
paid.

Charon is the choice on purpose: of Gemini's prebuilt set it is the deep,
informative male register, which is the brief — mature, professional, calm,
confident. JARVIS_GEMINI_VOICE can override it without a code change if a
different one sounds better in a real listening test, because that judgment
belongs to the person listening, not to this file.

No pitch shifting or rate manipulation is applied. Both audibly degrade
speech, and the natural-sounding result comes from the voice and the
delivery instructions below, not from post-processing a synthetic one.
"""
from __future__ import annotations

import base64
import logging
import os
import re
import struct
from typing import Any

logger = logging.getLogger("jarvis.gemini_tts")

# Gemini's TTS model and the default voice. Both overridable by environment
# so a voice change is a config edit, not a deploy.
MODEL = os.environ.get("JARVIS_GEMINI_TTS_MODEL", "gemini-2.5-flash-preview-tts")
DEFAULT_VOICE = os.environ.get("JARVIS_GEMINI_VOICE", "Charon")

# How the words should be delivered. Gemini's TTS models accept a natural
# language style directive in the prompt, which is the supported way to ask
# for pacing and emphasis — the alternative, forcing prosody with SSML-ish
# markup or resampling the output, is what makes assistants sound
# theatrical or artefacted.
STYLE_DIRECTIVE = (
    "Say the following the way a seasoned chief of staff speaks to the "
    "executive they work for: a calm, low, measured male voice. Unhurried "
    "but not slow. Natural pauses between thoughts, a real breath at "
    "sentence boundaries, and emphasis only on the words that carry the "
    "decision. Conversational, not read aloud. Confident and understated — "
    "never dramatic, never chirpy, never performing.\n\n"
)

TIMEOUT_MS = 30_000


def get_api_key() -> str | None:
    from core.headless import config
    return config.GEMINI_API_KEY


def is_configured() -> bool:
    return bool(get_api_key())


def selected_voice() -> str:
    return DEFAULT_VOICE


def _strip_style_markers(text: str) -> str:
    """The reply is prose meant for a person, not a prompt. Strip anything
    that would be read out as punctuation noise."""
    cleaned = re.sub(r"[*_`#]+", "", text or "")
    return re.sub(r"\s+", " ", cleaned).strip()


def _wav_header(pcm: bytes, sample_rate: int = 24_000,
                channels: int = 1, bits: int = 16) -> bytes:
    """Gemini returns raw signed 16-bit PCM. A browser <audio> element needs
    a container, so wrap it in a minimal RIFF/WAVE header rather than
    shipping bytes no player will accept."""
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    return (
        b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, sample_rate,
                                byte_rate, block_align, bits)
        + b"data" + struct.pack("<I", len(pcm))
    )


def _sample_rate_from_mime(mime: str | None) -> int:
    """Gemini reports the rate in the mime type (audio/L16;rate=24000).
    Trusting a hardcoded 24 kHz would play the voice at the wrong speed if
    that ever changes — which is precisely the "chipmunk" failure."""
    if mime:
        match = re.search(r"rate=(\d+)", mime)
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                pass
    return 24_000


def synthesize_speech(text: str, voice_id: str | None = None) -> dict[str, Any]:
    """Returns base64 WAV audio, matching the Cartesia/ElevenLabs provider
    contract so ui.py's chain treats all three identically."""
    api_key = get_api_key()
    if not api_key:
        return {"ok": False, "state": "NOT_CONFIGURED", "detail": "GEMINI_API_KEY is not configured."}
    spoken = _strip_style_markers(text)
    if not spoken:
        return {"ok": False, "state": "ERROR", "detail": "No text to speak."}

    voice = voice_id or DEFAULT_VOICE
    try:
        from google.genai import types
        from core.headless.gemini_client import get_client

        client = get_client(api_key, timeout_ms=TIMEOUT_MS)
        response = client.models.generate_content(
            model=MODEL,
            contents=STYLE_DIRECTIVE + spoken,
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
                    )
                ),
            ),
        )
        part = response.candidates[0].content.parts[0].inline_data
        pcm = part.data
        if isinstance(pcm, str):
            pcm = base64.b64decode(pcm)
        rate = _sample_rate_from_mime(getattr(part, "mime_type", None))
        wav = _wav_header(pcm, sample_rate=rate) + pcm
        return {
            "ok": True,
            "audio_base64": base64.b64encode(wav).decode("ascii"),
            "mime_type": "audio/wav",
            "voice": voice,
        }
    except Exception as exc:
        # Honest failure. The caller falls back to the next provider, and
        # ultimately to the browser voice — never to silence, and never to a
        # fake success.
        logger.warning("Gemini TTS failed: %s", exc)
        return {"ok": False, "state": "ERROR", "detail": str(exc)[:300]}
