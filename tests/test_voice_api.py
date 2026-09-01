"""The phone line's entry point into JARVIS.

The security property that matters most here: /api/voice/* is a public
internet endpoint that reaches the full tool executor. It must never be
callable without the token, and it must not become a second brain with its
own rules.
"""
import pytest
from fastapi import HTTPException

from core.headless import config, voice_api


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", "test-token")
    return "test-token"


@pytest.fixture
def client(token):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(voice_api.router, prefix="/api/voice")
    return TestClient(app)


def test_every_voice_route_requires_the_token(client):
    for method, path, body in (
        ("get", "/api/voice/context", None),
        ("post", "/api/voice/ask", {"message": "hello"}),
        ("post", "/api/voice/call-ended", {"agent_call_id": "ac_1"}),
    ):
        resp = getattr(client, method)(path, json=body) if body else getattr(client, method)(path)
        assert resp.status_code == 401, f"{path} answered without a token"


def test_wrong_token_is_rejected(client):
    resp = client.get("/api/voice/context", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


def test_context_carries_the_real_persona_plus_voice_rules(client, token):
    resp = client.get("/api/voice/context", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    prompt = resp.json()["system_prompt"]
    # The real prompt file, not a stub written for the phone.
    assert len(prompt) > 500
    # Voice-medium rules are appended on top of it.
    assert "phone call" in prompt.lower()
    assert "never read out lists" in prompt.lower()


def test_ask_routes_through_the_one_shared_brain(client, token, monkeypatch):
    """The phone must reach run_chat_turn — the same function the browser
    and the 3D console use — not a private copy of the conversation loop."""
    seen = {}

    async def fake_run_chat_turn(message, history, **kwargs):
        seen["message"] = message
        seen["history"] = history
        return "Pipeline reviewed.", [{"name": "buildpro_matching"}, {"name": "web_search"}]

    monkeypatch.setattr("core.headless.ui.run_chat_turn", fake_run_chat_turn)
    resp = client.post(
        "/api/voice/ask",
        headers={"Authorization": f"Bearer {token}"},
        json={"message": "check the pipeline", "history": [{"role": "user", "text": "earlier"}]},
    )
    assert resp.status_code == 200
    assert seen["message"] == "check the pipeline"
    assert seen["history"] == [{"role": "user", "text": "earlier"}]
    assert resp.json()["reply"] == "Pipeline reviewed."


def test_ask_returns_tool_names_only_never_tool_payloads(client, token, monkeypatch):
    """Tool results can hold client records. Names are all the voice layer
    needs, so that's all it gets."""
    async def fake_run_chat_turn(message, history, **kwargs):
        return "Done.", [{"name": "hubspot", "result": "Marcus Webb, marcus@acme.com, $240k deal"}]

    monkeypatch.setattr("core.headless.ui.run_chat_turn", fake_run_chat_turn)
    resp = client.post(
        "/api/voice/ask", headers={"Authorization": f"Bearer {token}"}, json={"message": "check hubspot"}
    )
    body = resp.json()
    assert body["tools_used"] == ["hubspot"]
    assert "marcus@acme.com" not in resp.text
    assert "240k" not in resp.text


def test_provider_outage_surfaces_as_an_error_not_a_fabricated_reply(client, token, monkeypatch):
    async def boom(message, history, **kwargs):
        raise HTTPException(status_code=503, detail="No AI provider is configured")

    monkeypatch.setattr("core.headless.ui.run_chat_turn", boom)
    resp = client.post(
        "/api/voice/ask", headers={"Authorization": f"Bearer {token}"}, json={"message": "hi"}
    )
    assert resp.status_code == 503


def test_call_logging_failure_never_breaks_the_hangup_path(client, token, monkeypatch):
    def broken_record(*a, **k):
        raise RuntimeError("audit db unavailable")

    monkeypatch.setattr("actions.audit_log.record", broken_record)
    resp = client.post(
        "/api/voice/call-ended",
        headers={"Authorization": f"Bearer {token}"},
        json={"agent_call_id": "ac_1", "summary": "discussed the pipeline"},
    )
    assert resp.status_code == 200
    assert resp.json()["logged"] is False
