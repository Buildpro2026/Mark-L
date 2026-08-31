"""Resume-intake automation chain (2026-08-19).

Lee's spec: when a candidate emails in or their resume arrives, JARVIS
should pull their info + resume, get them into HubSpot fully, make sure
they don't fall through the cracks on follow-up, and send them a welcome
packet (how the service works, why representation helps, interview tips,
and a representation agreement they can sign).

Every write here reuses infrastructure already live and tested this
session: actions/gmail_integration.py (attachments), actions/
hubspot_integration.py (contacts + file upload), actions/buildpro_data.py
(the local CRM-style candidate table — its existing "hasn't been touched
in FOLLOWUP_STALE_DAYS" staleness check already IS the "don't lose the
candidate" follow-up mechanism Lee asked for, once a candidate is in that
table with a fresh updated_ts; no separate "next follow-up date" field
was needed). Nothing here fabricates a candidate detail: only sender
name/email (from real headers) and whatever the resume file itself
carries get stored — no invented job title, years of experience, or
skills from a subject line.

The welcome email defaults to a DRAFT (gmail_integration.create_draft),
not an automatic send — auto_send_welcome=True is required to actually
send it. This isn't the intake chain being incomplete: it's deliberate,
because the representation agreement text (see actions/agreement_signing.py)
is still a placeholder pending Lee's real legal language. Once that's
replaced and reviewed, flip auto_send_welcome=True.
"""
from __future__ import annotations

import re
from typing import Any

from actions import agreement_signing
from actions import buildpro_data
from actions import gmail_integration
from actions import hubspot_integration

_SENDER_RE = re.compile(r'^\s*"?([^"<]*?)"?\s*<([^>]+)>\s*$')


def _parse_sender(sender: str) -> tuple[str, str]:
    """'Jane Doe <jane@example.com>' -> ('Jane Doe', 'jane@example.com').
    Falls back to treating the whole string as the email when there's no
    name/angle-bracket part — never invents a name that isn't there."""
    sender = (sender or "").strip()
    if not sender:
        return "", ""
    m = _SENDER_RE.match(sender)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return "", sender


def _compose_welcome_email(first_name: str, sign_url: str) -> tuple[str, str]:
    # sign_url is deliberately unused in the body below (2026-08-30, Lee's
    # spec): representation is Lee's own call after he's made contact and
    # decided whether to represent this candidate, not something the
    # auto-drafted first-contact email should presuppose. The pending
    # agreement record is still created by the caller either way — this
    # just stops the link from appearing here. Send the agreement
    # separately once Lee's made that decision.
    greeting_name = first_name or "there"
    subject = "Welcome to BuildPro Recruiters!"
    body = f"""Hi {greeting_name},

Thank you for reaching out to BuildPro Recruiters — we've received your resume and we're glad to be working with you.

HOW OUR SERVICE WORKS
BuildPro Recruiters represents YOU, at no cost to you. Our fee is paid entirely by the hiring employer once a placement is made — you will never be charged for our services.

WHY WORK WITH A RECRUITER
- We have direct relationships with hiring managers, often for roles that aren't publicly posted yet
- We advocate for you and provide context a resume alone can't
- We help you understand and negotiate the full offer, not just the base number
- We save you time by only bringing you opportunities that actually fit your background

WHAT TO EXPECT FROM US
From here, we may send you specific job descriptions that match your background, just to gauge your interest — you're never obligated to pursue anything we send, and every submission of your resume to a client happens only with your OK first. Someone from our team will be in touch personally soon.

A FEW INTERVIEW TIPS FROM OUR TEAM
- Research the company and the specific role before every interview
- Dress one level more polished than the role's day-to-day — for field/trade roles, clean business casual is usually right; for office, PM, or executive roles, business professional
- Arrive 10-15 minutes early, never late
- Bring extra printed copies of your resume
- Prepare 2-3 thoughtful questions about the role and team
- Send a short thank-you note within 24 hours of the interview

We're looking forward to representing you in your search. If you have any questions at all, just reply to this email.

BuildPro Recruiters
"""
    return subject, body


