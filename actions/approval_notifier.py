"""Tells Lee when JARVIS is blocked on him, instead of waiting silently.

The orchestrator has always been able to park a task in PENDING_APPROVAL,
and priorities_engine has always surfaced it — but only to someone already
looking at the dashboard. A chief of staff who needs a decision does not
wait for you to walk past his desk. He texts you, and if it is urgent
enough he calls.

What this is careful NOT to do: it never approves anything, never executes
the parked task, and never invents urgency. It reports that a decision is
waiting and what it is about. The approval gate itself is untouched — this
is a notification path, not a new authority.

Deduplicated by task id in the same SQLite database everything else uses,
so a task that sits pending for two days produces one text, not one every
five minutes. Re-notification after ESCALATE_AFTER_HOURS is deliberate and
capped at one escalation per task.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any

logger = logging.getLogger("jarvis.approval_notifier")

# A decision left waiting this long gets one more nudge, escalated to a
# phone call if calling is configured. Once. Then it stays quiet.
ESCALATE_AFTER_HOURS = 4.0


def _connect() -> sqlite3.Connection:
    from core.headless import config
    config.ensure_data_dir()
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS approval_notifications (
            task_id     TEXT PRIMARY KEY,
            first_sent  REAL NOT NULL,
            escalated   INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.commit()
    return conn


def _already_notified(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row | None:
    cur = conn.execute("SELECT * FROM approval_notifications WHERE task_id = ?", (task_id,))
    return cur.fetchone()


def _compose(item: dict[str, Any]) -> str:
    """The message a person can act on from a lock screen: what happened,
    what decision is needed, and how to answer. Vague alerts ("you have a
    notification") are worse than no alert — they cost attention and
    return nothing."""
    title = (item.get("title") or "a task").strip()
    waited = item.get("waited_hours")
    waited_str = f" It's been waiting {waited:.0f}h." if isinstance(waited, (int, float)) and waited >= 1 else ""
    return (
        f"JARVIS: I need your approval before I can continue.\n\n"
        f"{title}{waited_str}\n\n"
        f"Reply APPROVE {item.get('task_id', '')} or DENY {item.get('task_id', '')}, "
        f"or open the console to review it."
    )


def pending_approvals() -> list[dict[str, Any]]:
    """Only the approval items, straight from the existing priorities
    engine — not a second definition of what 'pending' means."""
    from actions.priorities_engine import get_todays_priorities
    try:
        items = get_todays_priorities(limit=25, min_severity=1)
    except Exception:
        logger.exception("could not read priorities for approval notification")
        return []
    return [i for i in items if i.get("kind") == "approval" and i.get("task_id")]


def notify_pending(dry_run: bool = False) -> list[dict[str, Any]]:
    """One pass. Returns what it did, so the background loop can log it
    and tests can assert on it without sending anything."""
    from actions import twilio_integration as twilio
    from actions import cartesia_calls
    from actions import audit_log
    from core.headless import config

    actions_taken: list[dict[str, Any]] = []
    items = pending_approvals()
    if not items:
        return actions_taken

    owner = config.JARVIS_OWNER_PHONE
    if not owner:
        logger.info("%d approval(s) pending but JARVIS_OWNER_PHONE isn't set — not texting anyone.", len(items))
        return actions_taken

    conn = _connect()
    try:
        now = time.time()
        for item in items:
            task_id = str(item["task_id"])
            row = _already_notified(conn, task_id)

            if row is None:
                body = _compose(item)
                if dry_run:
                    actions_taken.append({"task_id": task_id, "action": "sms", "dry_run": True, "body": body})
                    continue
                result = twilio.send_sms(owner, body)
                conn.execute(
                    "INSERT OR REPLACE INTO approval_notifications (task_id, first_sent, escalated) VALUES (?,?,0)",
                    (task_id, now),
                )
                conn.commit()
                audit_log.record(
                    "approval_notification",
                    task=task_id,
                    execution_status="succeeded" if result.get("ok") else "failed",
                    result=result, error=None if result.get("ok") else result.get("detail"),
                    external_system="twilio", reference_id=result.get("sid"),
                )
                actions_taken.append({"task_id": task_id, "action": "sms", "ok": bool(result.get("ok"))})
                continue

            # Already texted. Escalate to a call exactly once, and only
            # after it has genuinely been ignored for hours.
            waited_h = (now - float(row["first_sent"])) / 3600.0
            if row["escalated"] or waited_h < ESCALATE_AFTER_HOURS:
                continue
            if not cartesia_calls.is_configured():
                continue
            reason = f"I still need your approval on {item.get('title', 'a task')}. It's been waiting {waited_h:.0f} hours."
            if dry_run:
                actions_taken.append({"task_id": task_id, "action": "call", "dry_run": True, "reason": reason})
                continue
            result = cartesia_calls.place_call(owner, reason)
            conn.execute("UPDATE approval_notifications SET escalated = 1 WHERE task_id = ?", (task_id,))
            conn.commit()
            audit_log.record(
                "approval_escalation_call",
                task=task_id,
                execution_status="succeeded" if result.get("ok") else "failed",
                result=result, error=None if result.get("ok") else result.get("detail"),
                external_system="cartesia", reference_id=result.get("agent_call_id"),
            )
            actions_taken.append({"task_id": task_id, "action": "call", "ok": bool(result.get("ok"))})
    finally:
        conn.close()

    return actions_taken


def clear_notification(task_id: str) -> None:
    """Called when a task leaves pending_approval, so the same task
    approved-then-reopened later can notify again."""
    try:
        conn = _connect()
        conn.execute("DELETE FROM approval_notifications WHERE task_id = ?", (task_id,))
        conn.commit()
        conn.close()
    except Exception:
        logger.debug("could not clear approval notification for %s", task_id, exc_info=True)
