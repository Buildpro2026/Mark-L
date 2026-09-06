"""main.py's "gmail" voice tool — read/draft/gated-send wiring into
actions/gmail_integration.py. Never makes a live Gmail/OAuth call: every
gmail_integration.py / google_auth.py function is monkeypatched.

This tool didn't exist before this prompt — gmail_integration.py was fully
built, tested, and OAuth-authorized, but wired into nothing (no voice tool,
no dashboard action, no agent), so JARVIS had no way to actually read or
send email despite the backend being real. Wiring it in follows the same
explicit-instruction-gate pattern already used by the "communications"
(Twilio) tool for send_sms/call: 'send' requires the recipient AND content
to already be present in the same tool call, and the tool description
tells Gemini this is only ever satisfied by an explicit user instruction —
there's no separate approval-queue step.
"""
import asyncio
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _new_live(name="jarvis_main_gmail"):
    main = load_module(name, "main.py")
    live = object.__new__(main.JarvisLive)
    return main, live


def _ui_stub():
    return type("UIStub", (), {
        "muted": False,
        "set_state": lambda self, s: None,
        "write_log": lambda self, m: None,
    })()


def _make_fc(**args):
    return type("FC", (), {"id": "call-1", "name": "gmail", "args": args})()


def _run(coro):
    return asyncio.run(coro)


def _live(main):
    live = object.__new__(main.JarvisLive)
    live.ui = _ui_stub()
    live._dashboard = None
    live._loop = None
    return live


def test_tool_declared_for_gemini():
    main, _ = _new_live()
    names = [t["name"] for t in main.TOOL_DECLARATIONS]
    assert "gmail" in names


# ── status ────────────────────────────────────────────────────────────

def test_status_reports_authorized(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.google_auth, "get_credential_status",
                         lambda: {"credential_file": "present", "token_cached": True, "authorized": True})

    response = _run(live._execute_tool(_make_fc(action="status")))
    assert "connected and authorized" in response.response["result"].lower()


def test_status_reports_missing_client_secret(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.google_auth, "get_credential_status",
                         lambda: {"credential_file": "missing", "token_cached": False, "authorized": False})

    response = _run(live._execute_tool(_make_fc(action="status")))
    assert "isn't set up" in response.response["result"].lower()


def test_status_reports_unauthorized_when_secret_present_but_not_authorized(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.google_auth, "get_credential_status",
                         lambda: {"credential_file": "present", "token_cached": False, "authorized": False})

    response = _run(live._execute_tool(_make_fc(action="status")))
    assert "aren't authorized yet" in response.response["result"].lower()


# ── list (read) ───────────────────────────────────────────────────────

def test_list_reports_no_matching_messages_when_empty(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.gmail_integration, "list_messages",
                         lambda query, max_results: {"ok": True, "messages": []})

    response = _run(live._execute_tool(_make_fc(action="list")))
    assert "no matching messages" in response.response["result"].lower()


def test_list_summarizes_sender_and_subject(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.gmail_integration, "list_messages", lambda query, max_results: {
        "ok": True,
        "messages": [{"sender": "boss@company.com", "subject": "Q3 numbers"}],
    })

    response = _run(live._execute_tool(_make_fc(action="list")))
    result = response.response["result"]
    assert "boss@company.com" in result
    assert "Q3 numbers" in result


def test_list_surfaces_a_failure_honestly_not_as_empty(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.gmail_integration, "list_messages",
                         lambda query, max_results: {"ok": False, "state": "NOT_AUTHORIZED", "detail": "no token", "messages": []})

    response = _run(live._execute_tool(_make_fc(action="list")))
    result = response.response["result"].lower()
    assert "couldn't read gmail" in result
    assert "not_authorized" in result


# ── draft (always safe) ──────────────────────────────────────────────

def test_draft_without_recipient_or_body_is_refused(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    called = {"n": 0}
    monkeypatch.setattr(main.gmail_integration, "create_draft", lambda *a: called.__setitem__("n", called["n"] + 1))

    response = _run(live._execute_tool(_make_fc(action="draft")))
    assert "recipient" in response.response["result"].lower()
    assert called["n"] == 0   # never even attempted


def test_draft_success_reports_saved(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.gmail_integration, "create_draft",
                         lambda to, subject, body: {"ok": True, "draft_id": "d1"})

    response = _run(live._execute_tool(_make_fc(action="draft", to="a@b.com", subject="Hi", body="Hello there")))
    assert "draft saved to a@b.com" in response.response["result"].lower()


def test_draft_failure_is_reported_not_fabricated_as_success(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.gmail_integration, "create_draft",
                         lambda to, subject, body: {"ok": False, "state": "ERROR", "detail": "quota exceeded"})

    response = _run(live._execute_tool(_make_fc(action="draft", to="a@b.com", body="Hello")))
    result = response.response["result"].lower()
    assert "couldn't create the draft" in result
    assert "quota exceeded" in result


# ── send (gated — the critical safety path) ─────────────────────────

def test_send_without_recipient_or_body_is_refused_and_never_calls_the_real_sender(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    called = {"n": 0}
    monkeypatch.setattr(main.gmail_integration, "send_email",
                         lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    response = _run(live._execute_tool(_make_fc(action="send")))
    assert "recipient" in response.response["result"].lower() or "content" in response.response["result"].lower()
    assert called["n"] == 0   # the code-level gate, independent of the LLM's own judgment


def test_send_without_body_only_is_also_refused(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    called = {"n": 0}
    monkeypatch.setattr(main.gmail_integration, "send_email",
                         lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    response = _run(live._execute_tool(_make_fc(action="send", to="a@b.com")))
    assert called["n"] == 0


def test_send_with_explicit_recipient_and_content_passes_approved_true(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    captured = {}

    def _fake_send(to, subject, body, approved=False):
        captured.update(to=to, subject=subject, body=body, approved=approved)
        return {"ok": True, "message_id": "m1"}

    monkeypatch.setattr(main.gmail_integration, "send_email", _fake_send)

    response = _run(live._execute_tool(_make_fc(
        action="send", to="john@example.com", subject="Meeting", body="Moved to 3pm",
    )))

    assert captured == {"to": "john@example.com", "subject": "Meeting", "body": "Moved to 3pm", "approved": True}
    assert "email sent to john@example.com" in response.response["result"].lower()


def test_send_failure_is_reported_not_fabricated_as_success(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.gmail_integration, "send_email",
                         lambda to, subject, body, approved=False: {"ok": False, "state": "NOT_AUTHORIZED", "detail": "no token"})

    response = _run(live._execute_tool(_make_fc(action="send", to="a@b.com", body="hi")))
    result = response.response["result"].lower()
    assert "couldn't send the email" in result
    assert "not_authorized" in result


def test_unknown_gmail_action_reports_clearly(monkeypatch):
    main, _ = _new_live()
    live = _live(main)

    response = _run(live._execute_tool(_make_fc(action="delete_everything")))
    assert "unknown gmail action" in response.response["result"].lower()
