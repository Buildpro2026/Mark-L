"""core/headless/anthropic_client.py — the Claude fallback client and its
Gemini-dialect -> Anthropic tool-schema translation.

ANTHROPIC_TOKEN sat in .env, validated live, completely unwired since the
Phase 1 audit. Found live 2026-08-19: the deployed Gemini key is on the
free tier's 20-requests/day cap and every chat request fails with a 429
once exhausted — this is the fix. These tests cover the schema
translation (the part most likely to silently drift from
tool_registry.py's real declarations) and that get_client() actually
applies a bounded timeout, matching gemini_client.py's own tests.
"""
from core.headless.anthropic_client import get_client, gemini_tools_to_anthropic, DEFAULT_TIMEOUT_S
from core.headless.tool_registry import TOOL_DECLARATIONS


def test_get_client_sets_a_bounded_timeout():
    client = get_client("fake-key-not-real")
    assert client.timeout == DEFAULT_TIMEOUT_S


def test_get_client_respects_a_custom_timeout():
    client = get_client("fake-key-not-real", timeout_s=5.0)
    assert client.timeout == 5.0


def test_converts_flat_gemini_types_to_json_schema():
    gemini_tools = [{
        "name": "example_tool",
        "description": "An example.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "a_string": {"type": "STRING", "description": "x"},
                "a_number": {"type": "NUMBER"},
                "an_int": {"type": "INTEGER"},
                "a_bool": {"type": "BOOLEAN"},
            },
            "required": ["a_string"],
        },
    }]
    out = gemini_tools_to_anthropic(gemini_tools)
    assert len(out) == 1
    schema = out[0]["input_schema"]
    assert schema["type"] == "object"
    assert schema["properties"]["a_string"]["type"] == "string"
    assert schema["properties"]["a_number"]["type"] == "number"
    assert schema["properties"]["an_int"]["type"] == "integer"
    assert schema["properties"]["a_bool"]["type"] == "boolean"
    assert schema["required"] == ["a_string"]
    assert out[0]["name"] == "example_tool"
    assert out[0]["description"] == "An example."


def test_converts_nested_array_and_object_types():
    gemini_tools = [{
        "name": "nested_tool",
        "description": "",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "items": {"type": "ARRAY", "items": {"type": "STRING"}},
                "nested": {"type": "OBJECT", "properties": {"inner": {"type": "INTEGER"}}},
            },
        },
    }]
    schema = gemini_tools_to_anthropic(gemini_tools)[0]["input_schema"]
    assert schema["properties"]["items"]["type"] == "array"
    assert schema["properties"]["items"]["items"]["type"] == "string"
    assert schema["properties"]["nested"]["type"] == "object"
    assert schema["properties"]["nested"]["properties"]["inner"]["type"] == "integer"


def _walk_type_values(node):
    # A schema can legitimately have a *property* named "type" (e.g. an
    # "event type" field) whose value is itself a nested schema dict, not
    # a type-name string — only treat a "type" key as a type declaration
    # when its value is actually a string, and always keep recursing
    # regardless of the key name so nested schemas still get checked.
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "type" and isinstance(v, str):
                yield v
            yield from _walk_type_values(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_type_values(v)


def test_every_real_tool_declaration_converts_without_error():
    # Runs the actual tool_registry.py declarations through the converter —
    # the real regression guard: a new tool with an unexpected schema shape
    # should fail here, not silently produce a broken Anthropic tool.
    out = gemini_tools_to_anthropic(TOOL_DECLARATIONS)
    assert len(out) == len(TOOL_DECLARATIONS)
    gemini_dialect = {"STRING", "OBJECT", "INTEGER", "NUMBER", "BOOLEAN", "ARRAY"}
    for tool in out:
        assert tool["name"]
        assert tool["input_schema"]["type"] == "object"
        # No leftover Gemini-dialect upper-case type should survive anywhere.
        found = set(_walk_type_values(tool["input_schema"]))
        assert not (found & gemini_dialect), f"{tool['name']} still has Gemini-dialect types: {found & gemini_dialect}"
