"""BuildPro Email Monitor (J3 Part 7) — the capability the J1/J2 audits
both flagged as the next real gap: agent_orchestrator.py's
buildpro_email_monitor agent used to unconditionally report
"no live email integration configured yet" even after Gmail became a
real, wired, OAuth-authorized capability (J2). It wasn't wired to
gmail_integration.py at all. This module is that wiring.

Read-only by default: scan_inbox() lists and classifies messages and logs
findings to business intelligence — no draft, no send, unless the caller
explicitly opts in. Classification reuses gmail_integration.classify_message()
(rule-based, already real and tested) rather than duplicating that logic
or reaching for an LLM call this module doesn't need.

Drafting (draft_replies=True) is real and safe — Gmail drafts are never
sent — but is NOT the default for a scheduled/background run, matching
the instruction to test with safe/read-only operations first. Sending is
never attempted here at all; gmail_integration.send_email() still refuses
without approved=True, and nothing in this module ever passes it.
"""
from __future__ import annotations

from typing import Any

from actions import gmail_integration
from actions import business_intelligence as biz_intel

# Only these two classifications represent a real recruiting-relevant
# signal worth logging/drafting for — "notification" and "uncategorized"
# are noise for BuildPro's purposes specifically.
_RELEVANT_CLASSIFICATIONS = {"candidate_reply", "client_inquiry"}

_ACK_TEMPLATES = {
    "candidate_reply": (
        "Thanks for reaching out about this opportunity — someone from BuildPro "
        "Recruiting will review your message and follow up shortly."
    ),
    "client_inquiry": (
        "Thanks for your message — someone from BuildPro Recruiting will follow up "
        "with you shortly regarding your project/staffing needs."
    ),
}


def _extract_signal(message: dict[str, Any], classification: str) -> dict[str, Any]:
    """What's actually extractable from the message without inventing
    anything — sender, subject, snippet, and the classification itself.
    Deliberately does not attempt to parse a name/phone/skills out of free
    text; that's real NLP work this module doesn't pretend to do."""
    return {
        "message_id": message.get("id"),
        "sender": message.get("sender"),
        "subject": message.get("subject"),
        "snippet": message.get("snippet"),
        "classification": classification,
    }


def scan_inbox(
    query: str = "in:inbox",
    max_results: int = 15,
    draft_replies: bool = False,
) -> dict[str, Any]:
    """Lists messages (default: whole inbox, read or unread), classifies
    each, logs a business intelligence entry for every candidate_reply/
    client_inquiry found, and — only if draft_replies=True — creates a real
    (but never sent) Gmail draft acknowledgment for each one. Honest about
    auth/API failure (never fabricates a scan result) via the same
    {"ok": False, ...} convention gmail_integration.py itself uses.

    2026-09-02 reliability audit finding: the default used to be
    "is:unread", which silently and permanently excluded any candidate/
    client email ever opened by anyone with mailbox access — see
    actions/agent_orchestrator.py's _INTAKE_QUERY comment for the full
    story. This function is read-only (log only, no HubSpot/draft writes
    unless draft_replies=True), so unlike the candidate/client intake
    handlers it doesn't need message-level dedup to stay safe when the
    scope widens."""
    r = gmail_integration.list_messages(query=query, max_results=max_results)
    if not r["ok"]:
        return {
            "ok": False, "state": r.get("state"), "detail": r.get("detail"),
            "scanned": 0, "relevant": [], "drafts_created": [],
        }

    relevant: list[dict[str, Any]] = []
    drafts_created: list[dict[str, Any]] = []

    for message in r["messages"]:
        classification = gmail_integration.classify_message(message)
        if classification not in _RELEVANT_CLASSIFICATIONS:
            continue

        signal = _extract_signal(message, classification)
        relevant.append(signal)

        biz_intel.add_entry(
            category="research", business="buildpro",
            title=f"[Email:{classification}] {message.get('subject') or '(no subject)'}",
            content=f"From: {message.get('sender')}\nSnippet: {message.get('snippet')}",
            data=signal,
        )

        if draft_replies:
            sender = (message.get("sender") or "").strip()
            reply_to = sender.split("<")[-1].rstrip(">") if "<" in sender else sender
            if reply_to:
                subject = message.get("subject") or ""
                reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
                draft = gmail_integration.create_draft(
                    reply_to, reply_subject, _ACK_TEMPLATES[classification],
                )
                if draft["ok"]:
                    drafts_created.append({"message_id": message.get("id"), "draft_id": draft["draft_id"]})

    return {
        "ok": True,
        "scanned": len(r["messages"]),
        "relevant": relevant,
        "drafts_created": drafts_created,
        "summary": (
            f"Scanned {len(r['messages'])} message(s); {len(relevant)} relevant "
            f"(candidate/client) message(s) found and logged to business intelligence"
            + (f"; {len(drafts_created)} acknowledgment draft(s) created." if draft_replies else ".")
        ),
    }
