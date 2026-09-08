"""Filing a resume in the recruiting mailbox under its label.

A Gmail label is not a folder on disk: it is an object with an id, and a
message is "in" it only because that id appears in the message's
labelIds. So this uses labels.list/create to resolve the label and
messages.insert to file the message — insert rather than send, because
the destination IS the authenticated mailbox and nothing leaves the
system, which is why it does not touch the send approval gate.
"""
import base64

import pytest

from actions import gmail_integration as gmail
from actions import resume_intake as ri


TXT = b"Jane Doe\njane@example.com\n(312) 555-0142\nSuperintendent.\n"


class _FakeGmail:
    """Records what was asked of the API so the calls can be asserted."""

    def __init__(self, labels=None, insert_error=None, label_error=None):
        self._labels = list(labels or [])
        self._insert_error = insert_error
        self._label_error = label_error
        self.created_labels = []
        self.inserted = []

    def users(self):
        return self

    def labels(self):
        return self

    def messages(self):
        return self

    def list(self, userId=None):
        if self._label_error:
            raise self._label_error
        return _Exec({"labels": self._labels})

    def create(self, userId=None, body=None):
        if self._label_error:
            raise self._label_error
        self.created_labels.append(body)
        label = {"id": "Label_9", "name": body["name"]}
        self._labels.append(label)
        return _Exec(label)

    def insert(self, userId=None, body=None, internalDateSource=None):
        if self._insert_error:
            raise self._insert_error
        self.inserted.append(body)
        return _Exec({"id": "msg-1", "threadId": "thr-1"})


class _Exec:
    def __init__(self, value): self._value = value
    def execute(self): return self._value


@pytest.fixture
def fake(monkeypatch):
    service = _FakeGmail()
    monkeypatch.setattr(gmail, "_service", lambda: service)
    return service


# ══ LABELS ═══════════════════════════════════════════════════════════════

def test_the_label_is_created_when_it_does_not_exist(fake):
    result = gmail.ensure_label("Candidate Resumes")
    assert result["ok"] is True
    assert result["created"] is True
    assert fake.created_labels[0]["name"] == "Candidate Resumes"


def test_an_existing_label_is_reused_not_duplicated(monkeypatch):
    service = _FakeGmail(labels=[{"id": "Label_1", "name": "Candidate Resumes"}])
    monkeypatch.setattr(gmail, "_service", lambda: service)
    result = gmail.ensure_label("Candidate Resumes")
    assert result["label_id"] == "Label_1"
    assert result["created"] is False
    assert service.created_labels == []


def test_the_label_lookup_is_case_insensitive(monkeypatch):
    service = _FakeGmail(labels=[{"id": "Label_1", "name": "candidate resumes"}])
    monkeypatch.setattr(gmail, "_service", lambda: service)
    assert gmail.ensure_label("Candidate Resumes")["created"] is False


def test_a_missing_scope_names_the_scope_to_grant(monkeypatch):
    service = _FakeGmail(label_error=RuntimeError(
        "Request had insufficient authentication scopes"))
    monkeypatch.setattr(gmail, "_service", lambda: service)
    result = gmail.ensure_label()
    assert result["ok"] is False
    assert result["state"] == "INSUFFICIENT_SCOPE"
    assert result["required_scope"] == gmail.LABEL_SCOPE


# ══ FILING THE MESSAGE ═══════════════════════════════════════════════════

def test_a_resume_is_filed_with_the_label_applied(fake):
    result = gmail.file_resume_in_mailbox(
        "jane.pdf", b"%PDF-1.4 data", "application/pdf",
        candidate_email="jane@example.com", candidate_name="Jane Doe")

    assert result["ok"] is True
    assert result["state"] == "FILED"
    assert result["message_id"] == "msg-1"
    assert result["labelled"] is True
    assert "Label_9" in fake.inserted[0]["labelIds"]


def test_the_message_actually_carries_the_resume_as_an_attachment(fake):
    gmail.file_resume_in_mailbox("jane.pdf", b"%PDF-1.4 the real bytes",
                                 "application/pdf", candidate_name="Jane Doe")
    raw = fake.inserted[0]["raw"]
    decoded = base64.urlsafe_b64decode(raw).decode("utf-8", errors="replace")
    assert "jane.pdf" in decoded
    assert "attachment" in decoded.lower()
    assert base64.b64encode(b"%PDF-1.4 the real bytes").decode()[:20] in decoded


def test_it_is_addressed_to_the_recruiting_mailbox(fake):
    gmail.file_resume_in_mailbox("j.pdf", b"x", "application/pdf")
    decoded = base64.urlsafe_b64decode(fake.inserted[0]["raw"]).decode(errors="replace")
    assert "buildprorecruiters@gmail.com" in decoded


