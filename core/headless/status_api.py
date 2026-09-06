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


@router.get("/priorities")
def priorities(limit: int = 8):
    """The main-screen "Today's Priorities" list (Phase 4 Part 8) — real
    risks, pending approvals, stale follow-ups, and standing
    recommendations merged into one ranked list, never a raw task dump.
    Filtered by the saved alert_sensitivity preference (Part 11) — this
    is what makes that setting do something real rather than sit
    stored and ignored."""
    from actions.priorities_engine import get_todays_priorities, ALERT_SENSITIVITY_MIN_SEVERITY
    from memory.preferences_manager import get_preference
    sensitivity = get_preference("alert_sensitivity", "normal")
    min_severity = ALERT_SENSITIVITY_MIN_SEVERITY.get(sensitivity, 2)
    return {"priorities": get_todays_priorities(limit=limit, min_severity=min_severity), "alert_sensitivity": sensitivity}


@router.get("/active-agents")
def active_agents():
    """The main-screen "Active Agents" list (Phase 4 Part 9) — only
    agents genuinely doing something right now, not the full 13-agent
    roster. See /api/orchestrator/agents for the complete list."""
    from actions.priorities_engine import get_active_agents_summary
    return {"agents": get_active_agents_summary()}


@router.get("/buildpro-overview")
def buildpro_overview():
    """BuildPro tab data for the /ui console (Phase 4 Part 16). Calls the
    same actions/buildpro_data.py + actions/buildpro_intelligence.py
    functions the 3D command center's /3d/api/module/candidates|clients|
    jobs|matches already use — not a second implementation, just a
    second HTTP surface reachable with the /ui session cookie (the /3d
    API only accepts a bearer header, which a same-origin browser
    session deliberately never has direct access to)."""
    from actions import buildpro_data
    from actions import buildpro_intelligence
    try:
        report = buildpro_intelligence.generate_morning_report_data()
    except Exception as e:
        report = {"recommended_actions": [], "error": str(e)}
    return {
        "candidates": buildpro_data.list_candidates(limit=25),
        "clients": buildpro_data.list_clients(limit=25),
        "jobs": buildpro_data.list_jobs(limit=25),
        "recommended_actions": report.get("recommended_actions", []),
        "last_hubspot_sync": report.get("last_hubspot_sync"),
    }


@router.get("/ddf-overview")
def ddf_overview():
    """DDF tab data for the /ui console (Phase 4 Part 16) — reuses the
    Phase 3 catalog/ranking functions directly, same data the public
    storefront and the 3D command center's "deals" module read from."""
    from actions import daily_deal_finders as ddf
    return {
        "high_ticket_picks": ddf.select_daily_high_ticket_picks(limit=2),
        "todays_deals": ddf.get_todays_deals(limit=10),
        "trending": ddf.get_trending_deals(limit=10),
        "this_weeks_hottest": ddf.get_this_weeks_hottest(limit=10),
        "you_might_have_missed": ddf.get_you_might_have_missed(limit=10),
    }


@router.get("/intelligence")
def intelligence():
    """Intelligence tab data for the /ui console (Phase 4 Part 16) —
    business-intelligence entries and cross-business ranked
    opportunities, both already real, working stores from Phase 1-3."""
    from actions import business_intelligence as biz_intel
    from actions import opportunity_engine as opp_engine
    return {
        "summary": biz_intel.summary(),
        "recent_entries": biz_intel.list_entries(limit=20),
        "opportunities": opp_engine.rank_opportunities(limit=15),
    }


@router.get("/calendar-snapshot")
def calendar_snapshot(max_results: int = 10):
    """The main-screen "Calendar" panel (Phase 4 Part 8) — today's events,
    the next appointment, and any genuine time-overlap conflict. Never
    estimates travel time or preparation needs; no data source backs
    those claims."""
    from actions.priorities_engine import get_calendar_snapshot
    return get_calendar_snapshot(max_results=max_results)


@router.get("/opportunities")
def opportunities(limit: int = 8):
    """The main-screen "Opportunities" list (Phase 4 Part 10) — cross-
    business, ranked by actions/opportunity_engine.py's existing
    weighted score (revenue potential, probability, time-to-revenue,
    cost, risk, alignment), not just the newest entries."""
    from actions import opportunity_engine as opp_engine
    return {"opportunities": opp_engine.rank_opportunities(limit=limit)}
