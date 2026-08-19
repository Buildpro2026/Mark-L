"""actions/candidate_intake.py — the resume-intake automation chain
(2026-08-19, Lee's spec). Mocks each real integration it calls
(gmail_integration, hubspot_integration, buildpro_data, agreement_signing)
so these tests cover the ORCHESTRATION (what gets called, in what order,
with what data, and how each optional step degrades) rather than
re-testing those modules' own already-covered internals.
"""
from actions import candidate_intake as intake


def _message(sender="Jane Doe <jane@example.com>", subject="Application - Electrician",
             attachments=None, message_id="m1"):
    return {
        "id": message_id, "sender": sender, "subject": subject,
        "snippet": "Please see my attached resume.",
        "attachments": attachments or [],
    }


# ── sender parsing ───────────────────────────────────────────────────────

def test_parse_sender_name_and_angle_bracket_email():
    assert intake._parse_sender("Jane Doe <jane@example.com>") == ("Jane Doe", "jane@example.com")


def test_parse_sender_bare_email_with_no_name():
    assert intake._parse_sender("jane@example.com") == ("", "jane@example.com")


def test_parse_sender_empty_string():
    assert intake._parse_sender("") == ("", "")


# ── process_candidate_email orchestration ────────────────────────────────

def test_refuses_when_no_email_can_be_determined(monkeypatch):
    result = intake.process_candidate_email(_message(sender=""))
    assert result["ok"] is False


def test_creates_local_candidate_record(monkeypatch):
    calls = {}
    monkeypatch.setattr(intake.buildpro_data, "upsert_candidate",
                         lambda name, **kw: (calls.setdefault("upsert_args", (name, kw)), (42, "created"))[1])
    monkeypatch.setattr(intake.buildpro_data, "update_candidate", lambda *a, **k: True)
    monkeypatch.setattr(intake.hubspot_integration, "is_configured", lambda: False)
    monkeypatch.setattr(intake.agreement_signing, "create_pending_agreement", lambda *a, **k: "tok123")
    monkeypatch.setattr(intake.gmail_integration, "create_draft", lambda *a, **k: {"ok": True, "draft_id": "d1"})

    result = intake.process_candidate_email(_message())

    assert result["ok"] is True
    assert result["candidate_id"] == 42
    assert result["candidate_action"] == "created"
    name, kwargs = calls["upsert_args"]
    assert name == "Jane Doe"
    assert kwargs["email"] == "jane@example.com"
    assert kwargs["source"] == "gmail_intake"


def test_hubspot_not_configured_still_completes_the_rest_of_the_chain(monkeypatch):
    monkeypatch.setattr(intake.buildpro_data, "upsert_candidate", lambda *a, **k: (1, "created"))
    monkeypatch.setattr(intake.buildpro_data, "update_candidate", lambda *a, **k: True)
    monkeypatch.setattr(intake.hubspot_integration, "is_configured", lambda: False)
    monkeypatch.setattr(intake.agreement_signing, "create_pending_agreement", lambda *a, **k: "tok123")
    monkeypatch.setattr(intake.gmail_integration, "create_draft", lambda *a, **k: {"ok": True, "draft_id": "d1"})

    result = intake.process_candidate_email(_message())

    assert result["ok"] is True
    assert result["hubspot_ok"] is False
    assert result["hubspot_contact_id"] is None
    assert result["welcome_email_drafted"] is True


def test_hubspot_upsert_links_contact_id_back_to_local_record(monkeypatch):
    linked = {}
    monkeypatch.setattr(intake.buildpro_data, "upsert_candidate", lambda *a, **k: (7, "created"))
    monkeypatch.setattr(intake.buildpro_data, "update_candidate",
                         lambda cid, **kw: linked.setdefault("call", (cid, kw)))
    monkeypatch.setattr(intake.hubspot_integration, "is_configured", lambda: True)
    monkeypatch.setattr(intake.hubspot_integration, "upsert_contact",
                         lambda email, props, approved: {"ok": True, "record": {"id": "hs-1"}, "action": "created"})
    monkeypatch.setattr(intake.agreement_signing, "create_pending_agreement", lambda *a, **k: "tok123")
    monkeypatch.setattr(intake.gmail_integration, "create_draft", lambda *a, **k: {"ok": True, "draft_id": "d1"})

    result = intake.process_candidate_email(_message())

    assert result["hubspot_ok"] is True
    assert result["hubspot_contact_id"] == "hs-1"
    assert linked["call"] == (7, {"hubspot_contact_id": "hs-1"})