def test_the_candidate_is_identified_in_the_message(fake):
    gmail.file_resume_in_mailbox("j.pdf", b"x", "application/pdf",
                                 candidate_email="jane@example.com",
                                 candidate_name="Jane Doe")
    decoded = base64.urlsafe_b64decode(fake.inserted[0]["raw"]).decode(errors="replace")
    assert "Jane Doe" in decoded
    assert "jane@example.com" in decoded


def test_filing_uses_insert_not_send(fake):
    # Nothing leaves the system, so the send approval gate is not involved
    # and must not be reachable from this path.
    gmail.file_resume_in_mailbox("j.pdf", b"x", "application/pdf")
    assert fake.inserted, "the message was not inserted"


def test_a_refused_insert_is_never_reported_as_filed(monkeypatch):
    service = _FakeGmail(insert_error=RuntimeError("insufficient authentication scopes"))
    monkeypatch.setattr(gmail, "_service", lambda: service)
    result = gmail.file_resume_in_mailbox("j.pdf", b"x", "application/pdf")
    assert result["ok"] is False
    assert result["state"] == "INSUFFICIENT_SCOPE"
    assert result["required_scope"] == gmail.INSERT_SCOPE
    assert "remedy" in result


def test_an_unlabelled_but_filed_message_says_so(monkeypatch):
    service = _FakeGmail(label_error=RuntimeError("insufficient authentication scopes"))
    monkeypatch.setattr(gmail, "_service", lambda: service)
    result = gmail.file_resume_in_mailbox("j.pdf", b"x", "application/pdf")
    assert result["ok"] is True
    assert result["labelled"] is False
    assert "label could not be applied" in result["detail"]


def test_a_response_with_no_message_id_is_not_success(monkeypatch):
    class _NoId(_FakeGmail):
        def insert(self, userId=None, body=None, internalDateSource=None):
            return _Exec({})
    monkeypatch.setattr(gmail, "_service", lambda: _NoId())
    result = gmail.file_resume_in_mailbox("j.pdf", b"x", "application/pdf")
    assert result["ok"] is False
    assert "no message id" in result["detail"]


# ══ THE UPLOAD PIPELINE END TO END ═══════════════════════════════════════

def test_an_upload_is_stored_recorded_and_delivered(tmp_path, monkeypatch):
    from actions import buildpro_data
    monkeypatch.setattr(buildpro_data, "DB_PATH", tmp_path / "bp.db")
    service = _FakeGmail()
    monkeypatch.setattr(gmail, "_service", lambda: service)

    result = ri.process_upload(TXT, "jane.txt", tmp_path / "r")
    assert result["ok"] is True
    assert result["stored"] is True
    assert result["delivered"] is True
    assert result["candidate_id"] is not None
    assert "filed in the recruiting mailbox" in result["detail"]
    assert service.inserted, "the resume never reached the mailbox"


def test_a_delivery_failure_does_not_become_a_failed_upload(tmp_path, monkeypatch):
    from actions import buildpro_data
    monkeypatch.setattr(buildpro_data, "DB_PATH", tmp_path / "bp.db")
    monkeypatch.setattr(gmail, "_service",
                        lambda: (_ for _ in ()).throw(RuntimeError("gmail down")))

    result = ri.process_upload(TXT, "jane.txt", tmp_path / "r")
    # The bytes ARE on disk. Telling the candidate it failed makes them
    # upload it again.
    assert result["ok"] is True
    assert result["stored"] is True
    assert result["delivered"] is False
    assert "could not be filed" in result["detail"]


def test_the_candidate_is_never_told_it_was_filed_when_it_was_not(tmp_path, monkeypatch):
    from actions import buildpro_data
    monkeypatch.setattr(buildpro_data, "DB_PATH", tmp_path / "bp.db")
    service = _FakeGmail(insert_error=RuntimeError("quota exceeded"))
    monkeypatch.setattr(gmail, "_service", lambda: service)

    result = ri.process_upload(TXT, "jane.txt", tmp_path / "r")
    assert result["delivered"] is False
    assert "filed in the recruiting mailbox." not in result["detail"]


def test_delivery_can_be_switched_off_for_a_local_run(tmp_path, monkeypatch):
    from actions import buildpro_data
    monkeypatch.setattr(buildpro_data, "DB_PATH", tmp_path / "bp.db")
    called = []
    monkeypatch.setattr(gmail, "_service", lambda: called.append(1))
    result = ri.process_upload(TXT, "j.txt", tmp_path / "r", deliver=False)
    assert result["delivered"] is False
    assert called == []


def test_a_rejected_file_is_never_delivered(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(gmail, "_service", lambda: called.append(1))
    result = ri.process_upload(b"MZ\x90\x00", "virus.pdf", tmp_path / "r")
    assert result["ok"] is False
    assert called == [], "a rejected file reached the mailbox"
