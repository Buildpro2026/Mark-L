"""Executive/CEO-Chief-of-Staff brief (J3 Part 16) — assembles the morning
report architecture that already existed (buildpro_intelligence.py) plus
everything else Part 16 asks for, and actually connects it to a delivery
mechanism (core/headless/status_api.py's /api/brief) instead of leaving it
as an unused data structure — the exact gap the J1/J2 audits both flagged.

Every section comes from a real data source and is honestly empty rather
than fabricated when there's nothing there: Gmail/Calendar sections report
NOT_AUTHORIZED if OAuth isn't set up rather than pretending to have data;
BuildPro/business-intelligence/opportunity sections just return whatever
is actually in the database, which may be zero rows.
"""
from __future__ import annotations

import time
from typing import Any

from actions import buildpro_intelligence
from actions import business_intelligence as biz_intel
from actions import opportunity_engine as opp_engine
from actions import strategic_objective
from actions.agent_orchestrator import orchestrator as agent_orchestrator
from actions import google_auth
from actions import gmail_integration
from actions import calendar_integration
from actions import audit_log

_OVERNIGHT_WINDOW_SECS = 18 * 3600


def _gmail_snapshot(max_results: int = 5) -> dict[str, Any]:
    status = google_auth.get_credential_status()
    if not status.get("authorized"):
        return {"available": False, "reason": "Gmail not authorized", "messages": []}
    r = gmail_integration.list_messages(query="is:unread", max_results=max_results)
    if not r["ok"]:
        return {"available": False, "reason": r.get("detail"), "messages": []}
    return {
        "available": True,
        "unread_count_shown": len(r["messages"]),
        "messages": [
            {"sender": m.get("sender"), "subject": m.get("subject")} for m in r["messages"]
        ],
    }


def _calendar_snapshot(max_results: int = 5) -> dict[str, Any]:
    status = google_auth.get_credential_status()
    if not status.get("authorized"):
        return {"available": False, "reason": "Calendar not authorized", "events": []}
    r = calendar_integration.list_upcoming_events(max_results)
    if not r["ok"]:
        return {"available": False, "reason": r.get("detail"), "events": []}
    return {
        "available": True,
        "events": [{"summary": e.get("summary"), "start": e.get("start")} for e in r["events"]],
    }


def _pending_approvals() -> list[dict[str, Any]]:
    return [
        t.to_public_dict() for t in agent_orchestrator.list_tasks()
        if t.status.value == "pending_approval"
    ]


def _completed_overnight(window_secs: float = _OVERNIGHT_WINDOW_SECS) -> dict[str, Any]:
    cutoff = time.time() - window_secs
    agent_tasks = [
        t.to_public_dict() for t in agent_orchestrator.list_tasks()
        if t.status.value == "done" and t.updated_ts >= cutoff
    ]
    audit_entries = [a for a in audit_log.list_recent(limit=100) if a["ts"] >= cutoff]
    return {"agent_tasks": agent_tasks, "audited_actions": audit_entries}


def generate_brief() -> dict[str, Any]:
    """The single entry point — real data, honestly labeled where a source
    isn't configured/available. Never sends or publishes anything itself;
    that's the delivery layer's job (core/headless/status_api.py), which
    this module doesn't know about."""
    buildpro = buildpro_intelligence.generate_morning_report_data()
    bi_summary = biz_intel.summary()
    top_quick = opp_engine.rank_opportunities(opp_type="quick_cash", limit=5)
    top_long = opp_engine.rank_opportunities(opp_type="long_term", limit=5)
    objective = strategic_objective.get_objective_status()
    pending_approvals = _pending_approvals()
    overnight = _completed_overnight()

    recommended_actions = list(buildpro["recommended_actions"])
    if pending_approvals:
        recommended_actions.append(
            f"{len(pending_approvals)} agent task(s) are waiting on your approval."
        )
    if not top_quick and not top_long:
        recommended_actions.append(
            "No business opportunities logged yet — consider running the opportunity_scout "
            "or business_research_agent agents."
        )

    return {
        "generated_ts": time.time(),
        "priority_tasks": buildpro["recommended_actions"],
        "buildpro": buildpro,
        "business_opportunities": {"quick_cash": top_quick, "long_term": top_long},
        "business_intelligence_summary": bi_summary,
        "strategic_objective": objective,
        "important_emails": _gmail_snapshot(),
        "calendar": _calendar_snapshot(),
        "pending_approvals": pending_approvals,
        "completed_overnight_work": overnight,
        "recommended_actions": recommended_actions,
    }
