"""core/headless/ui.py::run_chat_turn's Claude fallback path.

Found live 2026-08-19: the deployed Gemini key is on the free tier's
20-requests/day cap, and every chat request fails with a 429 once it's
hit, with ANTHROPIC_TOKEN sitting unused in .env the whole time. These
tests cover the actual decision logic (fall back only before any tool has
executed this turn — see run_chat_turn's docstring for why re-running a
turn on a different provider after a tool already ran risks re-executing
a consequential action twice), not just that anthropic_client.py exists.
"""
import asyncio

import pytest

from core.headless import ui as headless_ui


class _FakeGeminiClient:
    """Always raises — simulates Gemini being unavailable (429, 503, etc)."""
    class _Models:
        def generate_content(self, model, contents, config):
            raise RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded")
    models = _Models()


class _FakeGeminiThenSucceeds:
    """First call succeeds with a tool call, second call raises — the
    "Gemini worked for round 1, failed on round 2" case that must NOT
    fall back."""
    def __init__(self, first_response):
        self._first = first_response
        self._calls = 0

    class _Models:
        def __init__(self, outer):
            self._outer = outer

        def generate_content(self, model, contents, config):
            self._outer._calls += 1
            if self._outer._calls == 1:
                return self._outer._first
            raise RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded")

    @property
    def models(self):
        return self._Models(self)


class _FakePart:
    def __init__(self, text=None, function_call=None):
        self.text = text
        self.function_call = function_call


class _FakeContent:
    def __init__(self, parts):
        self.parts = parts


class _FakeCandidate:
    def __init__(self, content):
        self.content = content


class _FakeFunctionCall:
    def __init__(self, name, args):
        self.name = name
        self.args = args


class _FakeGeminiResponse:
    def __init__(self, text=None, function_calls=None):
        self.text = text
        self.function_calls = function_calls or []
        parts = [_FakePart(function_call=fc) for fc in self.function_calls] or [_FakePart(text=text)]
        self.candidates = [_FakeCandidate(_FakeContent(parts))]


# ── Anthropic-shape fakes ────────────────────────────────────────────────

class _FakeTextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeToolUseBlock:
    type = "tool_use"

    def __init__(self, id, name, input):
        self.id = id
        self.name = name
        self.input = input


class _FakeAnthropicResponse:
    def __init__(self, content):
        self.content = content


class _FakeAnthropicClient:
    def __init__(self, responses):
        self._responses = list(responses)

    class _Messages:
        def __init__(self, outer):
            self._outer = outer

        def create(self, model, max_tokens, system, tools, messages):
            return self._outer._responses.pop(0)

    @property
    def messages(self):
        return self._Messages(self)


@pytest.fixture(autouse=True)
def _base_config(monkeypatch):
    monkeypatch.setattr(headless_ui.config, "GEMINI_API_KEY", "fake-gemini-key-not-real")
    monkeypatch.setattr(headless_ui.config, "ANTHROPIC_TOKEN", None)


def test_falls_back_to_anthropic_when_gemini_fails_before_any_tool_call(monkeypatch):
    monkeypatch.setattr(headless_ui.config, "ANTHROPIC_TOKEN", "fake-anthropic-key-not-real")
    monkeypatch.setattr("core.headless.gemini_client.get_client", lambda *a, **k: _FakeGeminiClient())
    fake_anthropic = _FakeAnthropicClient([_FakeAnthropicResponse([_FakeTextBlock("Claude here, Gemini was down.")])])
    monkeypatch.setattr("core.headless.anthropic_client.get_client", lambda *a, **k: fake_anthropic)

    reply, calls = asyncio.run(headless_ui.run_chat_turn("hello", []))
    assert reply == "Claude here, Gemini was down."
    assert calls == []


def test_does_not_fall_back_when_anthropic_not_configured(monkeypatch):
    # ANTHROPIC_TOKEN stays None from the autouse fixture.
    monkeypatch.setattr("core.headless.gemini_client.get_client", lambda *a, **k: _FakeGeminiClient())

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(headless_ui.run_chat_turn("hello", []))
    assert exc_info.value.status_code == 502
    assert "Gemini request failed" in exc_info.value.detail


def test_does_not_fall_back_once_a_gemini_tool_already_executed(monkeypatch):
    monkeypatch.setattr(headless_ui.config, "ANTHROPIC_TOKEN", "fake-anthropic-key-not-real")
    fc = _FakeFunctionCall("system_status", {})
    fake_gemini = _FakeGeminiThenSucceeds(_FakeGeminiResponse(function_calls=[fc]))
    monkeypatch.setattr("core.headless.gemini_client.get_client", lambda *a, **k: fake_gemini)

    from core.headless.tool_executor import ToolExecutor
    monkeypatch.setattr(ToolExecutor, "execute", lambda self, name, args: asyncio.sleep(0, result="ok"))

    # If this silently fell back to Anthropic, this fake would blow up
    # (get_client not patched) or double-run the tool — either way proves
    # the guard failed. Deliberately NOT patching anthropic_client here.
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(headless_ui.run_chat_turn("check system status", []))
    assert exc_info.value.status_code == 502
    assert "Gemini request failed" in exc_info.value.detail


def test_anthropic_fallback_executes_tool_calls_via_the_same_executor(monkeypatch):
    monkeypatch.setattr(headless_ui.config, "ANTHROPIC_TOKEN", "fake-anthropic-key-not-real")
    monkeypatch.setattr("core.headless.gemini_client.get_client", lambda *a, **k: _FakeGeminiClient())

    responses = [
        _FakeAnthropicResponse([_FakeToolUseBlock("tu_1", "system_status", {})]),
        _FakeAnthropicResponse([_FakeTextBlock("All good, checked via Claude.")]),
    ]
    fake_anthropic = _FakeAnthropicClient(responses)
    monkeypatch.setattr("core.headless.anthropic_client.get_client", lambda *a, **k: fake_anthropic)

    from core.headless.tool_executor import ToolExecutor
    monkeypatch.setattr(ToolExecutor, "execute", lambda self, name, args: asyncio.sleep(0, result="ok"))

    reply, calls = asyncio.run(headless_ui.run_chat_turn("check status", []))
    assert reply == "All good, checked via Claude."
    assert calls == [{"name": "system_status", "args": {}, "result": "ok"}]


def test_anthropic_fallback_also_raises_cleanly_if_both_providers_fail(monkeypatch):
    monkeypatch.setattr(headless_ui.config, "ANTHROPIC_TOKEN", "fake-anthropic-key-not-real")
    monkeypatch.setattr("core.headless.gemini_client.get_client", lambda *a, **k: _FakeGeminiClient())

    class _AlwaysFailsAnthropic:
        class _Messages:
            def create(self, **kwargs):
                raise RuntimeError("anthropic also down")
        messages = _Messages()

    monkeypatch.setattr("core.headless.anthropic_client.get_client", lambda *a, **k: _AlwaysFailsAnthropic())

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(headless_ui.run_chat_turn("hello", []))
    assert exc_info.value.status_code == 502
    assert "after Gemini also failed" in exc_info.value.detail
