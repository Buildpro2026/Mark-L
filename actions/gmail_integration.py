"""Gmail integration — read email, identify sender/recipient, classify,
prepare drafts. Sending stays behind an explicit approval gate: send_email()
refuses to call Gmail's send API unless approved=True is passed by a caller
acting on an explicit instruction (the same pattern
calendar_integration.create_event()/update_event() uses, and the same
spirit as agent_orchestrator.py's OBSERVE -> SUGGEST -> EXECUTE model —
this module doesn't wire into that orchestrator itself, but is built so a
future EXECUTE-level agent/tool can gate real sends through it).

Uses actions/google_auth.py for OAuth — one shared Google credential/token
across Gmail and Calendar, not a second auth system. Every function here
returns an honest {"ok": False, ...} result on any auth/API failure rather
than raising into caller code or fabricating a result.
"""
from __future__ import annotations

import base64
from email.mime.text import MIMEText
from typing import Any

from actions import google_auth


def _service():
    return google_auth.build_service("gmail", "v1")


def get_own_email_address() -> dict[str, Any]:
    """The authenticated account's own address (e.g. for sending Lee a
    digest to himself) — a real API call (users.getProfile), not a
    guess or a config value that could drift from the actual OAuth
    account. Honest ok=False on any auth/API failure, same as every
    other function here."""
    try:
        service = _service()
        profile = service.users().getProfile(userId="me").execute()
        return {"ok": True, "email": profile.get("emailAddress")}
    except RuntimeError as exc:
        return {"ok": False, "state": "NOT_AUTHORIZED", "detail": str(exc)}
    except Exception as exc:
        return {"ok": False, "state": "ERROR", "detail": str(exc)}


def list_messages(query: str = "", max_results: int = 10) -> dict[str, Any]:
    """Read-only: lists messages matching an optional Gmail search query
    (e.g. 'is:unread', 'from:someone@example.com'), each with full
    sender/recipient/subject/body metadata via get_message(). Never
    fabricates results — an auth/API failure returns ok=False, not fake data."""
    try:
        service = _service()
        resp = service.users().messages().list(
            userId="me", q=query or None, maxResults=max_results
        ).execute()
        messages = [get_message(m["id"]) for m in resp.get("messages", [])]
        return {"ok": True, "messages": messages, "result_size_estimate": resp.get("resultSizeEstimate", 0)}
    except RuntimeError as exc:
        return {"ok": False, "state": "NOT_AUTHORIZED", "detail": str(exc), "messages": []}
    except Exception as exc:
        return {"ok": False, "state": "ERROR", "detail": str(exc), "messages": []}


def get_message(message_id: str) -> dict[str, Any]:
    """Sender/recipient/subject/date/body for one message id."""
    service = _service()
    msg = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
    return {
        "id": message_id,
        "thread_id": msg.get("threadId"),
        "sender": headers.get("from"),
        "recipient": headers.get("to"),
        "subject": headers.get("subject"),
        "date": headers.get("date"),
        "snippet": msg.get("snippet"),
        "body": _extract_body(msg.get("payload", {})),
        "labels": msg.get("labelIds", []),
    }


def _extract_body(payload: dict[str, Any]) -> str:
    """Best-effort plain-text body extraction, walking multipart payloads.
    Returns '' when Gmail didn't include a text/plain part — never invents
    body content."""
    if payload.get("mimeType") == "text/plain" and "data" in payload.get("body", {}):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
    for part in payload.get("parts") or []:
        text = _extract_body(part)
        if text:
            return text
    return ""


# Simple, transparent keyword rules — not ML, not inferred intent. Checked
# against "subject sender snippet body" lowercased; first match wins;
# nothing matching returns "uncategorized" rather than guessing.
_CLASSIFICATION_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("candidate_reply", ("resume", "cv", "interview", "application", "re: application")),
    ("client_inquiry", ("project", "quote", "bid", "estimate", "proposal")),
    ("notification", ("no-reply", "noreply", "notification", "do-not-reply")),
]


