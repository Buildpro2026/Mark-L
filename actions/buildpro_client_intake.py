"""Client-inquiry intake chain (2026-08-30, Lee's spec) — the client-side
counterpart to actions/candidate_intake.py. When a client_inquiry email
comes in: get them into HubSpot (as a company, matching
buildpro_data.buildpro_clients' hubspot_company_id column) and the local
BuildPro client table, draft (never auto-send) a short personal "we're
excited to help" email, and flag a real contact-by deadline so the
inquiry doesn't sit untouched.

Unlike candidate_intake.py, this never mentions representation or any
agreement — Lee's explicit instruction (2026-08-30) is that contracts are
his own call after he's personally made contact, not something an
auto-drafted first-contact email should presuppose for clients either.

Contact-by deadline: "an agent will contact them by the end of the
business day, or the next business day, depending on their time zone."
There's no reliable way to know a client's time zone from an email alone
— this makes a best effort from a US phone number's area code (a
necessarily partial lookup table, not every area code) and is explicit
about it via timezone_inferred=False when it has to fall back to
BuildPro's own business time zone. Nothing here ever claims certainty it
doesn't have.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from actions import buildpro_data
from actions import business_intelligence as biz_intel
from actions import gmail_integration
from actions import hubspot_integration

_SENDER_RE = re.compile(r'^\s*"?([^"<]*?)"?\s*<([^>]+)>\s*$')
_PHONE_RE = re.compile(r'(?:\+?1[-.\s]?)?\(?(\d{3})\)?[-.\s]?\d{3}[-.\s]?\d{4}')

# BuildPro's own operating time zone — the honest fallback when a client's
# real time zone can't be determined from anything in the message.
_HOME_TZ = "America/Chicago"
_BUSINESS_CLOSE_HOUR = 17  # 5 PM local

# Deliberately partial — the common US area codes, not an exhaustive or
# purchased area-code database. Area codes not listed here (or a non-US
# number, or no number at all) fall back to _HOME_TZ with
# timezone_inferred=False rather than guessing.
_AREA_CODE_TZ: dict[str, str] = {
    # Eastern
    **{c: "America/New_York" for c in (
        "201", "202", "203", "212", "215", "216", "239", "267", "301", "302",
        "305", "321", "347", "352", "386", "404", "407", "410", "412", "434",
        "470", "484", "561", "617", "678", "703", "704", "718", "727", "754",
        "757", "786", "813", "845", "856", "857", "863", "904", "917", "929",
        "941", "954",
    )},
    # Central
    **{c: "America/Chicago" for c in (
        "205", "214", "217", "225", "281", "312", "314", "318", "331", "337",
        "409", "469", "479", "501", "512", "601", "608", "612", "615", "618",
        "630", "651", "662", "708", "713", "731", "773", "832", "901", "913",
        "918", "972",
    )},
    # Mountain
    **{c: "America/Denver" for c in (
        "303", "385", "406", "435", "480", "505", "520", "602", "623", "719",
        "801", "928", "970",
    )},
    # Pacific
    **{c: "America/Los_Angeles" for c in (
        "206", "209", "213", "253", "310", "323", "360", "408", "415", "425",
        "503", "509", "530", "541", "559", "562", "619", "626", "650", "657",
        "702", "707", "714", "747", "760", "775", "805", "818", "858", "909",
        "916", "925", "949", "951",
    )},
}


def _parse_sender(sender: str) -> tuple[str, str]:
    sender = (sender or "").strip()
    if not sender:
        return "", ""
    m = _SENDER_RE.match(sender)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return "", sender


def _extract_phone(text: str) -> str | None:
    m = _PHONE_RE.search(text or "")
    return m.group(0) if m else None


def _infer_timezone(phone: str | None) -> tuple[str, bool]:
    """Returns (tz_name, inferred). inferred=False means this is
    BuildPro's own fallback zone, not a real read on the client."""
    if phone:
        digits = re.sub(r"\D", "", phone)
        if len(digits) >= 10:
            area_code = digits[-10:-7]
            tz = _AREA_CODE_TZ.get(area_code)
            if tz:
                return tz, True
    return _HOME_TZ, False


def _contact_by_deadline(tz_name: str) -> datetime:
    """End of business today in tz_name if it's still before close on a
    weekday; otherwise 5 PM on the next weekday."""
    now = datetime.now(ZoneInfo(tz_name))
    close_today = now.replace(hour=_BUSINESS_CLOSE_HOUR, minute=0, second=0, microsecond=0)
    candidate = close_today if (now.weekday() < 5 and now < close_today) else close_today + timedelta(days=1)
    while candidate.weekday() >= 5:  # roll weekend deadlines to Monday
        candidate += timedelta(days=1)
    return candidate


