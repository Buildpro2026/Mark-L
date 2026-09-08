"""Candidate resume upload.

There was no candidate-facing upload in this repository at all — the only
/api/upload is the desktop pairing-key file share, which a candidate
cannot authenticate against. These cover the new public endpoint, and in
particular the rule that the UI must never be told an upload succeeded
when the backend failed: ok and the HTTP status always agree.
"""
import zipfile
import io

import pytest
from fastapi.testclient import TestClient

from actions import resume_intake as ri


PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n%%EOF\n"
DOC_BYTES = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64
RTF_BYTES = rb"{\rtf1\ansi Jane Doe jane@example.com}"
TXT_BYTES = b"Jane Doe\njane@example.com\n(312) 555-0142\nSuperintendent, 12 years.\n"


def _docx_bytes(text="Jane Doe\njane@example.com"):
    """A real minimal .docx — a zip with word/document.xml, so the magic
    bytes and the structure are both genuine rather than a stub."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml",
                   "<w:document xmlns:w='x'><w:body>"
                   + "".join(f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>"
                             for line in text.splitlines())
                   + "</w:body></w:document>")
    return buf.getvalue()


# ══ CONTENT-BASED VALIDATION ═════════════════════════════════════════════

def test_a_real_pdf_is_recognised():
    assert ri.detect_kind(PDF_BYTES, "resume.pdf") == "pdf"


def test_a_real_docx_is_recognised():
    assert ri.detect_kind(_docx_bytes(), "resume.docx") == "docx"


def test_legacy_doc_and_rtf_are_recognised():
    assert ri.detect_kind(DOC_BYTES, "resume.doc") == "doc"
    assert ri.detect_kind(RTF_BYTES, "resume.rtf") == "rtf"


def test_plain_text_is_accepted_only_when_the_name_says_so():
    assert ri.detect_kind(TXT_BYTES, "resume.txt") == "txt"
    assert ri.detect_kind(TXT_BYTES, "resume") is None


def test_an_executable_renamed_to_pdf_is_refused():
    # The extension is a claim; the bytes are the evidence.
    result = ri.validate(b"MZ\x90\x00\x03" + b"\x00" * 100, "resume.pdf")
    assert result["ok"] is False
    assert result["state"] == ri.STATE_REJECTED


def test_an_empty_file_is_refused_with_a_reason():
    result = ri.validate(b"", "resume.pdf")
    assert result["ok"] is False
    assert "empty" in result["detail"].lower()


def test_an_oversized_file_is_refused_with_the_limit_named():
    huge = b"%PDF-" + b"0" * (ri.MAX_RESUME_MB * 1024 * 1024 + 10)
    result = ri.validate(huge, "resume.pdf")
    assert result["ok"] is False
    assert str(ri.MAX_RESUME_MB) in result["detail"]


def test_the_stored_name_cannot_escape_the_upload_directory():
    name = ri.safe_stored_name("../../../etc/passwd", "pdf", PDF_BYTES)
    assert "/" not in name and "\\" not in name and ".." not in name
    assert name.endswith(".pdf")


def test_the_stored_extension_matches_the_real_type_not_the_claim():
    name = ri.safe_stored_name("resume.pdf", "docx", _docx_bytes())
    assert name.endswith(".docx")


# ══ PARSING ══════════════════════════════════════════════════════════════

def test_contact_details_are_extracted_from_resume_text():
    found = ri.extract_contact(TXT_BYTES.decode())
    assert found["email"] == "jane@example.com"
    assert "555-0142" in found["phone"]
    assert found["name"] == "Jane Doe"


def test_a_missing_detail_is_absent_rather_than_invented():
    found = ri.extract_contact("Some resume with no contact details at all.")
    assert "email" not in found
    assert "phone" not in found


def test_a_docx_resume_has_its_text_read():
    docx = pytest.importorskip("docx")   # python-docx drives the real path
    text = ri.extract_text(_docx_bytes("Jane Doe\njane@example.com"), "docx")
    assert "jane@example.com" in text


# ══ THE FULL UPLOAD ══════════════════════════════════════════════════════

def test_a_text_resume_is_stored_parsed_and_recorded(tmp_path, monkeypatch):
    from actions import buildpro_data
    monkeypatch.setattr(buildpro_data, "DB_PATH", tmp_path / "bp.db")

    result = ri.process_upload(TXT_BYTES, "jane.txt", tmp_path / "resumes")
    assert result["ok"] is True
    assert result["state"] == ri.STATE_ACCEPTED
    assert result["stored"] is True
    assert result["candidate_email"] == "jane@example.com"
    assert result["candidate_id"] is not None
    assert (tmp_path / "resumes" / result["stored_name"]).exists()


def test_an_unparseable_file_is_still_stored_and_says_so(tmp_path, monkeypatch):
    from actions import buildpro_data
    monkeypatch.setattr(buildpro_data, "DB_PATH", tmp_path / "bp.db")

    # A scanned PDF is a real resume with no text layer — that is not the
    # candidate's fault and must not be rejected.
    result = ri.process_upload(PDF_BYTES, "scan.pdf", tmp_path / "resumes")
    assert result["ok"] is True
    assert result["state"] == ri.STATE_STORED_UNPARSED
    assert result["stored"] is True
    assert "review" in result["detail"].lower()


def test_a_rejected_file_is_never_written_to_disk(tmp_path):
    dest = tmp_path / "resumes"
    result = ri.process_upload(b"MZ\x90\x00", "virus.pdf", dest)
    assert result["ok"] is False
    assert result["stored"] is False
    assert not dest.exists() or list(dest.iterdir()) == []


def test_a_second_upload_updates_rather_than_duplicating(tmp_path, monkeypatch):
    from actions import buildpro_data
    monkeypatch.setattr(buildpro_data, "DB_PATH", tmp_path / "bp.db")

    first = ri.process_upload(TXT_BYTES, "jane.txt", tmp_path / "r")
    second = ri.process_upload(TXT_BYTES, "jane_v2.txt", tmp_path / "r")
    assert first["candidate_id"] == second["candidate_id"]
    assert second["candidate_action"] == "updated"


def test_a_record_failure_does_not_report_the_upload_as_failed(tmp_path, monkeypatch):
    from actions import buildpro_data
    monkeypatch.setattr(buildpro_data, "upsert_candidate",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))

    result = ri.process_upload(TXT_BYTES, "jane.txt", tmp_path / "r")
    # The bytes ARE on disk. Telling the candidate it failed makes them
    # upload it again.
    assert result["ok"] is True
    assert result["stored"] is True
    assert "record_error" in result
    assert "could not be created" in result["detail"]


def test_an_upload_with_no_email_is_stored_but_creates_no_record(tmp_path, monkeypatch):
    from actions import buildpro_data
    monkeypatch.setattr(buildpro_data, "DB_PATH", tmp_path / "bp.db")

    result = ri.process_upload(b"Just some text with no address.\n", "r.txt", tmp_path / "r")
    assert result["stored"] is True
    assert result["candidate_id"] is None
    assert "no candidate record" in result["detail"].lower()


# ══ THE HTTP ENDPOINT ════════════════════════════════════════════════════

@pytest.fixture
def client(monkeypatch, tmp_path):
    from core.headless import config, resume_routes
    from actions import buildpro_data
    monkeypatch.setattr(config, "API_TOKEN", "test-token-not-a-real-secret")
    monkeypatch.setattr(buildpro_data, "DB_PATH", tmp_path / "bp.db")
    monkeypatch.setattr(resume_routes, "uploads_dir", lambda: tmp_path / "resumes")
    from core.headless.app import create_app
    return TestClient(create_app(start_background_worker=False), base_url="https://testserver")


def test_a_candidate_can_upload_without_an_account(client):
    # No session cookie, no bearer token — a candidate has neither.
    r = client.post("/api/public/resume",
                    files={"file": ("jane.txt", TXT_BYTES, "text/plain")})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["stored"] is True


def test_a_pdf_upload_succeeds(client):
    r = client.post("/api/public/resume",
                    files={"file": ("resume.pdf", PDF_BYTES, "application/pdf")})
    assert r.status_code == 200
    assert r.json()["stored"] is True


def test_a_docx_upload_succeeds(client):
    r = client.post("/api/public/resume",
                    files={"file": ("resume.docx", _docx_bytes(),
                                    "application/vnd.openxmlformats-officedocument"
                                    ".wordprocessingml.document")})
    assert r.status_code == 200
    assert r.json()["stored"] is True


def test_an_invalid_file_returns_a_failure_status_not_a_200(client):
    r = client.post("/api/public/resume",
                    files={"file": ("bad.pdf", b"MZ\x90\x00", "application/pdf")})
    assert r.status_code == 400
    body = r.json()
    assert body["ok"] is False
    assert body["stored"] is False


def test_the_status_code_always_agrees_with_the_body(client):
    # A 200 carrying ok=false is exactly how a UI shows success for a
    # failed upload.
    for payload, expected_ok in (
        (("good.txt", TXT_BYTES), True),
        (("bad.pdf", b"MZ\x90\x00"), False),
    ):
        r = client.post("/api/public/resume", files={"file": payload + ("application/octet-stream",)})
        assert (r.status_code == 200) is expected_ok
        assert r.json()["ok"] is expected_ok


def test_an_oversized_upload_is_refused_with_413(client):
    huge = b"%PDF-" + b"0" * (ri.MAX_RESUME_MB * 1024 * 1024 + 1024)
    r = client.post("/api/public/resume",
                    files={"file": ("big.pdf", huge, "application/pdf")})
    assert r.status_code == 413
    assert r.json()["stored"] is False


def test_a_submitted_email_is_preferred_over_the_parsed_one(client):
    r = client.post("/api/public/resume",
                    files={"file": ("jane.txt", TXT_BYTES, "text/plain")},
                    data={"email": "typed@example.com", "name": "Typed Name"})
    body = r.json()
    assert body["candidate_email"] == "typed@example.com"
    assert body["candidate_name"] == "Typed Name"


def test_the_upload_endpoint_does_not_weaken_the_protected_routes(client):
    # The public route exists; the authenticated ones stay authenticated.
    assert client.get("/api/status").status_code in (401, 403)
    assert client.post("/ui/api/tts/speak", json={"text": "x"}).status_code == 401


def test_repeated_uploads_from_one_address_are_rate_limited(client, monkeypatch):
    # An open write endpoint without a ceiling can be used to fill the
    # disk with perfectly valid 10 MB PDFs.
    from core.headless import resume_routes
    monkeypatch.setattr(resume_routes, "_recent_uploads", {})
    monkeypatch.setattr(resume_routes, "_RATE_MAX_UPLOADS", 3)

    statuses = [client.post("/api/public/resume",
                            files={"file": ("r.txt", TXT_BYTES, "text/plain")}).status_code
                for _ in range(5)]
    assert statuses[:3] == [200, 200, 200]
    assert 429 in statuses[3:]


def test_a_rate_limited_upload_is_not_reported_as_stored(client, monkeypatch):
    from core.headless import resume_routes
    monkeypatch.setattr(resume_routes, "_recent_uploads", {})
    monkeypatch.setattr(resume_routes, "_RATE_MAX_UPLOADS", 1)

    client.post("/api/public/resume", files={"file": ("r.txt", TXT_BYTES, "text/plain")})
    r = client.post("/api/public/resume", files={"file": ("r.txt", TXT_BYTES, "text/plain")})
    assert r.status_code == 429
    assert r.json()["stored"] is False
