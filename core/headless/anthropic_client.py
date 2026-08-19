"""Anthropic (Claude) fallback client + Gemini-dialect -> Anthropic tool
schema translation.

ANTHROPIC_TOKEN has been sitting in .env, validated live, completely
unwired since the Phase 1 audit — this is that wiring. Used exactly one
place: core/headless/ui.py::run_chat_turn(), as a fallback when the
primary Gemini call fails before any tool has executed this turn (see
that function's docstring for why it's gated on "before any tool has
run" — avoids double-executing a consequential action like a HubSpot
write or an email send by switching providers mid-turn).

This module does not touch main.py's Gemini Live voice session — Claude
has no real-time audio API equivalent, so voice has no fallback here,
only the text/tool-calling chat path does.
"""
from __future__ import annotations

from typing import Any

# 60s to match gemini_client.py's DEFAULT_TIMEOUT_MS — same reasoning:
# generous for a real tool-calling round trip, short enough that a
# stalled connection fails fast rather than hanging the request queue.
DEFAULT_TIMEOUT_S = 60.0

CHAT_MODEL = "claude-sonnet-4-5"


def get_client(api_key: str, timeout_s: float = DEFAULT_TIMEOUT_S):
    import anthropic
    return anthropic.Anthropic(api_key=api_key, timeout=timeout_s)


# This codebase's TOOL_DECLARATIONS (core/headless/tool_registry.py) use
# Gemini's schema dialect (upper-case type names: "STRING", "OBJECT", ...).
# Anthropic's tool input_schema is plain JSON Schema (lower-case). Every
# other part of the shape (properties/required/items/description) already
# matches JSON Schema, so this only needs to fix "type" values, recursively
# (nested object/array parameters exist in a few tools).
_GEMINI_TO_JSON_SCHEMA_TYPE = {
    "STRING": "string", "OBJECT": "object", "INTEGER": "integer",
    "NUMBER": "number", "BOOLEAN": "boolean", "ARRAY": "array",
}


def _convert_schema_types(node: Any) -> Any:
    if isinstance(node, dict):
        return {
            k: (_GEMINI_TO_JSON_SCHEMA_TYPE.get(v, v) if k == "type" and isinstance(v, str) else _convert_schema_types(v))
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [_convert_schema_types(v) for v in node]
    return node


def gemini_tools_to_anthropic(tool_declarations: list[dict]) -> list[dict]:
    """Converts this codebase's Gemini-dialect tool declarations (name/
    description/parameters) into Anthropic's tool schema (name/description/
    input_schema)."""
    return [
        {
            "name": t["name"],
            "description": t.get("description", ""),
            "input_schema": _convert_schema_types(t.get("parameters") or {"type": "object", "properties": {}}),
        }
        for t in tool_declarations
    ]
