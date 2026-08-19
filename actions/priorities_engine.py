"""Phase 4 — the executive "Today's Priorities" and "Active Agents"
views. Both are synthesis layers over data that already exists
elsewhere (agent_orchestrator, executive_brief's risk detection,
buildpro_intelligence, buildpro_data) — nothing here invents a new
data source or fabricates urgency. A priority only appears here because
something real (a stalled approval, a flagged risk, a stale follow-up,
an existing recommendation) produced it.
"""
from __future__ import annotations

import time
from typing import Any

from datetime import datetime, timezone

from actions.agent_orchestrator import orchestrator as agent_orchestrator
from actions import buildpro_data
from actions import buildpro_intelligence
from actions import calendar_integration
from actions import executive_brief
from actions import google_auth

# How stale an agent's last activity can be and still count as "active"
# for the executive view — matches the agent scheduler's own 5-minute
# poll interval with generous headroom, not an arbitrary number.
_ACTIVE_WINDOW_SECS = 3600

# Alert sensitivity (Phase 4 Part 11) -> minimum severity that actually
# surfaces. This is the one thing that makes the setting real rather
# than stored-and-ignored: "quiet" genuinely hides everything but a
# real risk, "high_alert" genuinely surfaces standing recommendations
# that "normal" would leave for the user to check on their own.
ALERT_SENSITIVITY_MIN_SEVERITY = {
    "quiet": 4,        # risks only
    "normal": 2,        # risks, approvals, follow-ups
    "high_alert": 1,    # everything, including standing recommendations
}


def get_todays_priorities(limit: int = 8, min_severity: int = 1) -> list[dict[str, Any]]:
    """Merges every real "you should look at this" signal into one
    ranked list. Severity order: risks (something may already be going
    wrong) > pending approvals (blocked on Lee specifically) > stale
    follow-ups (a relationship going cold) > standing recommendations.
    Within a tier, more recent/older-waiting items sort first."""
    items: list[dict[str, Any]] = []

    for risk in executive_brief._operational_risks():
        items.append({
            "kind": "risk",
            "severity": 4,
            "title": risk["detail"],
            "source": risk["kind"],
        })

    for task in agent_orchestrator.list_tasks():
        if task.status.value == "pending_approval":
            waited_hours = round((time.time() - task.updated_ts) / 3600, 1)
            items.append({
                "kind": "approval",
                "severity": 3,
                "title": f"{task.agent_id}: {task.description}",
                "source": "pending_approval",
                "waited_hours": waited_hours,
                "task_id": task.id,
            })

    try:
        stale_clients = buildpro_data.list_clients_needing_followup(limit=5)
    except Exception:
        stale_clients = []
    for client in stale_clients:
        items.append({
            "kind": "followup",
            "severity": 2,
            "title": f"No contact with {client.get('name', 'a client')} in a while",
            "source": "buildpro_followup",
        })

    try:
        recs = buildpro_intelligence.generate_morning_report_data().get("recommended_actions", [])
    except Exception:
        recs = []
    for rec in recs:
        # buildpro_intelligence's own "all clear" fallback ("No urgent
        # recruiting follow-ups identified.") is a legitimate honest
        # signal in a full brief, but it isn't an actionable item — this
        # list should never contain "there's nothing to do" as an entry.
        if rec.startswith("No urgent"):
            continue
        items.append({"kind": "recommendation", "severity": 1, "title": rec, "source": "buildpro"})

    items = [i for i in items if i["severity"] >= min_severity]
    items.sort(key=lambda i: (-i["severity"], -i.get("waited_hours", 0)))
    return items[:limit]


def _parse_event_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def get_calendar_snapshot(max_results: int = 10) -> dict[str, Any]:
    """Today's schedule, the next appointment, and any genuine scheduling
    conflict (two events whose real start/end times overlap). Does not
    estimate travel time or invent preparation needs — no location/maps
    capability exists to back that claim, so it isn't shown rather than
    guessed at."""
    status = google_auth.get_credential_status()
    if not status.get("authorized"):
        return {"available": False, "reason": "Calendar not authorized", "events": [], "next_event": None, "conflicts": []}

    result = calendar_integration.list_upcoming_events(max_results)
    if not result["ok"]:
        return {"available": False, "reason": result.get("detail"), "events": [], "next_event": None, "conflicts": []}

    events = result["events"]
    today = datetime.now(timezone.utc).date()
    todays_events = []
    for e in events:
        start = _parse_event_dt(e.get("start"))
        if start and start.date() == today:
            todays_events.append({**e, "_start": start, "_end": _parse_event_dt(e.get("end"))})

    conflicts = []
    for i, a in enumerate(todays_events):
        for b in todays_events[i + 1:]:
            if a["_start"] and a["_end"] and b["_start"] and b["_end"] and a["_start"] < b["_end"] and b["_start"] < a["_end"]:
                conflicts.append({"a": a["summary"], "b": b["summary"]})

    for e in todays_events:
        e.pop("_start", None)
        e.pop("_end", None)

    return {
        "available": True,
        "events": todays_events,
        "next_event": events[0] if events else None,
        "conflicts": conflicts,
    }


def get_active_agents_summary() -> list[dict[str, Any]]:
    """Executive view, not the full roster — only agents genuinely doing
    something right now: RUNNING status, a task waiting on approval, or
    activity within the last hour. An idle, never-run agent doesn't
    appear here even though it's registered; see /ui/api/agents (or the
    Agents tab) for the complete list."""
    now = time.time()
    all_tasks = agent_orchestrator.list_tasks()
    result = []
    for agent in agent_orchestrator.list_agents():
        agent_tasks = [t for t in all_tasks if t.agent_id == agent.id]
        pending = [t for t in agent_tasks if t.status.value == "pending_approval"]
        running = agent.status.value == "running"
        recently_ran = agent.last_run_ts is not None and (now - agent.last_run_ts) < _ACTIVE_WINDOW_SECS

        if not (running or pending or recently_ran):
            continue

        if running:
            what = "Running now"
        elif pending:
            what = f"{len(pending)} task(s) waiting on your approval"
        else:
            minutes_ago = round((now - agent.last_run_ts) / 60)
            what = f"Last ran {minutes_ago} min ago" + (f" — error: {agent.last_error}" if agent.last_error else "")

        result.append({
            "agent_id": agent.id,
            "name": agent.name,
            "what": what,
            "needs_attention": bool(pending) or bool(agent.last_error),
            "permission_level": agent.permission_level.value,
        })

    result.sort(key=lambda a: (not a["needs_attention"], a["name"]))
    return result
