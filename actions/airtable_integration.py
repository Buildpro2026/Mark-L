"""Airtable integration — generic, schema-agnostic REST client.

Config lives in config/api_keys.json under "airtable_token" (a Personal
Access Token, scoped to specific bases/scopes at creation time in
Airtable's own UI — never a full-account token; see docs/AIRTABLE_VERIFICATION.md).
Never hardcoded — api_keys.json is gitignored and read at call time,
matching the pattern used by hubspot_integration.py / twilio_integration.py
/ buffer_integration.py.

This module makes NO assumptions about base ID, table names, or field
names — every function takes them as explicit parameters, since an
Airtable base is an entirely user-defined schema this codebase has no way
to know in advance. Writes (create_record/update_record) require
approved=True, the same gate gmail_integration.send_email() and
calendar_integration.create_event() use.

Uses Airtable's Web API (api.airtable.com/v0) with Bearer auth. Never
fabricates data — every function either returns a real API result or an
honest NOT_CONFIGURED / ERROR state. Never logs the token or record field
contents — only status codes and Airtable's own error messages.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"

API_BASE = "https://api.airtable.com/v0"


def get_airtable_token() -> str | None:
    from core.headless import config as _hc
    if _hc.AIRTABLE_TOKEN:
        return _hc.AIRTABLE_TOKEN
    if not CONFIG_PATH.exists():
        return None
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    token = str(data.get("airtable_token") or "").strip()
    return token or None


def is_configured() -> bool:
    return bool(get_airtable_token())


def get_status() -> dict[str, Any]:
    configured = is_configured()
    return {
        "configured": configured,
        "state": "CONFIGURED" if configured else "NOT_CONFIGURED",
        "detail": "Airtable token present." if configured else "No airtable_token in config/api_keys.json.",
    }


def _request(method: str, base_id: str, table_name: str, *, record_id: str = "", **kwargs: Any) -> dict[str, Any]:
    """Shared request wrapper — never raises, never logs the token or
    record contents (only status codes / Airtable's own error text),
    always returns a uniform dict so callers/tests can branch on state."""
    token = get_airtable_token()
    if not token:
        return {"ok": False, "state": "NOT_CONFIGURED", "detail": "Airtable isn't configured."}
    if not base_id or not table_name:
        return {"ok": False, "state": "ERROR", "detail": "base_id and table_name are both required."}
    path = f"/{quote(base_id, safe='')}/{quote(table_name, safe='')}"
    if record_id:
        path += f"/{quote(record_id, safe='')}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        resp = requests.request(method, f"{API_BASE}{path}", headers=headers, timeout=15, **kwargs)
    except Exception as exc:
        return {"ok": False, "state": "ERROR", "detail": str(exc)}
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("error", {}).get("message", resp.text[:300])
        except Exception:
            detail = resp.text[:300]
        return {"ok": False, "state": "ERROR", "status_code": resp.status_code, "detail": detail}
    try:
        data = resp.json()
    except Exception:
        data = {}
    return {"ok": True, "state": "OK", "status_code": resp.status_code, "data": data}


def list_records(
    base_id: str, table_name: str, max_records: int = 25,
    view: str = "", filter_by_formula: str = "",
) -> dict[str, Any]:
    """Read-only: lists records from any base/table you specify. Never
    fabricates results — an auth/API/config failure returns ok=False,
    not fake rows."""
    params: dict[str, Any] = {"maxRecords": max_records}
    if view:
        params["view"] = view
    if filter_by_formula:
        params["filterByFormula"] = filter_by_formula
    result = _request("GET", base_id, table_name, params=params)
    if not result["ok"]:
        return {"ok": False, "state": result["state"], "detail": result.get("detail"), "records": []}
    return {"ok": True, "state": "OK", "records": result["data"].get("records", [])}


def get_record(base_id: str, table_name: str, record_id: str) -> dict[str, Any]:
    """Read-only: a single record by id."""
    result = _request("GET", base_id, table_name, record_id=record_id)
    if not result["ok"]:
        return {"ok": False, "state": result["state"], "detail": result.get("detail")}
    return {"ok": True, "state": "OK", "record": result["data"]}


def create_record(base_id: str, table_name: str, fields: dict[str, Any], approved: bool = False) -> dict[str, Any]:
    """Refuses to create anything unless approved=True. `fields` is passed
    through to Airtable exactly as given — this function makes no
    assumptions about what fields exist in the caller's table; an unknown
    field name is Airtable's own 422 error to report, not something this
    function should guess or invent around."""
    if not approved:
        return {"ok": False, "state": "NOT_APPROVED", "detail": "Creating an Airtable record requires explicit approval."}
    if not isinstance(fields, dict) or not fields:
        return {"ok": False, "state": "ERROR", "detail": "fields must be a non-empty dict."}
    result = _request("POST", base_id, table_name, json={"records": [{"fields": fields}]})
    if not result["ok"]:
        return {"ok": False, "state": result["state"], "detail": result.get("detail")}
    created = result["data"].get("records", [])
    return {"ok": True, "state": "OK", "record": created[0] if created else None}


def update_record(
    base_id: str, table_name: str, record_id: str, fields: dict[str, Any], approved: bool = False,
) -> dict[str, Any]:
    """Refuses to update anything unless approved=True. Partial update —
    Airtable's PATCH semantics mean only the fields provided are touched,
    everything else on the record is left alone."""
    if not approved:
        return {"ok": False, "state": "NOT_APPROVED", "detail": "Updating an Airtable record requires explicit approval."}
    if not isinstance(fields, dict) or not fields:
        return {"ok": False, "state": "ERROR", "detail": "fields must be a non-empty dict."}
    result = _request("PATCH", base_id, table_name, record_id=record_id, json={"fields": fields})
    if not result["ok"]:
        return {"ok": False, "state": result["state"], "detail": result.get("detail")}
    return {"ok": True, "state": "OK", "record": result["data"]}
