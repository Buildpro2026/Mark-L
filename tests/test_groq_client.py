"""core/headless/groq_client.py — the Groq (zero-cost) provider client and
its Gemini-dialect -> OpenAI-dialect tool-schema translation.

Added as the P0 fix: the deployed Gemini key sits on the free tier's 20
requests/day cap and 429s the moment that's hit, with no way to test
JARVIS's actual tool-execution workloads without paying or waiting for
the daily reset. Groq's free tier requires no credit card and its daily
request caps are one to three orders of magnitude more generous. These
tests cover the schema translation (the part most likely to silently
drift from tool_registry.py's real declarations) and that get_client()
actually applies a bounded timeout, matching gemini_client.py's and
anthropic_client.py's own tests.
"""
from core.headless.groq_client import get_client, gemini_tools_to_openai, DEFAULT_TIMEOUT_S
from core.headless.tool_registry import TOOL_DECLARATIONS


def test_get_client_sets_a_bounded_timeout():
    client = get_client("fake-key-not-real")
    assert client.timeout == DEFAULT_TIMEOUT_S


def test_get_client_respects_a_custom_timeout():
    client = get_client("fake-key-not-real", timeout_s=5.0)
    assert client.timeout == 5.0


def test_converts_flat_gemini_types_to_json_schema_wrapped_in_function():
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
    out = gemini_tools_to_openai(gemini_tools)
    assert len(out) == 1
    assert out[0]["type"] == "function"
    fn = out[0]["function"]
    assert fn["name"] == "example_tool"
    assert fn["description"] == "An example."
    schema = fn["parameters"]
    assert schema["type"] == "object"
    assert schema["properties"]["a_string"]["type"] == "string"
    assert schema["properties"]["a_number"]["type"] == "number"
    assert schema["properties"]["an_int"]["type"] == "integer"
    assert schema["properties"]["a_bool"]["type"] == "boolean"
    assert schema["required"] == ["a_string"]


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
    schema = gemini_tools_to_openai(gemini_tools)[0]["function"]["parameters"]
    assert schema["properties"]["items"]["type"] == "array"
    assert schema["properties"]["items"]["items"]["type"] == "string"
    assert schema["properties"]["nested"]["type"] == "object"
    assert schema["properties"]["nested"]["properties"]["inner"]["type"] == "integer"


def _walk_type_values(node):
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
    # should fail here, not silently produce a broken Groq/OpenAI tool.
    out = gemini_tools_to_openai(TOOL_DECLARATIONS)
    assert len(out) == len(TOOL_DECLARATIONS)
    gemini_dialect = {"STRING", "OBJECT", "INTEGER", "NUMBER", "BOOLEAN", "ARRAY"}
    for tool in out:
        assert tool["type"] == "function"
        assert tool["function"]["name"]
        assert tool["function"]["parameters"]["type"] == "object"
        found = set(_walk_type_values(tool["function"]["parameters"]))
        assert not (found & gemini_dialect), f"{tool['function']['name']} still has Gemini-dialect types: {found & gemini_dialect}"


def test_gemini_tools_to_openai_trims_verbose_descriptions_for_groqs_tpm_limit():
    """The actual live bug: Groq's free tier caps at 8,000 tokens/minute,
    and the full, prose-length tool descriptions (written for Gemini's/
    Anthropic's much larger context budgets) alone exceeded it — a real
    request measured 9,276 tokens against that 8,000 limit before any
    trimming existed. Every tool/parameter must survive; only verbose
    description text shrinks."""
    long_desc = "x" * 500
    tools = [{
        "name": "verbose_tool",
        "description": long_desc,
        "parameters": {
            "type": "OBJECT",
            "properties": {"arg": {"type": "STRING", "description": long_desc}},
            "required": ["arg"],
        },
    }]
    out = gemini_tools_to_openai(tools)
    assert len(out) == 1  # tool itself is never dropped
    fn = out[0]["function"]
    assert fn["name"] == "verbose_tool"  # name intact
    assert len(fn["description"]) < 100  # description actually shrank
    assert "arg" in fn["parameters"]["properties"]  # parameter still present
    assert fn["parameters"]["required"] == ["arg"]  # required-ness preserved
    assert len(fn["parameters"]["properties"]["arg"]["description"]) < 100


def test_real_tool_declarations_fit_groqs_tpm_budget_after_trimming():
    """Direct proof against the real, full tool set (not a synthetic
    example) that the fix actually solves the reported failure."""
    import json
    from core.headless.tool_registry import TOOL_DECLARATIONS, SESSION_ONLY_TOOLS
    real_tools = [t for t in TOOL_DECLARATIONS if t["name"] not in SESSION_ONLY_TOOLS]
    trimmed_json = json.dumps(gemini_tools_to_openai(real_tools))
    # Calibrated against the live failure (9,276 real tokens measured for
    # ~52,000 untrimmed chars => ~5.6 chars/token for this JSON-heavy
    # content) — a generous 5.0 chars/token keeps this a safety-margin
    # check, not a brittle exact-count assertion.
    estimated_tokens = len(trimmed_json) / 5.0
    assert estimated_tokens < 7000, f"tool schema alone estimated at {estimated_tokens:.0f} tokens"
