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
from actions import daily_deal_finders as ddf
from actions import buffer_integration

_OVERNIGHT_WINDOW_SECS = 18 * 3600
_STALE_APPROVAL_SECS = 24 * 3600


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


def _operational_risks() -> list[dict[str, Any]]:
    """Real, derived risk signals — never fabricated. Every entry here
    traces back to something already recorded: a stalled approval, an
    agent's own last_error, a recently failed task, or an integration a
    revenue-relevant part of the business actually depends on reporting
    broken. If nothing here fires, the list is honestly empty, not padded
    with a generic 'no risks identified' filler."""
    risks: list[dict[str, Any]] = []
    now = time.time()

    for task in agent_orchestrator.list_tasks():
        if task.status.value == "pending_approval" and (now - task.updated_ts) > _STALE_APPROVAL_SECS:
            age_hours = round((now - task.updated_ts) / 3600, 1)
            risks.append({
                "kind": "stalled_approval",
                "detail": f"Task for agent '{task.agent_id}' has been waiting {age_hours}h for approval.",
                "task_id": task.id,
            })
        if task.status.value == "failed" and (now - task.updated_ts) < _OVERNIGHT_WINDOW_SECS:
            risks.append({
                "kind": "recent_task_failure",
                "detail": f"Agent '{task.agent_id}' task failed: {task.error}",
                "task_id": task.id,
            })

    for agent in agent_orchestrator.list_agents():
        if agent.last_error:
            risks.append({
                "kind": "agent_error_state",
                "detail": f"{agent.name} is in an error state: {agent.last_error}",
                "agent_id": agent.id,
            })

    # Live check, but bounded (buffer_integration._graphql has a 20s
    # timeout) — only Buffer actually offers a way to tell "configured but
    # broken" apart from "not configured" without a network call producing
    # a false negative; Twilio's own get_status() is presence-only by
    # design (see check_connection() for the live variant, deliberately
    # not called here on every brief generation).
    buffer_status = buffer_integration.verify_buffer()
    if buffer_status.get("configured") and buffer_status.get("status", "").startswith("UNAVAILABLE"):
        risks.append({
            "kind": "integration_broken",
            "detail": f"Buffer (social publishing) is configured but not working: {buffer_status.get('status')}.",
        })

    return risks


def _ddf_snapshot(limit: int = 5) -> dict[str, Any]:
    """Real DDF state for the morning brief: today's deliberate high-ticket
    picks and what's currently trending. Empty lists are honest when the
    catalog has nothing yet, not filled in with placeholder products."""
    return {
        "high_ticket_picks": ddf.select_daily_high_ticket_picks(limit=2),
        "trending": ddf.get_trending_deals(limit=limit),
        "todays_deals_count": len(ddf.get_todays_deals(limit=200)),
    }


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

    risks = _operational_risks()
    ddf_snapshot = _ddf_snapshot()

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
    if risks:
        recommended_actions.append(
            f"{len(risks)} operational risk(s) flagged below — worth a look before they compound."
        )
    if ddf_snapshot["high_ticket_picks"]:
        recommended_actions.append(
            f"DDF has {len(ddf_snapshot['high_ticket_picks'])} high-ticket pick(s) ready for today."
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
        "risks": risks,
        "daily_deal_finders": ddf_snapshot,
        "recommended_actions": recommended_actions,
    }
