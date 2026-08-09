import json
import sys
from pathlib import Path

def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

BASE_DIR = get_base_dir()
CONFIG_DIR = BASE_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "api_keys.json"

DEFAULT_CONFIG = {
    "gemini_api_key": "",
    "github_token": "",
    "vercel_token": "",
    "hubspot_token": "",
    "make_api_token": "",
    "google_credentials": "",
    "microsoft_credentials": "",
    "buffer_token": "",
    "airtable_token": "",
    "assistant_name": "J.A.R.V.I.S.",
    "user_name": "Mr. Chandler",
    "ui_color": "#00beff",
    "morning_brief_enabled": True,
}

def ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

def load_api_keys() -> dict:
    ensure_config_dir()

    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(
            json.dumps(DEFAULT_CONFIG, indent=4),
            encoding="utf-8"
        )
        return DEFAULT_CONFIG.copy()

    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return {**DEFAULT_CONFIG, **data}
    except Exception as e:
        print(f"Failed to load api_keys.json: {e}")
        return DEFAULT_CONFIG.copy()

def save_config(updates: dict) -> None:
    ensure_config_dir()
    data = load_api_keys()
    data.update(updates)

    CONFIG_FILE.write_text(
        json.dumps(data, indent=4),
        encoding="utf-8"
    )

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

def get_credential(service: str) -> str:
    return load_api_keys().get(service, "")

def save_credential(service: str, value: str) -> None:
    save_config({
        service: value.strip()
    })