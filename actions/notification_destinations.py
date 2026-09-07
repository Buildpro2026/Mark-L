"""Where a notification actually takes you.

A notification that says "3 draft emails ready" and then makes you go find
them yourself is a reminder, not a tool. This turns the record identity a
notification already carries into a concrete destination, so the whole
thing can be clicked.

Two rules, both load-bearing:

  * Destinations are DERIVED from real record ids, never hard-coded URLs
    handed to the frontend. A HubSpot contact link is built from the
    contact id; if there is no id, there is no link.
  * A missing or unsafe destination produces NO destination, and the
    notification stays informational. A dead link is worse than no link —
    it teaches you not to click.

Opening a destination is navigation, never execution. An approval
notification opens the approval for a human to decide; it can never carry
the decision itself. That boundary lives in the orchestrator's approval
gate, and nothing here can reach past it.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional
from urllib.parse import quote

logger = logging.getLogger("jarvis.notification_destinations")

# Destination kinds. `internal` paths are routes on JARVIS's own
# authenticated UI; `external` are third-party deep links.
INTERNAL = "internal"
EXTERNAL = "external"

# Hosts JARVIS is willing to link out to. An allowlist rather than a
# blocklist: a link built from a record whose fields came from an inbound
# email is only as trustworthy as that email, and an open redirect is how
# a phishing link would arrive wearing JARVIS's branding.
ALLOWED_EXTERNAL_HOSTS = {
    "app.hubspot.com",
    "mail.google.com",
    "calendar.google.com",
    "www.linkedin.com",
    "linkedin.com",
    "publish.buffer.com",
}

_SAFE_ID = re.compile(r"^[A-Za-z0-9_\-:.@]{1,128}$")


def _safe_id(value: Any) -> Optional[str]:
    """Record ids end up inside URLs. Anything that is not a plain
    identifier is rejected outright rather than escaped and hoped for."""
    if value is None:
        return None
    text = str(value).strip()
    return text if _SAFE_ID.match(text) else None


def _external(url: str, label: str, action: str) -> Optional[dict[str, Any]]:
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_EXTERNAL_HOSTS:
        logger.debug("refusing destination outside the allowlist: %s", parsed.hostname)
        return None
    return {"kind": EXTERNAL, "url": url, "label": label, "action": action}


def _internal(path: str, label: str, action: str) -> dict[str, Any]:
    # Relative to JARVIS's own origin, so the browser carries the existing
    # session cookie and the route enforces auth exactly as it always does.
    return {"kind": INTERNAL, "path": path, "label": label, "action": action}


# ── per-source builders ──────────────────────────────────────────────────

def hubspot_contact(contact_id: Any, portal_id: Any = None) -> Optional[dict[str, Any]]:
    cid = _safe_id(contact_id)
    if not cid:
        return None
    pid = _safe_id(portal_id)
    url = (f"https://app.hubspot.com/contacts/{pid}/contact/{cid}" if pid
           else f"https://app.hubspot.com/contacts/contact/{cid}")
    return _external(url, "Open in HubSpot", "open_hubspot_contact")


def hubspot_company(company_id: Any, portal_id: Any = None) -> Optional[dict[str, Any]]:
    cid = _safe_id(company_id)
    if not cid:
        return None
    pid = _safe_id(portal_id)
    url = (f"https://app.hubspot.com/contacts/{pid}/company/{cid}" if pid
           else f"https://app.hubspot.com/contacts/company/{cid}")
    return _external(url, "Open in HubSpot", "open_hubspot_company")


def email_thread(thread_id: Any = None, message_id: Any = None) -> Optional[dict[str, Any]]:
    ident = _safe_id(thread_id) or _safe_id(message_id)
    if not ident:
        return None
    return _external(f"https://mail.google.com/mail/u/0/#all/{ident}",
                     "Review Email", "open_email")


def email_drafts() -> dict[str, Any]:
    """Drafts live in JARVIS's own review area, not in Gmail — that is where
    they can still be approved or discarded before they are sent."""
    return _internal("/ui#drafts", "Review Drafts", "open_drafts")


def approval(task_id: Any) -> Optional[dict[str, Any]]:
    """Opens the approval for a decision. Deliberately internal and
    deliberately a VIEW: approving is a separate authenticated action
    through the orchestrator, and no link can stand in for it."""
    tid = _safe_id(task_id)
    if not tid:
        return None
    return _internal(f"/ui#approvals/{quote(tid)}", "Review Approval", "open_approval")


def calendar_event(event_id: Any) -> Optional[dict[str, Any]]:
    eid = _safe_id(event_id)
    if not eid:
        return None
    return _external(f"https://calendar.google.com/calendar/u/0/r/eventedit/{eid}",
                     "Open Calendar Event", "open_calendar_event")


def linkedin_conversation(thread_url: Any = None, conversation_id: Any = None) -> Optional[dict[str, Any]]:
    """LinkedIn only when a real, safe destination exists. LinkedIn's own
    notification emails carry the thread URL; when they do not, this returns
    None and the notification stays informational rather than guessing a URL
    that lands on a generic inbox."""
    if thread_url:
        return _external(str(thread_url), "Open LinkedIn", "open_linkedin")
    cid = _safe_id(conversation_id)
    if not cid:
        return None
    return _external(f"https://www.linkedin.com/messaging/thread/{cid}/",
                     "Open LinkedIn", "open_linkedin")


def opportunity(opportunity_id: Any) -> Optional[dict[str, Any]]:
    oid = _safe_id(opportunity_id)
    if not oid:
        return None
    return _internal(f"/ui#opportunities/{quote(oid)}", "Review Opportunity", "open_opportunity")


def social_post(post_id: Any) -> Optional[dict[str, Any]]:
    pid = _safe_id(post_id)
    if not pid:
        return None
    return _internal(f"/ui#social/{quote(pid)}", "Review Post", "open_social_post")


def task(task_id: Any) -> Optional[dict[str, Any]]:
    tid = _safe_id(task_id)
    if not tid:
        return None
    return _internal(f"/ui#tasks/{quote(tid)}", "Open Task", "open_task")


# ── dispatch by source ───────────────────────────────────────────────────

_BUILDERS = {
    "hubspot_contact": lambda d: hubspot_contact(d.get("contact_id"), d.get("portal_id")),
    "hubspot_company": lambda d: hubspot_company(d.get("company_id"), d.get("portal_id")),
    "email": lambda d: email_thread(d.get("thread_id"), d.get("message_id")),
    "email_drafts": lambda d: email_drafts(),
    "approval": lambda d: approval(d.get("task_id")),
    "calendar": lambda d: calendar_event(d.get("event_id")),
    "linkedin": lambda d: linkedin_conversation(d.get("thread_url"), d.get("conversation_id")),
    "opportunity": lambda d: opportunity(d.get("opportunity_id")),
    "social": lambda d: social_post(d.get("post_id")),
    "task": lambda d: task(d.get("task_id")),
}


def build(source: str, data: Optional[dict] = None) -> Optional[dict[str, Any]]:
    """The destination for one notification, or None when there is not a
    safe one. Never raises — a broken destination must not stop the
    notification itself from being delivered."""
    builder = _BUILDERS.get(source)
    if builder is None:
        return None
    try:
        return builder(data or {})
    except Exception:
        logger.debug("could not build a %s destination", source, exc_info=True)
        return None
