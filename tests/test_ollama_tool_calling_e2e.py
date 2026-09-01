"""End-to-end tool calling on Ollama Cloud.

Only the HTTP transport is faked. Everything else is production code: the
real _run_chat_turn_ollama loop, the real ToolExecutor, the real tool
registry, and the real clock tool. This is the test that proves a phone
call or a browser message can actually make JARVIS do something.
"""
import asyncio

import pytest

from core.headless import config


@pytest.fixture
def ollama_configured(monkeypatch):
    monkeypatch.setattr(config, "OLLAMA_API_KEY", "test-key")
    monkeypatch.setattr(config, "OLLAMA_MODEL", "gpt-oss:120b-cloud")


def _scripted_ollama(monkeypatch, replies):
    """Serves `replies` in order and records every request payload."""
    sent = []
    queue = list(replies)

    class FakeResp:
        ok = True
        status_code = 200
        content = b"{}"
        def __init__(self, body): self._body = body
        def json(self): return self._body

    def fake_post(url, headers=None, json=None, timeout=None):
        sent.append(json)
        return FakeResp({"message": queue.pop(0)})

    monkeypatch.setattr("requests.post", fake_post)
    return sent


def _run(message, history=None):
    from core.headless.ui import run_chat_turn
    return asyncio.run(run_chat_turn(message, history or []))


def test_plain_reply_with_no_tools(ollama_configured, monkeypatch):
    _scripted_ollama(monkeypatch, [{"content": "Good morning, sir.", "tool_calls": []}])
    reply, tools_used = _run("good morning")
    assert reply == "Good morning, sir."
    assert tools_used == []


def test_full_tool_round_trip_reaches_the_real_tool(ollama_configured, monkeypatch):
    """Model asks for current_time -> the REAL clock tool runs -> its result
    is fed back -> the model answers from it."""
    sent = _scripted_ollama(monkeypatch, [
        {"content": "", "tool_calls": [
            {"function": {"name": "current_time", "arguments": {"timezone": "Dallas"}}}
        ]},
        {"content": "It's just past nine in Dallas, sir.", "tool_calls": []},
    ])

    reply, tools_used = _run("what time is it in Dallas?")

    assert reply == "It's just past nine in Dallas, sir."
    assert [t["name"] for t in tools_used] == ["current_time"]

    # The tool genuinely ran — its real output, not a stub.
    result = tools_used[0]["result"]
    assert "Dallas" in result and ("am" in result or "pm" in result)

    # Two round trips, and the second carried the tool result back.
    assert len(sent) == 2
    tool_msgs = [m for m in sent[1]["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_name"] == "current_time"
    assert tool_msgs[0]["content"] == result


def test_tools_and_system_prompt_are_sent_on_every_request(ollama_configured, monkeypatch):
    sent = _scripted_ollama(monkeypatch, [{"content": "ok", "tool_calls": []}])
    _run("hello")
    payload = sent[0]
    assert payload["messages"][0]["role"] == "system"
    assert len(payload["messages"][0]["content"]) > 200      # the real prompt
    names = {t["function"]["name"] for t in payload["tools"]}
    assert "current_time" in names and "web_search" in names
    assert len(names) > 30                                    # the full registry


def test_conversation_history_is_passed_through(ollama_configured, monkeypatch):
    sent = _scripted_ollama(monkeypatch, [{"content": "ok", "tool_calls": []}])
    _run("and the second one?", [
        {"role": "user", "text": "check the pipeline"},
        {"role": "model", "text": "Two candidates are stalled."},
    ])
    roles = [(m["role"], m.get("content")) for m in sent[0]["messages"]]
    assert ("user", "check the pipeline") in roles
    assert ("assistant", "Two candidates are stalled.") in roles


def test_a_failing_tool_does_not_kill_the_turn(ollama_configured, monkeypatch):
    """A broken tool must come back to the model as an error string it can
    talk about — never a 500 that drops the call."""
    _scripted_ollama(monkeypatch, [
        {"content": "", "tool_calls": [
            {"function": {"name": "not_a_real_tool", "arguments": {}}}
        ]},
        {"content": "I can't do that one, sir.", "tool_calls": []},
    ])
    reply, tools_used = _run("do something impossible")
    assert reply == "I can't do that one, sir."
    assert "Error" in tools_used[0]["result"]


def test_provider_failure_is_fast_and_visible(ollama_configured, monkeypatch):
    """No silent fallback to another model, and no long retry — a 502 the
    caller can see."""
    from fastapi import HTTPException

    class FakeResp:
        ok = False
        status_code = 500
        text = "upstream boom"
        content = b"x"

    calls = []
    monkeypatch.setattr("requests.post", lambda *a, **k: (calls.append(1), FakeResp())[1])

    with pytest.raises(HTTPException) as excinfo:
        _run("hello")
    assert excinfo.value.status_code == 502
    assert len(calls) == 1, "retried instead of failing fast"


def test_no_provider_configured_is_a_clean_503(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(config, "OLLAMA_API_KEY", None)
    with pytest.raises(HTTPException) as excinfo:
        _run("hello")
    assert excinfo.value.status_code == 503
    assert "OLLAMA_API_KEY" in excinfo.value.detail


def test_no_groq_or_gemini_client_is_ever_constructed(ollama_configured, monkeypatch):
    """The migration's headline claim, asserted rather than assumed."""
    from core.headless import groq_client

    monkeypatch.setattr(config, "GROQ_API_KEY", "groq-key")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "gemini-key")
    monkeypatch.setattr(
        groq_client, "get_client",
        lambda *a, **k: pytest.fail("a Groq client was constructed after migration"),
    )
    _scripted_ollama(monkeypatch, [{"content": "ok", "tool_calls": []}])
    reply, _ = _run("hello")
    assert reply == "ok"


# ── the deterministic clock tool itself ─────────────────────────────────

def test_clock_answers_without_any_network(monkeypatch):
    from actions import clock
    monkeypatch.setattr("requests.post", lambda *a, **k: pytest.fail("clock must not use the network"))
    monkeypatch.setattr("requests.get", lambda *a, **k: pytest.fail("clock must not use the network"))
    out = clock.current_time()
    assert "It's" in out and ("am" in out or "pm" in out)


@pytest.mark.parametrize("where,expected", [
    ("Dallas", "America/Chicago"),
    ("dallas", "America/Chicago"),
    ("London", "Europe/London"),
    ("Tokyo", "Asia/Tokyo"),
    ("America/New_York", "America/New_York"),
    ("", "America/Chicago"),
])
def test_timezone_resolution(where, expected):
    from actions import clock
    assert clock.resolve_timezone(where) == expected


def test_unknown_timezone_says_so_instead_of_guessing():
    from actions import clock
    assert clock.resolve_timezone("Wakanda") is None
    assert "don't recognize" in clock.current_time("Wakanda")