def _compose_client_email(first_name: str) -> tuple[str, str]:
    greeting_name = first_name or "there"
    subject = "Thanks for reaching out to BuildPro Recruiters"
    body = f"""Hi {greeting_name},

Thank you for reaching out to BuildPro Recruiters — we're excited to help you with your staffing needs.

Someone from our team will personally follow up with you shortly to learn more about what you're looking for and how we can help.

Talk soon,
BuildPro Recruiters
"""
    return subject, body


def process_client_email(message: dict[str, Any], auto_send: bool = False) -> dict[str, Any]:
    """Runs the client-side intake chain for one already-classified
    client_inquiry Gmail message. Mirrors candidate_intake.py's error-
    handling contract: never raises, every external call already returns
    an honest ok/error dict, and each optional step degrades gracefully."""
    name, email = _parse_sender(message.get("sender") or "")
    if not email:
        return {"ok": False, "detail": "Could not determine a sender email address from this message."}

    body_text = f"{message.get('subject') or ''} {message.get('snippet') or ''}"
    phone = _extract_phone(body_text)
    tz_name, tz_inferred = _infer_timezone(phone)
    deadline = _contact_by_deadline(tz_name)
    # Built manually rather than via strftime's %-I/%-d (no leading zero):
    # those flags aren't portable — Windows needs %#I/%#d instead — and
    # this needs to read the same on every platform it might ever run on.
    hour_12 = deadline.hour % 12 or 12
    deadline_str = (
        f"{hour_12}:{deadline.minute:02d} {'AM' if deadline.hour < 12 else 'PM'} "
        f"{deadline.tzname()} on {deadline.strftime('%A, %B')} {deadline.day}"
    )

    # 1. Local BuildPro client table.
    display_name = name or email
    client_id, client_action = buildpro_data.upsert_client(
        display_name, email=email, phone=phone or "",
        source="gmail_intake",
        notes=(
            f"Auto-intake from email: {message.get('subject') or '(no subject)'}. "
            f"Contact by {deadline_str}"
            + ("" if tz_inferred else f" (time zone not determinable from message — assumed {_HOME_TZ})") + "."
        ),
    )

    # 2. HubSpot company — best-effort; the local record above already
    # exists regardless of whether HubSpot is configured/reachable.
    hubspot_company_id: str | None = None
    hubspot_ok = False
    if hubspot_integration.is_configured():
        props: dict[str, str] = {}
        if phone:
            props["phone"] = phone
        hs_result = hubspot_integration.upsert_company(display_name, props, approved=True)
        hubspot_ok = bool(hs_result.get("ok"))
        if hubspot_ok:
            hubspot_company_id = hs_result["record"]["id"]
            buildpro_data.update_client(client_id, hubspot_company_id=hubspot_company_id)

    # 3. Flag the contact-by deadline as a real, visible priority — same
    # mechanism buildpro_email_monitor already uses to surface findings.
    biz_intel.add_entry(
        category="research", business="buildpro",
        title=f"[Client inquiry] Contact {display_name} by {deadline_str}",
        content=(
            f"From: {message.get('sender')}\nSnippet: {message.get('snippet')}\n"
            f"Inferred time zone: {tz_name} ({'from phone number' if tz_inferred else 'assumed — no phone/location found'})"
        ),
        data={
            "client_id": client_id, "email": email, "phone": phone,
            "timezone": tz_name, "timezone_inferred": tz_inferred,
            "contact_by": deadline.isoformat(),
        },
    )

    # 4. Personal "excited to help" email — draft only, no contract content.
    subject, body = _compose_client_email(name)
    if auto_send:
        send_result = gmail_integration.send_email(email, subject, body, approved=True)
    else:
        send_result = gmail_integration.create_draft(email, subject, body)

    return {
        "ok": True,
        "client_id": client_id,
        "client_action": client_action,
        "client_email": email,
        "client_name": name,
        "hubspot_company_id": hubspot_company_id,
        "hubspot_ok": hubspot_ok,
        "phone_found": phone,
        "timezone": tz_name,
        "timezone_inferred": tz_inferred,
        "contact_by": deadline.isoformat(),
        "contact_by_display": deadline_str,
        "welcome_email_sent": auto_send and bool(send_result.get("ok")),
        "welcome_email_drafted": (not auto_send) and bool(send_result.get("ok")),
        "welcome_email_error": None if send_result.get("ok") else send_result.get("detail"),
    }
