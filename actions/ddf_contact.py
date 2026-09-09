"""Daily Deal Finders — public contact form backend.

Persists every submission to a durable `contact_messages` table in the
same jarvis2.db every other DDF action already uses (own `_connect()`,
own `CREATE TABLE IF NOT EXISTS` — the established per-module pattern in
this codebase, see actions/daily_deal_finders.py), then best-effort
forwards a notification to actions/gmail_integration.py's send_email().

Persistence is the source of truth for "message received": a row exists
the moment submit_contact_message() returns ok=True, independent of
whether the Gmail forward succeeds. A forward failure (no Gmail token
configured, API error, etc.) is recorded on the row and reported back as
notified=False — it never blocks the submission, and a submission that
fails to persist is never reported as ok=True.
"""
from __future__ import annotations

import re
import sqlite3
import time
import uuid
from typing import Any

from core.headless import config
from actions import gmail_integration

DB_PATH = config.DATA_DIR / "jarvis2.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

MAX_NAME_LEN = 200
MAX_EMAIL_LEN = 254
MAX_MESSAGE_LEN = 5000
DUPLICATE_WINDOW_SECONDS = 120  # re-submitting the same email+message this soon is a double-click/retry, not a new message

# Deliberately simple/permissive (not a full RFC 5322 parser) — this is a
# server-side sanity check against garbage input, not an email deliverability
# guarantee. Rejects the common "blank"/"missing @"/"missing domain" cases.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS contact_messages (
            id TEXT PRIMARY KEY,
            name TEXT,
            email TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at REAL NOT NULL,
            notified INTEGER NOT NULL DEFAULT 0,
            notify_error TEXT
        )
    """)
    return conn


def validate_email(email: str) -> bool:
    return bool(email) and len(email) <= MAX_EMAIL_LEN and bool(_EMAIL_RE.match(email.strip()))


def _find_recent_duplicate(conn: sqlite3.Connection, email: str, message: str, now: float) -> str | None:
    row = conn.execute(
        """SELECT id FROM contact_messages
           WHERE email = ? AND message = ? AND created_at >= ?
           ORDER BY created_at DESC LIMIT 1""",
        (email, message, now - DUPLICATE_WINDOW_SECONDS),
    ).fetchone()
    return row["id"] if row else None


def submit_contact_message(name: str, email: str, message: str) -> dict[str, Any]:
    """Validates, persists, and best-effort forwards one contact-form
    submission. Never raises — every failure path returns a typed
    {"ok": False, "error": ...} result the caller can render directly."""
    name = (name or "").strip()
    email = (email or "").strip()
    message = (message or "").strip()

    if not message:
        return {"ok": False, "error": "empty_message", "message": "Please enter a message before sending."}
    if len(message) > MAX_MESSAGE_LEN:
        return {"ok": False, "error": "message_too_long", "message": "That message is too long."}
    if len(name) > MAX_NAME_LEN:
        return {"ok": False, "error": "name_too_long", "message": "That name is too long."}
    if not validate_email(email):
        return {"ok": False, "error": "invalid_email", "message": "Please enter a valid email address."}

    now = time.time()
    conn = _connect()
    try:
        existing_id = _find_recent_duplicate(conn, email, message, now)
        if existing_id:
            return {
                "ok": True, "id": existing_id, "duplicate": True,
                "message": "We already have this message — we'll get back to you soon.",
            }

        msg_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO contact_messages (id, name, email, message, created_at, notified) VALUES (?, ?, ?, ?, ?, 0)",
            (msg_id, name or None, email, message, now),
        )
        conn.commit()
    except Exception as exc:
        return {"ok": False, "error": "storage_failed", "message": "Sorry, something went wrong. Please try again.", "detail": str(exc)}
    finally:
        conn.close()

    notified, notify_error = _notify(name, email, message)
    if notified or notify_error:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE contact_messages SET notified = ?, notify_error = ? WHERE id = ?",
                (1 if notified else 0, notify_error, msg_id),
            )
            conn.commit()
        finally:
            conn.close()

    return {
        "ok": True, "id": msg_id, "notified": notified,
        "message": "Thanks — we've received your message and will get back to you soon.",
    }


def _notify(name: str, email: str, message: str) -> tuple[bool, str | None]:
    subject = f"Daily Deal Finders contact form — {name or email}"
    body = f"From: {name or '(no name given)'} <{email}>\n\n{message}"
    try:
        result = gmail_integration.send_email(config.DDF_CONTACT_EMAIL, subject, body, approved=True)
    except Exception as exc:
        return False, str(exc)
    if result.get("ok"):
        return True, None
    return False, result.get("detail") or result.get("state") or "send_failed"


def list_contact_messages(limit: int = 50) -> list[dict[str, Any]]:
    """Read-only: most recent submissions, newest first — for an internal
    admin view, not exposed on the public site."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, name, email, message, created_at, notified, notify_error "
            "FROM contact_messages ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
