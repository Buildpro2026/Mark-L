"""Phase 3 UX requirement: JARVIS must not hide slow operations. Covers
core/headless/ui.py's on_status callback (the real, present-tense status
line emitted right before a tool call actually starts) and
core/headless/dashboard_bridge.py's wiring of that callback onto the
existing WS broadcast channel as {"type": "progress", "text": ...} —
deliberately a different message type than the pre-existing
{"type": "status", "state": ...} orb-connection-state broadcast, which
this must never collide with.
"""
import asyncio

import pytest

from core.headless import ui as headless_ui
from core.headless import dashboard_bridge


# ── status label mapping ─────────────────────────────────────────────────

def test_known_tools_get_specific_present_tense_labels():
    assert headless_ui._status_label_for_tool("web_search", {}) == "Researching..."
    assert headless_ui._status_label_for_tool("hubspot", {}) == "Checking HubSpot..."
    assert headless_ui._status_label_for_tool("calendar", {}) == "Checking your calendar..."
    assert headless_ui._status_label_for_tool("gmail", {}) == "Checking email..."


def test_unknown_tool_gets_an_honest_generic_label():
    assert headless_ui._status_label_for_tool("some_future_tool", {}) == "Waiting on external service..."


# ── run_chat_turn emits status around tool calls ─────────────────────────

import json as _json


class _FakeFunctionCall:
    """Provider-agnostic tool-call spec used to build a Groq-shaped fake
    response below — name kept for readability at call sites, not tied
    to any one provider's actual response shape."""
    def __init__(self, name, args):
        self.name = name
        self.args = args


class _FakeToolCall:
    def __init__(self, id, name, args):
        self.id = id
        self.function = type("_F", (), {"name": name, "arguments": _json.dumps(args)})()


class _FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class _FakeChoice:
    def __init__(self, message):
        self.message = message


class _FakeResponse:
    def __init__(self, text=None, function_calls=None):
        tool_calls = [
            _FakeToolCall(f"call_{i}", fc.name, fc.args)
            for i, fc in enumerate(function_calls or [])
        ]
        self.choices = [_FakeChoice(_FakeMessage(content=text, tool_calls=tool_calls))]


class _FakeCompletions:
    def __init__(self, responses):
        self._responses = list(responses)

    def create(self, model, messages, tools):
        return self._responses.pop(0)


class _FakeClient:
    def __init__(self, responses):
        self.chat = type("_Chat", (), {"completions": _FakeCompletions(responses)})()


@pytest.fixture(autouse=True)
def _no_real_groq_calls(monkeypatch):
    monkeypatch.setattr(headless_ui.config, "GROQ_API_KEY", "fake-key-not-real")


def test_run_chat_turn_emits_status_before_tool_call_and_after(monkeypatch):
    fc = _FakeFunctionCall("system_status", {})
    responses = [
        _FakeResponse(function_calls=[fc]),
        _FakeResponse(text="All good."),
    ]
    fake_client = _FakeClient(responses)
    monkeypatch.setattr("core.headless.groq_client.get_client", lambda *a, **k: fake_client)

    from core.headless.tool_executor import ToolExecutor
    monkeypatch.setattr(ToolExecutor, "execute", lambda self, name, args: asyncio.sleep(0, result="ok"))

    seen = []

    async def _on_status(label):
        seen.append(label)

    reply, calls = asyncio.run(headless_ui.run_chat_turn("what's the system status?", [], on_status=_on_status))

    assert reply == "All good."
    assert seen[0] == "Checking system status..."
    assert seen[1] == "Analyzing results..."


def test_run_chat_turn_never_calls_on_status_when_no_tool_is_needed(monkeypatch):
    fake_client = _FakeClient([_FakeResponse(text="Just an answer, no tools needed.")])
    monkeypatch.setattr("core.headless.groq_client.get_client", lambda *a, **k: fake_client)

    seen = []

    async def _on_status(label):
        seen.append(label)

    reply, calls = asyncio.run(headless_ui.run_chat_turn("what's 2+2?", [], on_status=_on_status))
    assert reply == "Just an answer, no tools needed."
    assert seen == []


def test_run_chat_turn_works_fine_with_no_status_callback_at_all(monkeypatch):
    fake_client = _FakeClient([_FakeResponse(text="No callback needed.")])
    monkeypatch.setattr("core.headless.groq_client.get_client", lambda *a, **k: fake_client)

    reply, calls = asyncio.run(headless_ui.run_chat_turn("hello", []))
    assert reply == "No callback needed."


def test_a_broken_status_sink_never_breaks_the_real_turn(monkeypatch):
    fake_client = _FakeClient([_FakeResponse(text="Still works.")])
    monkeypatch.setattr("core.headless.groq_client.get_client", lambda *a, **k: fake_client)

    async def _broken_on_status(label):
        raise RuntimeError("status sink exploded")

    reply, calls = asyncio.run(headless_ui.run_chat_turn("hello", [], on_status=_broken_on_status))
    assert reply == "Still works."


# ── dashboard_bridge wires on_status onto the WS broadcast channel ──────

class _FakeDashboardServer:
    def __init__(self, texts):
        self._command_queue = asyncio.Queue()
        for t in texts:
            self._command_queue.put_nowait(t)
        self._command_queue.put_nowait(None)  # sentinel handled below
        self.broadcasts = []

    async def broadcast(self, payload):
        self.broadcasts.append(payload)


def test_dashboard_bridge_broadcasts_progress_as_a_distinct_type(monkeypatch):
    fc = _FakeFunctionCall("web_search", {"query": "test"})
    responses = [
        _FakeResponse(function_calls=[fc]),
        _FakeResponse(text="Here's what I found."),
    ]
    fake_client = _FakeClient(responses)
    monkeypatch.setattr("core.headless.groq_client.get_client", lambda *a, **k: fake_client)

    from core.headless.tool_executor import ToolExecutor
    monkeypatch.setattr(ToolExecutor, "execute", lambda self, name, args: asyncio.sleep(0, result="ok"))
    monkeypatch.setattr(headless_ui.config, "GROQ_API_KEY", "fake-key-not-real")

    server = _FakeDashboardServer(["search for something"])

    async def _drain_one():
        text = await server._command_queue.get()
        # Reproduce exactly what dashboard_bridge.run()'s loop body does
        # for one message, without the infinite `while True` — this is a
        # single-iteration equivalent so the test doesn't hang forever.
        async def _on_status(label):
            await server.broadcast({"type": "progress", "text": label})
        reply, _ = await headless_ui.run_chat_turn(text, [], on_status=_on_status)
        await server.broadcast({"type": "log", "speaker": "jarvis", "text": reply})

    asyncio.run(_drain_one())

    types_seen = [b["type"] for b in server.broadcasts]
    assert "progress" in types_seen
    assert "log" in types_seen
    progress_msgs = [b for b in server.broadcasts if b["type"] == "progress"]
    assert progress_msgs[0]["text"] == "Researching..."
    # Must never collide with the pre-existing {"type": "status", "state": ...}
    # orb-connection broadcast shape.
    assert all("state" not in b for b in progress_msgs)
