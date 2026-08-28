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
    monkeypatch.setattr(headless_ui.config, "GROQ_API_KEY", None)
    monkeypatch.setattr(headless_ui.config, "GEMINI_API_KEY", "fake-gemini-key-not-real")
    monkeypatch.setattr(headless_ui.config, "ANTHROPIC_TOKEN", None)


class _FakeGroqToolCall:
    def __init__(self, id, name, arguments_json):
        self.id = id
        self.function = type("_F", (), {"name": name, "arguments": arguments_json})()


class _FakeGroqMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class _FakeGroqChoice:
    def __init__(self, message):
        self.message = message


class _FakeGroqResponse:
    def __init__(self, content=None, tool_calls=None):
        self.choices = [_FakeGroqChoice(_FakeGroqMessage(content=content, tool_calls=tool_calls))]


class _FakeGroqClient:
    def __init__(self, responses):
        self._responses = list(responses)

    class _Completions:
        def __init__(self, outer):
            self._outer = outer

        def create(self, model, messages, tools):
            return self._outer._responses.pop(0)

    class _Chat:
        def __init__(self, outer):
            self.completions = outer._Completions(outer)

    @property
    def chat(self):
        return self._Chat(self)


def test_gemini_is_excluded_from_the_active_chain_even_when_configured(monkeypatch):
    """Direct proof of the Groq-only instruction: GEMINI_API_KEY being set
    must not put Gemini in the active provider list at all."""
    monkeypatch.setattr(headless_ui.config, "GEMINI_API_KEY", "fake-gemini-key-not-real")
    monkeypatch.setattr(headless_ui.config, "GROQ_API_KEY", "fake-groq-key-not-real")
    assert "gemini" not in headless_ui._configured_providers()
    assert headless_ui._configured_providers() == ["groq"]


def test_groq_is_tried_first_when_configured_gemini_never_touched(monkeypatch):
    monkeypatch.setattr(headless_ui.config, "GROQ_API_KEY", "fake-groq-key-not-real")
    fake_groq = _FakeGroqClient([_FakeGroqResponse(content="Groq here, no need for Gemini.")])
    monkeypatch.setattr("core.headless.groq_client.get_client", lambda *a, **k: fake_groq)

    def _gemini_should_never_be_called(*a, **k):
        raise AssertionError("Gemini was called even though Groq (higher priority) succeeded")
    monkeypatch.setattr("core.headless.gemini_client.get_client", _gemini_should_never_be_called)

    reply, calls = asyncio.run(headless_ui.run_chat_turn("hello", []))
    assert reply == "Groq here, no need for Gemini."
    assert calls == []


def test_groq_failure_raises_cleanly_when_no_other_provider_is_configured(monkeypatch):
    """Gemini is deliberately excluded from the active chain (Lee's
    instruction: Groq-only for now) — a Groq failure with nothing else
    configured must fail cleanly, not silently fall back to Gemini."""
    monkeypatch.setattr(headless_ui.config, "GROQ_API_KEY", "fake-groq-key-not-real")

    class _AlwaysFailsGroq:
        class _Chat:
            class _Completions:
                def create(self, **kwargs):
                    raise RuntimeError("groq down")
            completions = _Completions()
        chat = _Chat()

    monkeypatch.setattr("core.headless.groq_client.get_client", lambda *a, **k: _AlwaysFailsGroq())

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(headless_ui.run_chat_turn("hello", []))
    assert exc_info.value.status_code == 502
    assert "Groq" in exc_info.value.detail



def test_groq_and_anthropic_chain_exhausted_raises_clean_error(monkeypatch):
    # Gemini is excluded from the active chain regardless of GEMINI_API_KEY
    # being set (default fixture) — only Groq and Anthropic are actually
    # attempted here.
    monkeypatch.setattr(headless_ui.config, "GROQ_API_KEY", "fake-groq-key-not-real")
    monkeypatch.setattr(headless_ui.config, "ANTHROPIC_TOKEN", "fake-anthropic-key-not-real")

    class _AlwaysFailsGroq:
        class _Chat:
            class _Completions:
                def create(self, **kwargs):
                    raise RuntimeError("groq down")
            completions = _Completions()
        chat = _Chat()

    monkeypatch.setattr("core.headless.groq_client.get_client", lambda *a, **k: _AlwaysFailsGroq())

    class _AlwaysFailsAnthropic:
        class _Messages:
            def create(self, **kwargs):
                raise RuntimeError("anthropic down too")
        messages = _Messages()

    monkeypatch.setattr("core.headless.anthropic_client.get_client", lambda *a, **k: _AlwaysFailsAnthropic())

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(headless_ui.run_chat_turn("hello", []))
    assert exc_info.value.status_code == 502
    assert "All configured AI providers failed" in exc_info.value.detail
    assert "Anthropic" in exc_info.value.detail  # last one tried


def test_no_provider_configured_raises_clear_503(monkeypatch):
    monkeypatch.setattr(headless_ui.config, "GEMINI_API_KEY", None)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(headless_ui.run_chat_turn("hello", []))
    assert exc_info.value.status_code == 503
    assert "GROQ_API_KEY" in exc_info.value.detail


