from __future__ import annotations

from typing import Any

try:
    from actions.google_auth import get_credential_status
except Exception:
    get_credential_status = None

# Microsoft/Outlook has no real integration anywhere in this codebase today
# (confirmed absent — no client, no auth flow, no config key ever read for
# it) — "not_connected" here is honest, not a bug, unlike the google check
# this replaced (see git history: it used to check a "google_credentials"
# config/api_keys.json key that the real Google OAuth flow never writes to,
# so email/calendar always reported not_connected even when genuinely
# authorized via config/google/token.json).


def _google_status() -> dict[str, Any]:
    if get_credential_status is None:
        return {"status": "not_connected", "account_count": 0}
    try:
        status = get_credential_status()
    except Exception:
        return {"status": "not_connected", "account_count": 0}
    return {
        "status": "connected" if status.get("authorized") else "not_connected",
        "account_count": 1 if status.get("authorized") else 0,
    }


def build_provider_status_payload() -> dict[str, Any]:
    google = _google_status()
    microsoft = {"status": "not_connected", "account_count": 0}
    google_connected = google["status"] == "connected"
    return {
        "email": {
            "status": "connected" if google_connected else "not_connected",
            "providers": {
                "google": google,
                "microsoft": microsoft,
                "ionos": {"status": "not_connected", "account_count": 0},
            },
        },
        "calendar": {
            "status": "connected" if google_connected else "not_connected",
            "providers": {
                "google": google,
                "microsoft": microsoft,
            },
        },
    }


def normalize_email_message(source: str, subject: str, sender: str, body: str, **extra: Any) -> dict[str, Any]:
    return {
        "provider": source,
        "subject": subject,
        "sender": sender,
        "body": body,
        "approved": False,
        "metadata": extra,
    }


def normalize_calendar_event(provider: str, title: str, start: str, end: str, **extra: Any) -> dict[str, Any]:
    return {
        "provider": provider,
        "title": title,
        "start": start,
        "end": end,
        "approved": False,
        "metadata": extra,
    }


def get_oauth_setup_hint(provider: str) -> dict[str, Any]:
    return {
        "provider": provider,
        "status": "pending_credentials",
        "authorization_url": f"/{provider}/oauth/start",
        "notes": "Real credentials are required to complete live authorization.",
    }


def get_approval_gate(action: str) -> dict[str, Any]:
    return {
        "action": action,
        "requires_approval": True,
        "message": f"{action} requires explicit approval before execution.",
    }
