"""Shared Ollama-shaped fake transport for the headless chat tests.

Before the Ollama migration these tests each built Groq-SDK-shaped objects
(choices[0].message.tool_calls, arguments as a JSON string) and patched
groq_client.get_client. Ollama is a plain HTTP call returning a message
dict with object-valued tool arguments, so the fake moved down a layer: we
patch requests.post and let the real ollama_client, the real
_run_chat_turn_ollama loop, and the real ToolExecutor run untouched.

The behaviors those tests cover — status labels, tool-activity events, SSE
framing — were never provider-specific, so only the plumbing changed.
"""
from __future__ import annotations


class FakeFunctionCall:
    """One tool call the fake model should ask for."""

    def __init__(self, name, args):
        self.name = name
        self.args = args


class FakeResponse:
    """One Ollama chat response: either text, or tool calls, or both."""

    def __init__(self, text=None, function_calls=None):
        self.message = {
            "content": text or "",
            "tool_calls": [
                {"function": {"name": fc.name, "arguments": fc.args}}
                for fc in (function_calls or [])
            ],
        }


def install(monkeypatch, responses, api_key="fake-key-not-real"):
    """Serve `responses` in order to the real ollama_client.

    Returns the list of request payloads actually sent, so a test can
    assert on what reached the provider.
    """
    from core.headless import config

    monkeypatch.setattr(config, "OLLAMA_API_KEY", api_key)
    sent: list[dict] = []
    queue = list(responses)

    class _Resp:
        ok = True
        status_code = 200
        content = b"{}"

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    def fake_post(url, headers=None, json=None, timeout=None):
        sent.append(json)
        if not queue:
            raise AssertionError("the fake provider ran out of scripted responses")
        return _Resp({"message": queue.pop(0).message})

    monkeypatch.setattr("requests.post", fake_post)
    return sent


def install_failing(monkeypatch, exc=None, api_key="fake-key-not-real"):
    """A provider that always fails, for fail-fast assertions. Returns a
    list that gets one entry per attempt, so a test can prove no retry."""
    from core.headless import config

    monkeypatch.setattr(config, "OLLAMA_API_KEY", api_key)
    attempts: list[int] = []

    def fake_post(*a, **k):
        attempts.append(1)
        raise exc or RuntimeError("ollama down")

    monkeypatch.setattr("requests.post", fake_post)
    return attempts
