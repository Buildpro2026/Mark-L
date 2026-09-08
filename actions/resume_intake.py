"""Accepting a resume file and turning it into a real candidate record.

WHY THIS EXISTS
There was no candidate-facing upload anywhere in this repository. The
only /api/upload is dashboard/server.py's paired-device file share, which
is authenticated with the desktop pairing key — a candidate has no such
key and no JARVIS account, so no resume a candidate sends could reach the
system at all. This is the missing backend half.

THE RULE THAT SHAPES IT
The UI must never show success when the backend failed. So every step
returns a real state, and the states are distinguishable rather than a
single boolean:

    ACCEPTED          stored, parsed, candidate record written
    STORED_UNPARSED   stored, but no text could be extracted
    REJECTED          validation refused it (type, size, empty)
    FAILED            something broke; the file is not stored

Validation is by CONTENT, not by the filename. A .pdf extension proves
nothing about what is inside — the magic bytes do. Renaming an executable
to resume.pdf gets it rejected here, which is also why the extension is
never trusted for the stored name either.

Nothing here sends mail, and nothing here publishes. It writes a
candidate record through the existing store and hands back what happened.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("jarvis.resume_intake")

STATE_ACCEPTED = "ACCEPTED"
STATE_STORED_UNPARSED = "STORED_UNPARSED"
STATE_REJECTED = "REJECTED"
STATE_FAILED = "FAILED"

MAX_RESUME_MB = 10
_MAX_BYTES = MAX_RESUME_MB * 1024 * 1024

# Content signatures, because the extension is a claim and these are
# evidence. DOCX is a zip; DOC is the old OLE compound format.
_SIGNATURES: tuple[tuple[bytes, str, str], ...] = (
    (b"%PDF-", "pdf", "application/pdf"),
    (b"PK\x03\x04", "docx",
     "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "doc", "application/msword"),
    (b"{\\rtf", "rtf", "application/rtf"),
)
ACCEPTED_KINDS = tuple(kind for _, kind, _ in _SIGNATURES) + ("txt",)

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE_RE = re.compile(r"(?:\+?\d{1,2}[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}")


def detect_kind(data: bytes, filename: str = "") -> Optional[str]:
    """The real file type from its leading bytes, or None.

    Plain text has no signature, so it is accepted only when the bytes
    actually decode as UTF-8 AND the name says .txt — a nameless blob of
    decodable bytes is not evidence of a resume."""
    if not data:
        return None
    for signature, kind, _ in _SIGNATURES:
        if data.startswith(signature):
            return kind
    if Path(filename or "").suffix.lower() in (".txt", ".text"):
        try:
            data[:4096].decode("utf-8")
            return "txt"
        except UnicodeDecodeError:
            return None
    return None


def validate(data: bytes, filename: str = "") -> dict[str, Any]:
    """Whether this file may be accepted, and precisely why not if it may
    not. The reason is returned to the UI verbatim: "upload failed" with
    no cause is how a candidate gives up and emails Lee instead."""
    if not data:
        return {"ok": False, "state": STATE_REJECTED,
                "detail": "The file is empty."}
    if len(data) > _MAX_BYTES:
        return {"ok": False, "state": STATE_REJECTED,
                "detail": f"That file is larger than the {MAX_RESUME_MB} MB limit."}
    kind = detect_kind(data, filename)
    if kind is None:
        return {"ok": False, "state": STATE_REJECTED,
                "detail": ("That does not look like a resume file. Please upload a "
                           "PDF, DOCX, DOC, RTF or TXT.")}
    return {"ok": True, "kind": kind}


def safe_stored_name(filename: str, kind: str, data: bytes) -> str:
    """A storage name that cannot escape the upload directory and whose
    extension matches what the file ACTUALLY is, not what it claimed."""
    stem = Path(filename or "resume").name
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(". ")
    stem = Path(stem).stem[:80] or "resume"
    digest = hashlib.sha256(data).hexdigest()[:12]
    return f"{stem}_{digest}.{kind}"


def extract_text(data: bytes, kind: str) -> str:
    """Resume text, or '' when it cannot be read.

    '' is a real outcome, not a failure: a scanned PDF is a legitimate
    resume that simply has no extractable text layer. The file is still
    stored and the candidate still recorded — see STORED_UNPARSED — rather
    than the upload being rejected for something the candidate cannot fix.
    """
    try:
        if kind == "txt":
            return data.decode("utf-8", errors="replace")
        if kind == "pdf":
            return _extract_pdf(data)
        if kind == "docx":
            return _extract_docx(data)
    except Exception:
        logger.debug("could not extract text from a %s resume", kind, exc_info=True)
    return ""


def _extract_pdf(data: bytes) -> str:
    import io
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return "\n".join((page.extract_text() or "") for page in pdf.pages)[:200_000]
    except ImportError:
        pass
    try:
        import PyPDF2
        reader = PyPDF2.PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages)[:200_000]
    except ImportError:
        return ""


def _extract_docx(data: bytes) -> str:
    import io
    try:
        import docx
    except ImportError:
        return ""
    document = docx.Document(io.BytesIO(data))
    return "\n".join(p.text for p in document.paragraphs)[:200_000]


def extract_contact(text: str) -> dict[str, str]:
    """Email, phone and name from resume text. Every field is either found
    in the document or absent — a resume with no email produces no email,
    never a placeholder."""
    found: dict[str, str] = {}
    email = _EMAIL_RE.search(text or "")
    if email:
        found["email"] = email.group(0).lower()
    phone = _PHONE_RE.search(text or "")
    if phone:
        digits = re.sub(r"\D", "", phone.group(0))
        if 10 <= len(digits) <= 15:
            found["phone"] = phone.group(0).strip()
    # The name is conventionally the first non-empty line, but only when it
    # looks like a name rather than a heading or an address.
    for line in (text or "").splitlines():
        candidate = line.strip()
        if not candidate or len(candidate) > 60:
            continue
        if _EMAIL_RE.search(candidate) or _PHONE_RE.search(candidate):
            continue
        words = candidate.split()
        if 1 < len(words) <= 4 and all(w[:1].isupper() for w in words if w[:1].isalpha()):
            found["name"] = candidate
        break
    return found


def store_resume(data: bytes, filename: str, uploads_dir: Path) -> Path:
    uploads_dir = Path(uploads_dir)
    uploads_dir.mkdir(parents=True, exist_ok=True)
    kind = detect_kind(data, filename) or "bin"
    path = uploads_dir / safe_stored_name(filename, kind, data)
    path.write_bytes(data)
    return path


def process_upload(data: bytes, filename: str, uploads_dir: Path,
                   submitted_email: str = "", submitted_name: str = "",
                   create_record: bool = True) -> dict[str, Any]:
    """Validate, store, parse, and record one uploaded resume.

    Returns exactly what happened. A caller rendering this must show
    success only for ACCEPTED or STORED_UNPARSED — the two states where
    the file is genuinely on disk."""
    check = validate(data, filename)
    if not check["ok"]:
        return {"ok": False, "state": check["state"], "detail": check["detail"],
                "stored": False, "candidate_id": None}

    kind = check["kind"]
    try:
        path = store_resume(data, filename, uploads_dir)
    except Exception as exc:
        logger.exception("could not store an uploaded resume")
        return {"ok": False, "state": STATE_FAILED,
                "detail": f"The file could not be saved: {str(exc)[:200]}",
                "stored": False, "candidate_id": None}

    text = extract_text(data, kind)
    parsed = extract_contact(text) if text else {}
    email = (submitted_email or parsed.get("email") or "").strip().lower()
    name = (submitted_name or parsed.get("name") or "").strip()

    result = {
        "ok": True,
        "state": STATE_ACCEPTED if text else STATE_STORED_UNPARSED,
        "stored": True,
        "stored_path": str(path),
        "stored_name": path.name,
        "kind": kind,
        "size": len(data),
        "text_extracted": bool(text),
        "parsed": parsed,
        "candidate_email": email or None,
        "candidate_name": name or None,
        "candidate_id": None,
        "detail": ("Resume received." if text else
                   "Resume received. No text could be read from it — a person will review it."),
    }

    if not create_record or not email:
        if not email:
            result["detail"] += (" No email address was found in the file, so no candidate "
                                 "record was created yet.")
        return result

    try:
        from actions import buildpro_data
        # upsert_candidate dedupes by email, so a candidate who uploads a
        # revised resume updates their row rather than creating a second
        # one. Only fields add_candidate() actually accepts are passed;
        # the resume itself lives on disk and is referenced by notes.
        candidate_id, action = buildpro_data.upsert_candidate(
            name or email, email=email,
            phone=parsed.get("phone", ""),
            source="resume_upload",
            source_url=str(path),
            notes=(f"Resume uploaded {datetime.now(timezone.utc).isoformat()}: "
                   f"{path.name}"),
        )
        result["candidate_id"] = candidate_id
        result["candidate_action"] = action
    except Exception as exc:
        # The file IS stored, so this is not a failed upload — it is a
        # stored resume whose record could not be written. Saying "upload
        # failed" here would make the candidate send it again.
        logger.exception("could not create a candidate record from an upload")
        result["record_error"] = str(exc)[:200]
        result["detail"] += " The file was saved, but the candidate record could not be created."
    return result
