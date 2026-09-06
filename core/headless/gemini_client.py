"""Shared, timeout-bounded google-genai client constructor.

genai.Client(api_key=...) alone sets NO request timeout at all. The SDK's
own httpx client args default to timeout=None, which in httpx means
"block forever," not "use a sane default." Found live during the Phase 3
latency investigation: a stalled connection to Gemini's REST API could
hang a request indefinitely, and since chat turns are processed one at a
time (core/headless/dashboard_bridge.py's queue), a single hung call
stalled every other message queued behind it too. This is the real
explanation behind "a normal question sometimes takes 5 to 10 minutes,"
not slow reasoning.

Every REST-based (generate_content) Gemini client construction in this
codebase should go through get_client() instead of calling genai.Client()
directly, so this fix has one place to live instead of sixteen.

Scope: this does not touch main.py's Gemini Live (voice, websocket)
client. That is a different protocol with its own already-robust
reconnect/backoff handling (see main.py's _classify_connection_error),
and a long-lived streaming session isn't the same problem as a single
REST call that should return in seconds. This module covers the
request/response text-generation path only.
"""
from __future__ import annotations

from google import genai
from google.genai import types

# 60s is generous for a real response (including a tool-calling round
# trip) and short enough that a stalled connection fails fast instead of
# blocking every later message behind it.
DEFAULT_TIMEOUT_MS = 60_000


def get_client(api_key: str, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> genai.Client:
    return genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=timeout_ms))
