"""Ollama Cloud — JARVIS's unified production LLM provider.

Replaces Groq as the single brain behind every headless surface (browser
chat, the 3D command center, the Cartesia phone agent, tools, everything
that reaches run_chat_turn). One provider, one personality, one place a
model change happens.

Why a plain requests call instead of an SDK: the whole reason for this
migration is that Groq's SDK silently retried a 429 twice with 17s and 44s
backoff — about a minute of dead air on a phone call, with no way to see
it from the call site. A direct HTTP call has exactly the retry behavior
written here, which is none.

Endpoint: POST {OLLAMA_URL} (default https://ollama.com/api/chat), the
native Ollama chat API. Cloud auth is a bearer token. The tools array is
OpenAI-shaped ({"type": "function", "function": {...}} with JSON-Schema
parameters), so the Gemini-dialect -> JSON-Schema conversion Groq already
needed is reused verbatim from groq_client rather than reimplemented.

Response shape (non-streaming):
    {"message": {"content": str, "tool_calls": [
        {"function": {"name": str, "arguments": {...}}}]}, "done": true}

Note the arguments difference that matters: Ollama returns tool arguments
as an OBJECT, where OpenAI/Groq return a JSON STRING. parse_tool_call()
below normalizes both, so the caller never has to care.

Key safety: the key travels in a header, never in the URL or a payload,
and no error path in this module ever formats the key or the raw headers
into a message. See _safe_error().
"""
from __future__ import annotations

import logging
from typing import Any

# Reused rather than duplicated — the Gemini-dialect tool registry needs
# exactly the same JSON-Schema conversion for Ollama as it does for Groq.
from core.headless.groq_client import _convert_schema_types, _trim_descriptions

logger = logging.getLogger("jarvis.headless.ollama")

# One turn, no retries, hard ceiling. A provider that hasn't answered in
# this long has failed as far as a conversation is concerned — better a
# clean, visible error than a caller waiting a minute for a maybe.
DEFAULT_TIMEOUT_S = 20.0

# Ollama Cloud's tool-calling workhorse. Overridable via OLLAMA_MODEL so a
# model swap is an env change, never a code change.
DEFAULT_MODEL = "gpt-oss:120b-cloud"

DEFAULT_URL = "https://ollama.com/api/chat"


class OllamaError(RuntimeError):
    """Raised for any Ollama failure. The message is always safe to log."""


def _cfg():
    from core.headless import config as _hc
    return _hc


def is_configured() -> bool:
    return bool(_cfg().OLLAMA_API_KEY)


def get_model() -> str:
    return _cfg().OLLAMA_MODEL or DEFAULT_MODEL


def get_url() -> str:
    return _cfg().OLLAMA_URL or DEFAULT_URL


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _safe_error(status: int | None, body: str) -> str:
    """Never lets a key, a header dump, or an unbounded body reach a log
    line or an HTTP response. Status plus a short body slice is enough to
    debug with and carries nothing secret."""
    snippet = (body or "").strip()[:300]
    if status is None:
        return f"Ollama request failed: {snippet}" if snippet else "Ollama request failed."
    if status in (401, 403):
        return "Ollama rejected the API key (401/403). Check OLLAMA_API_KEY."
    if status == 404:
        return f"Ollama returned 404 — model '{get_model()}' may not exist on this account."
    if status == 429:
        return "Ollama rate limit (429)."
    return f"Ollama returned HTTP {status}: {snippet}"


def gemini_tools_to_ollama(tool_declarations: list[dict]) -> list[dict]:
    """Gemini-dialect declarations -> Ollama/OpenAI tool schema.

    Preserves name, description, the full parameter schema, required
    fields, and argument types. Descriptions are trimmed with the same
    helper Groq uses: it shortens prose only, never drops a tool, a
    parameter, or a required field, and it keeps request size sane on a
    40-tool registry.
    """
    trimmed = [_trim_descriptions(t) for t in tool_declarations]
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": _convert_schema_types(
                    t.get("parameters") or {"type": "object", "properties": {}}
                ),
            },
        }
        for t in trimmed
    ]


def parse_tool_call(tc: dict) -> tuple[str, dict]:
    """(name, args) from one Ollama tool_call.

    Ollama hands back arguments already decoded as an object; some builds
    and OpenAI-compatible proxies hand back a JSON string instead. Both are
    accepted here so a proxy swap can't break tool dispatch.
    """
    import json as _json

    fn = tc.get("function") or {}
    name = fn.get("name") or ""
    raw = fn.get("arguments")
    if isinstance(raw, dict):
        return name, raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = _json.loads(raw)
            return name, parsed if isinstance(parsed, dict) else {}
        except Exception:
            logger.warning("tool '%s' had unparseable arguments; calling with none", name)
            return name, {}
    return name, {}


def chat(
    messages: list[dict],
    tools: list[dict] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict:
    """One non-streaming chat round. Returns the raw `message` dict.

    No retries by design — see the module docstring. Every failure mode
    raises OllamaError with a message that is safe to log and safe to
    show, so the caller can fail fast and visibly.
    """
    import requests

    cfg = _cfg()
    api_key = cfg.OLLAMA_API_KEY
    if not api_key:
        raise OllamaError("OLLAMA_API_KEY is not configured on this server.")

    payload: dict[str, Any] = {
        "model": get_model(),
        "messages": messages,
        "stream": False,
    }
    if tools:
        payload["tools"] = tools

    try:
        resp = requests.post(
            get_url(), headers=_headers(api_key), json=payload, timeout=timeout_s
        )
    except requests.exceptions.Timeout:
        raise OllamaError(f"Ollama timed out after {timeout_s:.0f}s.")
    except requests.exceptions.ConnectionError:
        raise OllamaError("Could not reach Ollama Cloud (connection error).")
    except requests.RequestException as e:
        # type(e).__name__ only — never str(e), which can carry the full
        # request including headers on some urllib3 versions.
        raise OllamaError(f"Ollama request failed ({type(e).__name__}).")

    if not resp.ok:
        raise OllamaError(_safe_error(resp.status_code, resp.text))

    try:
        data = resp.json()
    except ValueError:
        raise OllamaError(_safe_error(None, resp.text))

    message = data.get("message")
    if not isinstance(message, dict):
        raise OllamaError("Ollama returned no message in its response.")
    return message


def check_connection(timeout_s: float = 10.0) -> dict[str, Any]:
    """Live read-only diagnostic. Costs one tiny generation, so it is
    called explicitly (by /api/status), never on every request."""
    if not is_configured():
        return {"state": "NOT_CONFIGURED", "detail": "OLLAMA_API_KEY is not set."}
    try:
        msg = chat([{"role": "user", "content": "ping"}], timeout_s=timeout_s)
    except OllamaError as e:
        return {"state": "ERROR", "detail": str(e)}
    return {
        "state": "CONNECTED",
        "detail": f"Ollama Cloud reachable ({get_model()}).",
        "replied": bool((msg.get("content") or "").strip()),
    }
