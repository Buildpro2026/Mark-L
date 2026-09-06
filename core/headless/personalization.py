"""Unified personalization settings API — the single place the /ui
console (and any future settings surface) reads and writes every user-
controllable JARVIS preference, whether it's physically stored in
memory/preferences_manager.py's preferences.json, config/api_keys.json
(voice provider/name/speed, morning-briefing on/off — both already had
a real home before this phase), or derived facts (available voices,
available business identities).

This module never introduces a second copy of a value that already has
a home elsewhere — see preferences_manager.py's module docstring for
which file owns which field. get_all_settings() merges all three
sources into one dict for the UI to render; update_settings() routes
each key in the update to whichever store actually owns it.
"""
from __future__ import annotations

from typing import Any

from actions import voice_manager
from memory import config_manager
from memory import preferences_manager

_VOICE_MANAGER_KEYS = {"voice_provider", "voice_name", "voice_speed"}
_BRIEF_KEY = "startup_briefing_enabled"


def get_all_settings() -> dict[str, Any]:
    prefs = preferences_manager.load_preferences()
    voice_cfg = voice_manager.get_voice_provider_config()
    return {
        **prefs,
        "voice_provider": voice_cfg["provider"],
        "voice_name": voice_cfg["voice"],
        "voice_speed": voice_cfg["speed"],
        "available_voices": voice_manager.get_available_voices(),
        "available_voice_providers": ["gemini", "local", "elevenlabs"],
        "business_identities": list(preferences_manager.BUSINESS_IDENTITIES),
        "alert_levels": list(preferences_manager.ALERT_LEVELS),
        _BRIEF_KEY: config_manager.get_brief_enabled(),
    }


def update_settings(updates: dict[str, Any]) -> dict[str, Any]:
    """Splits the incoming update across whichever store actually owns
    each key, so a single settings-page save can touch appearance,
    voice, and briefing preferences in one call without the caller
    needing to know where any of them physically live."""
    voice_updates = {k: v for k, v in updates.items() if k in _VOICE_MANAGER_KEYS}
    brief_update = updates.get(_BRIEF_KEY)
    prefs_updates = {
        k: v for k, v in updates.items()
        if k not in _VOICE_MANAGER_KEYS and k != _BRIEF_KEY
    }

    if voice_updates:
        current = voice_manager.get_voice_provider_config()
        merged = {
            "provider": voice_updates.get("voice_provider", current["provider"]),
            "voice": voice_updates.get("voice_name", current["voice"]),
            "speed": voice_updates.get("voice_speed", current["speed"]),
        }
        voice_manager.save_voice_config(merged)

    if brief_update is not None:
        config_manager.save_brief_enabled(bool(brief_update))

    if prefs_updates:
        preferences_manager.save_preferences(prefs_updates)

    return get_all_settings()