def test_resume_attachment_downloaded_and_attached_to_hubspot(monkeypatch):
    attach_calls = []
    monkeypatch.setattr(intake.buildpro_data, "upsert_candidate", lambda *a, **k: (1, "created"))
    monkeypatch.setattr(intake.buildpro_data, "update_candidate", lambda *a, **k: True)
    monkeypatch.setattr(intake.hubspot_integration, "is_configured", lambda: True)
    monkeypatch.setattr(intake.hubspot_integration, "upsert_contact",
                         lambda *a, **k: {"ok": True, "record": {"id": "hs-1"}, "action": "created"})
    monkeypatch.setattr(intake.hubspot_integration, "upload_file",
                         lambda data, filename, approved: {"ok": True, "file_id": "file-1"})
    monkeypatch.setattr(intake.hubspot_integration, "attach_file_note",
                         lambda *a, **k: (attach_calls.append((a, k)), {"ok": True, "note_id": "note-1"})[1])
    monkeypatch.setattr(intake.gmail_integration, "is_likely_resume", lambda fn: fn.endswith(".pdf"))
    monkeypatch.setattr(intake.gmail_integration, "download_attachment",
                         lambda mid, aid: {"ok": True, "data": b"%PDF-fake"})
    monkeypatch.setattr(intake.agreement_signing, "create_pending_agreement", lambda *a, **k: "tok123")
    monkeypatch.setattr(intake.gmail_integration, "create_draft", lambda *a, **k: {"ok": True, "draft_id": "d1"})

    msg = _message(attachments=[{"filename": "resume.pdf", "attachment_id": "att-1", "mime_type": "application/pdf", "size": 100}])
    result = intake.process_candidate_email(msg)

    assert result["resume_found"] is True
    assert result["resume_attached_to_hubspot"] is True
    assert attach_calls[0][0][0] == "hs-1"
    assert attach_calls[0][0][1] == "file-1"


def test_non_resume_attachment_is_ignored(monkeypatch):
    monkeypatch.setattr(intake.buildpro_data, "upsert_candidate", lambda *a, **k: (1, "created"))
    monkeypatch.setattr(intake.buildpro_data, "update_candidate", lambda *a, **k: True)
    monkeypatch.setattr(intake.hubspot_integration, "is_configured", lambda: False)
    monkeypatch.setattr(intake.agreement_signing, "create_pending_agreement", lambda *a, **k: "tok123")
    monkeypatch.setattr(intake.gmail_integration, "create_draft", lambda *a, **k: {"ok": True, "draft_id": "d1"})
    download_calls = []
    monkeypatch.setattr(intake.gmail_integration, "download_attachment",
                         lambda *a, **k: download_calls.append(1))

    msg = _message(attachments=[{"filename": "logo.png", "attachment_id": "att-1", "mime_type": "image/png", "size": 10}])
    result = intake.process_candidate_email(msg)

    assert result["resume_found"] is False
    assert download_calls == []


