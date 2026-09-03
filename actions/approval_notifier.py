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
    # 2026-09-03 (Lee's autonomous-CEO spec, Section 16): generalizes this
    # table beyond pending-APPROVAL tasks to any urgent event JARVIS needs
    # to escalate about — reusing the exact dedup/escalate-once pattern
    # notify_pending() already proved, not a second notification system.
    # Additive-only migration (same _ensure_columns pattern buildpro_data.py
    # uses) so an existing approval_notifications row keeps working
    # unchanged; new columns default NULL/0 for it.
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(approval_notifications)").fetchall()}
    for name, col_type, default in (
        ("kind", "TEXT", "'approval'"),      # 'approval' (existing behavior) | 'urgent_event'
        ("level", "INTEGER", "2"),           # 0=log 1=dashboard 2=SMS 3=SMS+call
        ("title", "TEXT", "NULL"),
        ("acknowledged_at", "REAL", "NULL"),
        ("response_detected", "INTEGER", "0"),
        ("escalated_at", "REAL", "NULL"),
        ("escalation_status", "TEXT", "'pending'"),  # pending | sms_sent | escalated | acknowledged
    ):
        if name not in existing:
            conn.execute(f"ALTER TABLE approval_notifications ADD COLUMN {name} {col_type} DEFAULT {default}")
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


# ── Generic urgent-event escalation (Section 16) ───────────────────────────
# Levels: 0=log only (no send — caller already logged it, e.g. as a
# business-intelligence 'risks' entry), 1=dashboard (same, no send — the
# entry being visible on the Command Center IS the level-1 notification),
# 2=SMS, 3=SMS immediately + a phone call if still unacknowledged after
# escalate_after_minutes. Reserved for genuinely urgent events — callers
# decide urgency, this module only ever delivers what it's told to.
_URGENT_ESCALATE_AFTER_MINUTES_DEFAULT = 5.0


def notify_urgent_event(
    event_id: str, title: str, detail: str = "", level: int = 2,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Sends (or dry-runs) the level-2/3 SMS for one urgent event, exactly
    once per event_id (same dedup contract as notify_pending's approval
    texts). Levels 0/1 are a deliberate no-op here — nothing to send, by
    design; the caller's own log/dashboard entry already satisfies that
    level. Honestly reports NOT_CONFIGURED rather than pretending to send
    when JARVIS_OWNER_PHONE/Twilio aren't set up (true on this deployment
    today) — never silently drops it without saying so."""
    from actions import twilio_integration as twilio
    from actions import audit_log
    from core.headless import config

    if level < 2:
        return {"event_id": event_id, "action": "none", "level": level, "reason": "level < 2 — no send required"}

    owner = config.JARVIS_OWNER_PHONE
    if not owner:
        return {"event_id": event_id, "action": "none", "level": level, "configured": False, "reason": "JARVIS_OWNER_PHONE isn't set"}
    if not twilio.is_configured():
        return {"event_id": event_id, "action": "none", "level": level, "configured": False, "reason": "Twilio isn't configured"}

    conn = _connect()
    try:
        row = _already_notified(conn, event_id)
        if row is not None:
            return {"event_id": event_id, "action": "already_sent", "level": level, "configured": True}

        body = f"JARVIS URGENT: {title}\n\n{detail}".strip()
        now = time.time()
        if dry_run:
            return {"event_id": event_id, "action": "sms", "dry_run": True, "level": level, "body": body}
        result = twilio.send_sms(owner, body)
        conn.execute(
            "INSERT OR REPLACE INTO approval_notifications "
            "(task_id, first_sent, escalated, kind, level, title, escalation_status) "
            "VALUES (?, ?, 0, 'urgent_event', ?, ?, 'sms_sent')",
            (event_id, now, level, title),
        )
        conn.commit()
        audit_log.record(
            "urgent_event_notification", task=event_id,
            execution_status="succeeded" if result.get("ok") else "failed",
            result=result, error=None if result.get("ok") else result.get("detail"),
            external_system="twilio", reference_id=result.get("sid"),
        )
        return {"event_id": event_id, "action": "sms", "ok": bool(result.get("ok")), "level": level, "configured": True}
    finally:
        conn.close()


def sweep_urgent_escalations(
    escalate_after_minutes: float = _URGENT_ESCALATE_AFTER_MINUTES_DEFAULT, dry_run: bool = False,
) -> list[dict[str, Any]]:
    """One pass over every level-3 urgent event that's been SMS'd but not
    yet escalated and not yet acknowledged: if it's been at least
    escalate_after_minutes, place one call (never more than one per
    event — same escalate-once contract as the approval path). Honestly
    reports when Cartesia calling isn't configured rather than silently
    skipping without a reason."""
    from actions import cartesia_calls
    from actions import audit_log
    from core.headless import config

    owner = config.JARVIS_OWNER_PHONE
    actions_taken: list[dict[str, Any]] = []
    if not owner:
        return actions_taken

    conn = _connect()
    try:
        now = time.time()
        rows = conn.execute(
            "SELECT * FROM approval_notifications WHERE kind = 'urgent_event' AND level >= 3 "
            "AND escalation_status = 'sms_sent' AND (response_detected IS NULL OR response_detected = 0)"
        ).fetchall()
        for row in rows:
            waited_min = (now - float(row["first_sent"])) / 60.0
            if waited_min < escalate_after_minutes:
                continue
            if not cartesia_calls.is_configured():
                actions_taken.append({"event_id": row["task_id"], "action": "none", "configured": False, "reason": "Cartesia calling isn't configured"})
                continue
            reason = f"Urgent: {row['title'] or 'an event'} needs your attention — I texted you {waited_min:.0f} minutes ago with no response."
            if dry_run:
                actions_taken.append({"event_id": row["task_id"], "action": "call", "dry_run": True, "reason": reason})
                continue
            result = cartesia_calls.place_call(owner, reason)
            conn.execute(
                "UPDATE approval_notifications SET escalated = 1, escalated_at = ?, escalation_status = 'escalated' WHERE task_id = ?",
                (now, row["task_id"]),
            )
            conn.commit()
            audit_log.record(
                "urgent_event_escalation_call", task=row["task_id"],
                execution_status="succeeded" if result.get("ok") else "failed",
                result=result, error=None if result.get("ok") else result.get("detail"),
                external_system="cartesia", reference_id=result.get("agent_call_id"),
            )
            actions_taken.append({"event_id": row["task_id"], "action": "call", "ok": bool(result.get("ok"))})
    finally:
        conn.close()
    return actions_taken


def acknowledge_urgent_event(event_id: str) -> None:
    """Marks an urgent event acknowledged — stops sweep_urgent_escalations
    from ever calling about it. Never invents acknowledgment: only call
    this from a real signal (Lee opening the item in the Command Center,
    an inbound SMS reply, etc.)."""
    try:
        conn = _connect()
        conn.execute(
            "UPDATE approval_notifications SET response_detected = 1, acknowledged_at = ?, escalation_status = 'acknowledged' WHERE task_id = ?",
            (time.time(), event_id),
        )
        conn.commit()
        conn.close()
    except Exception:
        logger.debug("could not acknowledge urgent event %s", event_id, exc_info=True)


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
