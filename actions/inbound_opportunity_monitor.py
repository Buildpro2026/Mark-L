"""Inbound business opportunities, from email and LinkedIn, into the loop.

One monitor for both sources because the interesting part is identical: a
person wrote something, is it business, and does it need Lee now. Only the
retrieval differs.

On LinkedIn specifically, and stated plainly rather than implied: JARVIS
does not read LinkedIn's message store. LinkedIn's API does not expose
personal messaging to third-party applications, so any module claiming to
poll the inbox directly would be fiction. What it reads instead is
LinkedIn's own notification email — the "You have a new message from X" that
LinkedIn already sends — through the Gmail integration that is already
authenticated. That is a real, working signal with a real destination (the
thread URL LinkedIn puts in the mail), and it degrades honestly to
NOT_CONFIGURED when Gmail is not authorised.

Everything here is DETECTION. No message is sent, no reply drafted, no
commitment made. High-value findings become tasks and notifications; any
outbound action they lead to still passes the existing approval gate.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from actions import autonomous_ledger as ledger
from actions import business_intent
from actions import business_pipeline as bp

_EMAIL_ADDR_RE = re.compile(r"[\w.+\-]+@[\w\-]+\.[\w.\-]+")

logger = logging.getLogger("jarvis.inbound_monitor")

# LinkedIn's notification senders.
_LINKEDIN_SENDER = re.compile(r"@(?:linkedin\.com|e\.linkedin\.com|bounce\.linkedin\.com)", re.I)
# The thread URL LinkedIn embeds in a message notification.
_LINKEDIN_THREAD_URL = re.compile(
    r"https://www\.linkedin\.com/(?:messaging/thread/[A-Za-z0-9_\-=]+/?|comm/messaging/[^\s\"'>]+)", re.I)
# "You have a new message from Jane Doe" / "Jane Doe sent you a message"
_LINKEDIN_SENDER_NAME = re.compile(
    r"(?:new message from|message from|)\s*([A-Z][\w.'-]+(?: [A-Z][\w.'-]+){0,2})\s*(?:sent you|:|$)", re.M)

MAX_MESSAGES = 25


def _fetch_inbox(query: str, limit: int) -> dict[str, Any]:
    """Reads real inbox messages, or reports honestly why it could not."""
    from actions import gmail_integration
    listing = gmail_integration.list_messages(query=query, max_results=limit)
    if not listing.get("ok", True):
        detail = str(listing.get("detail") or listing.get("state") or "gmail unavailable")
        state = bp.AUTH_ERROR if listing.get("state") == "NOT_AUTHORIZED" else bp.FAILED
        return {"state": state, "detail": detail, "messages": []}

    messages = []
    for stub in (listing.get("messages") or []):
        mid = stub.get("id")
        if not mid:
            continue
        try:
            full = gmail_integration.get_message(mid)
        except Exception:
            logger.debug("could not fetch message %s", mid, exc_info=True)
            continue
        if not full.get("ok", True):
            continue
        messages.append(full)
    return {"state": bp.SUCCESS, "messages": messages}


def _linkedin_details(message: dict[str, Any]) -> dict[str, Any]:
    body = f"{message.get('subject') or ''}\n{message.get('body') or message.get('snippet') or ''}"
    url_match = _LINKEDIN_THREAD_URL.search(body)
    name_match = _LINKEDIN_SENDER_NAME.search(message.get("subject") or "")
    return {
        "thread_url": url_match.group(0) if url_match else None,
        "person": name_match.group(1).strip() if name_match else None,
    }


# ── the two sources ──────────────────────────────────────────────────────

def linkedin_findings(limit: int = MAX_MESSAGES) -> dict[str, Any]:
    """Inbound LinkedIn conversations worth acting on."""
    fetched = _fetch_inbox("from:linkedin.com newer_than:7d", limit)
    if fetched["state"] != bp.SUCCESS:
        return {"state": fetched["state"], "detail": fetched.get("detail"), "findings": []}

    findings, scanned = [], 0
    for message in fetched["messages"]:
        sender = str(message.get("sender") or message.get("from") or "")
        if not _LINKEDIN_SENDER.search(sender):
            continue
        scanned += 1
        details = _linkedin_details(message)
        # LinkedIn's own wrapper is automated mail; what matters is the
        # human message quoted inside it, so intent is scored on the body
        # while the automated-sender heuristic is deliberately bypassed.
        verdict = business_intent.classify(
            {"sender": details.get("person") or "linkedin-member",
             "subject": message.get("subject"), "body": message.get("body") or message.get("snippet")},
            source="linkedin")
        # No CRM cross-reference here, unlike email_opportunity_findings —
        # a genuine integration limitation, not an oversight: LinkedIn's
        # own notification email gives a display name, never an email
        # address, and matching "existing candidate/client" on a bare name
        # would risk crediting the wrong person entirely (name collisions
        # are common). CLIENT/CANDIDATE/GENERAL/NOISE are still classified
        # correctly; EXISTING_CLIENT/EXISTING_CANDIDATE just never fires
        # for this source, honestly, rather than guessing.
        if not business_intent.is_at_least(verdict["priority"], business_intent.HIGH):
            continue
        person = details.get("person") or "a LinkedIn member"
        findings.append({
            **bp._finding(
                "linkedin_inbound", str(message.get("id")),
                f"LinkedIn: {verdict['party'].replace('_', ' ')} — {person}",
                "buildpro_prospecting_agent",
                f"Inbound LinkedIn message from {person} classified "
                f"{verdict['priority']}/{verdict['party']}: {verdict['reason']}",
                {"message_id": message.get("id"), **details, **verdict},
            ),
            "verdict": verdict,
            "destination_source": "linkedin",
            "destination_data": {"thread_url": details.get("thread_url")},
        })
    return {"state": bp.SUCCESS, "findings": findings, "scanned": scanned}


def _sender_email(raw: str) -> str:
    """The bare address out of a "From" header ("Jane Doe <jane@co.com>" or
    a bare address) — '' when none is present."""
    m = _EMAIL_ADDR_RE.search(raw or "")
    return m.group(0).lower() if m else ""


def _apply_crm_cross_reference(verdict: dict[str, Any], sender_raw: str) -> dict[str, Any]:
    """Upgrades a fresh CLIENT/CANDIDATE verdict to EXISTING_CLIENT/
    EXISTING_CANDIDATE when the sender is already a known contact —
    business_intent.classify() has no CRM access of its own (it is
    deliberately pure pattern-matching over the message text), so this is
    the one place that cross-references the two real record stores
    (BuildPro's own candidate table, then HubSpot) that actually know who
    is already in the book of business. Never raises and never downgrades
    a verdict — a lookup failure just leaves the fresh classification as
    the honest answer it already was."""
    party = verdict.get("party")
    if party not in (business_intent.CLIENT, business_intent.CANDIDATE):
        return verdict
    email = _sender_email(sender_raw)
    if not email:
        return verdict

    try:
        if party == business_intent.CANDIDATE:
            from actions import buildpro_data
            existing = buildpro_data.find_candidate_by_email(email)
            if existing:
                return {**verdict, "party": business_intent.EXISTING_CANDIDATE,
                        "reason": verdict["reason"] + " — already a known BuildPro candidate",
                        "crm_state": "MATCHED_BUILDPRO"}
        else:
            from actions import buildpro_data
            existing = buildpro_data.find_client_by_email(email)
            if existing:
                return {**verdict, "party": business_intent.EXISTING_CLIENT,
                        "reason": verdict["reason"] + " — already a known BuildPro client",
                        "crm_state": "MATCHED_BUILDPRO"}
            crm = business_intent.crm_relationship(email)
            if crm.get("trusted") and crm.get("party") == business_intent.EXISTING_CLIENT:
                return {**verdict, "party": business_intent.EXISTING_CLIENT,
                        "reason": verdict["reason"] + " — already a known HubSpot contact",
                        "crm_state": crm.get("crm_state")}
    except Exception:
        logger.debug("CRM cross-reference failed for %s", email, exc_info=True)
    return verdict


def email_opportunity_findings(limit: int = MAX_MESSAGES) -> dict[str, Any]:
    """Inbound email showing real business intent."""
    fetched = _fetch_inbox("in:inbox newer_than:3d -from:linkedin.com", limit)
    if fetched["state"] != bp.SUCCESS:
        return {"state": fetched["state"], "detail": fetched.get("detail"), "findings": []}

    findings, scanned = [], 0
    for message in fetched["messages"]:
        scanned += 1
        sender = str(message.get("sender") or message.get("from") or "someone")
        verdict = business_intent.classify(message, source="email")
        verdict = _apply_crm_cross_reference(verdict, sender)
        if not business_intent.is_at_least(verdict["priority"], business_intent.HIGH):
            continue
        findings.append({
            **bp._finding(
                "email_opportunity", str(message.get("id")),
                f"Inbound: {verdict['party'].replace('_', ' ')} — {sender[:60]}",
                "buildpro_prospecting_agent",
                f"Inbound email from {sender} classified {verdict['priority']}/"
                f"{verdict['party']}: {verdict['reason']}",
                {"message_id": message.get("id"), "sender": sender, **verdict},
            ),
            "verdict": verdict,
            "destination_source": "email",
            "destination_data": {"thread_id": message.get("thread_id"),
                                 "message_id": message.get("id")},
        })
    return {"state": bp.SUCCESS, "findings": findings, "scanned": scanned}


# ── immediate notification ───────────────────────────────────────────────

def notify_if_urgent(finding: dict[str, Any], now=None) -> Optional[dict[str, Any]]:
    """Interrupts Lee only for a CRITICAL/HIGH opportunity, only in business
    hours, and only once per subject.

    The ledger claim is what stops a repeated sweep from re-notifying about
    the same message every fifteen minutes — the behaviour that makes people
    mute a channel."""
    verdict = finding.get("verdict") or {}
    if not business_intent.should_notify_now(verdict, now=now):
        return None
    claim_key = f"notified:{finding['subject_id']}"
    if not ledger.claim(finding["kind"], claim_key, detail={"title": finding["title"]}):
        return None

    from actions import notifications
    send = notifications.urgent if verdict.get("priority") == business_intent.CRITICAL \
        else notifications.business_alert
    return send(
        event_id=f"{finding['kind']}-{finding['subject_id']}",
        title=finding["title"],
        detail=finding["description"],
        destination_source=finding.get("destination_source"),
        destination_data=finding.get("destination_data"),
    )


def run(limit: int = MAX_MESSAGES, orchestrator=None, now=None) -> dict[str, Any]:
    """One monitoring pass over both sources.

    Sources are isolated from each other and every notification is isolated
    from the work: a LinkedIn failure must not stop the email sweep, and a
    failed text must not stop a task being created."""
    report: dict[str, Any] = {"sources": {}, "dispatched": [], "notified": []}

    for name, fn in (("linkedin", linkedin_findings), ("email", email_opportunity_findings)):
        try:
            result = fn(limit=limit)
        except Exception as exc:
            logger.exception("inbound source %r failed", name)
            result = {"state": bp.classify_failure(exc), "detail": str(exc), "findings": []}

        report["sources"][name] = {"state": result["state"],
                                   "detail": result.get("detail"),
                                   "found": len(result.get("findings") or []),
                                   "scanned": result.get("scanned", 0)}
        if result["state"] != bp.SUCCESS:
            continue

        findings = result["findings"]
        try:
            created = bp.dispatch_findings(findings, orchestrator=orchestrator)
        except Exception:
            logger.exception("dispatch failed for %r", name)
            created = []
        report["dispatched"].extend(created)

        for finding in findings:
            try:
                sent = notify_if_urgent(finding, now=now)
            except Exception:
                logger.exception("immediate notification failed for %s", finding["subject_id"])
                sent = None
            if sent:
                report["notified"].append({"subject_id": finding["subject_id"],
                                           "ok": sent.get("ok"),
                                           "actionable": sent.get("actionable")})

    report["total_dispatched"] = len(report["dispatched"])
    report["total_notified"] = len(report["notified"])
    return report
