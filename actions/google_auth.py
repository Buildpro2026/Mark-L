"""Google OAuth 2.0 — shared authorization for Gmail + Calendar.

Config: config/google/client_secret_*.json (a Desktop App OAuth credential
downloaded from Google Cloud Console — never hardcoded, never printed,
never committed; config/google/ is gitignored, see .gitignore). Token
cache: config/google/token.json, created only after the one-time
interactive consent flow completes; also gitignored.

Both actions/gmail_integration.py and actions/calendar_integration.py go
through get_credentials() here rather than each running their own OAuth
flow — one credential file, one consent screen, one cached token, matching
how every other integration in this codebase (Twilio/HubSpot/Buffer) has
exactly one auth entry point per external account. This is deliberately
NOT a second/parallel credential system.

Least-privilege scopes: gmail.readonly (read only) + gmail.compose (drafts
AND send — Gmail API's own scope semantics bundle those two; there is no
narrower scope that allows drafting without also permitting send) +
calendar.events (read/write events specifically, not full calendar
settings/sharing). Nothing broader (gmail.modify, full Calendar scope) is
requested.

CRITICAL: authorize_interactively() is the ONLY function in this module
that ever launches the interactive browser consent flow, and it must be
run deliberately by a human at a keyboard — no other function here
(get_credentials, get_credential_status, verify_google_auth) triggers it
automatically, even when no valid token exists.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

BASE_DIR = Path(__file__).resolve().parent.parent
GOOGLE_CONFIG_DIR = BASE_DIR / "config" / "google"
TOKEN_PATH = GOOGLE_CONFIG_DIR / "token.json"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/calendar.events",
]


def _find_client_secret_file() -> Path | None:
    if not GOOGLE_CONFIG_DIR.exists():
        return None
    candidates = sorted(GOOGLE_CONFIG_DIR.glob("client_secret*.json"))
    return candidates[0] if candidates else None


def is_configured() -> bool:
    return _find_client_secret_file() is not None


def _has_valid_cached_token() -> bool:
    if not TOKEN_PATH.exists():
        return False
    try:
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        return bool(creds and (creds.valid or (creds.expired and creds.refresh_token)))
    except Exception:
        return False


def get_credential_status() -> dict[str, Any]:
    """Structure-only check — never reads or returns client_id,
    client_secret, or token values, only presence/shape/validity. This is
    the 'don't assume OAuth works just because the file exists' check: it
    distinguishes credential-file-present from token-cached from
    actually-currently-valid, which are three different things."""
    secret_file = _find_client_secret_file()
    if secret_file is None:
        return {"credential_file": "missing", "credential_type": None, "token_cached": False, "authorized": False}
    try:
        data = json.loads(secret_file.read_text(encoding="utf-8"))
        if "installed" in data:
            cred_type = "installed"
        elif "web" in data:
            cred_type = "web"
        else:
            cred_type = "unknown"
    except Exception:
        cred_type = "malformed"
    return {
        "credential_file": "present",
        "credential_type": cred_type,
        "token_cached": TOKEN_PATH.exists(),
        "authorized": _has_valid_cached_token(),
    }


def get_credentials() -> Credentials:
    """Returns valid Credentials, refreshing an expired-but-refreshable
    token automatically. Raises RuntimeError (never triggers the
    interactive flow itself) when no usable token exists — the caller
    (gmail_integration.py / calendar_integration.py) surfaces that as an
    honest NOT_AUTHORIZED result rather than crashing or fabricating data."""
    if not TOKEN_PATH.exists():
        raise RuntimeError(
            "Google account not yet authorized. Run "
            "`python -c \"from actions.google_auth import authorize_interactively as a; a()\"` "
            "once to complete the one-time browser consent step."
        )
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
        return creds
    raise RuntimeError(
        "Cached Google token is invalid/expired and has no refresh token — "
        "re-run authorize_interactively() to re-authorize."
    )


def verify_google_auth() -> dict[str, Any]:
    """Safe, read-only live verification: if a valid token is cached, makes
    ONE cheap real Gmail API call (users.getProfile — returns just the
    account's own email address and message counts, no message content) to
    confirm the token genuinely authenticates right now, rather than just
    trusting that the cached file looks valid. Never triggers the
    interactive consent flow and never exposes token contents."""
    status = get_credential_status()
    if status["credential_file"] == "missing":
        return {**status, "verified": False, "detail": "No Google client-secret JSON found in config/google/."}
    if not status["token_cached"]:
        return {**status, "verified": False, "detail": "No cached token — authorize_interactively() has not been run yet."}
    try:
        creds = get_credentials()
        from googleapiclient.discovery import build
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        profile = service.users().getProfile(userId="me").execute()
        return {
            **status, "verified": True,
            "detail": f"Authenticated as {profile.get('emailAddress')}.",
            "messages_total": profile.get("messagesTotal"),
        }
    except Exception as exc:
        return {**status, "verified": False, "detail": f"Token present but verification call failed: {exc}"}


def ensure_credentials_from_env() -> None:
    """Cloud bootstrap (J3 Part 2): a headless container has no browser to
    run authorize_interactively() in, so it can't complete a fresh OAuth
    consent flow — but Lee already has, on the desktop. If
    GOOGLE_TOKEN_JSON / GOOGLE_CLIENT_SECRET_JSON env vars are set (a
    one-time copy of the already-authorized config/google/token.json and
    client_secret_*.json contents — see .env.example), this writes them
    into GOOGLE_CONFIG_DIR on first run so the deployed service starts
    already-authorized instead of NOT_AUTHORIZED.

    Never overwrites a file that already exists (a real local file always
    wins over the env var), never logs or returns their content, and is a
    complete no-op if neither env var is set — safe to call unconditionally
    at startup."""
    import os
    token_json = os.environ.get("GOOGLE_TOKEN_JSON")
    secret_json = os.environ.get("GOOGLE_CLIENT_SECRET_JSON")
    if not token_json and not secret_json:
        return
    GOOGLE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if token_json and not TOKEN_PATH.exists():
        TOKEN_PATH.write_text(token_json, encoding="utf-8")
    if secret_json and _find_client_secret_file() is None:
        (GOOGLE_CONFIG_DIR / "client_secret_from_env.json").write_text(secret_json, encoding="utf-8")


def authorize_interactively() -> dict[str, Any]:
    """Runs the one-time interactive OAuth consent flow: opens a real
    browser window to Google's consent screen and starts a local callback
    server, BLOCKING until a human completes it. Must be invoked
    deliberately (e.g. from a Python shell or a one-off script) — never
    called automatically by any other function in this module or by
    gmail_integration.py/calendar_integration.py."""
    secret_file = _find_client_secret_file()
    if secret_file is None:
        raise RuntimeError(f"No Google client-secret JSON found in {GOOGLE_CONFIG_DIR}")
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(str(secret_file), SCOPES)
    creds = flow.run_local_server(port=0)
    GOOGLE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    return {"authorized": True, "token_path": str(TOKEN_PATH)}
