import json
import os
import sys
import threading
import time
from pathlib import Path

def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

BASE_DIR    = get_base_dir()
CONFIG_DIR  = BASE_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "api_keys.json"

# Schema defaults for the keys real integrations actually read. Kept
# deliberately free of "dead" keys for integrations that don't exist in
# this codebase (github_token, vercel_token, make_api_token,
# google_credentials, microsoft_credentials) — those implied capabilities
# JARVIS never had.
DEFAULT_CONFIG: dict = {
    "gemini_api_key": "",
    "hubspot_token": "",
    "buffer_token": "",
    "airtable_token": "",
    "twilio": {"account_sid": "", "auth_token": "", "from_number": ""},
}

# Guards the whole load-merge-write cycle in save_config()/save_credential()
# so concurrent callers in this process never race and silently drop one
# another's write (each save is read-modify-write against one shared file).
_LOCK = threading.Lock()


def ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def config_exists() -> bool:
    return CONFIG_FILE.exists()


def _backup_corrupt_file() -> None:
    """Renames (never deletes) an unparseable api_keys.json so a real,
    partially-corrupted secret stays recoverable by hand instead of being
    silently replaced by blanks on the next save — see this module's test
    file docstring for the exact incident this guards against."""
    try:
        backup = CONFIG_DIR / f"api_keys.corrupt-{int(time.time())}.json"
        CONFIG_FILE.replace(backup)
    except Exception:
        pass


def _read_raw() -> dict:
    """Loads the config file, backing up (not discarding) unparseable
    content and falling back to DEFAULT_CONFIG. Never raises."""
    if not CONFIG_FILE.exists():
        return dict(DEFAULT_CONFIG)
    try:
        raw = CONFIG_FILE.read_text(encoding="utf-8")
    except Exception as e:
        print(f"❌ Failed to read api_keys.json: {e}")
        return dict(DEFAULT_CONFIG)
    try:
        data = json.loads(raw)
    except Exception:
        _backup_corrupt_file()
        return dict(DEFAULT_CONFIG)
    if not isinstance(data, dict):
        _backup_corrupt_file()
        return dict(DEFAULT_CONFIG)
    return data


def _write_raw(data: dict) -> None:
    """Atomic write: a crash or concurrent read mid-write never sees a
    truncated/partial file — write to a temp file in the same directory,
    then os.replace() it into place."""
    ensure_config_dir()
    tmp_path = CONFIG_DIR / f"api_keys.tmp-{os.getpid()}-{threading.get_ident()}"
    tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp_path, CONFIG_FILE)


def load_api_keys() -> dict:
    with _LOCK:
        data = _read_raw()
        if not CONFIG_FILE.exists():
            _write_raw(data)
        return data


def save_config(updates: dict) -> None:
    """Merges `updates` into the existing config (preserving every other
    key, including ones outside DEFAULT_CONFIG's schema) and writes
    atomically. The single read-modify-write choke point for every save_*
    helper below, so they all get corruption-safety and atomicity for free."""
    with _LOCK:
        data = _read_raw()
        data.update(updates)
        _write_raw(data)


def save_credential(key: str, value: str) -> None:
    save_config({key: (value or "").strip()})


def get_credential(key: str):
    return load_api_keys().get(key)


def save_api_keys(gemini_api_key: str) -> None:
    save_config({"gemini_api_key": gemini_api_key.strip()})


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
        "user_name": user_name.strip(),
    })


def get_brief_enabled() -> bool:
    return load_api_keys().get("morning_brief_enabled", True)


def save_brief_enabled(enabled: bool) -> None:
    save_config({"morning_brief_enabled": bool(enabled)})


def get_proactive_enabled() -> bool:
    """Whether the background proactive observer is enabled. Defaults on."""
    return bool(load_api_keys().get("proactive_enabled", True))


def save_proactive_enabled(enabled: bool) -> None:
    save_config({"proactive_enabled": bool(enabled)})


def get_proactive_quiet_hours():
    """A single (start_hour, end_hour) range, or None if disabled/unset —
    defaults to (22, 8). A range may cross midnight, e.g. (22, 8). Real
    callers (core/headless/tool_executor.py, core/headless/background.py)
    index this directly as quiet_hours[0]/[1], so the shape here must stay
    a single tuple, not a list of ranges."""
    raw = load_api_keys().get("proactive_quiet_hours", (22, 8))
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        try:
            start, end = int(raw[0]), int(raw[1])
            if 0 <= start <= 23 and 0 <= end <= 23:
                return (start, end)
        except (TypeError, ValueError):
            pass
    return None


def save_proactive_quiet_hours(start, end) -> None:
    if start is None or end is None:
        save_config({"proactive_quiet_hours": None})
    else:
        save_config({"proactive_quiet_hours": [int(start), int(end)]})
