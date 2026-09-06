"""Live tool-activity streaming for the JARVIS Main Interface (/ui) —
the Command console must show "JARVIS -> Gmail" the moment a tool
starts, not after the whole (possibly multi-tool-call) turn finishes.

core/headless/ui.py's run_chat_turn() gained a purely additive
on_tool_event hook (tool_start/tool_end, alongside the pre-existing
on_status human-readable labels dashboard_bridge.py already uses) and
POST /ui/api/chat now streams those events as Server-Sent Events instead
of returning one JSON blob. /3d is untouched by this: dashboard_bridge.py
(the /3d/phone command relay) calls run_chat_turn directly and never
passes on_tool_event — its own on_status-only behavior is covered by
tests/test_chat_status_progress.py, re-run unchanged here as part of the
same verification pass, not duplicated in this file.
"""
import asyncio
import json as _json

import pytest
from fastapi.testclient import TestClient

from core.headless import ui as headless_ui
from core.headless import config


# ── reuse the exact fake-Groq-client shape tests/test_llm_fallback.py and
# tests/test_chat_status_progress.py already use, so these tests exercise
# the real run_chat_turn -> _run_chat_turn_ollama loop, not a mock of it. ──

# Provider fakes live in tests/ollama_fake.py — see that module for why the
# fake moved from the Groq SDK's object shape down to the HTTP layer.
from tests.ollama_fake import FakeFunctionCall as _FakeFunctionCall
from tests.ollama_fake import FakeResponse as _FakeResponse
from tests.ollama_fake import install as _install_ollama


@pytest.fixture(autouse=True)
def _ollama_only(monkeypatch):
    monkeypatch.setattr(headless_ui.config, "OLLAMA_API_KEY", "fake-key-not-real")


# ── on_tool_event, exercised through the real run_chat_turn ─────────────

def test_on_tool_event_fires_start_then_end_for_a_single_tool(monkeypatch):
    fc = _FakeFunctionCall("gmail", {"action": "list"})
    responses = [_FakeResponse(function_calls=[fc]), _FakeResponse(text="Done.")]
    _install_ollama(monkeypatch, responses)
    from core.headless.tool_executor import ToolExecutor
    monkeypatch.setattr(ToolExecutor, "execute", lambda self, name, args: asyncio.sleep(0, result="ok"))

    events = []

    async def _on_tool_event(e):
        events.append(e)

    reply, calls = asyncio.run(headless_ui.run_chat_turn("check gmail", [], on_tool_event=_on_tool_event))

    assert reply == "Done."
    assert events == [
        {"type": "tool_start", "name": "gmail"},
        {"type": "tool_end", "name": "gmail", "ok": True},
    ]


def test_on_tool_event_fires_for_multiple_tools_in_order(monkeypatch):
    fc1 = _FakeFunctionCall("buildpro_matching", {})
    fc2 = _FakeFunctionCall("hubspot", {"action": "list_contacts"})
    responses = [
        _FakeResponse(function_calls=[fc1, fc2]),
        _FakeResponse(text="Found matches and synced HubSpot."),
    ]
    import unittest.mock as mock
    _install_ollama(monkeypatch, responses)
    if True:
        from core.headless.tool_executor import ToolExecutor
        with mock.patch.object(ToolExecutor, "execute", lambda self, name, args: asyncio.sleep(0, result="ok")):
            events = []

            async def _on_tool_event(e):
                events.append(e)

            reply, calls = asyncio.run(headless_ui.run_chat_turn("pipeline check", [], on_tool_event=_on_tool_event))

    assert [e["name"] for e in events] == ["buildpro_matching", "buildpro_matching", "hubspot", "hubspot"]
    assert [e["type"] for e in events] == ["tool_start", "tool_end", "tool_start", "tool_end"]
    assert all(e["ok"] for e in events if e["type"] == "tool_end")
    assert reply == "Found matches and synced HubSpot."


def test_on_tool_event_reports_a_failed_tool_honestly(monkeypatch):
    fc = _FakeFunctionCall("gmail", {"action": "send"})
    responses = [_FakeResponse(function_calls=[fc]), _FakeResponse(text="That didn't work.")]
    _install_ollama(monkeypatch, responses)
    from core.headless.tool_executor import ToolExecutor

    def _boom(self, name, args):
        raise RuntimeError("gmail send failed")
    monkeypatch.setattr(ToolExecutor, "execute", _boom)

    events = []

    async def _on_tool_event(e):
        events.append(e)

    reply, calls = asyncio.run(headless_ui.run_chat_turn("send it", [], on_tool_event=_on_tool_event))

    assert events[0] == {"type": "tool_start", "name": "gmail"}
    assert events[1] == {"type": "tool_end", "name": "gmail", "ok": False}
    assert "gmail send failed" in calls[0]["result"]


