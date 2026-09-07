"""One front door for everything JARVIS tells Lee.

This is a ROUTER, not a second notification system. Delivery still happens
exactly where it always did — actions/approval_notifier.py's
notify_urgent_event(), which owns the Twilio call, the per-event dedup
table, and the audit-log write. None of that is reimplemented, and the
Twilio credential architecture is untouched.

What was missing is a level above it. Callers were choosing `level=2` or
`level=3` inline, which meant the SEVERITY of a message was decided at each
call site by whoever wrote it, with no shared notion of what an
informational note is versus an urgent alert versus an approval request.
The result is predictable: everything drifts toward the level that gets
attention, and the channel stops meaning anything.

Here the message TYPE is what a caller states, and the type determines the
level. Adding a type is a deliberate edit in one place.

Two rules the router enforces that individual call sites kept getting wrong:

  * A failed delivery is reported, never retried here. Re-sending an SMS is
    a real-world side effect; the transport already deduplicates by event
    id, and a retry loop above it would defeat that. Persistent failures are
    the self-healing layer's job, through escalation, not this module's.
  * A delivery failure NEVER changes the outcome of the work that produced
    the message. Every function returns a result dict; none raise.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("jarvis.notifications")

# Message types, ordered by the attention they demand.
INFO = "info"                  # something happened; no action needed
BUSINESS_ALERT = "business"    # a real business signal worth reading today
APPROVAL_REQUEST = "approval"  # JARVIS wants permission to act
URGENT = "urgent"              # needs attention now
FAILURE = "failure"            # a subsystem failed and could not self-recover
DAILY_REPORT = "daily_report"  # the CEO morning brief

# Type -> the notifier's existing 0=log 1=dashboard 2=SMS 3=SMS+call scale.
# INFO stays at 1 on purpose: an informational note belongs on the dashboard,
# and a system that texts about everything gets muted, after which nothing
# reaches Lee at all.
_LEVELS = {
    INFO: 1,
    BUSINESS_ALERT: 2,
    APPROVAL_REQUEST: 2,
    DAILY_REPORT: 2,
    FAILURE: 2,
    URGENT: 3,
}

# Prefixes so a message's kind is legible from the phone's lock screen,
# without opening anything. An approval request must never look like an
# ordinary alert — Lee needs to know a decision is being asked of him.
_PREFIXES = {
    INFO: "",
    BUSINESS_ALERT: "",
    APPROVAL_REQUEST: "APPROVAL NEEDED: ",
    DAILY_REPORT: "",
    FAILURE: "FAILURE: ",
    URGENT: "URGENT: ",
}


def level_for(message_type: str) -> int:
    return _LEVELS.get(message_type, 2)


def notify(message_type: str, event_id: str, title: str, detail: str = "",
           data: Optional[dict] = None, dry_run: bool = False) -> dict[str, Any]:
    """Routes one message. `event_id` is the delivery identity — the
    existing notifier sends each one exactly once, so a caller that
    recomputes the same id for the same real-world event gets deduplication
    for free.

    Returns a result dict describing what happened, always. A transport
    failure comes back as ok=False with a reason; it is never raised, and it
    never means the underlying work failed."""
    from actions import approval_notifier
    from actions import operating_memory

    level = level_for(message_type)
    prefixed = f"{_PREFIXES.get(message_type, '')}{title}"

    try:
        result = approval_notifier.notify_urgent_event(
            event_id=event_id, title=prefixed, detail=detail,
            level=level, dry_run=dry_run,
        )
        ok = result.get("action") in ("sms", "already_sent") and result.get("ok", True) is not False
        outcome = {
            "ok": bool(ok), "message_type": message_type, "level": level,
            "event_id": event_id, "delivery": result,
        }
    except Exception as exc:
        # Deliberately no retry: re-sending is a real side effect, and the
        # transport's own dedup is what makes "exactly once" true.
        logger.warning("notification delivery failed for %s: %s", event_id, exc)
        outcome = {
            "ok": False, "message_type": message_type, "level": level,
            "event_id": event_id, "error": str(exc),
            "delivery": {"action": "failed", "ok": False},
        }

    # Observable either way — a notification that never arrived is exactly
    # the kind of failure that otherwise leaves no trace anywhere.
    operating_memory.record(
        operating_memory.NOTIFICATION, source=message_type,
        subject=event_id, summary=f"{prefixed}: {outcome['delivery'].get('action')}",
        data={"level": level, **({"data": data} if data else {})},
        ok=outcome["ok"],
    )
    return outcome


def info(event_id: str, title: str, detail: str = "", **kw) -> dict[str, Any]:
    return notify(INFO, event_id, title, detail, **kw)


def business_alert(event_id: str, title: str, detail: str = "", **kw) -> dict[str, Any]:
    return notify(BUSINESS_ALERT, event_id, title, detail, **kw)


def urgent(event_id: str, title: str, detail: str = "", **kw) -> dict[str, Any]:
    return notify(URGENT, event_id, title, detail, **kw)


def failure(event_id: str, title: str, detail: str = "", **kw) -> dict[str, Any]:
    return notify(FAILURE, event_id, title, detail, **kw)


def daily_report(event_id: str, title: str, detail: str = "", **kw) -> dict[str, Any]:
    return notify(DAILY_REPORT, event_id, title, detail, **kw)


def approval_request(task_id: str, agent_name: str, what: str,
                     why: str = "", dry_run: bool = False) -> dict[str, Any]:
    """Asks Lee to approve a specific pending task.

    The body carries what JARVIS wants to do, which agent would do it, and
    the task id — enough to decide without opening a dashboard first. An
    approval request that only says "a task needs approval" is not a
    request, it is a notification that a request exists somewhere."""
    detail = f"Agent: {agent_name}\nAction: {what}"
    if why:
        detail += f"\nWhy: {why}"
    detail += f"\nTask: {task_id}\n\nThis will NOT run until you approve it."
    return notify(
        APPROVAL_REQUEST, event_id=f"approval-{task_id}",
        title=f"{agent_name} needs approval", detail=detail, dry_run=dry_run,
    )


def recent_failures(limit: int = 10) -> list[dict[str, Any]]:
    """Notifications that did not get through — for the monitoring sweep and
    the morning brief."""
    from actions import operating_memory
    return [e for e in operating_memory.recall(
        entry_type=operating_memory.NOTIFICATION, limit=limit * 3) if e["ok"] is False][:limit]