def classify_message(message: dict[str, Any]) -> str:
    """Rule-based classification over subject/sender/body keywords. Returns
    one of the labels above, or 'uncategorized' — never a fabricated label.

    Originally checked subject+sender only. Real candidate/client emails
    routinely have a generic subject line ("Hi", "Following up", a forwarded
    thread with no keyword in it) with the actual signal — "attached is my
    resume", "requesting a quote for..." — sitting in the message body.
    get_message() already extracts body/snippet (see _extract_body above);
    this just actually uses them instead of leaving real signal unread.
    Body is capped so a long quoted thread or signature block can't drown
    out the check with irrelevant boilerplate."""
    subject = (message.get("subject") or "").lower()
    sender = (message.get("sender") or "").lower()
    snippet = (message.get("snippet") or "").lower()
    body = (message.get("body") or "")[:2000].lower()
    haystack = f"{subject} {sender} {snippet} {body}"
    for label, keywords in _CLASSIFICATION_RULES:
        if any(keyword in haystack for keyword in keywords):
            return label
    return "uncategorized"


def create_draft(to: str, subject: str, body: str) -> dict[str, Any]:
    """Creates a real Gmail draft (visible in the account's Drafts folder).
    Does NOT send anything — this is the 'prepare drafts' capability;
    send_email() below is the only function that can actually deliver mail."""
    try:
        service = _service()
        message = MIMEText(body)
        message["to"] = to
        message["subject"] = subject
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        draft = service.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
        return {"ok": True, "draft_id": draft.get("id")}
    except RuntimeError as exc:
        return {"ok": False, "state": "NOT_AUTHORIZED", "detail": str(exc)}
    except Exception as exc:
        return {"ok": False, "state": "ERROR", "detail": str(exc)}


def send_draft(draft_id: str, approved: bool = False) -> dict[str, Any]:
    """Sends an EXISTING draft (created earlier by create_draft() — see
    actions/buildpro_email_monitor.py) via Gmail's drafts().send(), rather
    than composing a new message. Same approval gate as send_email(): a
    real, irreversible send only happens with approved=True, set only by
    a caller acting on real authorization (the buildpro_email_responder
    EXECUTE agent — see agent_orchestrator.py — only reaches this after
    Lee's explicit approve_task() call, never on its own initiative)."""
    if not approved:
        return {"ok": False, "state": "NOT_APPROVED", "detail": "Sending requires explicit approval."}
    if not draft_id:
        return {"ok": False, "state": "ERROR", "detail": "No draft_id given."}
    try:
        service = _service()
        sent = service.users().drafts().send(userId="me", body={"id": draft_id}).execute()
        return {"ok": True, "message_id": sent.get("id")}
    except RuntimeError as exc:
        return {"ok": False, "state": "NOT_AUTHORIZED", "detail": str(exc)}
    except Exception as exc:
        return {"ok": False, "state": "ERROR", "detail": str(exc)}


def send_email(to: str, subject: str, body: str, approved: bool = False) -> dict[str, Any]:
    """Refuses to send unless approved=True is passed explicitly — no
    caller in this codebase sets that automatically today. Whoever wires
    this into a live workflow (a future EXECUTE-level agent task, or a
    tool call main.py only makes after an explicit user instruction) owns
    deciding when approved=True is warranted; this function's only job is
    to never send without it."""
    if not approved:
        return {"ok": False, "state": "NOT_APPROVED", "detail": "Sending requires explicit approval — use create_draft() instead until approved."}
    try:
        service = _service()
        message = MIMEText(body)
        message["to"] = to
        message["subject"] = subject
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
        return {"ok": True, "message_id": sent.get("id")}
    except RuntimeError as exc:
        return {"ok": False, "state": "NOT_AUTHORIZED", "detail": str(exc)}
    except Exception as exc:
        return {"ok": False, "state": "ERROR", "detail": str(exc)}
