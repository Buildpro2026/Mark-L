"""Tool dispatch for live-information questions.

The reported bug: JARVIS answered "what's the current price of an iPhone"
from the model's own (stale, training-cutoff) knowledge instead of calling
web_search. The dispatch mechanics themselves (core/headless/ui.py's
_run_chat_turn_ollama -> ToolExecutor.execute -> actions.web_search) were
already correct — verified below by simulating both a model that DOES
call a tool and one that doesn't, and confirming each is handled honestly.
The actual lever available here is the system prompt's tool-routing
guidance (core/prompt.txt), which is what steers the model's own judgment;
this suite checks that guidance is present and unambiguous, and that a
tool call the model DOES make is dispatched, its result returned, and its
failure never silently swallowed.
"""
import asyncio
from pathlib import Path

from tests.ollama_fake import FakeFunctionCall, FakeResponse, install

PROMPT_PATH = Path(__file__).resolve().parents[1] / "core" / "prompt.txt"


def _text() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


# ── prompt content: the actual lever for model judgment ──────────────────

def test_prompt_instructs_web_search_for_live_information():
    t = _text().lower()
    assert "web_search" in t
    assert "current" in t and ("price" in t or "news" in t)


def test_prompt_explicitly_warns_against_answering_from_stale_training_data():
    t = _text().lower()
    assert "training data" in t or "trained on" in t
    assert "cutoff" in t


def test_prompt_distinguishes_web_search_brain_and_normal_conversation():
    t = _text().lower()
    assert "obsidian" in t
    assert "normal conversation" in t or "not automatically" in t


def test_prompt_instructs_navigate_command_center_for_open_commands():
    t = _text()
    assert "navigate_command_center" in t
    assert "open google" in t.lower() or "open buildpro" in t.lower()


def test_prompt_forbids_claiming_a_browser_the_user_cannot_see():
    t = _text().lower()
    assert "not visible to the user" in t or "browser_control" in t


# ── dispatch mechanics: what happens once the model DOES call a tool ────

def _fake_web_search_result(monkeypatch, result="iPhone 17: $999 (Apple.com, checked live)."):
    # tool_executor.py binds `from actions.web_search import web_search as
    # web_search_action` — a bare name in ITS OWN namespace, not an
    # attribute read off the actions.web_search module at call time — so
    # patching actions.web_search.web_search would silently do nothing;
    # the consumer's own name has to be patched.
    from core.headless import tool_executor
    monkeypatch.setattr(tool_executor, "web_search_action", lambda **k: result)
    return result


def test_live_price_request_invokes_web_search_and_returns_the_real_result(monkeypatch):
    monkeypatch.setattr("core.headless.ui.config.OLLAMA_API_KEY", "fake-key-not-real")
    real_result = _fake_web_search_result(monkeypatch)

    fc = FakeFunctionCall("web_search", {"query": "current price of iPhone", "mode": "price"})
    responses = [FakeResponse(function_calls=[fc]), FakeResponse(text="It's $999.")]
    install(monkeypatch, responses)

    from core.headless import ui as headless_ui
    reply, calls = asyncio.run(
        headless_ui.run_chat_turn("what's the current price of an iphone", []))

    assert reply == "It's $999."
    assert calls and calls[0]["name"] == "web_search"
    assert calls[0]["result"] == real_result


def test_a_normal_question_the_model_answers_directly_never_touches_the_tool_executor(monkeypatch):
    # If the (fake) model doesn't call a tool, none is dispatched — the
    # honest baseline every routing improvement builds on. No web_search
    # patch here on purpose: if this test ever DID reach it, it would
    # raise AttributeError rather than silently pass.
    monkeypatch.setattr("core.headless.ui.config.OLLAMA_API_KEY", "fake-key-not-real")
    install(monkeypatch, [FakeResponse(text="Paris is the capital of France.")])

    from core.headless import ui as headless_ui
    reply, calls = asyncio.run(
        headless_ui.run_chat_turn("what is the capital of france", []))

    assert reply == "Paris is the capital of France."
    assert calls == []


def test_a_failed_tool_call_is_reported_as_an_error_not_a_fabricated_answer(monkeypatch):
    monkeypatch.setattr("core.headless.ui.config.OLLAMA_API_KEY", "fake-key-not-real")
    from core.headless import tool_executor

    def _boom(**k):
        raise RuntimeError("search backend unreachable")
    monkeypatch.setattr(tool_executor, "web_search_action", _boom)

    fc = FakeFunctionCall("web_search", {"query": "current price of iPhone"})
    responses = [FakeResponse(function_calls=[fc]),
                 FakeResponse(text="I couldn't look that up right now.")]
    install(monkeypatch, responses)

    from core.headless import ui as headless_ui
    reply, calls = asyncio.run(headless_ui.run_chat_turn("current price of an iphone", []))

    assert calls and calls[0]["name"] == "web_search"
    assert "Error" in calls[0]["result"] and "unreachable" in calls[0]["result"]
    # The model's own follow-up reply is whatever it says — this only
    # guarantees the TOOL RESULT it was given told the truth, never a
    # fabricated success.
    assert reply == "I couldn't look that up right now."


def test_navigation_commands_are_covered_by_the_existing_headless_navigation_suite():
    # "open Google"/"open BuildPro"/"go back"/"go home" through the real
    # Gemini-function-calling dispatch path are already covered end to end
    # in tests/test_navigate_command_center_headless.py (including a
    # voice/chat-originated call through this exact run_chat_turn path) —
    # not duplicated here.
    import tests.test_navigate_command_center_headless as nav_tests
    assert hasattr(nav_tests, "test_a_chat_turn_that_calls_the_tool_actually_navigates")
