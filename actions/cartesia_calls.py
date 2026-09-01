"""Outbound conversational phone calls through the Cartesia Line agent.

The difference from actions/twilio_integration.place_call(): that one dials
a number and reads a fixed sentence out of TwiML <Say> — a robot voicemail
with no ears. This one hands the call to the deployed JARVIS voice agent,
so Lee can actually talk back and the agent can use every JARVIS tool
mid-call. That is the whole point of "he will call me for information as
well": the call has to be a conversation, not an announcement.

Twilio stays exactly where it is for SMS — Cartesia does voice, not texts.

Permission model, same as every consequential integration here:
    OBSERVE: get_status / check_connection / get_history
    EXECUTE: place_call — a REAL phone call that costs real money. Only
             after an explicit instruction that a call should happen.

Never fabricates a successful call. If Cartesia isn't configured or the
API rejects the request, that comes back as an honest failure.
"""
from __future__ import annotations

from typing import Any

API_BASE = "https://api.cartesia.ai"


def _cfg():
    from core.headless import config as _hc
    return _hc


def _headers() -> dict[str, str]:
    cfg = _cfg()
    return {
        "X-API-Key": cfg.CARTESIA_API_KEY or "",
        "Cartesia-Version": cfg.CARTESIA_API_VERSION,
        "Content-Type": "application/json",
    }


def is_configured() -> bool:
    cfg = _cfg()
    return bool(cfg.CARTESIA_API_KEY and cfg.CARTESIA_AGENT_ID and cfg.CARTESIA_PHONE_NUMBER_ID)


def get_status() -> dict[str, Any]:
    """What's actually missing, named specifically — a generic 'not
    configured' sends you hunting through four env vars."""
    cfg = _cfg()
    missing = [
        name for name, val in (
            ("CARTESIA_API_KEY", cfg.CARTESIA_API_KEY),
            ("CARTESIA_AGENT_ID", cfg.CARTESIA_AGENT_ID),
            ("CARTESIA_PHONE_NUMBER_ID", cfg.CARTESIA_PHONE_NUMBER_ID),
        ) if not val
    ]
    if missing:
        return {
            "state": "NOT_CONFIGURED",
            "detail": "Outbound voice calls need: " + ", ".join(missing) + ".",
            "missing": missing,
        }
    return {
        "state": "CONFIGURED",
        "detail": "Cartesia outbound calling is ready.",
        "agent_id": cfg.CARTESIA_AGENT_ID,
        "owner_phone_set": bool(cfg.JARVIS_OWNER_PHONE),
    }


def check_connection() -> dict[str, Any]:
    """Live read-only diagnostic — verifies the key and agent id are real.
    Called explicitly, not on every status query, to avoid pointless API
    traffic."""
    if not is_configured():
        return get_status()
    import requests

    cfg = _cfg()
    try:
        resp = requests.get(
            f"{API_BASE}/agents/{cfg.CARTESIA_AGENT_ID}",
            headers=_headers(), timeout=15,
        )
    except requests.RequestException as e:
        return {"state": "ERROR", "detail": f"Cartesia unreachable: {e}"}
    if resp.status_code in (401, 403):
        return {"state": "UNAUTHORIZED", "detail": "Cartesia API key was rejected."}
    if resp.status_code == 404:
        return {"state": "ERROR", "detail": f"Agent {cfg.CARTESIA_AGENT_ID} does not exist on this account."}
    if not resp.ok:
        return {"state": "ERROR", "detail": f"Cartesia returned HTTP {resp.status_code}: {resp.text[:200]}"}
    return {"state": "CONNECTED", "detail": "Cartesia agent reachable.", "agent": resp.json()}


def place_call(to_number: str = "", reason: str = "", metadata: dict | None = None) -> dict[str, Any]:
    """Rings `to_number` and connects it to the deployed JARVIS voice agent.

    `reason` is passed through as call metadata, which voice_agent/main.py
    reads out of call_request.metadata and uses as its opening line — so an
    outbound call starts with "Sir, the Henderson contract just came back
    signed" instead of a generic greeting that makes Lee ask why he's being
    called.

    Falls back to JARVIS_OWNER_PHONE when no number is given: "call me"
    with no number is the common case, and it should not be an error.
    """
    cfg = _cfg()
    if not is_configured():
        return {"ok": False, **get_status()}

    to = (to_number or cfg.JARVIS_OWNER_PHONE or "").strip()
    if not to:
        return {
            "ok": False, "state": "NO_RECIPIENT",
            "detail": "No number to call, and JARVIS_OWNER_PHONE isn't set.",
        }
    if not to.startswith("+"):
        return {
            "ok": False, "state": "BAD_NUMBER",
            "detail": f"'{to}' isn't E.164 format — it needs a country code, like +13125550142.",
        }

    import requests

    call_metadata = {"source": "jarvis-headless-core"}
    if reason:
        call_metadata["reason"] = reason
    if metadata:
        call_metadata.update(metadata)

    payload = {
        "from_number_id": cfg.CARTESIA_PHONE_NUMBER_ID,
        "agent_id": cfg.CARTESIA_AGENT_ID,
        "ringing_timeout_seconds": 30,
        "outbound_calls": [{"to_number": to, "metadata": call_metadata}],
    }

    try:
        resp = requests.post(f"{API_BASE}/agents/calls", headers=_headers(), json=payload, timeout=20)
    except requests.RequestException as e:
        return {"ok": False, "state": "ERROR", "detail": f"Cartesia request failed: {e}"}

    if resp.status_code in (401, 403):
        return {"ok": False, "state": "UNAUTHORIZED", "detail": "Cartesia API key was rejected."}
    if not resp.ok:
        return {"ok": False, "state": "ERROR", "detail": f"Cartesia returned HTTP {resp.status_code}: {resp.text[:300]}"}

    body = resp.json() if resp.content else {}
    calls = body.get("calls") or []
    return {
        "ok": True,
        "state": "CALLING",
        "detail": f"Calling {to} now.",
        "to_number": to,
        "agent_call_id": (calls[0].get("agent_call_id") if calls else None),
        "reason": reason,
    }


def get_history(limit: int = 10) -> dict[str, Any]:
    """Recent calls on the agent, straight from Cartesia — read-only."""
    cfg = _cfg()
    if not cfg.CARTESIA_API_KEY:
        return {"ok": False, **get_status()}
    import requests

    params = {"limit": max(1, min(limit, 50))}
    if cfg.CARTESIA_AGENT_ID:
        params["agent_id"] = cfg.CARTESIA_AGENT_ID
    try:
        resp = requests.get(f"{API_BASE}/agents/calls", headers=_headers(), params=params, timeout=15)
    except requests.RequestException as e:
        return {"ok": False, "state": "ERROR", "detail": f"Cartesia unreachable: {e}"}
    if not resp.ok:
        return {"ok": False, "state": "ERROR", "detail": f"Cartesia returned HTTP {resp.status_code}: {resp.text[:200]}"}
    body = resp.json() if resp.content else {}
    return {"ok": True, "state": "OK", "calls": body.get("data") or body.get("calls") or []}