def process_candidate_email(message: dict[str, Any], auto_send_welcome: bool = False) -> dict[str, Any]:
    """Runs the full intake chain for one already-classified candidate_reply
    Gmail message. Never raises — every external call already returns an
    honest ok/error dict, and each optional step (resume upload, HubSpot)
    degrades gracefully rather than blocking the rest of the chain."""
    from core.headless.config import PUBLIC_BASE_URL

    name, email = _parse_sender(message.get("sender") or "")
    if not email:
        return {"ok": False, "detail": "Could not determine a sender email address from this message."}

    # 1. Resume attachment (best-effort — optional, never blocks the chain).
    resume_bytes: bytes | None = None
    resume_filename: str | None = None
    attachments = [a for a in (message.get("attachments") or []) if gmail_integration.is_likely_resume(a.get("filename") or "")]
    if attachments:
        att = attachments[0]
        dl = gmail_integration.download_attachment(message["id"], att["attachment_id"])
        if dl["ok"]:
            resume_bytes = dl["data"]
            resume_filename = att["filename"]

    # 2. Local BuildPro candidate table — the real "don't lose the
    # candidate" mechanism: upsert_candidate() sets/refreshes updated_ts,
    # and list_candidates_needing_followup() already surfaces anything
    # that goes FOLLOWUP_STALE_DAYS untouched.
    display_name = name or email
    candidate_id, candidate_action = buildpro_data.upsert_candidate(
        display_name, email=email, source="gmail_intake",
        notes=f"Auto-intake from email: {message.get('subject') or '(no subject)'}",
    )

    # 3. HubSpot contact — best-effort; the local record above already
    # exists regardless of whether HubSpot is configured/reachable.
    hubspot_contact_id: str | None = None
    hubspot_ok = False
    if hubspot_integration.is_configured():
        props: dict[str, str] = {}
        if name:
            parts = name.split(" ", 1)
            props["firstname"] = parts[0]
            if len(parts) > 1:
                props["lastname"] = parts[1]
        hs_result = hubspot_integration.upsert_contact(email, props, approved=True)
        hubspot_ok = bool(hs_result.get("ok"))
        if hubspot_ok:
            hubspot_contact_id = hs_result["record"]["id"]
            buildpro_data.update_candidate(candidate_id, hubspot_contact_id=hubspot_contact_id)

    # 4. Resume upload + attach to the HubSpot contact — best-effort,
    # requires both a resume file and a successful HubSpot contact above.
    resume_attached = False
    if resume_bytes and resume_filename and hubspot_contact_id:
        upload = hubspot_integration.upload_file(resume_bytes, resume_filename, approved=True)
        if upload.get("ok"):
            attach = hubspot_integration.attach_file_note(
                hubspot_contact_id, upload["file_id"],
                note_body=f"Resume received via email: {resume_filename}", approved=True,
            )
            resume_attached = bool(attach.get("ok"))

    # 5. Representation agreement link + welcome email.
    sign_token = agreement_signing.create_pending_agreement(candidate_id, display_name, email)
    sign_url = f"{PUBLIC_BASE_URL}/agreement/{sign_token}"
    subject, body = _compose_welcome_email(name, sign_url)
    if auto_send_welcome:
        send_result = gmail_integration.send_email(email, subject, body, approved=True)
    else:
        send_result = gmail_integration.create_draft(email, subject, body)

    return {
        "ok": True,
        "candidate_id": candidate_id,
        "candidate_action": candidate_action,
        "candidate_email": email,
        "candidate_name": name,
        "hubspot_contact_id": hubspot_contact_id,
        "hubspot_ok": hubspot_ok,
        "resume_found": resume_bytes is not None,
        "resume_attached_to_hubspot": resume_attached,
        "sign_token": sign_token,
        "sign_url": sign_url,
        "welcome_email_sent": auto_send_welcome and bool(send_result.get("ok")),
        "welcome_email_drafted": (not auto_send_welcome) and bool(send_result.get("ok")),
        "welcome_email_error": None if send_result.get("ok") else send_result.get("detail"),
    }
