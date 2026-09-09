"""One place to ask "what can JARVIS actually do right now?".

Every integration already knows its own state, and each reports it well —
but only individually, and each in its own vocabulary. Nothing could answer
the question the CEO cycle actually needs before it starts work: which
business operations are available this morning? So the cycle attempted
everything every time and discovered the answer through failures.

This aggregates the existing checks. It does not re-implement any of them,
does not introduce new environment variables, and does not change how any
credential is loaded — it calls each integration's own is_configured() /
verify_*() / status functions and normalises the answers onto one
vocabulary.

Secret safety is structural, not a convention to remember: this module
reads booleans and status strings from the integrations and never handles a
credential value. redact() exists for the one place values could otherwise
leak — a detail string produced by an upstream library — and every detail
that reaches a report passes through it.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable

logger = logging.getLogger("jarvis.integration_health")

# Shared vocabulary, matching actions/business_pipeline.py so the CEO cycle
# reads one set of words across both layers.
CONFIGURED = "CONFIGURED"          # credentials present and a live check passed
NOT_CONFIGURED = "NOT_CONFIGURED"  # no credential set — expected, not a fault
AUTH_ERROR = "AUTH_ERROR"          # credential present but rejected/expired/unconsented
UNAVAILABLE = "UNAVAILABLE"        # configured, but the service could not be reached
UNKNOWN = "UNKNOWN"                # the integration offers no way to tell

# Anything that looks like a credential, redacted before it can reach a log,
# a report, or an API response. Deliberately broad: a false positive costs a
# less readable error message, a false negative leaks a secret.
_SECRETISH = re.compile(
    r"(?i)\b(?:bearer\s+)?"
    r"(?:sk-[A-Za-z0-9_\-]{8,}|AC[0-9a-f]{30,}|ya29\.[A-Za-z0-9_\-]{10,}"
    r"|pat-[A-Za-z0-9_\-]{8,}|[A-Za-z0-9_\-]{32,})\b"
)


def redact(text: Any) -> str:
    """Replaces anything credential-shaped with [REDACTED]."""
    if text is None:
        return ""
    return _SECRETISH.sub("[REDACTED]", str(text))


def _probe(name: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Runs one integration's own check, isolated. An integration that
    raises is reported as UNAVAILABLE with a redacted reason — it can never
    take down the survey or the caller."""
    try:
        result = fn()
    except Exception as exc:
        logger.debug("integration probe %r raised", name, exc_info=True)
        return {"name": name, "state": UNAVAILABLE, "detail": redact(exc)}
    result.setdefault("name", name)
    result["detail"] = redact(result.get("detail"))
    return result


# ── individual probes ────────────────────────────────────────────────────

def _google() -> dict[str, Any]:
    from actions import google_auth
    status = google_auth.get_credential_status()
    if status.get("credential_file") == "missing" and not status.get("token_cached"):
        return {"state": NOT_CONFIGURED, "detail": "no Google client secret or cached token"}
    if not status.get("authorized"):
        return {"state": AUTH_ERROR, "detail": "cached Google token is missing or not refreshable"}
    # Names only, never values. A non-empty list means those specific APIs
    # will fail with insufficient-scope until someone re-consents — it does
    # NOT mean Gmail and Calendar are broken, which is the whole point of
    # the Phase B scope fix.
    missing = status.get("missing_scopes") or []
    return {
        "state": CONFIGURED,
        "detail": (f"consented; not granted: {', '.join(s.rsplit('/', 1)[-1] for s in missing)}"
                   if missing else None),
        "missing_scopes": missing,
    }


def _hubspot() -> dict[str, Any]:
    """HubSpot API health — and ONLY the API.

    Two things are deliberately kept apart here, because conflating them is
    how a broken CRM looks healthy:

      * API ACCESS — can JARVIS read the CRM with its token, and is that
        token pointed at the RIGHT portal. Both are checkable from here.
      * HUMAN LOGIN — can a person sign in to that portal in a browser.
        Nothing a private-app token can call reveals this. Whether an
        address is an active login, which authentication method it uses,
        whether 2FA is on, who the super-admin is — all of that lives in
        account settings that no token can read. So this never reports on
        it, and never lets API success imply it.

    A token on the wrong portal is reported as AUTH_ERROR rather than
    CONFIGURED: reading a different company's CRM is a failure, even though
    every call succeeds."""
    from actions import hubspot_integration as hs
    if not hs.is_configured():
        return {"state": NOT_CONFIGURED, "detail": "HUBSPOT_TOKEN is not set",
                "human_login_verifiable": False}

    portal = hs.verify_expected_portal()
    base = {
        "portal_id": portal.get("portal_id"),
        "portal_match": portal.get("portal_match"),
        "ui_domain": portal.get("ui_domain"),
        # Stated on every result so no reader can mistake API health for
        # a working human login.
        "human_login_verifiable": False,
        "human_login_note": ("A private-app token cannot see logins, auth methods or 2FA. "
                             "Human portal access must be confirmed in the HubSpot UI."),
    }

    if not portal.get("verified"):
        detail = str(portal.get("detail") or portal.get("state") or "")
        state = AUTH_ERROR if "401" in detail or "auth" in detail.lower() else UNAVAILABLE
        return {**base, "state": state, "detail": detail}

    if portal.get("portal_match") == "MISMATCH":
        return {**base, "state": AUTH_ERROR, "detail": portal.get("detail")}

    return {**base, "state": CONFIGURED, "detail": portal.get("detail")}


