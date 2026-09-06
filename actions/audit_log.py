"""Auditable record of every consequential JARVIS action (J3 Part 18).

Separate from actions/agent_orchestrator.py's agent_events table on
purpose: that table only ever sees agent-orchestrator activity. Direct
voice/API tool calls that take real external action — sending an email,
creating a calendar event, writing to Airtable/HubSpot, publishing a
social post, placing a call/SMS — never went through the orchestrator at
all, so nothing recorded them beyond a print() statement and whatever the
external service itself logs. This table is that missing record, written
from core/headless/tool_executor.py right after each consequential call
resolves (success or failure — a failed attempt is exactly as auditable
as a successful one).
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Optional

from core.headless.config import DATA_DIR

DB_PATH = DATA_DIR / "jarvis2.db"


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            actor TEXT NOT NULL,
            task TEXT,
            action TEXT NOT NULL,
            approval_status TEXT NOT NULL,
            execution_status TEXT NOT NULL,
            result_json TEXT,
            error TEXT,
            external_system TEXT,
            reference_id TEXT
        )
    """)
    return conn


def record(
    action: str,
    *,
    actor: str = "jarvis",
    task: str = "",
    approval_status: str = "approved",
    execution_status: str = "succeeded",
    result: Any = None,
    error: Optional[str] = None,
    external_system: Optional[str] = None,
    reference_id: Optional[str] = None,
) -> int:
    """Best-effort — a logging failure must never block the real action it's
    recording (the action already happened by the time this is called).
    Returns the new row id, or -1 if the write itself failed."""
    try:
        result_json = None
        if result is not None:
            try:
                result_json = json.dumps(result)
            except Exception:
                result_json = json.dumps(str(result))
        conn = _connect()
        try:
            cur = conn.execute(
                "INSERT INTO audit_log (ts, actor, task, action, approval_status, execution_status, "
                "result_json, error, external_system, reference_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (time.time(), actor, task, action, approval_status, execution_status,
                 result_json, error, external_system, reference_id),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()
    except Exception:
        return -1


def list_recent(limit: int = 50) -> list[dict[str, Any]]:
    """Read-only, most recent first. Never raises — an unreadable log
    degrades to an empty list rather than crashing a dashboard/API call."""
    try:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM audit_log ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return []
    out = []
    for r in rows:
        d = dict(r)
        if d.get("result_json"):
            try:
                d["result"] = json.loads(d["result_json"])
            except Exception:
                d["result"] = d["result_json"]
        del d["result_json"]
        out.append(d)
    return out