def test_failed_resume_download_does_not_block_the_rest_of_the_chain(monkeypatch):
    monkeypatch.setattr(intake.buildpro_data, "upsert_candidate", lambda *a, **k: (1, "created"))
    monkeypatch.setattr(intake.buildpro_data, "update_candidate", lambda *a, **k: True)
    monkeypatch.setattr(intake.hubspot_integration, "is_configured", lambda: True)
    monkeypatch.setattr(intake.hubspot_integration, "upsert_contact",
                         lambda *a, **k: {"ok": True, "record": {"id": "hs-1"}, "action": "created"})
    monkeypatch.setattr(intake.gmail_integration, "download_attachment",
                         lambda *a, **k: {"ok": False, "state": "ERROR", "detail": "boom"})
    monkeypatch.setattr(intake.agreement_signing, "create_pending_agreement", lambda *a, **k: "tok123")
    monkeypatch.setattr(intake.gmail_integration, "create_draft", lambda *a, **k: {"ok": True, "draft_id": "d1"})

    msg = _message(attachments=[{"filename": "resume.pdf", "attachment_id": "att-1", "mime_type": "application/pdf", "size": 100}])
    result = intake.process_candidate_email(msg)

    assert result["ok"] is True
    assert result["resume_found"] is False
    assert result["resume_attached_to_hubspot"] is False


def test_welcome_email_defaults_to_draft_not_send(monkeypatch):
    monkeypatch.setattr(intake.buildpro_data, "upsert_candidate", lambda *a, **k: (1, "created"))
    monkeypatch.setattr(intake.buildpro_data, "update_candidate", lambda *a, **k: True)
    monkeypatch.setattr(intake.hubspot_integration, "is_configured", lambda: False)
    monkeypatch.setattr(intake.agreement_signing, "create_pending_agreement", lambda *a, **k: "tok123")
    send_calls = []
    draft_calls = []
    monkeypatch.setattr(intake.gmail_integration, "send_email", lambda *a, **k: send_calls.append(1))
    monkeypatch.setattr(intake.gmail_integration, "create_draft",
                         lambda *a, **k: (draft_calls.append(1), {"ok": True, "draft_id": "d1"})[1])

    result = intake.process_candidate_email(_message())

    assert send_calls == []
    assert len(draft_calls) == 1
    assert result["welcome_email_drafted"] is True
    assert result["welcome_email_sent"] is False


def test_auto_send_welcome_true_actually_sends(monkeypatch):
    monkeypatch.setattr(intake.buildpro_data, "upsert_candidate", lambda *a, **k: (1, "created"))
    monkeypatch.setattr(intake.buildpro_data, "update_candidate", lambda *a, **k: True)
    monkeypatch.setattr(intake.hubspot_integration, "is_configured", lambda: False)
    monkeypatch.setattr(intake.agreement_signing, "create_pending_agreement", lambda *a, **k: "tok123")
    send_calls = []
    monkeypatch.setattr(intake.gmail_integration, "send_email",
                         lambda to, subject, body, approved: (send_calls.append(approved), {"ok": True, "message_id": "sent-1"})[1])

    result = intake.process_candidate_email(_message(), auto_send_welcome=True)

    assert send_calls == [True]  # approved=True, since a human explicitly opted in via auto_send_welcome
    assert result["welcome_email_sent"] is True
    assert result["welcome_email_drafted"] is False


def test_sign_url_uses_the_configured_public_base_url(monkeypatch):
    monkeypatch.setattr(intake.buildpro_data, "upsert_candidate", lambda *a, **k: (1, "created"))
    monkeypatch.setattr(intake.buildpro_data, "update_candidate", lambda *a, **k: True)
    monkeypatch.setattr(intake.hubspot_integration, "is_configured", lambda: False)
    monkeypatch.setattr(intake.agreement_signing, "create_pending_agreement", lambda *a, **k: "tok-abc")
    monkeypatch.setattr("core.headless.config.PUBLIC_BASE_URL", "https://example-test.invalid")
    captured = {}
    monkeypatch.setattr(intake.gmail_integration, "create_draft",
                         lambda to, subject, body: (captured.setdefault("body", body), {"ok": True, "draft_id": "d1"})[1])

    result = intake.process_candidate_email(_message())

    assert result["sign_url"] == "https://example-test.invalid/agreement/tok-abc"
    assert "https://example-test.invalid/agreement/tok-abc" in captured["body"]
