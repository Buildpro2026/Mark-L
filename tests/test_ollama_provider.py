"""Ollama Cloud is JARVIS's only LLM provider. These tests prove the three
things that migration has to get right: it is genuinely the only provider
selected, tool calling survives a full round trip through the real 40-tool
registry, and no failure path can leak the API key.
"""
import json

import pytest

from core.headless import config, ollama_client


# ── provider selection ──────────────────────────────────────────────────

def test_ollama_is_the_only_provider(monkeypatch):
    from core.headless import ui
    monkeypatch.setattr(config, "OLLAMA_API_KEY", "test-key")
    monkeypatch.setattr(config, "GROQ_API_KEY", "groq-key-still-set")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "gemini-key-still-set")
    # Even with the old keys present, neither may re-enter the chain.
    assert ui._configured_providers() == ["ollama"]


def test_no_provider_when_ollama_key_is_absent(monkeypatch):
    from core.headless import ui
    monkeypatch.setattr(config, "OLLAMA_API_KEY", None)
    monkeypatch.setattr(config, "GROQ_API_KEY", "groq-key-still-set")
    # Fails clearly rather than silently answering with a different model.
    assert ui._configured_providers() == []


def test_ollama_runner_is_registered():
    from core.headless import ui
    assert hasattr(ui, "_run_chat_turn_ollama")


# ── configuration ───────────────────────────────────────────────────────

def test_cloud_url_and_model_defaults_and_overrides(monkeypatch):
    monkeypatch.setattr(config, "OLLAMA_URL", None)
    monkeypatch.setattr(config, "OLLAMA_MODEL", None)
    assert ollama_client.get_url() == "https://ollama.com/api/chat"
    assert ollama_client.get_model() == "gpt-oss:120b-cloud"
    assert "localhost" not in ollama_client.get_url()
    assert "11434" not in ollama_client.get_url()

    monkeypatch.setattr(config, "OLLAMA_MODEL", "qwen3:32b-cloud")
    assert ollama_client.get_model() == "qwen3:32b-cloud"


# ── tool schema conversion ──────────────────────────────────────────────

def test_every_real_tool_converts_to_valid_json_schema():
    from core.headless.tool_registry import TOOL_DECLARATIONS

    converted = ollama_client.gemini_tools_to_ollama(TOOL_DECLARATIONS)
    assert len(converted) == len(TOOL_DECLARATIONS), "a tool was dropped in conversion"

    gemini_types = {"STRING", "OBJECT", "INTEGER", "NUMBER", "BOOLEAN", "ARRAY"}

    def assert_no_gemini_types(node):
        if isinstance(node, dict):
            if isinstance(node.get("type"), str):
                assert node["type"] not in gemini_types, f"unconverted type {node['type']}"
            for v in node.values():
                assert_no_gemini_types(v)
        elif isinstance(node, list):
            for v in node:
                assert_no_gemini_types(v)

    for tool in converted:
        assert tool["type"] == "function"
        fn = tool["function"]
        assert fn["name"] and isinstance(fn["description"], str)
        assert fn["parameters"]["type"] == "object"
        assert_no_gemini_types(fn["parameters"])


def test_required_fields_and_parameter_names_survive_conversion():
    """Trimming shortens prose only — it must never cost a parameter or a
    required field, or a tool silently becomes uncallable."""
    from core.headless.tool_registry import TOOL_DECLARATIONS

    converted = {t["function"]["name"]: t["function"] for t in
                 ollama_client.gemini_tools_to_ollama(TOOL_DECLARATIONS)}
    for original in TOOL_DECLARATIONS:
        params = original.get("parameters") or {}
        out = converted[original["name"]]["parameters"]
        assert set((params.get("properties") or {})) == set((out.get("properties") or {}))
        assert params.get("required", []) == out.get("required", [])


def test_tools_are_json_serializable():
    from core.headless.tool_registry import TOOL_DECLARATIONS
    json.dumps(ollama_client.gemini_tools_to_ollama(TOOL_DECLARATIONS))


