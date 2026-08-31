"""Headless runtime configuration — the CODE/CONFIG/SECRETS/RUNTIME split."""
from __future__ import annotations
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent

def _load_dotenv() -> None:
    env_path = BASE_DIR / ".env"
    if not env_path.is_file():
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception:
        pass

_load_dotenv()

def _env(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name)
    return val if val not in (None, "") else default

DATA_DIR = Path(_env("JARVIS_DATA_DIR", str(BASE_DIR / "data")))
DB_PATH = DATA_DIR / "jarvis2.db"
LOG_DIR = DATA_DIR / "logs"

def ensure_data_dir() -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

API_TOKEN = _env("JARVIS_API_TOKEN")
HEADLESS_HOST = _env("JARVIS_HEADLESS_HOST", "0.0.0.0")
HEADLESS_PORT = int(_env("JARVIS_HEADLESS_PORT") or _env("PORT", "8787"))
PUBLIC_BASE_URL = _env("JARVIS_PUBLIC_URL", "https://jarvis-headless-core.onrender.com").rstrip("/")

# Cloud JARVIS ships with the operational knowledge in the repository.
# An explicit environment path still wins when a true synced Obsidian vault
# is supplied later. Until then the committed JARVIS Brain is the canonical
# read-only cloud knowledge source.
_OBSIDIAN_DEFAULT = BASE_DIR / "knowledge" / "JARVIS Brain"
OBSIDIAN_VAULT_PATH = _env("JARVIS_OBSIDIAN_VAULT_PATH", str(_OBSIDIAN_DEFAULT))

GEMINI_API_KEY = _env("GEMINI_API_KEY")
GROQ_API_KEY = _env("GROQ_API_KEY")
# Anthropic is intentionally disabled. This is kept as None so older fallback
# code paths cannot activate even if an obsolete Render environment variable
# remains behind. Gemini is the sole cloud chat provider.
ANTHROPIC_TOKEN = None
AIRTABLE_TOKEN = _env("AIRTABLE_TOKEN")
HUBSPOT_TOKEN = _env("HUBSPOT_TOKEN")
BUFFER_TOKEN = _env("BUFFER_TOKEN")
TWILIO_ACCOUNT_SID = _env("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = _env("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = _env("TWILIO_FROM_NUMBER")
ELEVENLABS_API_KEY = _env("ELEVENLABS_API_KEY")

def summarize() -> dict:
    return {
        "data_dir": str(DATA_DIR),
        "data_dir_exists": DATA_DIR.exists(),
        "api_token_configured": bool(API_TOKEN),
        "obsidian_vault_configured": bool(OBSIDIAN_VAULT_PATH),
        "obsidian_vault_exists": bool(OBSIDIAN_VAULT_PATH and Path(OBSIDIAN_VAULT_PATH).is_dir()),
        "headless_host": HEADLESS_HOST,
        "headless_port": HEADLESS_PORT,
        "gemini_api_key_env_set": bool(GEMINI_API_KEY),
        "groq_api_key_env_set": bool(GROQ_API_KEY),
        "anthropic_token_env_set": False,
        "airtable_token_env_set": bool(AIRTABLE_TOKEN),
        "hubspot_token_env_set": bool(HUBSPOT_TOKEN),
        "buffer_token_env_set": bool(BUFFER_TOKEN),
        "twilio_env_set": bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER),
        "elevenlabs_api_key_env_set": bool(ELEVENLABS_API_KEY),
    }
