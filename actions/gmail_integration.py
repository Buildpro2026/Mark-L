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
import html as _html
import re
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


_SENDER_ADDR_RE = re.compile(r'<([^>]+)>')
_DOMAIN_RE = re.compile(r'@([\w.-]+)')


def _sender_address(sender: str) -> str:
    """'Jane Doe <jane@example.com>' -> 'jane@example.com'; a bare address
    passes through unchanged."""
    sender = (sender or "").strip()
    m = _SENDER_ADDR_RE.search(sender)
    return (m.group(1) if m else sender).strip().lower()


def _sender_domain(sender: str) -> str:
    """The domain half of the sender address, lowercased ('' if there's no
    '@' to find one from) — used for JARVIS's-own-infra detection and for
    company/domain provenance, never invented when absent."""
    addr = _sender_address(sender)
    m = _DOMAIN_RE.search(addr)
    return m.group(1) if m else ""


def build_message_url(thread_id: str | None, message_id: str | None = None) -> str:
    """A real, clickable Gmail deep link for one message — the exact
    permalink format Gmail itself uses (https://mail.google.com/mail/u/0/#all/<id>),
    keyed on thread_id when available (Gmail's own URL scheme is
    thread-keyed; message_id is a safe fallback for the rare case a
    thread_id wasn't captured). '#all' rather than '#inbox' so the link
    still resolves for a message that's been archived/labeled elsewhere.
    Returns '' — never a fabricated URL — when neither id is available."""
    target = thread_id or message_id
    if not target:
        return ""
    return f"https://mail.google.com/mail/u/0/#all/{target}"


def get_message(message_id: str) -> dict[str, Any]:
    """Sender/recipient/subject/date/body/attachments for one message id.

    2026-09-03 (Lee's autonomous-CEO spec): added sender_domain (own-infra
    detection + provenance), to/cc (recipients — 'recipient' is kept as an
    alias of 'to' so existing callers of that key don't break), and
    permalink (a real Gmail deep link, never fabricated — '' when there's
    no thread/message id to build one from, which practically never
    happens for a message Gmail itself returned)."""
    service = _service()
    msg = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
    thread_id = msg.get("threadId")
    sender = headers.get("from")
    return {
        "id": message_id,
        "thread_id": thread_id,
        "sender": sender,
        "sender_domain": _sender_domain(sender or ""),
        "recipient": headers.get("to"),
        "to": headers.get("to"),
        "cc": headers.get("cc"),
        "subject": headers.get("subject"),
        "date": headers.get("date"),
        "snippet": msg.get("snippet"),
        "body": _extract_body(msg.get("payload", {})),
        "attachments": _extract_attachments(msg.get("payload", {})),
        "labels": msg.get("labelIds", []),
        "permalink": build_message_url(thread_id, message_id),
    }


def _html_to_text(raw_html: str) -> str:
    """Minimal, dependency-free HTML->text: drops script/style blocks,
    turns <br>/<p>/block tags into line breaks so paragraphs don't run
    together, strips remaining tags, and unescapes entities. Not a
    real renderer — good enough to make an HTML-only email's actual
    content (not just its subject) reach the classifier and JARVIS,
    which is the point; not used for anything that needs to preserve
    exact formatting."""
    if not raw_html:
        return ""
    text = re.sub(r'(?is)<(script|style)[^>]*>.*?</\1>', ' ', raw_html)
    text = re.sub(r'(?i)<(br|/p|/div|/tr|/li|/h[1-6])\s*/?>', '\n', text)
    text = re.sub(r'(?s)<[^>]+>', '', text)
    text = _html.unescape(text)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n\s*\n+', '\n\n', text)
    return text.strip()


def _extract_body(payload: dict[str, Any]) -> str:
    """Body extraction, walking multipart payloads. Prefers a real
    text/plain part; when Gmail only gave an HTML body (common for
    marketing/notification mail, and not rare for real candidate/client
    replies sent from a rich-text mail client), falls back to that HTML
    converted to plain text via _html_to_text() rather than returning ''.

    2026-09-03 fix (Lee's autonomous-CEO spec, Section 3): before this,
    an HTML-only message silently produced an empty body — JARVIS could
    see the subject/snippet but never the actual content, which is
    exactly the 'can see subject, not body' gap the spec called out.
    Still returns '' — never invents content — when a message truly has
    neither a text/plain nor a text/html part (e.g. an attachment-only
    or calendar-invite-only message)."""
    html_fallback = ""

    def _walk(node: dict[str, Any]) -> str:
        nonlocal html_fallback
        mime = node.get("mimeType")
        data = node.get("body", {}).get("data")
        if mime == "text/plain" and data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        if mime == "text/html" and data and not html_fallback:
            html_fallback = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        for part in node.get("parts") or []:
            text = _walk(part)
            if text:
                return text
        return ""

    plain = _walk(payload)
    if plain:
        return plain
    if html_fallback:
        return _html_to_text(html_fallback)
    return ""


