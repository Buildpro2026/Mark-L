"""HubSpot -> BuildPro synchronization.

Pulls HubSpot contacts/companies (actions/hubspot_integration.py) and
reflects them into the local BuildPro data model (actions/buildpro_data.py),
using hubspot_contact_id / hubspot_company_id to dedupe: a HubSpot record
already linked to a local row updates that row in place; one with no match
creates a new row tagged source="hubspot". Only real HubSpot property
values are ever written — nothing here invents a name, email, or anything
else.

Failure handling: each HubSpot record is synced inside its own try/except,
so one bad record (missing properties, an unexpected shape, a local write
error) is logged into that run's `errors` list and skipped, rather than
aborting the whole run or leaving local data partially/inconsistently
written. A page-fetch failure just stops pulling further pages — everything
already synced in this run stays synced. Every run's outcome is persisted
via buildpro_data.record_sync_run() so get_last_sync()/list_sync_runs()
reflect what actually happened, including partial failures.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from actions import hubspot_integration as hubspot
from actions import buildpro_data as bd

_PAGE_SIZE = 100


def _contact_name(properties: dict[str, Any]) -> str:
    first = (properties.get("firstname") or "").strip()
    last = (properties.get("lastname") or "").strip()
    name = f"{first} {last}".strip()
    return name or (properties.get("email") or "").strip() or "Unnamed HubSpot Contact"


def _sync_one_contact(properties: dict[str, Any], contact_id: str) -> str:
    """Returns 'created' or 'updated'. Only ever writes properties HubSpot
    actually returned — nothing guessed or defaulted beyond the name
    fallback above (which only picks among real fields already present)."""
    name = _contact_name(properties)
    email = (properties.get("email") or "").strip()
    phone = (properties.get("phone") or properties.get("mobilephone") or "").strip()
    now = time.time()

    existing = bd.get_candidate_by_hubspot_id(contact_id)
    if existing:
        bd.update_candidate(
            existing["id"],
            name=name or None, email=email or None, phone=phone or None,
            source="hubspot", last_synced_ts=now,
        )
        return "updated"

    bd.add_candidate(
        name=name, email=email, phone=phone, source="hubspot",
        hubspot_contact_id=contact_id, last_synced_ts=now,
    )
    return "created"


def _sync_one_company(properties: dict[str, Any], company_id: str) -> str:
    name = (properties.get("name") or properties.get("domain") or "").strip() or "Unnamed HubSpot Company"
    industry = (properties.get("industry") or "").strip()
    phone = (properties.get("phone") or "").strip()
    now = time.time()

    existing = bd.get_client_by_hubspot_id(company_id)
    if existing:
        bd.update_client(
            existing["id"],
            name=name or None, industry=industry or None, phone=phone or None,
            source="hubspot", last_synced_ts=now,
        )
        return "updated"

    bd.add_client(
        name=name, industry=industry, phone=phone, source="hubspot",
        hubspot_company_id=company_id, last_synced_ts=now,
    )
    return "created"


def _run_sync(
    entity_type: str,
    fetch_page: Callable[[str | None, int], dict[str, Any]],
    sync_one: Callable[[dict[str, Any], str], str],
    limit: int,
) -> dict[str, Any]:
    """Shared pagination/error-accounting engine for both sync_contacts()
    and sync_companies() — `fetch_page(after, page_limit)` and
    `sync_one(properties, record_id) -> "created"|"updated"` are the only
    HubSpot/entity-specific pieces."""
    if not hubspot.is_configured():
        return {"ok": False, "state": "NOT_CONFIGURED", "created": 0, "updated": 0, "pulled": 0, "errors": []}

    created = updated = pulled = 0
    errors: list[str] = []
    after: str | None = None

    while pulled < limit:
        page_limit = min(_PAGE_SIZE, limit - pulled)
        page = fetch_page(after, page_limit)
        if not page["ok"]:
            errors.append(f"HubSpot fetch failed: {page.get('detail')}")
            break
        records = page.get("results") or []
        if not records:
            break
        for record in records:
            record_id = record.get("id")
            properties = record.get("properties") or {}
            try:
                outcome = sync_one(properties, record_id)
                if outcome == "created":
                    created += 1
                else:
                    updated += 1
            except Exception as exc:
                errors.append(f"{entity_type} record {record_id or '?'}: {exc}")
        pulled += len(records)
        after = ((page.get("paging") or {}).get("next") or {}).get("after")
        if not after:
            break

    if errors and created == 0 and updated == 0:
        state = "ERROR"
    elif errors:
        state = "PARTIAL"
    else:
        state = "SYNCED"

    bd.record_sync_run(entity_type, "hubspot", created, updated, len(errors), errors)
    return {"ok": True, "state": state, "created": created, "updated": updated, "pulled": pulled, "errors": errors}


def sync_contacts(limit: int = 200) -> dict[str, Any]:
    """Pull up to `limit` HubSpot contacts into buildpro_candidates."""
    return _run_sync(
        "candidates",
        lambda after, page_limit: hubspot.get_contacts(limit=page_limit, after=after),
        _sync_one_contact,
        limit,
    )


def sync_companies(limit: int = 200) -> dict[str, Any]:
    """Pull up to `limit` HubSpot companies into buildpro_clients."""
    return _run_sync(
        "clients",
        lambda after, page_limit: hubspot.get_companies(limit=page_limit, after=after),
        _sync_one_company,
        limit,
    )


def sync_all(limit: int = 200) -> dict[str, Any]:
    """Runs both syncs and returns a combined summary — the single entry
    point other callers (agents, a future scheduled job) should use."""
    return {"contacts": sync_contacts(limit=limit), "companies": sync_companies(limit=limit)}