def test_falls_back_to_anthropic_when_groq_fails_before_any_tool_call(monkeypatch):
    monkeypatch.setattr(headless_ui.config, "GROQ_API_KEY", "fake-groq-key-not-real")
    monkeypatch.setattr(headless_ui.config, "ANTHROPIC_TOKEN", "fake-anthropic-key-not-real")

    class _AlwaysFailsGroq:
        class _Chat:
            class _Completions:
                def create(self, **kwargs):
                    raise RuntimeError("groq down")
            completions = _Completions()
        chat = _Chat()

    monkeypatch.setattr("core.headless.groq_client.get_client", lambda *a, **k: _AlwaysFailsGroq())
    fake_anthropic = _FakeAnthropicClient([_FakeAnthropicResponse([_FakeTextBlock("Claude here, Groq was down.")])])
    monkeypatch.setattr("core.headless.anthropic_client.get_client", lambda *a, **k: fake_anthropic)

    reply, calls = asyncio.run(headless_ui.run_chat_turn("hello", []))
    assert reply == "Claude here, Groq was down."
    assert calls == []


def test_does_not_fall_back_when_anthropic_not_configured(monkeypatch):
    # ANTHROPIC_TOKEN stays None from the autouse fixture; Gemini is
    # excluded from the chain, so Groq is the only configured provider.
    monkeypatch.setattr(headless_ui.config, "GROQ_API_KEY", "fake-groq-key-not-real")

    class _AlwaysFailsGroq:
        class _Chat:
            class _Completions:
                def create(self, **kwargs):
                    raise RuntimeError("groq down")
            completions = _Completions()
        chat = _Chat()

    monkeypatch.setattr("core.headless.groq_client.get_client", lambda *a, **k: _AlwaysFailsGroq())

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(headless_ui.run_chat_turn("hello", []))
    assert exc_info.value.status_code == 502
    assert "Groq" in exc_info.value.detail


def test_falls_back_after_a_groq_tool_already_ran_without_repeating_it(monkeypatch):
    # Same real-world pattern this project hit with Gemini's daily quota
    # (a turn's first call succeeds and runs a tool; a LATER call is what
    # actually hits the rate limit) — verified here against Groq, since
    # Groq is now the sole active provider. Confirms the real behavior:
    # fall back to Anthropic, but tell it what already ran instead of
    # letting it call system_status a second time.
    monkeypatch.setattr(headless_ui.config, "GROQ_API_KEY", "fake-groq-key-not-real")
    monkeypatch.setattr(headless_ui.config, "ANTHROPIC_TOKEN", "fake-anthropic-key-not-real")

    class _GroqRunsToolThenFails:
        class _Chat:
            class _Completions:
                def __init__(self):
                    self._call_num = 0

                def create(self, model, messages, tools):
                    self._call_num += 1
                    if self._call_num == 1:
                        return _FakeGroqResponse(tool_calls=[
                            _FakeGroqToolCall("call_1", "system_status", "{}"),
                        ])
                    raise RuntimeError("groq down on round 2")
            completions = _Completions()
        chat = _Chat()

    monkeypatch.setattr("core.headless.groq_client.get_client", lambda *a, **k: _GroqRunsToolThenFails())

    from core.headless.tool_executor import ToolExecutor
    execute_calls = []

    async def _fake_execute(self, name, args):
        execute_calls.append(name)
        return "ok"
    monkeypatch.setattr(ToolExecutor, "execute", _fake_execute)

    captured_messages = {}

    class _CapturingAnthropicClient:
        class _Messages:
            def __init__(self, outer):
                self._outer = outer

            def create(self, model, max_tokens, system, tools, messages):
                captured_messages["value"] = messages
                return _FakeAnthropicResponse([_FakeTextBlock("Handled by Claude, no repeat call.")])

        @property
        def messages(self):
            return self._Messages(self)

    monkeypatch.setattr("core.headless.anthropic_client.get_client", lambda *a, **k: _CapturingAnthropicClient())

    reply, calls = asyncio.run(headless_ui.run_chat_turn("check system status", []))

    assert reply == "Handled by Claude, no repeat call."
    # system_status ran exactly once (by Groq) — Claude was never asked
    # to call it again, it was just told the result.
    assert execute_calls == ["system_status"]
    assert calls == [{"name": "system_status", "args": {}, "result": "ok"}]
    already_done_note = captured_messages["value"][-1]["content"]
    assert "system_status" in already_done_note
    assert "must NOT be repeated" in already_done_note


def test_anthropic_fallback_executes_tool_calls_via_the_same_executor(monkeypatch):
    # Anthropic as the sole configured provider (Gemini excluded from the
    # active chain, Groq not set here) — real tool execution through it.
    monkeypatch.setattr(headless_ui.config, "ANTHROPIC_TOKEN", "fake-anthropic-key-not-real")

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
    # Anthropic as the sole configured provider, failing.
    monkeypatch.setattr(headless_ui.config, "ANTHROPIC_TOKEN", "fake-anthropic-key-not-real")

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
    assert "All configured AI providers failed" in exc_info.value.detail
    assert "Anthropic" in exc_info.value.detail


def test_groq_client_construction_failure_raises_a_clean_error_not_a_500(monkeypatch):
    """The actual live bug: get_client() ran outside any try/except, so a
    construction-time failure (missing package, bad key, etc.) was an
    unhandled exception -> raw HTTP 500 instead of a clean error."""
    monkeypatch.setattr(headless_ui.config, "GROQ_API_KEY", "fake-groq-key-not-real")

    def _broken_get_client(*a, **k):
        raise RuntimeError("simulated construction failure")

    monkeypatch.setattr("core.headless.groq_client.get_client", _broken_get_client)

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(headless_ui.run_chat_turn("hello", []))
    assert exc_info.value.status_code == 502
    assert "Groq" in exc_info.value.detail
