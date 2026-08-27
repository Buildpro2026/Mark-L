import json
import sys
from pathlib import Path

def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

BASE_DIR    = get_base_dir()
CONFIG_DIR  = BASE_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "api_keys.json"

def ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

def config_exists() -> bool:
    return CONFIG_FILE.exists()

def save_api_keys(gemini_api_key: str) -> None:
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["gemini_api_key"] = gemini_api_key.strip()
    CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")

def load_api_keys() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"❌ Failed to load api_keys.json: {e}")
        return {}

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
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["assistant_name"] = assistant_name.strip() or "JARVIS"
    data["user_name"] = user_name.strip()
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")

def get_brief_enabled() -> bool:
    return load_api_keys().get("morning_brief_enabled", True)

def save_brief_enabled(enabled: bool) -> None:
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["morning_brief_enabled"] = enabled
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")

def get_proactive_enabled() -> bool:
    """Whether the background proactive observer is enabled. Defaults on."""
    return bool(load_api_keys().get("proactive_enabled", True))

def save_proactive_enabled(enabled: bool) -> None:
    ensure_config_dir()
    data = load_api_keys()
    data["proactive_enabled"] = bool(enabled)
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")

def get_proactive_quiet_hours() -> list[tuple[int, int]]:
    """Quiet-hour ranges as [(start_hour, end_hour)]. Empty by default.

    A range may cross midnight, e.g. [22, 7]. The headless observer uses
    this as a policy input rather than guessing at a user's schedule.
    """
    raw = load_api_keys().get("proactive_quiet_hours", [])
    result: list[tuple[int, int]] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                try:
                    start, end = int(item[0]), int(item[1])
                    if 0 <= start <= 23 and 0 <= end <= 23:
                        result.append((start, end))
                except (TypeError, ValueError):
                    continue
    return result

def save_proactive_quiet_hours(hours: list[tuple[int, int]]) -> None:
    ensure_config_dir()
    data = load_api_keys()
    data["proactive_quiet_hours"] = [[int(start), int(end)] for start, end in hours]
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")
