"""Headless runtime configuration — the CODE/CONFIG/SECRETS/RUNTIME split.

Local desktop JARVIS keeps reading config/api_keys.json (memory/config_manager.py)
for everything, unchanged — that file is git-ignored and this module doesn't
touch it. The headless service adds one more layer on top for the pieces
that only make sense for a deployed process: a bearer token for its own
API, and where on disk (or which volume) runtime data lives.

Precedence for every setting here: environment variable first, then a
sane local-dev default. Nothing in this file has a default secret — if
JARVIS_API_TOKEN isn't set, the headless API's auth always fails closed
(see auth.py) rather than falling back to something guessable.
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent


def _load_dotenv() -> None:
    """Minimal .env loader — real env vars (the ones a cloud platform sets)
    always win, this only fills in gaps. Deliberately not the python-dotenv
    package: this repo's dependency list stays lean for four lines of
    KEY=VALUE parsing. .env itself is gitignored (see .gitignore) — this
    is a no-op if the file doesn't exist, which is the normal case on a
    cloud host that sets real env vars directly."""
    env_path = BASE_DIR / ".env"
    if not env_path.is_file():
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception:
        pass   # a malformed .env must never block startup


_load_dotenv()


def _env(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name)
    return val if val not in (None, "") else default


# ── Runtime data location ───────────────────────────────────────────────
# J3 fix: the nine modules that open data/jarvis2.db (agent_orchestrator,
# business_intelligence, buildpro_data, opportunity_engine, proactive,
# twilio_integration, buffer_integration, daily_deal_finders) plus
# core/startup.py's app-instance lock now all import DATA_DIR from this
# module directly and derive their own DB_PATH/LOCK_PATH from it — see
# tests/test_headless_core.py's test_jarvis_data_dir_env_var_relocates_
# all_runtime_state_paths for end-to-end proof this actually works, not
# just that the constant exists. config/api_keys.json (secrets) and
# memory/long_term.json (personal facts) deliberately still resolve
# relative to the repo root regardless of this var — see those modules'
# own docstrings; moving them is a separate, not-yet-made decision (J4).
DATA_DIR = Path(_env("JARVIS_DATA_DIR", str(BASE_DIR / "data")))
DB_PATH = DATA_DIR / "jarvis2.db"
LOG_DIR = DATA_DIR / "logs"


def ensure_data_dir() -> None:
    """Create DATA_DIR (and LOG_DIR) if missing. Safe to call repeatedly;
    never raises — a permissions problem should surface when something
    actually tries to write, not block startup here."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


# ── Headless API auth ───────────────────────────────────────────────────
# A single pre-shared secret for the headless orchestrator API (see
# auth.py) and, optionally, the dashboard's /3d/api/* surface (see the
# dashboard/server.py patch — it accepts this as an alternate credential
# alongside its existing pairing-key session tokens). Never has a default
# value: unset means "nothing can authenticate," not "anything can."
API_TOKEN = _env("JARVIS_API_TOKEN")


# ── Network ──────────────────────────────────────────────────────────────
# Most PaaS platforms (Render, Railway, Heroku) inject a bare $PORT and
# expect the app to bind it — JARVIS_HEADLESS_PORT takes precedence if
# set explicitly, but $PORT is the realistic default in an actual deploy.
HEADLESS_HOST = _env("JARVIS_HEADLESS_HOST", "0.0.0.0")
HEADLESS_PORT = int(_env("JARVIS_HEADLESS_PORT") or _env("PORT", "8787"))

# This service's own public URL — used to build links this process sends
# out (e.g. the candidate agreement-signing link in a welcome email, see
# actions/candidate_intake.py). Same default actions/cloud_bridge.py's
# JARVIS_CLOUD_URL falls back to; a distinct env var name since that one
# is read from the desktop side of the bridge, not from inside this
# process, and the two happening to share a value today isn't guaranteed
# to stay true (e.g. once the Oracle migration lands).
PUBLIC_BASE_URL = _env("JARVIS_PUBLIC_URL", "https://jarvis-headless-core.onrender.com").rstrip("/")


# ── Obsidian vault (Step 9) ──────────────────────────────────────────────
# No hardcoded personal path — unset means "no vault configured," and
# core/headless/obsidian.py degrades to reporting that clearly rather
# than guessing a path or failing silently.
OBSIDIAN_VAULT_PATH = _env("JARVIS_OBSIDIAN_VAULT_PATH")


# ── J3 Part 2: business-integration secrets ──────────────────────────────
# Env var takes precedence; each integration module falls back to
# config/api_keys.json if the env var is unset, so local desktop use
# (which has never set these env vars) keeps working exactly as before.
# None of these have a default value — an unset credential means that
# integration reports NOT_CONFIGURED, never a fabricated success.
GEMINI_API_KEY = _env("GEMINI_API_KEY")
# Text-chat fallback only (core/headless/ui.py::run_chat_turn) when Gemini
# fails before any tool has executed this turn — see anthropic_client.py.
ANTHROPIC_TOKEN = _env("ANTHROPIC_TOKEN")
AIRTABLE_TOKEN = _env("AIRTABLE_TOKEN")
HUBSPOT_TOKEN = _env("HUBSPOT_TOKEN")
BUFFER_TOKEN = _env("BUFFER_TOKEN")
TWILIO_ACCOUNT_SID = _env("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = _env("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = _env("TWILIO_FROM_NUMBER")


def summarize() -> dict:
    """Read-only, secret-free summary for the health endpoint and startup
    banner — presence/location only, never the token value itself."""
    return {
        "data_dir": str(DATA_DIR),
        "data_dir_exists": DATA_DIR.exists(),
        "api_token_configured": bool(API_TOKEN),
        "obsidian_vault_configured": bool(OBSIDIAN_VAULT_PATH),
        "obsidian_vault_exists": bool(OBSIDIAN_VAULT_PATH and Path(OBSIDIAN_VAULT_PATH).is_dir()),
        "headless_host": HEADLESS_HOST,
        "headless_port": HEADLESS_PORT,
        "gemini_api_key_env_set": bool(GEMINI_API_KEY),
        "anthropic_token_env_set": bool(ANTHROPIC_TOKEN),
        "airtable_token_env_set": bool(AIRTABLE_TOKEN),
        "hubspot_token_env_set": bool(HUBSPOT_TOKEN),
        "buffer_token_env_set": bool(BUFFER_TOKEN),
        "twilio_env_set": bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER),
    }
