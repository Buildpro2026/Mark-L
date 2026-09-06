"""2026-09-02 production reliability audit — the 20-scenario deterministic
suite Lee asked for, covering the BuildPro email -> candidate -> HubSpot
pipeline (Problem 2) and the /3d approval banner (Problem 1) end to end.

Every external boundary (Gmail, HubSpot, Twilio) is mocked; nothing here
makes a real network call, sends a real SMS, or touches a real mailbox.
Where the codebase genuinely has no equivalent to something Lee described
(there is no resume *parsing*/text-extraction step — see scenario 7/10
below), the test says so explicitly rather than inventing one.

Numbering matches Lee's own list:
  1  candidate email with PDF resume
  2  candidate email with DOCX resume
  3  candidate email with candidate information in the body
  4  candidate email with attachment but minimal body
  5  non-candidate email
  6  duplicate candidate
  7  malformed resume
  8  Gmail authentication failure
  9  attachment download failure
  10 resume parsing failure
  11 HubSpot authentication failure
  12 HubSpot create failure
  13 HubSpot update path
  14 notification success
  15 notification failure
  16 complete successful candidate pipeline
  17 no false "0 relevant" when candidate detection actually failed
  18 stale approval banner with zero pending approvals
  19 real pending approval appearing
  20 approval being completed and banner disappearing
"""
from actions import agent_orchestrator as ao
from actions import candidate_intake as intake
from actions import gmail_integration as gmail


def _msg(sender="Jane Doe <jane@example.com>", subject="Application - Electrician",
         body="", snippet="", attachments=None, message_id="m1"):
    return {
        "id": message_id, "sender": sender, "subject": subject,
        "body": body, "snippet": snippet, "attachments": attachments or [],
    }


def _mock_happy_intake_chain(monkeypatch, hubspot_configured=True, hubspot_action="created"):
    """Shared plumbing for the candidate_intake module-level scenarios —
    mirrors tests/test_candidate_intake.py's own mocking conventions."""
    monkeypatch.setattr(intake.buildpro_data, "upsert_candidate", lambda *a, **k: (1, "created"))
    monkeypatch.setattr(intake.buildpro_data, "update_candidate", lambda *a, **k: True)
    monkeypatch.setattr(intake.hubspot_integration, "is_configured", lambda: hubspot_configured)
    if hubspot_configured:
        monkeypatch.setattr(intake.hubspot_integration, "upsert_contact",
                             lambda *a, **k: {"ok": True, "record": {"id": "hs-1"}, "action": hubspot_action})
    monkeypatch.setattr(intake.agreement_signing, "create_pending_agreement", lambda *a, **k: "tok123")
    monkeypatch.setattr(intake.gmail_integration, "create_draft", lambda *a, **k: {"ok": True, "draft_id": "d1"})


# ── 1-2: real resume file types are recognized and attached ────────────────

def test_01_candidate_email_with_pdf_resume(monkeypatch):
    _mock_happy_intake_chain(monkeypatch)
    monkeypatch.setattr(gmail, "download_attachment", lambda mid, aid: {"ok": True, "data": b"%PDF-1.4 fake"})
    monkeypatch.setattr(intake.hubspot_integration, "upload_file",
                         lambda data, filename, approved: {"ok": True, "file_id": "file-1"})
    monkeypatch.setattr(intake.hubspot_integration, "attach_file_note", lambda *a, **k: {"ok": True, "note_id": "n1"})

    msg = _msg(attachments=[{"filename": "jane_doe_resume.pdf", "attachment_id": "att-1",
                              "mime_type": "application/pdf", "size": 5000}])
    result = intake.process_candidate_email(msg)

    assert result["ok"] is True
    assert result["resume_found"] is True
    assert result["resume_attached_to_hubspot"] is True


