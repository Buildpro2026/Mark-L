import base64

import pytest

from actions import gmail_integration as gmail
from actions import google_auth


class _Execute:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class FakeGmailService:
    """Minimal chain-mock for service.users().messages()/.drafts()...() -
    only implements the exact call shapes gmail_integration.py uses."""

    def __init__(self, messages_list=None, message_by_id=None, draft_result=None, send_result=None):
        self._messages_list = messages_list or {"messages": []}
        self._message_by_id = message_by_id or {}
        self._draft_result = draft_result
        self._send_result = send_result
        self.sent_bodies = []
        self.drafted_bodies = []

    def users(self):
        return self

    def messages(self):
        return self

    def drafts(self):
        return self

    def list(self, userId, q=None, maxResults=None):
        return _Execute(self._messages_list)

    def get(self, userId, id, format=None):
        return _Execute(self._message_by_id[id])

    def send(self, userId, body):
        self.sent_bodies.append(body)
        return _Execute(self._send_result)

    def create(self, userId, body):
        self.drafted_bodies.append(body)
        return _Execute(self._draft_result)


def _raw_message_payload():
    return {
        "id": "m1", "threadId": "t1", "snippet": "hi there",
        "labelIds": ["INBOX"],
        "payload": {
            "headers": [
                {"name": "From", "value": "candidate@example.com"},
                {"name": "To", "value": "recruiter@buildpro.example"},
                {"name": "Subject", "value": "Re: Application - Electrician"},
                {"name": "Date", "value": "Mon, 10 Aug 2026 10:00:00 -0500"},
            ],
            "mimeType": "text/plain",
            "body": {"data": base64.urlsafe_b64encode(b"Attached is my resume.").decode()},
        },
    }


# ── list_messages / get_message ───────────────────────────────────────────

def test_get_message_extracts_all_fields(monkeypatch):
    fake = FakeGmailService(message_by_id={"m1": _raw_message_payload()})
    monkeypatch.setattr(gmail, "_service", lambda: fake)

    msg = gmail.get_message("m1")
    assert msg["sender"] == "candidate@example.com"
    assert msg["recipient"] == "recruiter@buildpro.example"
    assert msg["subject"] == "Re: Application - Electrician"
    assert msg["body"] == "Attached is my resume."
    assert msg["snippet"] == "hi there"


def test_extract_body_walks_multipart_payload():
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/html", "body": {"data": base64.urlsafe_b64encode(b"<p>hi</p>").decode()}},
            {"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(b"plain text body").decode()}},
        ],
    }
    assert gmail._extract_body(payload) == "plain text body"


def test_extract_body_returns_empty_string_when_no_plain_part():
    payload = {"mimeType": "text/html", "body": {"data": base64.urlsafe_b64encode(b"<p>hi</p>").decode()}}
    assert gmail._extract_body(payload) == ""


def test_list_messages_returns_full_message_details(monkeypatch):
    fake = FakeGmailService(
        messages_list={"messages": [{"id": "m1"}], "resultSizeEstimate": 1},
        message_by_id={"m1": _raw_message_payload()},
    )
    monkeypatch.setattr(gmail, "_service", lambda: fake)

    result = gmail.list_messages(query="is:unread")
    assert result["ok"] is True
    assert len(result["messages"]) == 1
    assert result["messages"][0]["subject"] == "Re: Application - Electrician"


def test_list_messages_not_authorized_reports_honestly(monkeypatch):
    def raise_not_authorized():
        raise RuntimeError("Google account not yet authorized.")

    monkeypatch.setattr(gmail, "_service", raise_not_authorized)
    result = gmail.list_messages()
    assert result["ok"] is False
    assert result["state"] == "NOT_AUTHORIZED"
    assert result["messages"] == []


def test_list_messages_api_error_does_not_crash(monkeypatch):
    def raise_error():
        raise Exception("quota exceeded")

    monkeypatch.setattr(gmail, "_service", raise_error)
    result = gmail.list_messages()
    assert result["ok"] is False
    assert result["state"] == "ERROR"


# ── classify_message ──────────────────────────────────────────────────────

def test_classify_candidate_reply():
    msg = {"subject": "Re: Application for Electrician role", "sender": "jane@example.com"}
    assert gmail.classify_message(msg) == "candidate_reply"


def test_classify_client_inquiry():
    msg = {"subject": "Quote request for new project", "sender": "client@construction.com"}
    assert gmail.classify_message(msg) == "client_inquiry"


def test_classify_notification():
    msg = {"subject": "Your weekly digest", "sender": "no-reply@service.com"}
    assert gmail.classify_message(msg) == "notification"


def test_classify_uncategorized_when_nothing_matches():
    msg = {"subject": "Hello", "sender": "friend@example.com"}
    assert gmail.classify_message(msg) == "uncategorized"


# ── create_draft (real capability, never sends) ──────────────────────────

def test_create_draft_succeeds_and_never_calls_send(monkeypatch):
    fake = FakeGmailService(draft_result={"id": "draft1"})
    monkeypatch.setattr(gmail, "_service", lambda: fake)

    result = gmail.create_draft("candidate@example.com", "Interview scheduling", "Are you free Tuesday?")
    assert result["ok"] is True
    assert result["draft_id"] == "draft1"
    assert fake.sent_bodies == []   # send() was never called
    assert len(fake.drafted_bodies) == 1


def test_create_draft_not_authorized(monkeypatch):
    def raise_not_authorized():
        raise RuntimeError("Google account not yet authorized.")

    monkeypatch.setattr(gmail, "_service", raise_not_authorized)
    result = gmail.create_draft("x@example.com", "s", "b")
    assert result["ok"] is False
    assert result["state"] == "NOT_AUTHORIZED"


# ── send_email — the approval gate ────────────────────────────────────────

def test_send_email_refuses_without_approval_and_never_touches_the_api(monkeypatch):
    calls = []
    monkeypatch.setattr(gmail, "_service", lambda: calls.append("called"))

    result = gmail.send_email("candidate@example.com", "Offer", "We'd like to offer you the role.")
    assert result["ok"] is False
    assert result["state"] == "NOT_APPROVED"
    assert calls == []   # _service() was never even constructed


def test_send_email_sends_when_approved_and_authorized(monkeypatch):
    fake = FakeGmailService(send_result={"id": "sent1"})
    monkeypatch.setattr(gmail, "_service", lambda: fake)

    result = gmail.send_email("candidate@example.com", "Offer", "Body", approved=True)
    assert result["ok"] is True
    assert result["message_id"] == "sent1"
    assert len(fake.sent_bodies) == 1


def test_send_email_approved_but_not_authorized(monkeypatch):
    def raise_not_authorized():
        raise RuntimeError("Google account not yet authorized.")

    monkeypatch.setattr(gmail, "_service", raise_not_authorized)
    result = gmail.send_email("x@example.com", "s", "b", approved=True)
    assert result["ok"] is False
    assert result["state"] == "NOT_AUTHORIZED"
