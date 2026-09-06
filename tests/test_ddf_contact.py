import pytest
from fastapi.testclient import TestClient

from actions import ddf_contact
from ddf_site.server import DDFSiteServer


@pytest.fixture(autouse=True)
def _isolate_db(monkeypatch, tmp_path):
    monkeypatch.setattr(ddf_contact, "DB_PATH", tmp_path / "test_ddf_contact.db")
    # Gmail isn't authorized in test environments — assert on notified=False
    # explicitly rather than mocking it away, so a real regression in the
    # notify path (e.g. an exception leaking as a 500) would still surface.


def _client():
    return TestClient(DDFSiteServer().app)


def test_valid_submission_persists_and_returns_success():
    client = _client()
    r = client.post("/api/contact", json={
        "name": "Jane Doe", "email": "jane@example.com", "message": "Is this still in stock?",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["id"]
    assert "notified" in data

    rows = ddf_contact.list_contact_messages()
    assert len(rows) == 1
    assert rows[0]["email"] == "jane@example.com"
    assert rows[0]["message"] == "Is this still in stock?"


def test_invalid_email_is_rejected_and_not_persisted():
    client = _client()
    r = client.post("/api/contact", json={"name": "Bad", "email": "not-an-email", "message": "hi"})
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_email"
    assert ddf_contact.list_contact_messages() == []


def test_blank_message_is_rejected():
    client = _client()
    r = client.post("/api/contact", json={"name": "", "email": "a@b.com", "message": "   "})
    assert r.status_code == 400
    assert r.json()["error"] == "empty_message"


def test_missing_email_field_is_rejected_by_schema():
    client = _client()
    r = client.post("/api/contact", json={"name": "X", "message": "hi"})
    assert r.status_code == 422


def test_duplicate_submission_does_not_create_a_second_row():
    client = _client()
    payload = {"name": "Jane Doe", "email": "jane@example.com", "message": "Is this still in stock?"}
    first = client.post("/api/contact", json=payload).json()
    second = client.post("/api/contact", json=payload).json()

    assert second["ok"] is True
    assert second.get("duplicate") is True
    assert second["id"] == first["id"]
    assert len(ddf_contact.list_contact_messages()) == 1


def test_different_message_from_same_email_is_not_treated_as_duplicate():
    client = _client()
    client.post("/api/contact", json={"name": "Jane", "email": "jane@example.com", "message": "First question"})
    r = client.post("/api/contact", json={"name": "Jane", "email": "jane@example.com", "message": "Second question"})
    assert r.json().get("duplicate") is not True
    assert len(ddf_contact.list_contact_messages()) == 2


def test_gmail_send_failure_does_not_fail_the_submission(monkeypatch):
    # A real-world failure mode (Gmail down, not authorized, quota) must
    # never turn an already-persisted message into a reported failure.
    def _boom(*a, **k):
        raise RuntimeError("simulated Gmail outage")

    monkeypatch.setattr("actions.gmail_integration.send_email", _boom)
    client = _client()
    r = client.post("/api/contact", json={"name": "Jane", "email": "jane@example.com", "message": "hello"})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["notified"] is False

    rows = ddf_contact.list_contact_messages()
    assert rows[0]["notified"] == 0
    assert "simulated Gmail outage" in rows[0]["notify_error"]


def test_validate_email_rejects_common_garbage():
    assert ddf_contact.validate_email("a@b.com") is True
    assert ddf_contact.validate_email("") is False
    assert ddf_contact.validate_email("no-at-sign") is False
    assert ddf_contact.validate_email("missing-domain@") is False
    assert ddf_contact.validate_email("@missing-local.com") is False
