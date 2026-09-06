"""Groq client + Gemini-dialect -> OpenAI-dialect tool schema translation.

Added as the P0 zero-cost provider fix: Gemini's free tier caps at 20
requests/day and 429s the moment that's hit, with no way to test JARVIS's
actual tool-execution workloads without either paying or waiting for the
daily reset. Groq's free tier requires no credit card, supports real
OpenAI-compatible function/tool calling on current models (e.g.
openai/gpt-oss-120b), and its daily request caps are one to three orders
of magnitude more generous (published limits: roughly 1,000-14,400
requests/day depending on model, vs Gemini's 20) — see the P0 audit for
the sourcing. It is not a permanent replacement for Gemini or Anthropic,
just the actual zero-cost option for development/testing that Gemini's
free tier no longer practically is.

Used in core/headless/ui.py::run_chat_turn's provider chain — see
_configured_providers() there for the priority order and how a provider
is added/removed/reordered purely through which API key env vars are set,
no code change required.
"""
from __future__ import annotations

from typing import Any

# 60s to match gemini_client.py/anthropic_client.py's DEFAULT_TIMEOUT —
# same reasoning: generous for a real tool-calling round trip, short
# enough that a stalled connection fails fast rather than hanging the
# request queue behind it.
DEFAULT_TIMEOUT_S = 60.0

# Groq has deprecated its earlier Llama chat models (llama-3.3-70b-versatile,
# llama-3.1-8b-instant) in favor of the GPT-OSS family, which supports real
# function/tool calling on the free tier. Kept as one named constant so a
# future model swap (deprecation, better option) is a one-line change.
CHAT_MODEL = "openai/gpt-oss-120b"


def get_client(api_key: str, timeout_s: float = DEFAULT_TIMEOUT_S):
    from groq import Groq
    return Groq(api_key=api_key, timeout=timeout_s)


# Groq's API is OpenAI-compatible: tool schema is
# {"type": "function", "function": {"name", "description", "parameters"}}
# where `parameters` is plain (lower-case-typed) JSON Schema — the exact
# same shape Anthropic's `input_schema` needs, so this reuses the identical
# Gemini-dialect (upper-case "STRING"/"OBJECT"/...) -> JSON-Schema type-name
# mapping anthropic_client.py already established, rather than a second
# copy of the same table.
_GEMINI_TO_JSON_SCHEMA_TYPE = {
    "STRING": "string", "OBJECT": "object", "INTEGER": "integer",
    "NUMBER": "number", "BOOLEAN": "boolean", "ARRAY": "array",
}

# Groq's free tier caps at 8,000 tokens/minute (TPM) — far below Gemini's
# and Anthropic's limits, which is why the full, prose-length tool
# descriptions (written for those larger-context models; several run
# 800-1600+ characters each) were blowing the budget on their own, before
# the system prompt or the user's message were even counted (a real
# request measured 9,276 tokens against an 8,000 limit). Capping
# description length — for Groq's request only, nothing else — keeps
# every tool, every parameter, and every required field fully intact;
# only the descriptive prose shrinks. No tool becomes uncallable.
_MAX_DESCRIPTION_CHARS = 60


def _trim_descriptions(node: Any) -> Any:
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "description" and isinstance(v, str) and len(v) > _MAX_DESCRIPTION_CHARS:
                out[k] = v[:_MAX_DESCRIPTION_CHARS].rsplit(" ", 1)[0] + "..."
            else:
                out[k] = _trim_descriptions(v)
        return out
    if isinstance(node, list):
        return [_trim_descriptions(v) for v in node]
    return node


def _convert_schema_types(node: Any) -> Any:
    if isinstance(node, dict):
        return {
            k: (_GEMINI_TO_JSON_SCHEMA_TYPE.get(v, v) if k == "type" and isinstance(v, str) else _convert_schema_types(v))
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [_convert_schema_types(v) for v in node]
    return node


def gemini_tools_to_openai(tool_declarations: list[dict]) -> list[dict]:
    """Converts this codebase's Gemini-dialect tool declarations (name/
    description/parameters) into OpenAI/Groq's tool schema (a
    {"type": "function", "function": {...}} wrapper around name/
    description/parameters), with every description (tool-level and
    parameter-level) capped to _MAX_DESCRIPTION_CHARS — see that
    constant's comment for why this exists and why it's safe."""
    trimmed = [_trim_descriptions(t) for t in tool_declarations]
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": _convert_schema_types(t.get("parameters") or {"type": "object", "properties": {}}),
            },
        }
        for t in trimmed
    ]
