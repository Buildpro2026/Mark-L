"""actions/notifications.py's notify() computes a structured destination for
every actionable message but was never actually putting it in the message
the transport sends — an SMS that says "Open in HubSpot" with no link isn't
actionable, it just sounds like it should be. These tests are the real
transport body, not just the returned outcome dict's destination field."""
import pytest

from actions import notifications


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    from core.headless import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "jarvis2.db")
    monkeypatch.setattr(config, "JARVIS_OWNER_PHONE", "+13125550142")
    return tmp_path


@pytest.fixture
def sent(isolated_db, monkeypatch):
    from actions import twilio_integration as twilio
    captured = []
    monkeypatch.setattr(twilio, "is_configured", lambda: True)
    monkeypatch.setattr(twilio, "send_sms",
                        lambda to, body: (captured.append((to, body)), {"ok": True, "sid": "SM1"})[1])
    return captured


def test_a_hubspot_destination_link_actually_reaches_the_sms_body(sent):
    outcome = notifications.business_alert(
        "evt-hubspot-1", "New candidate added", "Jane Doe applied.",
        destination_source="hubspot_contact", destination_data={"contact_id": "42"})
    assert outcome["actionable"] is True
    assert len(sent) == 1
    assert "Open in HubSpot" in sent[0][1]
    assert "https://app.hubspot.com/contacts/contact/42" in sent[0][1]


def test_an_internal_destination_becomes_an_absolute_clickable_url(sent):
    outcome = notifications.approval_request("t-99", "buildpro_prospecting_agent", "Send intro email")
    assert outcome["actionable"] is True
    assert len(sent) == 1
    from core.headless.config import PUBLIC_BASE_URL
    assert f"{PUBLIC_BASE_URL}/ui#approvals/t-99" in sent[0][1]
    assert "Review Approval" in sent[0][1]


def test_no_destination_source_stays_informational_with_no_dead_link(sent):
    outcome = notifications.urgent("evt-plain", "Server is down", "jarvis-headless-core")
    assert outcome["actionable"] is False
    assert len(sent) == 1
    # No fabricated link — the original detail text, untouched.
    assert sent[0][1].strip().endswith("jarvis-headless-core")


def test_a_destination_that_cannot_be_built_never_produces_a_dead_link(sent):
    # No id at all -> notification_destinations.build() returns None for
    # this source -> stays informational rather than a broken link.
    outcome = notifications.business_alert(
        "evt-no-id", "New candidate added", "Someone applied.",
        destination_source="hubspot_contact", destination_data={})
    assert outcome["actionable"] is False
    assert "http" not in sent[0][1]


def test_an_approval_link_only_opens_a_review_view_never_executes(sent):
    # Structural guarantee, not just a naming convention: the destination
    # notifications.py builds for an approval is notification_destinations.
    # approval()'s INTERNAL /ui#approvals/<id> view route — there is no
    # execute-style destination anywhere in that builder for JARVIS to
    # accidentally wire a click straight through to.
    from actions import notification_destinations as nd
    dest = nd.approval("t-1")
    assert dest["kind"] == nd.INTERNAL
    assert dest["path"] == "/ui#approvals/t-1"
    assert dest["action"] == "open_approval"