def _buffer() -> dict[str, Any]:
    from actions import buffer_integration as buf
    result = buf.verify_buffer()
    if not result.get("configured"):
        return {"state": NOT_CONFIGURED, "detail": "BUFFER_TOKEN is not set"}
    status = str(result.get("status", "")).lower()
    if status in ("ok", "connected", "success"):
        return {"state": CONFIGURED}
    return {"state": AUTH_ERROR if "auth" in status or "unauthor" in status else UNAVAILABLE,
            "detail": result.get("detail") or result.get("status")}


def _twilio() -> dict[str, Any]:
    from actions import twilio_integration as tw
    if not tw.is_configured():
        return {"state": NOT_CONFIGURED, "detail": "Twilio credentials are not set"}
    status = tw.get_status()
    state = str(status.get("state", "")).lower()
    if state in ("ok", "ready", "configured"):
        return {"state": CONFIGURED}
    return {"state": UNAVAILABLE, "detail": status.get("detail") or status.get("state")}


def _owner_phone() -> dict[str, Any]:
    """Not an API, but every alert path depends on it — an unset owner
    number means notifications are silently going nowhere."""
    from core.headless import config
    if not config.JARVIS_OWNER_PHONE:
        return {"state": NOT_CONFIGURED, "detail": "JARVIS_OWNER_PHONE is not set — no alert can be delivered"}
    return {"state": CONFIGURED}


def _llm() -> dict[str, Any]:
    from core.headless import config
    for key, label in ((getattr(config, "OLLAMA_API_KEY", None), "Ollama"),
                       (getattr(config, "GROQ_API_KEY", None), "Groq"),
                       (getattr(config, "GEMINI_API_KEY", None), "Gemini")):
        if key:
            return {"state": CONFIGURED, "detail": f"{label} key present"}
    return {"state": NOT_CONFIGURED, "detail": "no LLM provider key is set"}


def _product_data() -> dict[str, Any]:
    from actions import ddf_discovery
    if not ddf_discovery.is_configured():
        return {"state": NOT_CONFIGURED,
                "detail": "no product-data API key — discovery falls back to existing sources"}
    return {"state": CONFIGURED}


def _brain() -> dict[str, Any]:
    from actions import jarvis_brain
    st = jarvis_brain.status()
    if not st.get("configured"):
        return {"state": NOT_CONFIGURED, "detail": "no knowledge vault configured"}
    if not st.get("available"):
        return {"state": UNAVAILABLE, "detail": st.get("detail") or "vault path is not readable"}
    return {"state": CONFIGURED}


def _web_research() -> dict[str, Any]:
    """No credential to check — the search path is DuckDuckGo scraping, not
    an API key. The only honest question is whether it can currently reach
    the network, so this is a live one-result search rather than an
    env-var check, matching how _hubspot() and _buffer() already work."""
    from actions import web_research
    result = web_research.search("jarvis integration health check", max_results=1)
    if result.get("ok"):
        return {"state": CONFIGURED}
    return {"state": UNAVAILABLE, "detail": result.get("detail") or "search returned no sources"}


