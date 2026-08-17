"""API keys / app settings — see docs/MEMORY_SCHEMA.md for the on-disk
schema and corruption-recovery behavior. This file (config/api_keys.json)
holds secrets and is git-ignored; it is deliberately separate from
memory/long_term.json, which holds only personal facts the user shared
(no credentials belong there, and vice versa).
"""
import json
import os
import time
from pathlib import Path
from threading import Lock
import sys

def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

BASE_DIR = get_base_dir()
CONFIG_DIR = BASE_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "api_keys.json"
_lock = Lock()

# Only keys actually read by real code belong here. Previously included
# github_token/vercel_token/make_api_token/airtable_token/
# google_credentials/microsoft_credentials — none of those are read
# anywhere in this codebase (confirmed by repo-wide search); they were
# dead scaffolding that misleadingly implied those integrations existed.
# Gmail/Calendar auth is a separate mechanism entirely (config/google/,
# see actions/google_auth.py) and intentionally has no key here.
DEFAULT_CONFIG = {
    "gemini_api_key": "",
    "hubspot_token": "",
    "buffer_token": "",
    "airtable_token": "",
    "twilio": {"account_sid": "", "auth_token": "", "from_number": ""},
    "assistant_name": "J.A.R.V.I.S.",
    "user_name": "Mr. Chandler",
    "ui_color": "#00beff",
    "morning_brief_enabled": True,
    "proactive_enabled": True,
    "proactive_quiet_hours": [22, 8],
}


def ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def _backup_corrupt_file() -> None:
    """Preserve an unreadable config file for manual recovery. Without
    this, a corrupted read silently falls back to DEFAULT_CONFIG (empty
    secrets) in memory, and the next save_config() call would overwrite
    the real file with those blanks — permanently losing every API key
    it held."""
    if not CONFIG_FILE.exists():
        return
    backup_path = CONFIG_FILE.with_name(f"api_keys.corrupt-{int(time.time())}.json")
    try:
        CONFIG_FILE.replace(backup_path)
        print(f"[Config] ⚠️ Corrupted file preserved as {backup_path.name} for manual recovery")
    except Exception as e:
        print(f"[Config] ⚠️ Could not back up corrupted file: {e}")


def _read_raw() -> dict:
    """Caller must hold `_lock`. Never raises."""
    ensure_config_dir()
    if not CONFIG_FILE.exists():
        return DEFAULT_CONFIG.copy()
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Failed to load api_keys.json: {e}")
        _backup_corrupt_file()
        return DEFAULT_CONFIG.copy()
    if not isinstance(data, dict):
        _backup_corrupt_file()
        return DEFAULT_CONFIG.copy()
    return {**DEFAULT_CONFIG, **data}


def _write_atomic(data: dict) -> None:
    """Caller must hold `_lock`. Atomic same-filesystem replace — a crash
    mid-write leaves the previous, intact file in place instead of a
    truncated one (previously wrote directly via write_text(), which is
    not atomic)."""
    ensure_config_dir()
    tmp_path = CONFIG_FILE.with_name(f"api_keys.tmp-{os.getpid()}")
    tmp_path.write_text(json.dumps(data, indent=4), encoding="utf-8")
    os.replace(tmp_path, CONFIG_FILE)


def load_api_keys() -> dict:
    with _lock:
        data = _read_raw()
        if not CONFIG_FILE.exists():
            _write_atomic(data)
        return data


def save_config(updates: dict) -> None:
    """Read-modify-write under a single lock acquisition — previously the
    read (load_api_keys()) and the write happened with no lock at all,
    so two concurrent settings saves (e.g. a UI save racing a credential
    refresh) could silently clobber each other."""
    with _lock:
        data = _read_raw()
        data.update(updates)
        _write_atomic(data)


def config_exists() -> bool:
    return CONFIG_FILE.exists()

def save_api_keys(gemini_api_key: str) -> None:
    save_config({
        "gemini_api_key": gemini_api_key.strip()
    })

def get_gemini_key() -> str | None:
    return load_api_keys().get("gemini_api_key")

def is_configured() -> bool:
    key = get_gemini_key()
    return bool(key and len(key) > 15)

def get_assistant_name() -> str:
    return load_api_keys().get("assistant_name", "JARVIS") or "JARVIS"

def get_user_name() -> str:
    return load_api_keys().get("user_name", "")

def save_assistant_config(assistant_name: str, user_name: str) -> None:
    save_config({
        "assistant_name": assistant_name.strip() or "JARVIS",
        "user_name": user_name.strip()
    })

def get_brief_enabled() -> bool:
    return load_api_keys().get("morning_brief_enabled", True)

def save_brief_enabled(enabled: bool) -> None:
    save_config({
        "morning_brief_enabled": enabled
    })

def get_proactive_enabled() -> bool:
    return load_api_keys().get("proactive_enabled", True)

def save_proactive_enabled(enabled: bool) -> None:
    save_config({
        "proactive_enabled": enabled
    })

def get_proactive_quiet_hours() -> tuple[int, int] | None:
    """(start_hour, end_hour) in 24h local time, wrapping midnight — e.g.
    (22, 8) means quiet from 10pm to 8am. None disables the quiet-hours
    check entirely."""
    val = load_api_keys().get("proactive_quiet_hours", [22, 8])
    if not val:
        return None
    try:
        start, end = int(val[0]), int(val[1])
        return (start, end)
    except (TypeError, ValueError, IndexError):
        return None

def save_proactive_quiet_hours(start_hour: int | None, end_hour: int | None) -> None:
    """Pass (None, None) to disable quiet hours entirely."""
    save_config({
        "proactive_quiet_hours": None if start_hour is None else [int(start_hour), int(end_hour)]
    })

def get_credential(service: str) -> str:
    return load_api_keys().get(service, "")

def save_credential(service: str, value: str) -> None:
    save_config({
        service: value.strip()
    })
