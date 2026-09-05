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

# ── LLM provider ────────────────────────────────────────────────────────
# Ollama Cloud is THE provider. One brain behind every surface — browser
# chat, the 3D console, and the Cartesia phone agent all reach it through
# run_chat_turn(). The model is env-driven so swapping it never touches
# application logic.
OLLAMA_API_KEY = _env("OLLAMA_API_KEY")
OLLAMA_URL = _env("OLLAMA_URL", "https://ollama.com/api/chat")
OLLAMA_MODEL = _env("OLLAMA_MODEL", "gpt-oss:120b-cloud")

# Kept as read-but-unused config so an existing Render variable can stay
# where it is without silently re-entering the provider chain: neither is
# listed in ui.py's _configured_providers() any more (see that function's
# own docstring — Ollama Cloud is THE provider now, not these). Groq
# specifically was removed as primary because its SDK retried a 429 twice
# (17s then 44s) before failing — a minute of dead air mid-conversation.
GEMINI_API_KEY = _env("GEMINI_API_KEY")
GROQ_API_KEY = _env("GROQ_API_KEY")
# Anthropic is intentionally disabled. This is kept as None so older fallback
# code paths cannot activate even if an obsolete Render environment variable
# remains behind.
ANTHROPIC_TOKEN = None
AIRTABLE_TOKEN = _env("AIRTABLE_TOKEN")
HUBSPOT_TOKEN = _env("HUBSPOT_TOKEN")
BUFFER_TOKEN = _env("BUFFER_TOKEN")
TWILIO_ACCOUNT_SID = _env("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = _env("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = _env("TWILIO_FROM_NUMBER")
ELEVENLABS_API_KEY = _env("ELEVENLABS_API_KEY")

# ── Cartesia (voice) ────────────────────────────────────────────────────
# One provider covers both voice surfaces Lee actually uses: the browser
# /ui speaks with Sonic (actions/cartesia_tts.py) and the phone line is a
# Cartesia Line agent (voice_agent/main.py) whose brain is this service.
# Same CARTESIA_VOICE_ID in both places is the entire reason JARVIS sounds
# like one person rather than two different assistants.
CARTESIA_API_KEY = _env("CARTESIA_API_KEY")
CARTESIA_VOICE_ID = _env("CARTESIA_VOICE_ID")
CARTESIA_TTS_MODEL = _env("CARTESIA_TTS_MODEL", "sonic-3")
# Set after `cartesia deploy` and after a number is provisioned — only
# outbound calling needs them; inbound works with neither set here.
CARTESIA_AGENT_ID = _env("CARTESIA_AGENT_ID")
CARTESIA_PHONE_NUMBER_ID = _env("CARTESIA_PHONE_NUMBER_ID")
CARTESIA_API_VERSION = _env("CARTESIA_API_VERSION", "2026-03-01")
# Where JARVIS calls when he's the one initiating (E.164, e.g. +13125550142).
JARVIS_OWNER_PHONE = _env("JARVIS_OWNER_PHONE")

# ── Product-data discovery (2026-09-03, autonomous-CEO/COS spec Section
# FIFTH) ────────────────────────────────────────────────────────────────
# Amazon's own Product Advertising API requires an approved Associates
# account (and, per Amazon's policy, 3 qualifying sales in the trailing 180
# days just to KEEP API access) — nothing in this codebase has that
# credential, so it is deliberately not the default path. This is a
# provider-agnostic key/URL pair for a third-party product-data API (e.g.
# Rainforest API, which mirrors Amazon listings for a plain API key with no
# Associates approval needed) — see actions/ddf_discovery.py. Absent, real
# discovery honestly reports NOT_CONFIGURED; nothing here fabricates data.
PRODUCT_DATA_API_KEY = _env("PRODUCT_DATA_API_KEY")
PRODUCT_DATA_API_PROVIDER = _env("PRODUCT_DATA_API_PROVIDER", "rainforest")
PRODUCT_DATA_API_URL = _env("PRODUCT_DATA_API_URL", "https://api.rainforestapi.com/request")

# ── CEO operating cycle (Section THIRD) ─────────────────────────────────
# UTC hour the autonomous morning cycle targets. No per-user timezone
# concept exists anywhere else in this codebase (see BackgroundWorker's
# other loops, all plain interval polls) — documented here rather than
# silently assumed. Runs at most once per UTC calendar date; see
# actions/ceo_operating_cycle.py's own dedup table for the exact contract.
JARVIS_CEO_CYCLE_HOUR_UTC = int(_env("JARVIS_CEO_CYCLE_HOUR_UTC", "11") or 11)  # ~06:00 US Central

def summarize() -> dict:
    return {
        "data_dir": str(DATA_DIR),
        "data_dir_exists": DATA_DIR.exists(),
        "api_token_configured": bool(API_TOKEN),
        "obsidian_vault_configured": bool(OBSIDIAN_VAULT_PATH),
        "obsidian_vault_exists": bool(OBSIDIAN_VAULT_PATH and Path(OBSIDIAN_VAULT_PATH).is_dir()),
        "headless_host": HEADLESS_HOST,
        "headless_port": HEADLESS_PORT,
        "llm_provider": "ollama",
        "ollama_api_key_env_set": bool(OLLAMA_API_KEY),
        "ollama_model": OLLAMA_MODEL,
        "ollama_url": OLLAMA_URL,
        # Present but deliberately not in the provider chain.
        "gemini_api_key_env_set": bool(GEMINI_API_KEY),
        "groq_api_key_env_set": bool(GROQ_API_KEY),
        "anthropic_token_env_set": False,
        "airtable_token_env_set": bool(AIRTABLE_TOKEN),
        "hubspot_token_env_set": bool(HUBSPOT_TOKEN),
        "buffer_token_env_set": bool(BUFFER_TOKEN),
        "twilio_env_set": bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER),
        "elevenlabs_api_key_env_set": bool(ELEVENLABS_API_KEY),
        "cartesia_api_key_env_set": bool(CARTESIA_API_KEY),
        "cartesia_voice_configured": bool(CARTESIA_API_KEY and CARTESIA_VOICE_ID),
        "cartesia_outbound_calls_ready": bool(
            CARTESIA_API_KEY and CARTESIA_AGENT_ID and CARTESIA_PHONE_NUMBER_ID
        ),
        "product_data_api_key_env_set": bool(PRODUCT_DATA_API_KEY),
        "product_data_api_provider": PRODUCT_DATA_API_PROVIDER,
        "ceo_cycle_hour_utc": JARVIS_CEO_CYCLE_HOUR_UTC,
    }
