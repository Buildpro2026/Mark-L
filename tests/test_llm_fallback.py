"""core/headless/ui.py::run_chat_turn's provider chain — after the Ollama
Cloud migration.

History, because it explains why this file shrank rather than grew: it used
to cover a Gemini -> Groq -> Anthropic fallback chain, written when the
deployed Gemini key kept hitting a 20-requests/day cap. That chain is gone
on purpose. Multi-provider fallback bought resilience at the cost of a
JARVIS whose answers and tool-calling behavior changed depending on which
provider had quota left that minute, and Groq's SDK made the failure
expensive anyway: it retried a 429 twice, sleeping 17s then ~44s before the
caller learned anything.

So the contract these tests now guard is the opposite one: exactly one
provider, and a failure that surfaces immediately instead of quietly
becoming a different model's answer. The old fallback machinery
(_run_chat_turn_gemini / _run_chat_turn_anthropic / _run_provider_chain)
is deliberately still present and still correct — re-enabling a provider
is one name in _configured_providers() — so the "don't re-run a turn after
a tool already executed" reasoning in run_chat_turn's docstring stays
relevant if that day ever comes.
"""
import asyncio

import pytest
from fastapi import HTTPException

from core.headless import ui as headless_ui
from tests.ollama_fake import FakeFunctionCall, FakeResponse, install, install_failing


def _run(message="hello", history=None, **kw):
    return asyncio.run(headless_ui.run_chat_turn(message, history or [], **kw))


# ── exactly one provider ────────────────────────────────────────────────

def test_ollama_is_the_only_provider_in_the_chain(monkeypatch):
    monkeypatch.setattr(headless_ui.config, "OLLAMA_API_KEY", "fake-ollama-key-not-real")
    monkeypatch.setattr(headless_ui.config, "GROQ_API_KEY", "fake-groq-key-not-real")
    monkeypatch.setattr(headless_ui.config, "GEMINI_API_KEY", "fake-gemini-key-not-real")
    monkeypatch.setattr(headless_ui.config, "ANTHROPIC_TOKEN", "fake-anthropic-key-not-real")
    assert headless_ui._configured_providers() == ["ollama"]


def test_gemini_is_excluded_from_the_active_chain_even_when_configured(monkeypatch):
    monkeypatch.setattr(headless_ui.config, "GEMINI_API_KEY", "fake-gemini-key-not-real")
    monkeypatch.setattr(headless_ui.config, "OLLAMA_API_KEY", "fake-ollama-key-not-real")
    assert "gemini" not in headless_ui._configured_providers()


def test_groq_is_excluded_from_the_active_chain_even_when_configured(monkeypatch):
    monkeypatch.setattr(headless_ui.config, "GROQ_API_KEY", "fake-groq-key-not-real")
    monkeypatch.setattr(headless_ui.config, "OLLAMA_API_KEY", "fake-ollama-key-not-real")
    assert "groq" not in headless_ui._configured_providers()


def test_no_provider_configured_raises_clear_503(monkeypatch):
    monkeypatch.setattr(headless_ui.config, "OLLAMA_API_KEY", None)
    monkeypatch.setattr(headless_ui.config, "GROQ_API_KEY", None)
    monkeypatch.setattr(headless_ui.config, "ANTHROPIC_TOKEN", None)
    with pytest.raises(HTTPException) as exc_info:
        _run()
    assert exc_info.value.status_code == 503
    assert "OLLAMA_API_KEY" in exc_info.value.detail


# ── failure is fast and visible, never a silent substitution ────────────

def test_provider_failure_raises_cleanly_instead_of_switching_models(monkeypatch):
    """The whole point of a single provider: a bad turn is an error the
    caller can see, not the same question quietly answered by a different
    model with different tool behavior."""
    monkeypatch.setattr(headless_ui.config, "GROQ_API_KEY", "fake-groq-key-not-real")
    monkeypatch.setattr(headless_ui.config, "ANTHROPIC_TOKEN", "fake-anthropic-key-not-real")

    from core.headless import groq_client, anthropic_client
    monkeypatch.setattr(groq_client, "get_client",
                        lambda *a, **k: pytest.fail("Groq must never be reached"))
    monkeypatch.setattr(anthropic_client, "get_client",
                        lambda *a, **k: pytest.fail("Anthropic must never be reached"))

    attempts = install_failing(monkeypatch)
    with pytest.raises(HTTPException) as exc_info:
        _run()
    assert exc_info.value.status_code == 502
    assert "Ollama" in exc_info.value.detail
    assert len(attempts) == 1, "the turn retried instead of failing fast"


def test_failure_detail_never_carries_the_api_key(monkeypatch):
    import requests
    install_failing(monkeypatch, requests.exceptions.Timeout(), api_key="sk-super-secret-1234")
    with pytest.raises(HTTPException) as exc_info:
        _run()
    assert "sk-super-secret-1234" not in exc_info.value.detail


# ── tools still run exactly once per turn ───────────────────────────────

def test_a_tool_runs_once_and_its_result_reaches_the_answer(monkeypatch):
    """The original file's real concern — never executing a consequential
    action twice — restated for a single-provider world."""
    runs = []

    install(monkeypatch, [
        FakeResponse(function_calls=[FakeFunctionCall("system_status", {})]),
        FakeResponse(text="CPU is fine, sir."),
    ])

    from core.headless.tool_executor import ToolExecutor

    async def _once(self, name, args):
        runs.append(name)
        return "cpu 12%"

    monkeypatch.setattr(ToolExecutor, "execute", _once)

    reply, calls = _run("how's the box doing?")
    assert reply == "CPU is fine, sir."
    assert runs == ["system_status"], "the tool ran more than once"
    assert calls == [{"name": "system_status", "args": {}, "result": "cpu 12%"}]


def test_a_failure_after_a_tool_ran_does_not_re_run_that_tool(monkeypatch):
    """A provider failure on round 2, after a real action already happened
    on round 1, must not replay the action."""
    runs = []
    import requests

    from core.headless import ollama_client
    from core.headless.tool_executor import ToolExecutor

    async def _once(self, name, args):
        runs.append(name)
        return "sent"

    monkeypatch.setattr(ToolExecutor, "execute", _once)
    monkeypatch.setattr(headless_ui.config, "OLLAMA_API_KEY", "fake-key-not-real")

    calls_made = {"n": 0}
    first = FakeResponse(function_calls=[FakeFunctionCall("communications", {"action": "send_sms"})])

    def flaky(url, headers=None, json=None, timeout=None):
        calls_made["n"] += 1
        if calls_made["n"] == 1:
            class _R:
                ok = True
                status_code = 200
                content = b"{}"
                def json(self): return {"message": first.message}
            return _R()
        raise requests.exceptions.Timeout()

    monkeypatch.setattr("requests.post", flaky)

    with pytest.raises(HTTPException):
        _run("text Marcus")
    assert runs == ["communications"], "the consequential tool ran more than once"
