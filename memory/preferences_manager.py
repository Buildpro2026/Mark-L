"""JARVIS personalization preferences — config/preferences.json.

Deliberately a separate file from config/api_keys.json (secrets) and
memory/long_term.json (personal facts Lee has shared). This file holds
JARVIS's own configurable appearance/voice/interface behavior — nothing
here is a credential, and nothing here is a fact about Lee's life.
Follows the exact same atomic-write / corruption-recovery / single-lock
pattern as memory/config_manager.py and memory/memory_manager.py, for
the same reasons documented there.

Every field here is real and actually read by the code that uses it —
see docs/MEMORY_SCHEMA.md for which module consumes which key. Nothing
in DEFAULT_PREFERENCES represents a setting that doesn't actually do
anything yet.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from threading import Lock
from typing import Any


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = get_base_dir()
CONFIG_DIR = BASE_DIR / "config"
PREFS_FILE = CONFIG_DIR / "preferences.json"
_lock = Lock()

BUSINESS_IDENTITIES = ("executive", "buildpro", "careerrocket", "ddf")
ALERT_LEVELS = ("quiet", "normal", "high_alert")

# Deliberately does NOT duplicate settings that already have a real home:
# voice_provider/voice_name/voice_speed live in config/api_keys.json via
# actions/voice_manager.py (desktop + voice pipeline both already read
# from there), and morning-briefing on/off lives in config/api_keys.json
# via memory/config_manager.get_brief_enabled(). core/headless/
# personalization.py's get_all_settings()/update_settings() present a
# single merged view across all three files without ever storing the
# same value in two places.
DEFAULT_PREFERENCES: dict[str, Any] = {
    # ── appearance (the /ui web console specifically — the desktop PyQt6
    # HUD has its own separate ui_color setting in api_keys.json, since
    # it's a different rendering surface with no shared theme engine) ──
    "business_identity": "executive",       # which branded identity is active — see BUSINESS_IDENTITIES
    "theme": "dark",                        # dark | light
    "accent_color": "#4f8cff",
    "animation_intensity": "normal",        # minimal | normal | full
    "interface_density": "normal",          # compact | normal | spacious

    # ── voice — the one genuinely new voice setting, output gain on the
    # Gemini audio path (see main.py's _AudioSink.set_gain) ────────────
    "voice_volume": 1.0,                    # 0.0 (mute) - 1.5

    # ── interface ───────────────────────────────────────────────────
    "chat_expanded_by_default": False,
    "visible_dashboard_modules": ["priorities", "calendar", "agents", "opportunities"],
    "default_view": "command",              # which /ui tab loads first

    # ── alerts ──────────────────────────────────────────────────────
    "alert_sensitivity": "normal",          # quiet | normal | high_alert
}


def ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def _backup_corrupt_file() -> None:
    if not PREFS_FILE.exists():
        return
    backup_path = PREFS_FILE.with_name(f"preferences.corrupt-{int(time.time())}.json")
    try:
        PREFS_FILE.replace(backup_path)
        print(f"[Preferences] Corrupted file preserved as {backup_path.name} for manual recovery")
    except Exception as e:
        print(f"[Preferences] Could not back up corrupted file: {e}")


def _read_raw() -> dict:
    """Caller must hold `_lock`. Never raises."""
    ensure_config_dir()
    if not PREFS_FILE.exists():
        return DEFAULT_PREFERENCES.copy()
    try:
        data = json.loads(PREFS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[Preferences] Load error: {e}")
        _backup_corrupt_file()
        return DEFAULT_PREFERENCES.copy()
    if not isinstance(data, dict):
        _backup_corrupt_file()
        return DEFAULT_PREFERENCES.copy()
    return {**DEFAULT_PREFERENCES, **data}


def _write_atomic(data: dict) -> None:
    """Caller must hold `_lock`. Same-filesystem atomic replace."""
    ensure_config_dir()
    tmp_path = PREFS_FILE.with_name(f"preferences.tmp-{os.getpid()}")
    tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp_path, PREFS_FILE)


def load_preferences() -> dict[str, Any]:
    with _lock:
        data = _read_raw()
        if not PREFS_FILE.exists():
            _write_atomic(data)
        return data


def _validate(updates: dict) -> dict:
    """Rejects/clamps values that don't correspond to a real, working
    option — never silently accepts a setting the actual code can't do
    anything with."""
    clean = dict(updates)
    if "business_identity" in clean and clean["business_identity"] not in BUSINESS_IDENTITIES:
        raise ValueError(f"Unknown business_identity: {clean['business_identity']!r}. Valid: {BUSINESS_IDENTITIES}")
    if "alert_sensitivity" in clean and clean["alert_sensitivity"] not in ALERT_LEVELS:
        raise ValueError(f"Unknown alert_sensitivity: {clean['alert_sensitivity']!r}. Valid: {ALERT_LEVELS}")
    if "voice_volume" in clean:
        clean["voice_volume"] = max(0.0, min(float(clean["voice_volume"]), 1.5))
    if "theme" in clean and clean["theme"] not in ("dark", "light"):
        raise ValueError(f"Unknown theme: {clean['theme']!r}. Valid: dark, light")
    if "animation_intensity" in clean and clean["animation_intensity"] not in ("minimal", "normal", "full"):
        raise ValueError(f"Unknown animation_intensity: {clean['animation_intensity']!r}.")
    if "interface_density" in clean and clean["interface_density"] not in ("compact", "normal", "spacious"):
        raise ValueError(f"Unknown interface_density: {clean['interface_density']!r}.")
    return clean


def save_preferences(updates: dict[str, Any]) -> dict[str, Any]:
    """Read-modify-write under one lock acquisition, same race-safety
    pattern as memory/config_manager.py's save_config(). Validates before
    writing — an invalid value raises rather than getting silently
    persisted, so a rejected setting change can't corrupt the file."""
    clean = _validate(updates)
    with _lock:
        data = _read_raw()
        data.update(clean)
        _write_atomic(data)
        return data


def get_preference(key: str, default: Any = None) -> Any:
    return load_preferences().get(key, default)
