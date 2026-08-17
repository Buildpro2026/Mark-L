"""Status/activity-feed/brief API (J3 Parts 15, 16, 18, 19) — the delivery
mechanism the J2 report flagged as missing: the background worker recorded
proactive/monitor activity but nothing surfaced it anywhere a human could
actually see it. This is that surface: what JARVIS is doing, what
happened, what needs approval, and what it recommends — all behind
require_auth, all read-only.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from actions.agent_orchestrator import orchestrator as agent_orchestrator
from actions import audit_log
from actions import business_intelligence as biz_intel
from actions import proactive as proactive_module
from actions import background_monitor
from actions import google_auth
from actions import airtable_integration
from actions import hubspot_integration
from actions import buffer_integration
from actions import twilio_integration
from core.headless.auth import require_auth
from core.headless.obsidian import ObsidianVault

router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("/status")
def status():
    """Consolidated command-center view (Part 19): agent/task state,
    integrations status, memory/knowledge status — one call, not five."""
    google_status = google_auth.get_credential_status()
    vault = ObsidianVault()
    return {
        "orchestrator": agent_orchestrator.summary(),
        "business_intelligence": biz_intel.summary(),
        "integrations": {
            "gmail_calendar": {"configured": google_status.get("credential_file") == "present", "authorized": google_status.get("authorized", False)},
            "airtable": airtable_integration.get_status(),
            "hubspot": {"configured": hubspot_integration.is_configured()},
            "buffer": buffer_integration.verify_buffer(),
            "twilio": twilio_integration.get_status(),
        },
        "obsidian": vault.status(),
        "monitored_topics": background_monitor.list_monitors(),
    }


@router.get("/activity")
def activity(limit: int = 50):
    """Merged, time-sorted feed (Part 15): agent orchestrator events,
    audit-logged consequential actions, and proactive check-in triggers —
    the single feed 'what happened while I was away' answers from."""
    events = [
        {"ts": e["ts"], "source": "agent", "kind": e["kind"], "message": e["message"], "agent_id": e["agent_id"]}
        for e in [ev.to_public_dict() for ev in agent_orchestrator.list_events(limit=limit)]
    ]
    audited = [
        {"ts": a["ts"], "source": "audit", "kind": a["execution_status"], "message": f"{a['action']} ({a['external_system'] or 'n/a'})", "detail": a}
        for a in audit_log.list_recent(limit=limit)
    ]
    proactive_triggers = [
        {"ts": p["triggered_ts"], "source": "proactive", "kind": "check_in_due", "message": f"Proactive check-in due ({p['focus_area']})"}
        for p in proactive_module.get_recent_triggers(limit=limit)
    ]
    merged = sorted(events + audited + proactive_triggers, key=lambda x: x["ts"], reverse=True)
    return {"activity": merged[:limit]}


@router.get("/brief")
def brief():
    """The executive/morning brief (Part 16) — assembled fresh on every
    call from real, current data (see actions/executive_brief.py); nothing
    cached or stale. Also loggable on a schedule by the background worker
    (see core/headless/background.py) if a future delivery channel wants
    a daily snapshot rather than an on-demand pull."""
    from actions.executive_brief import generate_brief
    return generate_brief()
