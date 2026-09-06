"""Bearer-token auth for the headless orchestrator API.

Fixes the J1 finding that the desktop dashboard's /3d/api/* surface (see
dashboard/server.py) had no authentication at all — this headless API
never repeats that mistake: every route that isn't the bare health check
requires a valid Authorization: Bearer <token> header, checked against
JARVIS_API_TOKEN (core/headless/config.py). No default token, no
hardcoded fallback — if the env var isn't set, every protected route
returns 401, closed by default rather than open by default.
"""
from __future__ import annotations

import hmac

from fastapi import HTTPException, Request

from core.headless import config


def _extract_token(req: Request) -> str:
    return req.headers.get("authorization", "").removeprefix("Bearer ").strip()


def require_auth(req: Request) -> None:
    """FastAPI dependency — raises 401 unless the request carries a token
    that matches JARVIS_API_TOKEN exactly (constant-time compare, so a
    timing side-channel can't be used to guess it one byte at a time)."""
    configured = config.API_TOKEN
    if not configured:
        raise HTTPException(status_code=503, detail="JARVIS_API_TOKEN is not configured on this server.")
    provided = _extract_token(req)
    if not provided or not hmac.compare_digest(provided, configured):
        raise HTTPException(status_code=401, detail="Unauthorized")


def check_token(token: str) -> bool:
    """Same check as require_auth, usable outside a FastAPI Request (e.g.
    a WebSocket's query-string token, since browsers can't set custom
    headers on a WebSocket handshake)."""
    configured = config.API_TOKEN
    if not configured or not token:
        return False
    return hmac.compare_digest(token, configured)