def test_02_candidate_email_with_docx_resume(monkeypatch):
    _mock_happy_intake_chain(monkeypatch)
    monkeypatch.setattr(gmail, "download_attachment", lambda mid, aid: {"ok": True, "data": b"PK\x03\x04 fake docx"})
    monkeypatch.setattr(intake.hubspot_integration, "upload_file",
                         lambda data, filename, approved: {"ok": True, "file_id": "file-2"})
    monkeypatch.setattr(intake.hubspot_integration, "attach_file_note", lambda *a, **k: {"ok": True, "note_id": "n2"})

    msg = _msg(attachments=[{"filename": "jane_doe_resume.docx", "attachment_id": "att-2",
                              "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                              "size": 8000}])
    result = intake.process_candidate_email(msg)

    assert result["ok"] is True
    assert result["resume_found"] is True
    assert result["resume_attached_to_hubspot"] is True


# ── 3-5: classification — the root cause of "0 relevant" for real resumes ──

def test_03_candidate_information_in_body_with_generic_subject_classifies_correctly():
    msg = _msg(subject="Hi there", body="Hi, I saw your posting and I'm attaching my resume for the electrician role.")
    assert gmail.classify_message(msg) == "candidate_reply"


def test_04_attachment_with_minimal_body_still_classifies_as_candidate(monkeypatch):
    # THE root-cause scenario: before this audit's fix, classify_message()'s
    # haystack never included attachment filenames, so an email with a real
    # resume attached but no matching body keyword fell through to
    # "uncategorized" and was silently dropped — this is almost certainly
    # why real candidate resumes were counted as "0 relevant" in production.
    msg = _msg(subject="Hi", body="Hi, please see attached.",
               attachments=[{"filename": "resume.pdf", "attachment_id": "a1"}])
    assert gmail.classify_message(msg) == "candidate_reply"


def test_04b_pdf_attachment_from_an_automated_sender_is_not_mistaken_for_a_candidate():
    # Guards the false-positive side of the same fix: a no-reply/automated
    # sender with a PDF attached (an invoice, a platform notification) must
    # not be misclassified just because it has a PDF.
    msg = _msg(sender="no-reply@vendor.com", subject="Your invoice", body="Your invoice is attached.",
               attachments=[{"filename": "invoice.pdf", "attachment_id": "a1"}])
    assert gmail.classify_message(msg) != "candidate_reply"


def test_05_non_candidate_email_is_never_processed(monkeypatch):
    google_auth_calls = []
    import actions.google_auth as google_auth
    monkeypatch.setattr(google_auth, "get_credential_status", lambda: {"authorized": True})
    monkeypatch.setattr(gmail, "list_messages", lambda query, max_results: {
        "ok": True, "messages": [_msg(message_id="m1", subject="Your weekly digest", sender="no-reply@service.com")],
    })
    processed_calls = []
    monkeypatch.setattr(intake, "process_candidate_email", lambda *a, **k: processed_calls.append(1))

    orch = ao.AgentOrchestrator()
    task = orch.assign_task("buildpro_candidate_intake", "check for new candidates")

    assert task.result["processed"] == []
    assert processed_calls == []


# ── 6: duplicate candidate — dedup by email, never a false rejection ───────

def test_06_duplicate_candidate_updates_the_same_local_record_not_a_second_one(monkeypatch, tmp_path):
    from actions import buildpro_data as bd
    monkeypatch.setattr(bd, "DB_PATH", tmp_path / "test_dup_candidate.db")
    monkeypatch.setattr(intake, "buildpro_data", bd)
    monkeypatch.setattr(intake.hubspot_integration, "is_configured", lambda: False)
    monkeypatch.setattr(intake.agreement_signing, "create_pending_agreement", lambda *a, **k: "tok123")
    monkeypatch.setattr(intake.gmail_integration, "create_draft", lambda *a, **k: {"ok": True, "draft_id": "d1"})

    first = intake.process_candidate_email(_msg(message_id="m1"))
    second = intake.process_candidate_email(_msg(message_id="m2"))  # same sender email, different message

    assert first["candidate_action"] == "created"
    assert second["candidate_action"] == "updated"
    assert second["candidate_id"] == first["candidate_id"]
    assert len(bd.list_candidates()) == 1


# ── 7 / 10: no resume-parsing step exists — documented, not invented ───────

def test_07_and_10_malformed_resume_bytes_still_upload_without_crashing(monkeypatch):
    # Honest architectural note for this audit: candidate_intake.py never
    # parses resume *content* (no PDF/DOCX text extraction) — by its own
    # docstring, it deliberately stores only sender name/email and the raw
    # file itself, specifically to avoid fabricating candidate details from
    # parsed content. So "malformed resume" / "resume parsing failure" have
    # no code path to fail: a corrupt/garbage file still downloads and
    # uploads fine, because nothing ever tries to read it as a document.
    _mock_happy_intake_chain(monkeypatch)
    monkeypatch.setattr(gmail, "download_attachment", lambda mid, aid: {"ok": True, "data": b"\x00\x01not a real pdf"})
    monkeypatch.setattr(intake.hubspot_integration, "upload_file",
                         lambda data, filename, approved: {"ok": True, "file_id": "file-3"})
    monkeypatch.setattr(intake.hubspot_integration, "attach_file_note", lambda *a, **k: {"ok": True, "note_id": "n3"})

    msg = _msg(attachments=[{"filename": "resume.pdf", "attachment_id": "att-1", "size": 20}])
    result = intake.process_candidate_email(msg)

    assert result["ok"] is True
    assert result["resume_attached_to_hubspot"] is True


# ── 8-9: honest failure reporting for the real failure points ──────────────

def test_08_gmail_authentication_failure_is_reported_not_silently_zeroed(monkeypatch):
    import actions.google_auth as google_auth
    monkeypatch.setattr(google_auth, "get_credential_status", lambda: {"authorized": False})

    orch = ao.AgentOrchestrator()
    task = orch.assign_task("buildpro_candidate_intake", "check for new candidates")

    assert task.result["configured"] is False
    assert "authoriz" in task.result["summary"].lower()


def test_09_attachment_download_failure_does_not_block_the_rest_of_the_chain(monkeypatch):
    _mock_happy_intake_chain(monkeypatch)
    monkeypatch.setattr(gmail, "download_attachment",
                         lambda mid, aid: {"ok": False, "state": "ERROR", "detail": "attachment fetch failed"})

    msg = _msg(attachments=[{"filename": "resume.pdf", "attachment_id": "att-1", "size": 100}])
    result = intake.process_candidate_email(msg)

    assert result["ok"] is True  # the candidate record itself still gets created
    assert result["resume_found"] is False
    assert result["resume_attached_to_hubspot"] is False


# ── 11-13: HubSpot auth/create/update paths, each honestly reported ────────

def test_11_hubspot_authentication_failure_is_surfaced_with_a_reason(monkeypatch):
    monkeypatch.setattr(intake.buildpro_data, "upsert_candidate", lambda *a, **k: (1, "created"))
    monkeypatch.setattr(intake.buildpro_data, "update_candidate", lambda *a, **k: True)
    monkeypatch.setattr(intake.hubspot_integration, "is_configured", lambda: True)
    monkeypatch.setattr(intake.hubspot_integration, "upsert_contact",
                         lambda *a, **k: {"ok": False, "state": "ERROR", "status_code": 401, "detail": "Invalid API key"})
    monkeypatch.setattr(intake.agreement_signing, "create_pending_agreement", lambda *a, **k: "tok123")
    monkeypatch.setattr(intake.gmail_integration, "create_draft", lambda *a, **k: {"ok": True, "draft_id": "d1"})

    result = intake.process_candidate_email(_msg())

    assert result["ok"] is True          # local record still created — never a total failure
    assert result["hubspot_configured"] is True
    assert result["hubspot_ok"] is False
    assert result["hubspot_error"] == "Invalid API key"  # a real reason, not silence


def test_12_hubspot_create_failure_is_distinguished_from_not_configured(monkeypatch):
    monkeypatch.setattr(intake.buildpro_data, "upsert_candidate", lambda *a, **k: (1, "created"))
    monkeypatch.setattr(intake.buildpro_data, "update_candidate", lambda *a, **k: True)
    monkeypatch.setattr(intake.hubspot_integration, "is_configured", lambda: True)
    monkeypatch.setattr(intake.hubspot_integration, "upsert_contact",
                         lambda *a, **k: {"ok": False, "state": "ERROR", "status_code": 500, "detail": "internal error"})
    monkeypatch.setattr(intake.agreement_signing, "create_pending_agreement", lambda *a, **k: "tok123")
    monkeypatch.setattr(intake.gmail_integration, "create_draft", lambda *a, **k: {"ok": True, "draft_id": "d1"})

    result = intake.process_candidate_email(_msg())

    assert result["hubspot_configured"] is True
    assert result["hubspot_ok"] is False
    assert "internal error" in result["hubspot_error"]


def test_13_hubspot_update_path_for_a_returning_candidate(monkeypatch):
    _mock_happy_intake_chain(monkeypatch, hubspot_configured=True, hubspot_action="updated")
    result = intake.process_candidate_email(_msg())
    assert result["hubspot_ok"] is True
    assert result["hubspot_contact_id"] == "hs-1"


# ── 14-15: notification path — real channel, honest success/failure ────────

def test_14_notification_success_is_reported_true(monkeypatch):
    import actions.google_auth as google_auth
    from core.headless import config as headless_config
    from actions import twilio_integration

    monkeypatch.setattr(google_auth, "get_credential_status", lambda: {"authorized": True})
    monkeypatch.setattr(headless_config, "JARVIS_OWNER_PHONE", "+13125550100", raising=False)
    monkeypatch.setattr(twilio_integration, "is_configured", lambda: True)
    sent = []
    monkeypatch.setattr(twilio_integration, "send_sms",
                         lambda to, body: (sent.append((to, body)), {"ok": True, "sid": "SM1"})[1])
    monkeypatch.setattr(gmail, "list_messages", lambda query, max_results: {
        "ok": True, "messages": [_msg(message_id="m1")],
    })
    monkeypatch.setattr(gmail, "classify_message", lambda m: "candidate_reply")
    monkeypatch.setattr(intake, "process_candidate_email", lambda msg, auto_send_welcome: {
        "ok": True, "candidate_name": "Jane Doe", "candidate_email": "jane@example.com",
        "hubspot_configured": True, "hubspot_ok": True, "resume_found": True,
        "resume_attached_to_hubspot": True, "welcome_email_drafted": True,
    })

    orch = ao.AgentOrchestrator()
    task = orch.assign_task("buildpro_candidate_intake", "check for new candidates")

    assert task.result["notification_sent"] is True
    assert task.result["notification_error"] is None
    assert len(sent) == 1
    assert sent[0][0] == "+13125550100"
    assert "Jane Doe" in sent[0][1]


def test_15_notification_failure_is_reported_not_swallowed(monkeypatch):
    import actions.google_auth as google_auth
    from core.headless import config as headless_config
    from actions import twilio_integration

    monkeypatch.setattr(google_auth, "get_credential_status", lambda: {"authorized": True})
    monkeypatch.setattr(headless_config, "JARVIS_OWNER_PHONE", "+13125550100", raising=False)
    monkeypatch.setattr(twilio_integration, "is_configured", lambda: True)
    monkeypatch.setattr(twilio_integration, "send_sms",
                         lambda to, body: {"ok": False, "state": "ERROR", "detail": "Twilio rejected the request"})
    monkeypatch.setattr(gmail, "list_messages", lambda query, max_results: {
        "ok": True, "messages": [_msg(message_id="m1")],
    })
    monkeypatch.setattr(gmail, "classify_message", lambda m: "candidate_reply")
    monkeypatch.setattr(intake, "process_candidate_email", lambda msg, auto_send_welcome: {
        "ok": True, "candidate_name": "Jane Doe", "candidate_email": "jane@example.com",
        "hubspot_configured": False, "hubspot_ok": False, "resume_found": False,
        "resume_attached_to_hubspot": False, "welcome_email_drafted": True,
    })

    orch = ao.AgentOrchestrator()
    task = orch.assign_task("buildpro_candidate_intake", "check for new candidates")

    # The candidate itself was still processed successfully — a
    # notification failure must never be reported as a pipeline failure.
    assert len(task.result["processed"]) == 1
    assert task.result["notification_sent"] is False
    assert task.result["notification_error"] == "Twilio rejected the request"


# ── 16: the complete, real, successful pipeline end to end ─────────────────

def test_16_complete_successful_candidate_pipeline_end_to_end(monkeypatch, tmp_path):
    import actions.google_auth as google_auth
    from actions import buildpro_data as bd
    from core.headless import config as headless_config
    from actions import twilio_integration

    monkeypatch.setattr(bd, "DB_PATH", tmp_path / "test_full_pipeline.db")
    monkeypatch.setattr(intake, "buildpro_data", bd)
    monkeypatch.setattr(google_auth, "get_credential_status", lambda: {"authorized": True})
    monkeypatch.setattr(headless_config, "JARVIS_OWNER_PHONE", "+13125550100", raising=False)
    monkeypatch.setattr(twilio_integration, "is_configured", lambda: True)
    sms_sent = []
    monkeypatch.setattr(twilio_integration, "send_sms",
                         lambda to, body: (sms_sent.append(body), {"ok": True, "sid": "SM1"})[1])

    real_msg = _msg(
        sender="Jane Doe <jane@example.com>", subject="Hi there",
        body="Hi, I saw your posting and I'm attaching my resume for the electrician role.",
        attachments=[{"filename": "jane_doe_resume.pdf", "attachment_id": "att-1", "size": 4000}],
        message_id="m-full",
    )
    monkeypatch.setattr(gmail, "list_messages", lambda query, max_results: {"ok": True, "messages": [real_msg]})
    # classify_message itself is real/unmocked here — proves the attachment
    # + body-text classification fix actually drives the real chain.
    monkeypatch.setattr(gmail, "download_attachment", lambda mid, aid: {"ok": True, "data": b"%PDF-1.4 real-ish"})
    monkeypatch.setattr(intake.hubspot_integration, "is_configured", lambda: True)
    monkeypatch.setattr(intake.hubspot_integration, "upsert_contact",
                         lambda *a, **k: {"ok": True, "record": {"id": "hs-99"}, "action": "created"})
    monkeypatch.setattr(intake.hubspot_integration, "upload_file",
                         lambda data, filename, approved: {"ok": True, "file_id": "file-99"})
    monkeypatch.setattr(intake.hubspot_integration, "attach_file_note", lambda *a, **k: {"ok": True, "note_id": "n99"})
    monkeypatch.setattr(intake.agreement_signing, "create_pending_agreement", lambda *a, **k: "tok-99")
    monkeypatch.setattr(intake.gmail_integration, "create_draft", lambda *a, **k: {"ok": True, "draft_id": "d99"})

    orch = ao.AgentOrchestrator()
    task = orch.assign_task("buildpro_candidate_intake", "check for new candidates")

    assert task.status == ao.TaskStatus.DONE
    assert len(task.result["processed"]) == 1
    p = task.result["processed"][0]
    assert p["candidate_email"] == "jane@example.com"
    assert p["hubspot_ok"] is True
    assert p["resume_attached_to_hubspot"] is True
    assert p["welcome_email_drafted"] is True
    assert task.result["notification_sent"] is True
    assert len(bd.list_candidates()) == 1
    assert "Jane Doe" in sms_sent[0]


# ── 17: an operational failure must never look like "0 relevant" ──────────

def test_17_gmail_scan_error_is_never_reported_as_zero_relevant(monkeypatch):
    import actions.google_auth as google_auth
    monkeypatch.setattr(google_auth, "get_credential_status", lambda: {"authorized": True})
    monkeypatch.setattr(gmail, "list_messages", lambda query, max_results: {
        "ok": False, "state": "NOT_AUTHORIZED", "detail": "token expired",
    })

    orch = ao.AgentOrchestrator()
    task = orch.assign_task("buildpro_candidate_intake", "check for new candidates")

    assert task.result["error"] == "token expired"
    assert "0 relevant" not in task.result["summary"]
    assert "relevant" not in task.result["summary"]  # this handler's error path never uses that word at all


def test_17b_run_task_logs_a_distinguishable_event_for_a_handler_reported_error():
    # 2026-09-02 fix: run_task() used to log the exact same generic
    # "completed task" event whether a handler succeeded or reported an
    # internal error — a real Gmail-auth failure was invisible in the
    # activity trail unless something read task.result directly.
    agent = ao.AgentDefinition(
        id="failing_agent", name="Failing Agent", description="x", nucleus_id="system",
        permission_level=ao.PermissionLevel.SUGGEST,
        handler=lambda task: {"summary": "Gmail scan failed", "error": "token expired"},
    )
    orch = ao.AgentOrchestrator(agents={"failing_agent": agent})
    task = orch.assign_task("failing_agent", "scan")

    events = orch.list_events("failing_agent")
    assert events[-1].kind == "task_error"
    assert "Gmail scan failed" in events[-1].message
    assert orch.get_agent("failing_agent").last_error == "token expired"


# ── 18-20: the /3d approval banner — Problem 1's acceptance criterion ──────

def test_18_approvals_endpoint_returns_empty_when_nothing_pending(monkeypatch):
    from fastapi.testclient import TestClient
    from dashboard.server import DashboardServer
    from actions import agent_orchestrator as ao_module

    monkeypatch.setattr(ao_module, "orchestrator", ao_module.AgentOrchestrator())
    server = DashboardServer()
    client = TestClient(server.app, headers={"Authorization": "Bearer test-dashboard-token-not-a-real-secret"})

    response = client.get("/3d/api/approvals")

    assert response.status_code == 200
    assert response.json()["approvals"] == []


def test_19_approvals_endpoint_shows_a_real_pending_approval(monkeypatch):
    from fastapi.testclient import TestClient
    from dashboard.server import DashboardServer
    from actions import agent_orchestrator as ao_module

    fresh = ao_module.AgentOrchestrator(agents={"test_execute_agent": ao_module.AgentDefinition(
        id="test_execute_agent", name="Test Execute Agent", description="x",
        nucleus_id="system", permission_level=ao_module.PermissionLevel.EXECUTE,
        handler=lambda task: {"ok": True},
    )})
    task = fresh.assign_task("test_execute_agent", "Send a real email")
    monkeypatch.setattr(ao_module, "orchestrator", fresh)
    server = DashboardServer()
    client = TestClient(server.app, headers={"Authorization": "Bearer test-dashboard-token-not-a-real-secret"})

    response = client.get("/3d/api/approvals")
    data = response.json()

    assert len(data["approvals"]) == 1
    assert data["approvals"][0]["id"] == task.id
    assert data["approvals"][0]["agent_name"] == "Test Execute Agent"


def test_20_approving_a_task_removes_it_from_the_approvals_list(monkeypatch):
    from fastapi.testclient import TestClient
    from dashboard.server import DashboardServer
    from actions import agent_orchestrator as ao_module

    fresh = ao_module.AgentOrchestrator(agents={"test_execute_agent": ao_module.AgentDefinition(
        id="test_execute_agent", name="Test Execute Agent", description="x",
        nucleus_id="system", permission_level=ao_module.PermissionLevel.EXECUTE,
        handler=lambda task: {"ok": True},
    )})
    task = fresh.assign_task("test_execute_agent", "Send a real email")
    monkeypatch.setattr(ao_module, "orchestrator", fresh)
    server = DashboardServer()
    client = TestClient(server.app, headers={"Authorization": "Bearer test-dashboard-token-not-a-real-secret"})

    assert len(client.get("/3d/api/approvals").json()["approvals"]) == 1

    fresh.approve_task(task.id)

    assert client.get("/3d/api/approvals").json()["approvals"] == []