def _voice() -> dict[str, Any]:
    """The configured voice provider's credential, not the audio pipeline
    itself (there is no server-side speaker/mic to probe). Gemini is the
    project's primary/default provider (voice 'Charon'); a non-Gemini
    provider is reported CONFIGURED as-is — this probe states facts, it
    does not enforce the project's provider policy."""
    from actions import voice_manager
    from core.headless import config
    cfg = voice_manager.get_voice_provider_config()
    provider = cfg.get("provider")
    if provider == "gemini":
        if not getattr(config, "GEMINI_API_KEY", None):
            return {"state": NOT_CONFIGURED,
                    "detail": "gemini voice selected but GEMINI_API_KEY is not set"}
        return {"state": CONFIGURED, "detail": f"gemini voice '{cfg.get('voice')}'"}
    if provider == "elevenlabs":
        from core.headless import config as _cfg
        if not getattr(_cfg, "ELEVENLABS_API_KEY", None):
            return {"state": NOT_CONFIGURED, "detail": "elevenlabs voice selected but no API key is set"}
        return {"state": CONFIGURED, "detail": "elevenlabs voice configured"}
    return {"state": CONFIGURED, "detail": f"local voice engine ('{provider}')"}


def _buildpro_store() -> dict[str, Any]:
    """Local sqlite storage — never NOT_CONFIGURED (there is no credential),
    only CONFIGURED or UNAVAILABLE if the file can't be opened."""
    from actions import buildpro_data
    try:
        conn = buildpro_data._connect()
        conn.execute("SELECT 1")
        conn.close()
        return {"state": CONFIGURED}
    except Exception as exc:
        return {"state": UNAVAILABLE, "detail": str(exc)}


def _ceo_cycle() -> dict[str, Any]:
    """Whether the daily CEO cycle is actually running, not merely wired
    correctly. NOT_CONFIGURED covers a fresh install that has never
    completed a run yet — expected, not a fault. Past that, staleness is
    the only honest signal available here: the cycle runs once a day (see
    ceo_operating_cycle's own already_ran_today()), so a gap past a day
    plus slack for a delayed trigger means the thing that should be
    waking it — the Render Cron Job, or the in-process loop when that flag
    is set — stopped, and JARVIS should say so rather than silently make
    decisions on stale business state."""
    from actions import ceo_operating_cycle
    last = ceo_operating_cycle.last_run_info()
    if not last:
        return {"state": NOT_CONFIGURED, "detail": "the CEO cycle has never completed a run"}
    run_ts = float(last.get("run_ts") or 0)
    age_hours = (time.time() - run_ts) / 3600.0 if run_ts else None
    if age_hours is None or age_hours > 36:
        return {"state": UNAVAILABLE,
                "detail": f"last cycle ran {last.get('run_date')}"
                          + (f" ({age_hours:.0f}h ago)" if age_hours is not None else "")}
    return {"state": CONFIGURED, "detail": f"last ran {last.get('run_date')}"}


PROBES: dict[str, Callable[[], dict[str, Any]]] = {
    "google": _google,
    "hubspot": _hubspot,
    "buffer": _buffer,
    "twilio": _twilio,
    "owner_phone": _owner_phone,
    "llm": _llm,
    "product_data": _product_data,
    "jarvis_brain": _brain,
    "web_research": _web_research,
    "buildpro_store": _buildpro_store,
    "voice": _voice,
    "ceo_cycle": _ceo_cycle,
}

# What each business operation actually needs to be possible at all.
CAPABILITY_REQUIREMENTS: dict[str, list[str]] = {
    "email_triage": ["google"],
    "calendar_intelligence": ["google"],
    "crm_operations": ["hubspot"],
    "social_publishing": ["buffer"],
    "owner_alerts": ["twilio", "owner_phone"],
    "product_discovery": [],          # falls back to existing local sources
    "knowledge_recall": ["jarvis_brain"],
    "job_discovery": ["web_research", "buildpro_store"],
    "candidate_matching": ["buildpro_store"],
}


def check_all(probes: dict | None = None) -> dict[str, Any]:
    """Every integration's current state. Never raises."""
    probes = probes if probes is not None else PROBES
    integrations = {name: _probe(name, fn) for name, fn in probes.items()}
    healthy = [n for n, r in integrations.items() if r["state"] == CONFIGURED]
    degraded = {n: r["state"] for n, r in integrations.items() if r["state"] != CONFIGURED}
    return {
        "integrations": integrations,
        "healthy": sorted(healthy),
        "degraded": degraded,
        "capabilities": available_capabilities(integrations),
    }


def available_capabilities(integrations: dict[str, Any]) -> dict[str, bool]:
    """Which business operations are possible right now. This is what the
    CEO cycle consults BEFORE attempting work, so an unavailable capability
    is a decision not to try rather than a failure to explain afterwards."""
    out = {}
    for capability, required in CAPABILITY_REQUIREMENTS.items():
        out[capability] = all(
            (integrations.get(dep) or {}).get("state") == CONFIGURED for dep in required
        )
    return out


def can(capability: str) -> bool:
    """Convenience for a single capability check."""
    return bool(check_all()["capabilities"].get(capability, False))
