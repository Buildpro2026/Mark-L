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


def test_prompt_routes_price_and_product_research_to_web_research():
    # "Current-price/product research must actually use the research
    # capability" — web_research is that capability (it opens pages and
    # returns sourced, timestamped, evidence-graded findings); web_search
    # only returns prose. The prompt must send these questions to the
    # right tool, not just mention web_research exists somewhere.
    t = _text().lower()
    assert "web_research" in t
    assert "current price" in t
    assert "source" in t and ("timestamp" in t or "observed" in t or "when it was" in t.replace("_", " "))


def test_prompt_never_lets_web_search_absorb_research_grade_questions():
    t = _text().lower()
    assert "prefer web_research" in t or "research capability" in t


def test_prompt_forbids_inferring_non_existence_from_a_failed_search():
    # The reported production bug: a failed/unreachable web_research call
    # produced "does not appear to be released" — a fabricated conclusion
    # about the SUBJECT drawn from a failure of the TOOL. This instruction
    # is what's supposed to stop that.
    t = _text().lower()
    assert "could not be completed" in t
    assert "does not exist" in t or "not evidence" in t


# ── browser_control vs navigate_command_center: two tools that both claim
# "open a website" ─────────────────────────────────────────────────────────
# The reported bug: "open Google" launched the user's real, separate Chrome
# window instead of showing the page inside the Command Center. core/
# prompt.txt already told the model to always use navigate_command_center
# for a plain "open X" — but browser_control's OWN function declaration
# (the JSON schema actually offered to the model) said "Use for: opening
# websites... any web-based task" with no carve-out, competing for the same
# intent. A function declaration's own description carries real weight in
# tool selection independent of prose system-prompt guidance, so the
# declaration itself has to stop claiming ownership of a plain open/show
# request, not just the surrounding prompt.

def test_browser_control_declaration_defers_plain_opens_to_command_center():
    from core.headless.tool_registry import TOOL_DECLARATIONS
    decl = next(t for t in TOOL_DECLARATIONS if t["name"] == "browser_control")
    desc = decl["description"].lower()
    assert "navigate_command_center" in desc
    assert "do not use this for" in desc or "not for" in desc


def test_navigate_command_center_declaration_still_claims_plain_opens():
    from core.headless.tool_registry import TOOL_DECLARATIONS
    decl = next(t for t in TOOL_DECLARATIONS if t["name"] == "navigate_command_center")
    desc = decl["description"].lower()
    assert "open google" in desc
    assert "only way" in desc


def test_prompt_forbids_markdown_in_replies():
    # Reported bug: JARVIS read "**", "//" and other markdown/code syntax
    # aloud instead of the human-readable text — every reply is either
    # spoken or shown as plain (non-rendering) text, so markdown syntax
    # must never appear in a reply to begin with.
    t = _text().lower()
    assert "markdown" in t
    assert "**bold**" in t or "bullet" in t


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


def test_a_price_question_the_model_routes_to_web_research_returns_real_sources(monkeypatch):
    # The actual capability the prompt now points price/product questions
    # at: web_research.research() -> real source URLs, an observed_at
    # timestamp per source, and a confidence figure — not prose with
    # nothing behind it.
    monkeypatch.setattr("core.headless.ui.config.OLLAMA_API_KEY", "fake-key-not-real")
    from actions import web_research

    outcome = {
        "ok": True, "state": web_research.OK, "question": "current price of iPhone 17",
        "domain": "product", "sources_considered": 3, "duplicates_removed": 0,
        "sources_read": [{
            "url": "https://www.apple.com/iphone-17/", "title": "iPhone 17 - Apple",
            "observed_at": "2026-09-09T12:00:00+00:00", "source_type": "retailer",
            "reliability": 0.9, "freshness": {"state": "CURRENT"},
        }],
        "sources_failed": [], "results": [{"fields": {}}], "confidence": 0.33,
    }
    monkeypatch.setattr(web_research, "research", lambda question, max_sources=3: outcome)

    fc = FakeFunctionCall("web_research", {"question": "current price of iPhone 17"})
    responses = [FakeResponse(function_calls=[fc]), FakeResponse(text="It's $999, per Apple's own site.")]
    install(monkeypatch, responses)

    from core.headless import ui as headless_ui
    reply, calls = asyncio.run(
        headless_ui.run_chat_turn("what's the current price of an iphone 17", []))

    assert reply == "It's $999, per Apple's own site."
    assert calls and calls[0]["name"] == "web_research"
    result = calls[0]["result"]
    assert "https://www.apple.com/iphone-17/" in result   # the real source URL
    assert "observed 2026-09-09" in result                # the real timestamp
    assert "Confidence: 33%" in result                     # the real confidence figure


def test_navigation_commands_are_covered_by_the_existing_headless_navigation_suite():
    # "open Google"/"open BuildPro"/"go back"/"go home" through the real
    # Gemini-function-calling dispatch path are already covered end to end
    # in tests/test_navigate_command_center_headless.py (including a
    # voice/chat-originated call through this exact run_chat_turn path) —
    # not duplicated here.
    import tests.test_navigate_command_center_headless as nav_tests
    assert hasattr(nav_tests, "test_a_chat_turn_that_calls_the_tool_actually_navigates")
