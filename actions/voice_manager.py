from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.tts import TTSPlayer, create_tts_player

BASE_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"

# voice_manager provider names -> core.tts engine names
_PROVIDER_ENGINE_MAP = {"local": "kokoro", "elevenlabs": "elevenlabs"}

# Gemini prebuilt voice names — never valid as a Kokoro/ElevenLabs voice id,
# so a leftover Gemini voice selection must not be forwarded to those engines.
_GEMINI_VOICE_NAMES = {"Charon", "Puck", "Kore", "Leda", "Orus", "Zephyr"}


def load_voice_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {"provider": "gemini", "voice": "Charon", "speed": 1.0}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"provider": "gemini", "voice": "Charon", "speed": 1.0}
    return {
        "provider": (data.get("voice_provider") or "gemini").lower(),
        "voice": data.get("voice_name") or "Charon",
        "speed": float(data.get("voice_speed") or 1.0),
    }


def save_voice_config(cfg: dict[str, Any]) -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        data = {}
    else:
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["voice_provider"] = (cfg.get("provider") or "gemini").lower()
    data["voice_name"] = cfg.get("voice") or "Charon"
    data["voice_speed"] = float(cfg.get("speed") or 1.0)
    CONFIG_PATH.write_text(json.dumps(data, indent=4), encoding="utf-8")
    return load_voice_config()


def get_voice_provider_config() -> dict[str, Any]:
    cfg = load_voice_config()
    provider = cfg.get("provider") or "gemini"
    if provider not in {"gemini", "local", "elevenlabs"}:
        provider = "gemini"
    return {"provider": provider, "voice": cfg.get("voice") or "Charon", "speed": float(cfg.get("speed") or 1.0)}


def get_available_voices() -> list[str]:
    return ["Charon", "Puck", "Kore", "Leda", "Orus", "Zephyr"]


def build_tts_player(voice_cfg: dict[str, Any] | None = None) -> TTSPlayer | None:
    """Build a core.tts.TTSPlayer for the configured non-Gemini voice provider.

    Returns None for provider == "gemini" — Gemini Live produces its own
    audio output directly, so no separate TTS engine is needed in that case.
    Raises whatever the underlying engine raises (e.g. missing API key,
    missing model) so callers can decide how to fall back.
    """
    cfg = voice_cfg or get_voice_provider_config()
    provider = (cfg.get("provider") or "gemini").lower()
    if provider == "gemini":
        return None

    engine_name = _PROVIDER_ENGINE_MAP.get(provider, provider)

    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    elevenlabs_key = raw.get("elevenlabs_api_key", "")

    tts_config: dict[str, Any] = {
        "tts_engine": engine_name,
        "tts_speed": cfg.get("speed", 1.0),
        "elevenlabs_api_key": elevenlabs_key,
    }
    voice_val = cfg.get("voice")
    if voice_val and voice_val not in _GEMINI_VOICE_NAMES:
        tts_config["tts_voice"] = voice_val

    return create_tts_player(tts_config)
