"""HubSpot CRM integration.

Config lives in config/api_keys.json under "hubspot_token" (a HubSpot
Private App token). Never hardcoded — api_keys.json is gitignored and read
at call time, matching the pattern used by twilio_integration.py and
buffer_integration.py.

Uses HubSpot's current CRM API v3 (api.hubapi.com) with Bearer auth —
confirmed live against the configured token: account-info, contacts,
companies, and search all authenticate and return real data.

This module never fabricates data — every function either returns a real
API result or an honest NOT_CONFIGURED / ERROR state, and API-level errors
(4xx/5xx) are captured and reported rather than raised.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import requests

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"

API_BASE = "https://api.hubapi.com"


def get_hubspot_token() -> str | None:
    from core.headless import config as _hc
    if _hc.HUBSPOT_TOKEN:
        return _hc.HUBSPOT_TOKEN
    if not CONFIG_PATH.exists():
        return None
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    token = str(data.get("hubspot_token") or "").strip()
    return token or None


def is_configured() -> bool:
    return bool(get_hubspot_token())


def _request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    """Shared request wrapper — always returns a uniform dict, never raises.
    Distinguishes NOT_CONFIGURED (no token) from ERROR (network failure or
    a 4xx/5xx from HubSpot) so callers/tests can tell them apart."""
    token = get_hubspot_token()
    if not token:
        return {"ok": False, "state": "NOT_CONFIGURED", "detail": "HubSpot isn't configured."}
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        resp = requests.request(method, f"{API_BASE}{path}", headers=headers, timeout=15, **kwargs)
    except Exception as exc:
        return {"ok": False, "state": "ERROR", "detail": str(exc)}
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("message", resp.text[:300])
        except Exception:
            detail = resp.text[:300]
        return {"ok": False, "state": "ERROR", "status_code": resp.status_code, "detail": detail}
    try:
        data = resp.json()
    except Exception:
        data = {}
    return {"ok": True, "state": "OK", "status_code": resp.status_code, "data": data}


def verify_hubspot() -> dict[str, Any]:
    """Live auth check against a lightweight, read-only endpoint (no CRM
    data pulled) — the HubSpot equivalent of twilio_integration.check_connection()."""
    if not is_configured():
        return {"configured": False, "verified": False, "status": "NOT_CONFIGURED"}
    result = _request("GET", "/account-info/v3/details")
    if result["ok"]:
        return {"configured": True, "verified": True, "status": "VERIFIED", "account": result["data"]}
    detail = result.get("detail", "unknown error")
    code = result.get("status_code")
    return {
        "configured": True, "verified": False,
        "status": f"UNAVAILABLE:{code}" if code else f"UNAVAILABLE:{detail}",
        "detail": detail,
    }


def _list_result(result: dict[str, Any]) -> dict[str, Any]:
    if not result["ok"]:
        return {"ok": False, "state": result["state"], "detail": result.get("detail"), "results": []}
    data = result["data"]
    return {
        "ok": True, "state": "OK",
        "results": data.get("results", []),
        "total": data.get("total"),
        "paging": data.get("paging"),
    }


def _item_result(result: dict[str, Any]) -> dict[str, Any]:
    if not result["ok"]:
        return {"ok": False, "state": result["state"], "detail": result.get("detail")}
    return {"ok": True, "state": "OK", "record": result["data"]}


# ── Contacts ────────────────────────────────────────────────────────

def get_contacts(limit: int = 20, after: str | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit}
    if after:
        params["after"] = after
    return _list_result(_request("GET", "/crm/v3/objects/contacts", params=params))


def get_contact(contact_id: str) -> dict[str, Any]:
    return _item_result(_request("GET", f"/crm/v3/objects/contacts/{contact_id}"))


def search_contacts(query: str, property_name: str = "email", limit: int = 20) -> dict[str, Any]:
    """CONTAINS_TOKEN search on a single property (email by default) — the
    minimum useful search shape; HubSpot's search API supports far more
    (multiple filter groups, other operators) but this covers the common
    'find this candidate/contact' case without over-building."""
    body = {
        "filterGroups": [{"filters": [
            {"propertyName": property_name, "operator": "CONTAINS_TOKEN", "value": query}
        ]}],
        "limit": limit,
    }
    return _list_result(_request("POST", "/crm/v3/objects/contacts/search", json=body))


def create_contact(properties: dict[str, Any], approved: bool = False) -> dict[str, Any]:
    """Refuses to create anything unless approved=True — the same gate
    gmail_integration.send_email()/calendar_integration.create_event()/
    airtable_integration.create_record() use."""
    if not approved:
        return {"ok": False, "state": "NOT_APPROVED", "detail": "Creating a HubSpot contact requires explicit approval."}
    return _item_result(_request("POST", "/crm/v3/objects/contacts", json={"properties": properties}))


def update_contact(contact_id: str, properties: dict[str, Any], approved: bool = False) -> dict[str, Any]:
    if not approved:
        return {"ok": False, "state": "NOT_APPROVED", "detail": "Updating a HubSpot contact requires explicit approval."}
    return _item_result(_request("PATCH", f"/crm/v3/objects/contacts/{contact_id}", json={"properties": properties}))


def upsert_contact(email: str, properties: dict[str, Any], approved: bool = False) -> dict[str, Any]:
    """Idempotent create-or-update by email: searches for an existing
    contact with this email first; updates it if found, creates a new one
    (with email included) if not. Calling this twice with the same email
    and properties results in one contact, not two — the deduplication
    HubSpot's own API doesn't do for you across separate create calls."""
    if not approved:
        return {"ok": False, "state": "NOT_APPROVED", "detail": "Writing a HubSpot contact requires explicit approval."}
    if not email:
        return {"ok": False, "state": "ERROR", "detail": "email is required for upsert_contact."}
    found = search_contacts(email, property_name="email", limit=1)
    if not found["ok"]:
        return {"ok": False, "state": found["state"], "detail": found.get("detail")}
    matches = found["results"]
    if matches:
        contact_id = matches[0].get("id")
        result = update_contact(contact_id, properties, approved=True)
        if result["ok"]:
            result["action"] = "updated"
        return result
    full_properties = {**properties, "email": email}
    result = create_contact(full_properties, approved=True)
    if result["ok"]:
        result["action"] = "created"
    return result


# ── Companies ─────────────────────────────────────────────────

def get_companies(limit: int = 20, after: str | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit}
    if after:
        params["after"] = after
    return _list_result(_request("GET", "/crm/v3/objects/companies", params=params))


def get_company(company_id: str) -> dict[str, Any]:
    return _item_result(_request("GET", f"/crm/v3/objects/companies/{company_id}"))


def search_companies(query: str, property_name: str = "name", limit: int = 20) -> dict[str, Any]:
    body = {
        "filterGroups": [{"filters": [
            {"propertyName": property_name, "operator": "CONTAINS_TOKEN", "value": query}
        ]}],
        "limit": limit,
    }
    return _list_result(_request("POST", "/crm/v3/objects/companies/search", json=body))


def create_company(properties: dict[str, Any], approved: bool = False) -> dict[str, Any]:
    if not approved:
        return {"ok": False, "state": "NOT_APPROVED", "detail": "Creating a HubSpot company requires explicit approval."}
    return _item_result(_request("POST", "/crm/v3/objects/companies", json={"properties": properties}))


def update_company(company_id: str, properties: dict[str, Any], approved: bool = False) -> dict[str, Any]:
    if not approved:
        return {"ok": False, "state": "NOT_APPROVED", "detail": "Updating a HubSpot company requires explicit approval."}
    return _item_result(_request("PATCH", f"/crm/v3/objects/companies/{company_id}", json={"properties": properties}))


def upsert_company(name: str, properties: dict[str, Any], approved: bool = False) -> dict[str, Any]:
    """Idempotent create-or-update by name: searches for an existing
    company with this name first; updates it if found, creates a new one
    (with name included) if not."""
    if not approved:
        return {"ok": False, "state": "NOT_APPROVED", "detail": "Writing a HubSpot company requires explicit approval."}
    if not name:
        return {"ok": False, "state": "ERROR", "detail": "name is required for upsert_company."}
    found = search_companies(name, property_name="name", limit=1)
    if not found["ok"]:
        return {"ok": False, "state": found["state"], "detail": found.get("detail")}
    matches = found["results"]
    if matches:
        company_id = matches[0].get("id")
        result = update_company(company_id, properties, approved=True)
        if result["ok"]:
            result["action"] = "updated"
        return result
    full_properties = {**properties, "name": name}
    result = create_company(full_properties, approved=True)
    if result["ok"]:
        result["action"] = "created"
    return result


# ── Associations ──────────────────────────────────────────────────
# HubSpot's v4 default-association-type endpoint — same shape
# attach_file_note() already uses for note<->contact, generalized so any
# two CRM object types can be linked (contact<->company, deal<->contact,
# deal<->company, task<->contact, task<->deal). "default" association
# type works for every standard object pair HubSpot ships; a custom
# pipeline's non-default association types aren't needed here.

def _associate(from_type: str, from_id: str, to_type: str, to_id: str, approved: bool = False) -> dict[str, Any]:
    if not approved:
        return {"ok": False, "state": "NOT_APPROVED", "detail": f"Associating a HubSpot {from_type} with a {to_type} requires explicit approval."}
    result = _request("PUT", f"/crm/v4/objects/{from_type}/{from_id}/associations/default/{to_type}/{to_id}")
    if not result["ok"]:
        return {"ok": False, "state": result["state"], "detail": result.get("detail")}
    return {"ok": True, "state": "OK"}


def associate_contact_with_company(contact_id: str, company_id: str, approved: bool = False) -> dict[str, Any]:
    return _associate("contacts", contact_id, "companies", company_id, approved=approved)


# ── Deals (recruiting opportunities) ────────────────────────

def get_deal(deal_id: str) -> dict[str, Any]:
    return _item_result(_request("GET", f"/crm/v3/objects/deals/{deal_id}"))


def create_deal(properties: dict[str, Any], approved: bool = False) -> dict[str, Any]:
    """Creates a HubSpot deal — the recruiting-opportunity record for an
    employer identified as needing recruiting help (Section SIXTH: JOB ->
    EMPLOYER -> ... -> RECRUITING OPPORTUNITY -> HUBSPOT). Same
    approval-gated, never-fabricated contract as create_contact/
    create_company: every property persisted is exactly what the caller
    passed, nothing invented (a real dealname/pipeline/stage/amount must
    come from the caller, not a guess)."""
    if not approved:
        return {"ok": False, "state": "NOT_APPROVED", "detail": "Creating a HubSpot deal requires explicit approval."}
    return _item_result(_request("POST", "/crm/v3/objects/deals", json={"properties": properties}))


def update_deal(deal_id: str, properties: dict[str, Any], approved: bool = False) -> dict[str, Any]:
    if not approved:
        return {"ok": False, "state": "NOT_APPROVED", "detail": "Updating a HubSpot deal requires explicit approval."}
    return _item_result(_request("PATCH", f"/crm/v3/objects/deals/{deal_id}", json={"properties": properties}))


def associate_deal_with_company(deal_id: str, company_id: str, approved: bool = False) -> dict[str, Any]:
    return _associate("deals", deal_id, "companies", company_id, approved=approved)


def associate_deal_with_contact(deal_id: str, contact_id: str, approved: bool = False) -> dict[str, Any]:
    return _associate("deals", deal_id, "contacts", contact_id, approved=approved)


# ── Tasks (follow-ups) ───────────────────────────────────────

def get_task(task_id: str) -> dict[str, Any]:
    return _item_result(_request("GET", f"/crm/v3/objects/tasks/{task_id}"))


def create_task(properties: dict[str, Any], approved: bool = False) -> dict[str, Any]:
    """Creates a HubSpot follow-up task. Expected properties (all real,
    caller-supplied — e.g. hs_task_subject, hs_task_body, hs_timestamp,
    hs_task_status, hs_task_priority) match HubSpot's own task property
    names; nothing here renames or reinterprets them."""
    if not approved:
        return {"ok": False, "state": "NOT_APPROVED", "detail": "Creating a HubSpot task requires explicit approval."}
    return _item_result(_request("POST", "/crm/v3/objects/tasks", json={"properties": properties}))


def update_task(task_id: str, properties: dict[str, Any], approved: bool = False) -> dict[str, Any]:
    if not approved:
        return {"ok": False, "state": "NOT_APPROVED", "detail": "Updating a HubSpot task requires explicit approval."}
    return _item_result(_request("PATCH", f"/crm/v3/objects/tasks/{task_id}", json={"properties": properties}))


def associate_task_with_contact(task_id: str, contact_id: str, approved: bool = False) -> dict[str, Any]:
    return _associate("tasks", task_id, "contacts", contact_id, approved=approved)


def associate_task_with_deal(task_id: str, deal_id: str, approved: bool = False) -> dict[str, Any]:
    return _associate("tasks", task_id, "deals", deal_id, approved=approved)


# ── Files (resume attachments) ───────────────────────────
# Separate from _request() above because file upload is multipart/form-data,
# not JSON — reusing _request()'s hardcoded Content-Type: application/json
# header would corrupt the multipart boundary. Everything else (token
# lookup, honest NOT_CONFIGURED/ERROR states, never fabricating a result)
# matches that function's contract.

def upload_file(file_bytes: bytes, filename: str, approved: bool = False) -> dict[str, Any]:
    """Uploads a file (e.g. a candidate's resume) to HubSpot's file manager.
    Returns the file's id/url on success — pass that id to
    attach_file_note() to actually associate it with a contact record.
    PRIVATE access (not publicly linkable) since a resume is personal data."""
    if not approved:
        return {"ok": False, "state": "NOT_APPROVED", "detail": "Uploading a file to HubSpot requires explicit approval."}
    token = get_hubspot_token()
    if not token:
        return {"ok": False, "state": "NOT_CONFIGURED", "detail": "HubSpot isn't configured."}
    try:
        resp = requests.post(
            f"{API_BASE}/files/v3/files",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": (filename, file_bytes)},
            data={
                "folderPath": "/candidate-resumes",
                "options": json.dumps({"access": "PRIVATE", "overwrite": False}),
            },
            timeout=30,
        )
    except Exception as exc:
        return {"ok": False, "state": "ERROR", "detail": str(exc)}
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("message", resp.text[:300])
        except Exception:
            detail = resp.text[:300]
        return {"ok": False, "state": "ERROR", "status_code": resp.status_code, "detail": detail}
    data = resp.json()
    return {"ok": True, "state": "OK", "file_id": data.get("id"), "url": data.get("url")}


def attach_file_note(contact_id: str, file_id: str, note_body: str = "Resume received.", approved: bool = False) -> dict[str, Any]:
    """Creates a Note engagement carrying the uploaded file and associates
    it with the contact — HubSpot has no direct "attach this file to this
    contact" call; a note-with-attachment is the standard way a file shows
    up on a contact's timeline in the UI."""
    if not approved:
        return {"ok": False, "state": "NOT_APPROVED", "detail": "Attaching a file to a HubSpot contact requires explicit approval."}
    import time as _time
    note_result = _request("POST", "/crm/v3/objects/notes", json={
        "properties": {
            "hs_note_body": note_body,
            "hs_timestamp": int(_time.time() * 1000),
            "hs_attachment_ids": file_id,
        },
    })
    if not note_result["ok"]:
        return {"ok": False, "state": note_result["state"], "detail": note_result.get("detail")}
    note_id = note_result["data"].get("id")
    assoc_result = _request(
        "PUT", f"/crm/v4/objects/notes/{note_id}/associations/default/contacts/{contact_id}"
    )
    if not assoc_result["ok"]:
        return {
            "ok": False, "state": assoc_result["state"],
            "detail": f"Note {note_id} created but couldn't be linked to contact {contact_id}: {assoc_result.get('detail')}",
            "note_id": note_id,
        }
    return {"ok": True, "state": "OK", "note_id": note_id}
