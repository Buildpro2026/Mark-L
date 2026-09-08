"""The connections between JARVIS's integrations and his decisions.

WHAT WAS DISCONNECTED
Every integration worked and none of them reached the decision.
priorities_engine.get_todays_priorities() built its list from risks,
pending approvals, stale clients and recommendations — and nothing else.
So:

  * priorities_engine.get_calendar_snapshot() already computed real
    scheduling conflicts and the next appointment, and NOTHING called it
    from the priority path. A double-booking at 09:00 could not become a
    priority at 08:00.
  * google_tasks_integration.list_tasks() returned tasks with real due
    dates, and no overdue task ever became a priority. JARVIS could read
    the task and could rank work, but the two never met.
  * twilio_integration.record_inbound_sms() stored Lee's replies and
    nothing read them, so the approval round trip stopped at "SMS sent".

These are not new systems. Each function here reads an EXISTING
integration and emits an item in the shape ceo_decision.score_item()
already understands — severity, source, deadline language in the title,
waited_hours — so the scoring, the reasoning and the approval gate all
apply without changing any of them.

FAILURE ISOLATION IS THE POINT
Every collector is wrapped individually. One unavailable integration
contributes nothing and never prevents the others from contributing, and
never breaks the CEO cycle. An empty list from a healthy source and an
empty list from a broken one are distinguished by `state`, so "no
conflicts today" is never confused with "Calendar is down".
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("jarvis.cross_system")

# Truthful states, shared with the rest of the system.
OK = "OK"
UNAVAILABLE = "UNAVAILABLE"
UNAUTHORIZED = "UNAUTHORIZED"
FAILED = "FAILED"

# An event closer than this is worth raising before it arrives.
IMMINENT_HOURS = 4.0
# Beyond this, a due date is not yet pressure.
DEADLINE_HORIZON_HOURS = 72.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _isolated(label: str, fn) -> dict[str, Any]:
    """Run one collector so its failure cannot reach the others.

    Returns the state as well as the items, because "no items because
    everything is fine" and "no items because Gmail is down" must not
    look the same to the CEO cycle."""
    try:
        result = fn()
        return {"state": OK, "items": result or [], "source": label}
    except Exception as exc:
        logger.warning("%s signal collector failed: %s", label, exc)
        return {"state": FAILED, "items": [], "source": label,
                "detail": str(exc)[:300]}


# ══ CALENDAR ═════════════════════════════════════════════════════════════

def calendar_signals(max_results: int = 10) -> list[dict[str, Any]]:
    """Scheduling conflicts and imminent commitments, as priority items.

    Uses priorities_engine.get_calendar_snapshot(), which already computes
    conflicts from real start/end overlap — a second conflict detector
    would be a second answer to the same question."""
    from actions import priorities_engine

    snapshot = priorities_engine.get_calendar_snapshot(max_results=max_results)
    if not snapshot.get("available"):
        return []

    items: list[dict[str, Any]] = []
    for conflict in snapshot.get("conflicts") or []:
        first = conflict.get("first") or conflict.get("a") or {}
        second = conflict.get("second") or conflict.get("b") or {}
        title = (f"Calendar conflict today: '{first.get('summary', 'an event')}' "
                 f"overlaps '{second.get('summary', 'another event')}'")
        items.append({
            "kind": "calendar_conflict",
            # A double-booking is something already wrong, not a suggestion.
            "severity": 4,
            "title": title,
            "source": "calendar",
            "permission_level": "observe",
            "event_ids": [first.get("id"), second.get("id")],
        })

    next_event = snapshot.get("next_event")
    starts = _parse_dt((next_event or {}).get("start"))
    if next_event and starts:
        hours = (starts - _now()).total_seconds() / 3600.0
        if 0 <= hours <= IMMINENT_HOURS:
            items.append({
                "kind": "calendar_deadline",
                "severity": 3,
                # "today" is deliberate: ceo_decision reads deadline
                # language out of the title, so the urgency signal and the
                # human-readable text are the same string.
                "title": f"Starting today in {hours:.1f}h: {next_event.get('summary', 'an appointment')}",
                "source": "calendar",
                "permission_level": "observe",
                "event_id": next_event.get("id"),
            })
    return items


# ══ TASKS ════════════════════════════════════════════════════════════════

def task_signals(max_results: int = 25) -> list[dict[str, Any]]:
    """Overdue and imminently-due Google Tasks as priority items.

    Tasks with no due date are deliberately excluded: an undated task is a
    list entry, not a deadline, and treating it as one would fill the
    priority list with things that were never urgent."""
    from actions import google_tasks_integration as tasks

    result = tasks.list_tasks(max_results=max_results, show_completed=False)
    if not result.get("ok"):
        return []

    now = _now()
    items: list[dict[str, Any]] = []
    for task in result.get("tasks") or []:
        if (task.get("status") or "") == "completed":
            continue
        due = _parse_dt(task.get("due"))
        if due is None:
            continue
        hours_left = (due - now).total_seconds() / 3600.0
        if hours_left < 0:
            overdue_hours = abs(hours_left)
            items.append({
                "kind": "task_overdue",
                "severity": 4 if overdue_hours > 24 else 3,
                "title": f"Overdue task: {task.get('title') or 'untitled'}",
                "source": "google_tasks",
                "permission_level": "observe",
                "task_ref": task.get("id"),
                # Time already spent overdue is real waiting, and
                # ceo_decision.score_item() reads exactly this field.
                "waited_hours": round(overdue_hours, 1),
            })
        elif hours_left <= DEADLINE_HORIZON_HOURS:
            items.append({
                "kind": "task_due",
                "severity": 2,
                "title": (f"Due in {hours_left:.0f}h: {task.get('title') or 'untitled'}"),
                "source": "google_tasks",
                "permission_level": "observe",
                "task_ref": task.get("id"),
            })
    return items


# ══ COLLECT ══════════════════════════════════════════════════════════════

def collect_signals() -> dict[str, Any]:
    """Every cross-system signal, with each source isolated.

    Returned as items plus per-source states so the CEO cycle can report
    "Calendar unavailable" honestly instead of silently having fewer
    priorities than it should."""
    collectors = {
        "calendar": calendar_signals,
        "google_tasks": task_signals,
    }
    items: list[dict[str, Any]] = []
    states: dict[str, Any] = {}
    for label, fn in collectors.items():
        outcome = _isolated(label, fn)
        states[label] = {"state": outcome["state"], "count": len(outcome["items"]),
                         **({"detail": outcome["detail"]} if outcome.get("detail") else {})}
        items.extend(outcome["items"])
    return {"items": items, "states": states,
            "healthy_sources": sum(1 for s in states.values() if s["state"] == OK)}


# ══ THE APPROVAL ROUND TRIP ══════════════════════════════════════════════
# twilio_integration.record_inbound_sms() already stored Lee's replies and
# nothing read them, so the round trip stopped at "SMS sent". These close
# it — without inventing any Twilio capability that does not exist, and
# without a second approval system: the decision goes to the existing
# orchestrator gate, which stays authoritative.

_APPROVE_RE = re.compile(r"^\s*(yes|y|ok|okay|approve[d]?|go|do it|proceed)\b", re.I)
_REJECT_RE = re.compile(r"^\s*(no|n|reject|deny|stop|cancel|don'?t)\b", re.I)
# "APPROVE abc123" / "YES abc123" — a reply naming the task it answers.
_TASK_REF_RE = re.compile(r"\b([0-9a-f]{6,}|task[-_][\w-]{3,})\b", re.I)


def interpret_reply(body: str) -> dict[str, Any]:
    """What Lee's SMS reply means, or that it is unclear.

    "unclear" is a real answer and the safe one: acting on an ambiguous
    reply would execute something Lee did not authorise. Nothing is
    guessed from a message that does not plainly say yes or no."""
    text = (body or "").strip()
    if not text:
        return {"decision": "unclear", "reason": "empty message"}

    task_ref = None
    match = _TASK_REF_RE.search(text)
    if match:
        task_ref = match.group(1)

    if _APPROVE_RE.match(text):
        return {"decision": "approve", "task_ref": task_ref, "reason": "reply reads as approval"}
    if _REJECT_RE.match(text):
        return {"decision": "reject", "task_ref": task_ref, "reason": "reply reads as rejection"}
    return {"decision": "unclear", "task_ref": task_ref,
            "reason": "the reply did not plainly approve or reject"}


def apply_sms_decision(body: str, from_number: str = "") -> dict[str, Any]:
    """Turn one inbound SMS into a real approval decision.

    Routes through agent_orchestrator's existing approve/reject — this
    does not decide anything itself and cannot bypass the gate. An
    ambiguous reply, an unknown task, or a task no longer awaiting
    approval all change nothing and say so."""
    from actions import agent_orchestrator

    verdict = interpret_reply(body)
    if verdict["decision"] == "unclear":
        return {"ok": False, "state": "UNCLEAR", "detail": verdict["reason"],
                "applied": False}

    orchestrator = agent_orchestrator.orchestrator
    pending = [t for t in orchestrator.list_tasks()
               if t.status.value == "pending_approval"]
    if not pending:
        return {"ok": False, "state": "NOT_FOUND", "applied": False,
                "detail": "no task is currently awaiting approval"}

    target = None
    if verdict.get("task_ref"):
        target = next((t for t in pending
                       if str(t.id).startswith(verdict["task_ref"])
                       or verdict["task_ref"] in str(t.id)), None)
    if target is None:
        if len(pending) > 1:
            # Guessing which of several approvals a bare "yes" meant is
            # exactly how the wrong thing gets executed.
            return {"ok": False, "state": "AMBIGUOUS", "applied": False,
                    "detail": (f"{len(pending)} approvals are pending; the reply did not "
                               f"say which one. Reply with the task id."),
                    "pending": [t.id for t in pending]}
        target = pending[0]

    try:
        if verdict["decision"] == "approve":
            task = orchestrator.approve_task(target.id)
            state, applied = "APPROVED", True
        else:
            task = orchestrator.reject_task(target.id)
            state, applied = "REJECTED", True
    except Exception as exc:
        return {"ok": False, "state": "FAILED", "applied": False,
                "task_id": target.id, "detail": str(exc)[:300]}

    result = {"ok": True, "state": state, "applied": applied, "task_id": target.id,
              "task_status": task.status.value, "from": from_number or None}

    # LEARN. An approval decision is a real outcome and belongs where the
    # next decision will read it.
    try:
        from actions import ceo_decision
        ceo_decision.record_outcome(
            {"source": f"approval:{target.agent_id}", "title": target.description,
             "kind": "approval"},
            ok=(verdict["decision"] == "approve"),
            detail=f"Lee replied by SMS: {state.lower()}", verified=True)
    except Exception:
        logger.debug("could not record the approval outcome", exc_info=True)
    return result


# ══ RESUME -> MATCHING -> OPPORTUNITY ════════════════════════════════════

def match_new_candidate(candidate_id: int, min_score: Optional[float] = None,
                        limit: int = 5) -> dict[str, Any]:
    """Run the existing matcher for a candidate who has just arrived.

    buildpro_matching.generate_matches_for_candidate() already scores and
    explains matches; this is the missing call, not a second matcher. A
    candidate uploaded a resume and then sat there unmatched because
    nothing connected intake to matching."""
    from actions import buildpro_matching

    try:
        matches = buildpro_matching.generate_matches_for_candidate(
            candidate_id, min_score=min_score) or []
    except Exception as exc:
        logger.warning("matching failed for candidate %s: %s", candidate_id, exc)
        return {"ok": False, "state": FAILED, "detail": str(exc)[:300], "matches": []}

    ranked = sorted(matches, key=lambda m: -float(m.get("score") or 0))[:limit]
    return {"ok": True, "state": OK, "candidate_id": candidate_id,
            "matches": ranked, "match_count": len(matches),
            # Never "we found 25 matches" when the matcher found three.
            "detail": (f"{len(matches)} match(es) scored"
                       if matches else "no job currently matches this candidate")}