# ── tool-call parsing (Ollama gives objects; proxies give strings) ───────

def test_parse_tool_call_accepts_object_arguments():
    name, args = ollama_client.parse_tool_call(
        {"function": {"name": "current_time", "arguments": {"timezone": "Dallas"}}}
    )
    assert (name, args) == ("current_time", {"timezone": "Dallas"})


def test_parse_tool_call_accepts_string_arguments():
    name, args = ollama_client.parse_tool_call(
        {"function": {"name": "web_search", "arguments": '{"query": "test"}'}}
    )
    assert (name, args) == ("web_search", {"query": "test"})


def test_parse_tool_call_survives_garbage_arguments():
    name, args = ollama_client.parse_tool_call(
        {"function": {"name": "web_search", "arguments": "{not json"}}
    )
    assert (name, args) == ("web_search", {})


# ── transport ───────────────────────────────────────────────────────────

def test_chat_posts_bearer_auth_to_the_cloud_endpoint(monkeypatch):
    seen = {}

    class FakeResp:
        ok = True
        status_code = 200
        content = b"{}"
        def json(self): return {"message": {"content": "hello", "tool_calls": []}}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen.update(url=url, headers=headers, payload=json, timeout=timeout)
        return FakeResp()

    monkeypatch.setattr(config, "OLLAMA_API_KEY", "secret-key-abc123")
    monkeypatch.setattr("requests.post", fake_post)

    msg = ollama_client.chat([{"role": "user", "content": "hi"}])
    assert msg["content"] == "hello"
    assert seen["url"] == "https://ollama.com/api/chat"
    assert seen["headers"]["Authorization"] == "Bearer secret-key-abc123"
    assert seen["payload"]["stream"] is False
    assert seen["timeout"] == ollama_client.DEFAULT_TIMEOUT_S
    # The key must travel in the header only — never the URL or the body.
    assert "secret-key-abc123" not in seen["url"]
    assert "secret-key-abc123" not in json.dumps(seen["payload"])


def test_no_retry_on_failure(monkeypatch):
    """The entire reason for leaving Groq: one request, one failure, no
    17s/44s backoff sleep before the caller finds out."""
    calls = []

    class FakeResp:
        ok = False
        status_code = 429
        text = "rate limited"
        content = b"x"

    def fake_post(*a, **k):
        calls.append(1)
        return FakeResp()

    monkeypatch.setattr(config, "OLLAMA_API_KEY", "k")
    monkeypatch.setattr("requests.post", fake_post)

    with pytest.raises(ollama_client.OllamaError):
        ollama_client.chat([{"role": "user", "content": "hi"}])
    assert len(calls) == 1, "the client retried — it must not"


def test_errors_never_leak_the_api_key(monkeypatch):
    class FakeResp:
        ok = False
        status_code = 401
        text = "Unauthorized: token sk-secret-value-9999 rejected"
        content = b"x"

    monkeypatch.setattr(config, "OLLAMA_API_KEY", "sk-secret-value-9999")
    monkeypatch.setattr("requests.post", lambda *a, **k: FakeResp())

    with pytest.raises(ollama_client.OllamaError) as excinfo:
        ollama_client.chat([{"role": "user", "content": "hi"}])
    assert "sk-secret-value-9999" not in str(excinfo.value)


def test_timeout_raises_immediately(monkeypatch):
    import requests
    monkeypatch.setattr(config, "OLLAMA_API_KEY", "k")

    def boom(*a, **k):
        raise requests.exceptions.Timeout()

    monkeypatch.setattr("requests.post", boom)
    with pytest.raises(ollama_client.OllamaError, match="timed out"):
        ollama_client.chat([{"role": "user", "content": "hi"}])


def test_missing_key_is_a_clean_error(monkeypatch):
    monkeypatch.setattr(config, "OLLAMA_API_KEY", None)
    with pytest.raises(ollama_client.OllamaError, match="OLLAMA_API_KEY"):
        ollama_client.chat([{"role": "user", "content": "hi"}])