def _extract_attachments(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Metadata only (filename/mime type/size/attachment id) — walks the
    same multipart tree _extract_body does. A part counts as an attachment
    when Gmail gave it both a filename and a body.attachmentId; inline
    text/html alternative parts have neither and are correctly skipped.
    The actual bytes are a separate, larger call — see download_attachment()
    — so listing attachments to decide "is there a resume here" stays
    cheap even for a big inbox scan."""
    found: list[dict[str, Any]] = []
    filename = payload.get("filename")
    attachment_id = payload.get("body", {}).get("attachmentId")
    if filename and attachment_id:
        found.append({
            "filename": filename,
            "mime_type": payload.get("mimeType"),
            "size": payload.get("body", {}).get("size"),
            "attachment_id": attachment_id,
        })
    for part in payload.get("parts") or []:
        found.extend(_extract_attachments(part))
    return found


# File extensions that are plausibly a resume/CV — used to filter
# list_attachments() down to what a candidate-intake flow actually cares
# about, not every inline image/logo a message happens to carry.
_RESUME_EXTENSIONS = (".pdf", ".doc", ".docx", ".rtf", ".odt")


def is_likely_resume(filename: str) -> bool:
    return filename.lower().endswith(_RESUME_EXTENSIONS)


def download_attachment(message_id: str, attachment_id: str) -> dict[str, Any]:
    """Real bytes for one attachment, base64-decoded. Honest ok=False on
    any auth/API failure, same convention as every other function here —
    never returns fabricated/placeholder file content."""
    try:
        service = _service()
        att = service.users().messages().attachments().get(
            userId="me", messageId=message_id, id=attachment_id
        ).execute()
        data = base64.urlsafe_b64decode(att.get("data", ""))
        return {"ok": True, "data": data, "size": att.get("size")}
    except RuntimeError as exc:
        return {"ok": False, "state": "NOT_AUTHORIZED", "detail": str(exc)}
    except Exception as exc:
        return {"ok": False, "state": "ERROR", "detail": str(exc)}


# Simple, transparent keyword rules — not ML, not inferred intent. Checked
# against "subject sender snippet body" lowercased; first match wins;
# nothing matching returns "uncategorized" rather than guessing.
# Found live 2026-08-19, running the candidate-intake chain against a
# real inbox: bare "cv" and "application" false-matched a Skool newsletter
# (a tracking-URL substring happened to contain "cv") and a Render.com
# welcome email ("cloud application platform"). Once body text got scanned
# too (see classify_message's own history), a whole email's worth of copy
# is exposed to a 2-letter/common-word match, not just a short subject
# line. Specific phrases fix both: they still catch the real case (Bryan
# Brady's actual subject was "...Resume Attached") while excluding
# "application" used as an unrelated noun and "cv" as an accidental
# substring — a random tracking string essentially never contains "my cv"
# or "cv attached" as connected words.
_CLASSIFICATION_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("candidate_reply", (
        "my resume", "resume attached", "attached resume", "attached my resume", "attached is my resume",
        "my cv", "cv attached", "attached cv", "attached my cv",
        "job application", "re: application", "interview",
    )),
    ("client_inquiry", ("project", "quote", "bid", "estimate", "proposal")),
    ("notification", ("no-reply", "noreply", "notification", "do-not-reply")),
]


def classify_message(message: dict[str, Any]) -> str:
    """Rule-based classification over subject/sender/body keywords, plus a
    real-resume-attachment check. Returns one of the labels above, or
    'uncategorized' — never a fabricated label.

    Originally checked subject+sender only. Real candidate/client emails
    routinely have a generic subject line ("Hi", "Following up", a forwarded
    thread with no keyword in it) with the actual signal — "attached is my
    resume", "requesting a quote for..." — sitting in the message body.
    get_message() already extracts body/snippet (see _extract_body above);
    this just actually uses them instead of leaving real signal unread.
    Body is capped so a long quoted thread or signature block can't drown
    out the check with irrelevant boilerplate.

    2026-09-02 reliability audit finding: even with body text scanned, the
    haystack never included attachment filenames. A candidate who writes
    "Hi, please see attached." and attaches resume.pdf matched none of the
    keyword phrases above and fell through to 'uncategorized' — silently
    dropped despite carrying the single strongest, most specific signal
    there is (a real resume file). get_message() already returns attachment
    metadata (see _extract_attachments) and is_likely_resume() already
    exists to filter it; this was a wiring gap, not a missing capability.
    Automated/no-reply senders are excluded so a PDF receipt or platform
    notification never gets mistaken for a candidate just because it has a
    PDF attached."""
    subject = (message.get("subject") or "").lower()
    sender = (message.get("sender") or "").lower()
    snippet = (message.get("snippet") or "").lower()
    body = (message.get("body") or "")[:2000].lower()
    haystack = f"{subject} {sender} {snippet} {body}"

    is_automated_sender = any(k in sender for k in ("no-reply", "noreply", "do-not-reply"))
    if not is_automated_sender:
        attachments = message.get("attachments") or []
        if any(is_likely_resume(a.get("filename") or "") for a in attachments):
            return "candidate_reply"

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
