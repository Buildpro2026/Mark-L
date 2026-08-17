"""Google Calendar integration — read events; create/update only when
explicitly approved. Uses actions/google_auth.py for OAuth — the same
shared Google credential/token as gmail_integration.py, not a second auth
system.

create_event()/update_event() refuse to call the Calendar API unless
approved=True is passed by a caller acting on an explicit instruction — the
same gate pattern gmail_integration.send_email() uses.
"""
from __future__ import annotations

import datetime
from typing import Any

from actions import google_auth


def _service():
    from googleapiclient.discovery import build
    creds = google_auth.get_credentials()
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _ensure_tz(iso_str: str) -> str:
    """If `iso_str` has no UTC offset, attaches the system's local timezone
    rather than letting the Calendar API either reject it or silently
    interpret it in a zone the user didn't mean. Google Calendar requires
    an explicit offset (or a separate timeZone field) on a dateTime value
    that doesn't already have one — a naive datetime from an LLM-produced
    ISO string (no offset) is exactly the case this guards against."""
    try:
        dt = datetime.datetime.fromisoformat(iso_str)
    except ValueError:
        return iso_str   # not parseable — pass through, let the API's own error surface
    if dt.tzinfo is None:
        dt = dt.astimezone()   # attaches the system's local timezone
        return dt.isoformat()
    return iso_str


def _check_conflicts(start_iso: str, end_iso: str, calendar_id: str = "primary") -> list[dict[str, Any]]:
    """Read-only: events on `calendar_id` that overlap [start_iso, end_iso).
    Never raises — a conflict-check failure must not block event creation
    outright (that would turn a safety nicety into a hard blocker on its
    own failure); callers get an empty list and proceed as if unchecked."""
    try:
        service = _service()
        resp = service.events().list(
            calendarId=calendar_id, timeMin=start_iso, timeMax=end_iso,
            singleEvents=True, orderBy="startTime",
        ).execute()
        return [_normalize_event(e) for e in resp.get("items", [])]
    except Exception:
        return []


def _normalize_event(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": event.get("id"),
        "summary": event.get("summary"),
        "description": event.get("description"),
        "start": (event.get("start") or {}).get("dateTime") or (event.get("start") or {}).get("date"),
        "end": (event.get("end") or {}).get("dateTime") or (event.get("end") or {}).get("date"),
        "location": event.get("location"),
        "html_link": event.get("htmlLink"),
    }


def list_upcoming_events(max_results: int = 10, calendar_id: str = "primary") -> dict[str, Any]:
    """Read-only: upcoming events from now, ordered by start time. Never
    fabricates results — an auth/API failure returns ok=False, not fake events."""
    try:
        service = _service()
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        resp = service.events().list(
            calendarId=calendar_id, timeMin=now, maxResults=max_results,
            singleEvents=True, orderBy="startTime",
        ).execute()
        events = [_normalize_event(e) for e in resp.get("items", [])]
        return {"ok": True, "events": events}
    except RuntimeError as exc:
        return {"ok": False, "state": "NOT_AUTHORIZED", "detail": str(exc), "events": []}
    except Exception as exc:
        return {"ok": False, "state": "ERROR", "detail": str(exc), "events": []}


def create_event(
    summary: str, start_iso: str, end_iso: str, description: str = "",
    location: str = "", calendar_id: str = "primary", approved: bool = False,
    ignore_conflicts: bool = False,
) -> dict[str, Any]:
    """Refuses to create anything unless approved=True. start_iso/end_iso
    are full RFC3339 datetimes (e.g. '2026-08-15T09:00:00-05:00') — this
    function does not interpret relative dates/times; that's a caller
    concern (e.g. a future voice-nav-style parser), not this connector's.
    A naive datetime with no UTC offset is given the system's local
    timezone rather than left ambiguous (see _ensure_tz).

    Unless ignore_conflicts=True, checks for existing events overlapping
    the requested time range first and refuses (state=CONFLICT) rather
    than silently double-booking — the caller (or the human, via JARVIS)
    decides whether to proceed anyway."""
    if not approved:
        return {"ok": False, "state": "NOT_APPROVED", "detail": "Creating a calendar event requires explicit approval."}
    start_iso = _ensure_tz(start_iso)
    end_iso = _ensure_tz(end_iso)
    if not ignore_conflicts:
        conflicts = _check_conflicts(start_iso, end_iso, calendar_id)
        if conflicts:
            return {
                "ok": False, "state": "CONFLICT",
                "detail": f"{len(conflicts)} existing event(s) overlap this time.",
                "conflicts": conflicts,
            }
    try:
        service = _service()
        body = {
            "summary": summary, "description": description, "location": location,
            "start": {"dateTime": start_iso}, "end": {"dateTime": end_iso},
        }
        created = service.events().insert(calendarId=calendar_id, body=body).execute()
        return {"ok": True, "event_id": created.get("id"), "html_link": created.get("htmlLink")}
    except RuntimeError as exc:
        return {"ok": False, "state": "NOT_AUTHORIZED", "detail": str(exc)}
    except Exception as exc:
        return {"ok": False, "state": "ERROR", "detail": str(exc)}


def update_event(event_id: str, calendar_id: str = "primary", approved: bool = False, **fields: Any) -> dict[str, Any]:
    """Refuses to update anything unless approved=True. Accepts any of
    summary/description/location/start_iso/end_iso as keyword fields —
    only fields actually provided are patched, nothing else is touched."""
    if not approved:
        return {"ok": False, "state": "NOT_APPROVED", "detail": "Updating a calendar event requires explicit approval."}
    patch: dict[str, Any] = {}
    if "summary" in fields:
        patch["summary"] = fields["summary"]
    if "description" in fields:
        patch["description"] = fields["description"]
    if "location" in fields:
        patch["location"] = fields["location"]
    if "start_iso" in fields:
        patch["start"] = {"dateTime": _ensure_tz(fields["start_iso"])}
    if "end_iso" in fields:
        patch["end"] = {"dateTime": _ensure_tz(fields["end_iso"])}
    try:
        service = _service()
        updated = service.events().patch(calendarId=calendar_id, eventId=event_id, body=patch).execute()
        return {"ok": True, "event_id": updated.get("id")}
    except RuntimeError as exc:
        return {"ok": False, "state": "NOT_AUTHORIZED", "detail": str(exc)}
    except Exception as exc:
        return {"ok": False, "state": "ERROR", "detail": str(exc)}