def test_ordinary_chat_with_no_tools_never_fires_tool_events(monkeypatch):
    _install_ollama(monkeypatch, [_FakeResponse(text="Hi there.")])

    events = []

    async def _on_tool_event(e):
        events.append(e)

    reply, calls = asyncio.run(headless_ui.run_chat_turn("hello", [], on_tool_event=_on_tool_event))
    assert reply == "Hi there."
    assert events == []
    assert calls == []


def test_a_broken_tool_event_sink_never_breaks_the_real_turn(monkeypatch):
    fc = _FakeFunctionCall("system_status", {})
    responses = [_FakeResponse(function_calls=[fc]), _FakeResponse(text="All good.")]
    _install_ollama(monkeypatch, responses)
    from core.headless.tool_executor import ToolExecutor
    monkeypatch.setattr(ToolExecutor, "execute", lambda self, name, args: asyncio.sleep(0, result="ok"))

    async def _broken(e):
        raise RuntimeError("sink exploded")

    reply, calls = asyncio.run(headless_ui.run_chat_turn("status", [], on_tool_event=_broken))
    assert reply == "All good."


def test_run_chat_turn_still_works_with_neither_callback_at_all(monkeypatch):
    _install_ollama(monkeypatch, [_FakeResponse(text="fine")])
    reply, calls = asyncio.run(headless_ui.run_chat_turn("hi", []))
    assert reply == "fine"


# ── the real HTTP route now streams SSE ──────────────────────────────

def _sse_events(text):
    events = []
    for block in text.strip().split("\n\n"):
        block = block.strip()
        if not block:
            continue
        assert block.startswith("data: ")
        events.append(_json.loads(block[len("data: "):]))
    return events


def _logged_in_client(monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", "test-ui-token-not-a-real-secret")
    from core.headless.app import create_app
    app = create_app(start_background_worker=False)
    client = TestClient(app, base_url="https://testserver")
    client.post("/ui/login", json={"token": "test-ui-token-not-a-real-secret"})
    return client


def test_ui_api_chat_route_streams_sse_frames_and_ends_with_done(monkeypatch):
    fc = _FakeFunctionCall("web_search", {"query": "x"})
    responses = [_FakeResponse(function_calls=[fc]), _FakeResponse(text="Here you go.")]
    _install_ollama(monkeypatch, responses)
    from core.headless.tool_executor import ToolExecutor
    monkeypatch.setattr(ToolExecutor, "execute", lambda self, name, args: asyncio.sleep(0, result="ok"))

    client = _logged_in_client(monkeypatch)
    r = client.post("/ui/api/chat", json={"message": "search for x", "history": []})

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")

    events = _sse_events(r.text)
    assert events[0] == {"type": "tool_start", "name": "web_search"}
    assert events[1] == {"type": "tool_end", "name": "web_search", "ok": True}
    assert events[-1]["type"] == "done"
    assert events[-1]["reply"] == "Here you go."
    assert events[-1]["tool_calls"][0]["name"] == "web_search"


def test_ui_api_chat_route_with_no_tools_streams_just_the_done_frame(monkeypatch):
    _install_ollama(monkeypatch, [_FakeResponse(text="Just an answer.")])

    client = _logged_in_client(monkeypatch)
    r = client.post("/ui/api/chat", json={"message": "what's 2+2?", "history": []})

    events = _sse_events(r.text)
    assert events == [{"type": "done", "reply": "Just an answer.", "tool_calls": []}]


def test_ui_api_chat_route_reports_provider_failure_as_an_error_frame(monkeypatch):
    import requests
    from tests.ollama_fake import install_failing
    attempts = install_failing(monkeypatch, requests.exceptions.Timeout())

    client = _logged_in_client(monkeypatch)
    r = client.post("/ui/api/chat", json={"message": "hello", "history": []})

    events = _sse_events(r.text)
    assert events[-1]["type"] == "error"
    assert "Ollama" in events[-1]["detail"]
    # Fail fast: one attempt, no retry storm behind the error frame.
    assert len(attempts) == 1


def test_ui_api_chat_route_requires_a_session(monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", "test-ui-token-not-a-real-secret")
    from core.headless.app import create_app
    app = create_app(start_background_worker=False)
    client = TestClient(app, base_url="https://testserver")

    r = client.post("/ui/api/chat", json={"message": "hi", "history": []})
    assert r.status_code == 401
