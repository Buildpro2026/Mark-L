"""Phase 4 Part 19 — a real, working proactive delivery mechanism:
emailing the executive brief on demand through the already-authorized
Gmail integration. On-demand only (Lee asking is the approval), not a
scheduled unattended send — see the gmail tool's send_brief docstring
for why that's a deliberate scope decision, not an oversight.
"""
import asyncio
import importlib.util
from pathlib import Path

import pytest

from actions.executive_brief import format_brief_as_email

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _new_live(name="jarvis_main_brief_email"):
    main = load_module(name, "main.py")
    live = object.__new__(main.JarvisLive)
    return main, live


def _ui_stub():
    return type("UIStub", (), {"muted": False, "set_state": lambda self, s: None, "write_log": lambda self, m: None})()


def _live(main):
    live = object.__new__(main.JarvisLive)
    live.ui = _ui_stub()
    live._dashboard = None
    live._loop = None
    return live


def _make_fc(**args):
    return type("FC", (), {"id": "call-1", "name": "gmail", "args": args})()


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    from actions import audit_log
    monkeypatch.setattr(audit_log, "DB_PATH", tmp_path / "test_audit.db")


# ── format_brief_as_email — pure formatting ─────────────────────────

def test_format_includes_risks_when_present():
    brief = {"risks": [{"kind": "stalled_approval", "detail": "Something waiting"}], "pending_approvals": [], "daily_deal_finders": {}, "recommended_actions": [], "strategic_objective": {}}
    subject, body = format_brief_as_email(brief)
    assert "attention needed" in subject.lower()
    assert "Something waiting" in body


def test_format_reports_all_clear_when_nothing_flagged():
    brief = {"risks": [], "pending_approvals": [], "daily_deal_finders": {}, "recommended_actions": [], "strategic_objective": {}}
    subject, body = format_brief_as_email(brief)
    assert "all clear" in subject.lower()


def test_format_includes_high_ticket_picks():
    brief = {
        "risks": [], "pending_approvals": [], "recommended_actions": [], "strategic_objective": {},
        "daily_deal_finders": {"high_ticket_picks": [{"name": "Premium Grill", "current_price": 300.0}]},
    }
    _, body = format_brief_as_email(brief)
    assert "Premium Grill" in body
    assert "300.0" in body


def test_format_never_fabricates_a_section_that_has_no_data():
    brief = {"risks": [], "pending_approvals": [], "daily_deal_finders": {}, "recommended_actions": [], "strategic_objective": {}}
    _, body = format_brief_as_email(brief)
    assert "WAITING ON YOUR APPROVAL" not in body
    assert "DDF" not in body


# ── send_brief tool action ───────────────────────────────────────────

def test_send_brief_reports_honestly_when_gmail_not_authorized(monkeypatch):
    from actions import gmail_integration
    monkeypatch.setattr(gmail_integration, "get_own_email_address", lambda: {"ok": False, "state": "NOT_AUTHORIZED", "detail": "not set up"})
    main, live = _new_live()
    l = _live(main)
    response = _run(l._execute_tool(_make_fc(action="send_brief")))
    assert "couldn't send" in response.response["result"].lower()


def test_send_brief_sends_to_the_authenticated_account_own_address(monkeypatch):
    from actions import gmail_integration
    from actions import executive_brief
    monkeypatch.setattr(gmail_integration, "get_own_email_address", lambda: {"ok": True, "email": "lee@example.com"})
    monkeypatch.setattr(executive_brief, "generate_brief", lambda: {"risks": [], "pending_approvals": [], "daily_deal_finders": {}, "recommended_actions": [], "strategic_objective": {}})
    captured = {}

    def _fake_send(to, subject, body, approved=False):
        captured.update(to=to, subject=subject, body=body, approved=approved)
        return {"ok": True, "message_id": "m1"}

    monkeypatch.setattr(gmail_integration, "send_email", _fake_send)
    main, live = _new_live()
    l = _live(main)
    response = _run(l._execute_tool(_make_fc(action="send_brief")))
    assert "sent the brief to lee@example.com" in response.response["result"].lower()
    assert captured["to"] == "lee@example.com"
    assert captured["approved"] is True


def test_send_brief_failure_is_reported_not_fabricated(monkeypatch):
    from actions import gmail_integration
    from actions import executive_brief
    monkeypatch.setattr(gmail_integration, "get_own_email_address", lambda: {"ok": True, "email": "lee@example.com"})
    monkeypatch.setattr(executive_brief, "generate_brief", lambda: {"risks": [], "pending_approvals": [], "daily_deal_finders": {}, "recommended_actions": [], "strategic_objective": {}})
    monkeypatch.setattr(gmail_integration, "send_email", lambda *a, **k: {"ok": False, "state": "ERROR", "detail": "quota exceeded"})
    main, live = _new_live()
    l = _live(main)
    response = _run(l._execute_tool(_make_fc(action="send_brief")))
    assert "couldn't send the brief" in response.response["result"].lower()
