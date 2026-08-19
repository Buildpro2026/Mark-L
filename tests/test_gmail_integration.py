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

    def __init__(self, messages_list=None, message_by_id=None, draft_result=None, send_result=None, attachment_by_id=None):
        self._messages_list = messages_list or {"messages": []}
        self._message_by_id = message_by_id or {}
        self._draft_result = draft_result
        self._send_result = send_result
        self._attachment_by_id = attachment_by_id or {}
        self.sent_bodies = []
        self.drafted_bodies = []

    def users(self):
        return self

    def messages(self):
        return self

    def drafts(self):
        return self

    def attachments(self):
        return self

    def list(self, userId, q=None, maxResults=None):
        return _Execute(self._messages_list)

    def get(self, userId, id, format=None, messageId=None):
        # Same method name serves both messages().get(id=...) and
        # attachments().get(messageId=..., id=...) — the real googleapiclient
        # dispatches by which chain called it, dict lookup does the same here.
        if messageId is not None:
            return _Execute(self._attachment_by_id[(messageId, id)])
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


# ── attachments ────────────────────────────────────────────────────────────

def _payload_with_resume_attachment():
    return {
        "mimeType": "multipart/mixed",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(b"See attached.").decode()}},
            {
                "mimeType": "application/pdf", "filename": "resume.pdf",
                "body": {"attachmentId": "att-1", "size": 12345},
            },
            # Inline image with no filename/attachmentId — must be skipped.
            {"mimeType": "image/png", "body": {"data": "abc"}},
        ],
    }


def test_extract_attachments_finds_the_named_attachment_and_skips_inline_parts():
    found = gmail._extract_attachments(_payload_with_resume_attachment())
    assert len(found) == 1
    assert found[0]["filename"] == "resume.pdf"
    assert found[0]["attachment_id"] == "att-1"
    assert found[0]["mime_type"] == "application/pdf"
    assert found[0]["size"] == 12345


def test_extract_attachments_returns_empty_list_with_no_attachments():
    payload = {"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(b"hi").decode()}}
    assert gmail._extract_attachments(payload) == []


def test_get_message_includes_attachments(monkeypatch):
    raw = _raw_message_payload()
    raw["payload"] = _payload_with_resume_attachment()
    fake = FakeGmailService(message_by_id={"m1": raw})
    monkeypatch.setattr(gmail, "_service", lambda: fake)

    msg = gmail.get_message("m1")
    assert len(msg["attachments"]) == 1
    assert msg["attachments"][0]["filename"] == "resume.pdf"


def test_is_likely_resume():
    assert gmail.is_likely_resume("resume.pdf") is True
    assert gmail.is_likely_resume("CV.DOCX") is True
    assert gmail.is_likely_resume("cover_letter.doc") is True
    assert gmail.is_likely_resume("headshot.png") is False
    assert gmail.is_likely_resume("signature.jpg") is False


def test_download_attachment_decodes_real_bytes(monkeypatch):
    raw_bytes = b"%PDF-1.4 fake pdf content"
    fake = FakeGmailService(attachment_by_id={
        ("m1", "att-1"): {"data": base64.urlsafe_b64encode(raw_bytes).decode(), "size": len(raw_bytes)},
    })
    monkeypatch.setattr(gmail, "_service", lambda: fake)

    r = gmail.download_attachment("m1", "att-1")
    assert r["ok"] is True
    assert r["data"] == raw_bytes


def test_download_attachment_not_authorized_reports_honestly(monkeypatch):
    def raise_not_authorized():
        raise RuntimeError("no token")
    monkeypatch.setattr(gmail, "_service", raise_not_authorized)

    r = gmail.download_attachment("m1", "att-1")
    assert r["ok"] is False
    assert r["state"] == "NOT_AUTHORIZED"


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


def test_classify_does_not_false_positive_on_application_as_a_generic_word():
    # Found live 2026-08-19: a Render.com welcome email ("cloud application
    # platform") got classified candidate_reply and created a real HubSpot
    # contact. Bare "application" is too generic once body text is scanned.
    msg = {
        "subject": "Ready to ship with Render?",
        "sender": "hello@render.com",
        "body": "Welcome! Render gives you a fast, reliable cloud application platform that scales with you.",
    }
    assert gmail.classify_message(msg) == "uncategorized"


def test_classify_does_not_false_positive_on_cv_inside_a_tracking_url():
    # Found live 2026-08-19: a Skool newsletter got classified
    # candidate_reply because a base64-ish tracking-link substring
    # happened to contain the two letters "cv". Bare "cv" is too short to
    # safely substring-match against a whole email body/URL soup. Sender
    # is a plain address here (not noreply@) so the notification rule
    # can't mask what's actually being tested: that "cv" alone no longer
    # triggers candidate_reply.
    msg = {
        "subject": "1 event happening tomorrow",
        "sender": "events@skool.com",
        "body": "Click here: https://skool.com/t/bdnm9-2finiup5jloaxcxg27utjtdhbjzp13p6e3jxsnwk-2bktrhbx-2fcwcvhop7n0eiqnih4iv2knyb57xdjor0kzmxvnij9yy5x",
    }
    assert gmail.classify_message(msg) == "uncategorized"


def test_classify_candidate_reply_from_body_with_generic_subject():
    # The bug Lee reported: a generic subject ("Hi there") with the real
    # signal only in the body used to fall through to "uncategorized"
    # because classify_message() never looked past subject+sender.
    msg = {
        "subject": "Hi there",
        "sender": "jane@example.com",
        "body": "Hi, I saw your posting and I'm attaching my resume for the electrician role.",
    }
    assert gmail.classify_message(msg) == "candidate_reply"


def test_classify_client_inquiry_from_snippet_with_generic_subject():
    msg = {
        "subject": "Question",
        "sender": "someone@construction.com",
        "snippet": "Could you send me a quote for a project starting next month?",
    }
    assert gmail.classify_message(msg) == "client_inquiry"


def test_classify_body_is_capped_so_a_long_quoted_thread_cant_dominate():
    # A keyword sitting way past the 2000-char cap (e.g. deep in a quoted
    # thread or legal signature) should not flip classification — this is
    # a cap, not a promise to scan an unbounded amount of quoted history.
    msg = {
        "subject": "Hello",
        "sender": "friend@example.com",
        "body": ("x" * 2500) + "resume",
    }
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
