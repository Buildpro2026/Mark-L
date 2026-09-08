"""Public resume upload — the endpoint the BuildPro site posts to.

There was no candidate-facing upload anywhere in this repository. The only
existing /api/upload is dashboard/server.py's paired-device file share,
gated on the desktop pairing key; a candidate has no such key and no
JARVIS account, so a resume could not reach the system at all.

Unauthenticated on purpose, the same way agreement_routes.py is: a
candidate arrives from a public page with no credentials. That means the
protections have to come from validation rather than from auth, so:

  * the file is validated by CONTENT, not by its extension
  * there is a hard size ceiling
  * the stored filename is derived, never taken from the client
  * nothing here sends mail or publishes anything

The response distinguishes stored from not-stored explicitly, because the
UI must never show success when the backend failed. ok=true means the
bytes are on disk; anything else is a real failure with a real reason.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from pathlib import Path

from fastapi import APIRouter, Form, Request, UploadFile, File
from fastapi.responses import JSONResponse

from actions import resume_intake

logger = logging.getLogger("jarvis.resume_routes")

router = APIRouter()

# Read in bounded chunks so a huge upload is refused as it arrives rather
# than after it has already been buffered into memory.
_CHUNK = 64 * 1024

# An unauthenticated endpoint that writes files needs a ceiling that does
# not depend on the caller being well behaved. Without one, anyone can
# fill the disk with valid 10 MB PDFs. Per-IP and in-memory: this is a
# single-process service, and a shared store would be a new dependency
# for a control that only has to stop the obvious abuse.
_RATE_WINDOW_SECONDS = 3600
_RATE_MAX_UPLOADS = 10
_recent_uploads: dict[str, deque] = {}


def _rate_limited(client_ip: str) -> bool:
    now = time.monotonic()
    seen = _recent_uploads.setdefault(client_ip, deque())
    while seen and (now - seen[0]) > _RATE_WINDOW_SECONDS:
        seen.popleft()
    if len(seen) >= _RATE_MAX_UPLOADS:
        return True
    seen.append(now)
    # Keep the table from growing without bound on a long-running process.
    if len(_recent_uploads) > 5000:
        for ip in [k for k, v in _recent_uploads.items() if not v]:
            _recent_uploads.pop(ip, None)
    return False


def uploads_dir() -> Path:
    from core.headless import config
    return Path(getattr(config, "DATA_DIR", Path("data"))) / "resumes"


@router.post("/api/public/resume")
async def upload_resume(
    request: Request,
    file: UploadFile = File(...),
    email: str = Form(""),
    name: str = Form(""),
):
    client_ip = (request.client.host if request.client else "") or "unknown"
    if _rate_limited(client_ip):
        return JSONResponse(
            {"ok": False, "state": resume_intake.STATE_REJECTED, "stored": False,
             "detail": "Too many uploads from this address. Please try again later."},
            status_code=429)

    max_bytes = resume_intake.MAX_RESUME_MB * 1024 * 1024
    chunks, size = [], 0
    try:
        while True:
            chunk = await file.read(_CHUNK)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                return JSONResponse(
                    {"ok": False, "state": resume_intake.STATE_REJECTED, "stored": False,
                     "detail": f"That file is larger than the "
                               f"{resume_intake.MAX_RESUME_MB} MB limit."},
                    status_code=413)
            chunks.append(chunk)
    except Exception as exc:
        logger.exception("resume upload could not be read")
        return JSONResponse(
            {"ok": False, "state": resume_intake.STATE_FAILED, "stored": False,
             "detail": f"The upload could not be read: {str(exc)[:200]}"},
            status_code=400)

    result = resume_intake.process_upload(
        b"".join(chunks), file.filename or "resume",
        uploads_dir(), submitted_email=email, submitted_name=name)

    # The status code has to agree with the body. A 200 carrying
    # ok=false is exactly how a frontend ends up showing success for a
    # failed upload.
    status = 200 if result.get("ok") else (
        400 if result.get("state") == resume_intake.STATE_REJECTED else 500)
    return JSONResponse(result, status_code=status)
