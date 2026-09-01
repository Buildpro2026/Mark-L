"""The approval notifier must reach Lee exactly once per decision, say
something he can act on, and never grant the approval it is reporting."""
import time

import pytest

from actions import approval_notifier


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    from core.headless import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "jarvis2.db")
    monkeypatch.setattr(config, "JARVIS_OWNER_PHONE", "+13125550142")
    return tmp_path


@pytest.fixture
def one_pending(monkeypatch):
    item = {
        "kind": "approval", "severity": 3, "task_id": "task_abc",
        "title": "buildpro_agent: send the Henderson contract for signature",
        "waited_hours": 2.0, "source": "pending_approval",
    }
    monkeypatch.setattr(approval_notifier, "pending_approvals", lambda: [item])
    return item


def test_message_names_the_decision_and_how_to_answer(one_pending):
    body = approval_notifier._compose(one_pending)
    assert "approval" in body.lower()
    assert "Henderson" in body                  # what it's actually about
    assert "APPROVE task_abc" in body           # how to respond
    assert "waiting 2h" in body                 # how long it's been sitting


def test_texts_once_then_stays_quiet(isolated_db, one_pending, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "actions.twilio_integration.send_sms",
        lambda to, body: (sent.append((to, body)), {"ok": True, "sid": "SM1"})[1],
    )

    first = approval_notifier.notify_pending()
    assert [a["action"] for a in first] == ["sms"]
    assert len(sent) == 1
    assert sent[0][0] == "+13125550142"

    # Same decision still pending on the next pass — must not text again.
    second = approval_notifier.notify_pending()
    assert second == []
    assert len(sent) == 1


def test_escalates_to_a_call_only_after_hours_and_only_once(isolated_db, one_pending, monkeypatch):
    monkeypatch.setattr("actions.twilio_integration.send_sms", lambda to, body: {"ok": True, "sid": "SM1"})
    calls = []
    monkeypatch.setattr("actions.cartesia_calls.is_configured", lambda: True)
    monkeypatch.setattr(
        "actions.cartesia_calls.place_call",
        lambda to, reason="", metadata=None: (calls.append((to, reason)), {"ok": True, "agent_call_id": "ac_1"})[1],
    )

    approval_notifier.notify_pending()
    assert calls == []          # just texted; far too early to phone anyone

    # Backdate the notification past the escalation window.
    conn = approval_notifier._connect()
    conn.execute(
        "UPDATE approval_notifications SET first_sent = ?",
        (time.time() - (approval_notifier.ESCALATE_AFTER_HOURS + 1) * 3600,),
    )
    conn.commit()
    conn.close()

    escalated = approval_notifier.notify_pending()
    assert [a["action"] for a in escalated] == ["call"]
    assert len(calls) == 1
    assert "Henderson" in calls[0][1]

    # Escalation is one-shot — a pending task must never become a phone
    # call every five minutes.
    assert approval_notifier.notify_pending() == []
    assert len(calls) == 1


def test_silent_when_owner_phone_is_unset(isolated_db, one_pending, monkeypatch):
    from core.headless import config
    monkeypatch.setattr(config, "JARVIS_OWNER_PHONE", None)
    monkeypatch.setattr(
        "actions.twilio_integration.send_sms",
        lambda to, body: pytest.fail("must not text with no configured number"),
    )
    assert approval_notifier.notify_pending() == []


def test_notifier_never_approves_anything(isolated_db, one_pending, monkeypatch):
    """The whole point of the approval gate is that a notification is not
    an approval. Guard it explicitly."""
    monkeypatch.setattr("actions.twilio_integration.send_sms", lambda to, body: {"ok": True, "sid": "SM1"})
    from actions.agent_orchestrator import orchestrator

    monkeypatch.setattr(
        orchestrator, "approve_task",
        lambda *a, **k: pytest.fail("notifier must never approve a task"),
        raising=False,
    )
    approval_notifier.notify_pending()


def test_clearing_lets_a_reopened_decision_notify_again(isolated_db, one_pending, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "actions.twilio_integration.send_sms",
        lambda to, body: (sent.append(body), {"ok": True, "sid": "SM1"})[1],
    )
    approval_notifier.notify_pending()
    approval_notifier.clear_notification("task_abc")
    approval_notifier.notify_pending()
    assert len(sent) == 2
