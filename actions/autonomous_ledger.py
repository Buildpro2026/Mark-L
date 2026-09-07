"""One shared record of "JARVIS already acted on this", across every agent.

Phase A gave tasks identity and stopped a single task from running twice.
That is not the same problem as this one: the autonomous layer keeps
REDISCOVERING the same real-world thing — the same email in the inbox, the
same HubSpot contact, the same product, the same calendar meeting — on
every sweep, and would happily create a fresh task for it every time. A
task-level guard cannot help, because each of those is a genuinely new,
correctly-unique task about an old subject.

Several subsystems already solved their own slice of this well
(buildpro_data's intake table, buffer_integration's recent-duplicate check,
daily_deal_finders' product matching), and none of those are replaced here.
This covers the gap between them: a durable, deterministic key per
(kind, external identity) that any agent can claim exactly once.

Identity rules, in order of preference:
  1. The external system's own stable id — a Gmail message id, a HubSpot
     contact id, a calendar event id. Always use this when it exists.
  2. Failing that, a deterministic hash of the fields that make the thing
     what it is (see subject_key). Never a timestamp, never a uuid — a key
     that changes on every sweep is not a key.

Deliberately NOT a lock. This says "already handled", not "being handled
right now"; a container that dies mid-work leaves no claim behind, so the
next sweep retries rather than silently skipping. The task engine owns
in-flight concurrency (see agent_orchestrator.run_task).
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from typing import Any, Optional

from core.headless import config

logger = logging.getLogger("jarvis.autonomous_ledger")


# Module-level, matching actions/agent_orchestrator.py, buildpro_data.py and
# business_intelligence.py: they all name the same physical file, and the
# test suite isolates each by name (see tests/conftest.py). Reading
# config.DB_PATH directly at call time would bypass that isolation and let a
# test permanently claim a real subject in the live database — exactly the
# leak that fixture was written to close.
DB_PATH = config.DB_PATH


def _connect() -> sqlite3.Connection:
    config.ensure_data_dir()
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS autonomous_ledger (
            kind        TEXT NOT NULL,
            subject_id  TEXT NOT NULL,
            first_seen  REAL NOT NULL,
            task_id     TEXT,
            detail      TEXT,
            PRIMARY KEY (kind, subject_id)
        )
    """)
    conn.commit()
    return conn


def subject_key(*parts: Any) -> str:
    """A deterministic id for something with no external id of its own.

    Order matters and is part of the identity, so callers must pass fields
    in a fixed order. Values are normalised (stripped, lowercased) so
    trivial formatting differences in the same source record don't read as
    two different subjects."""
    norm = "|".join(str(p).strip().lower() for p in parts if p is not None and str(p).strip())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:32]


def already_handled(kind: str, subject_id: str) -> bool:
    try:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM autonomous_ledger WHERE kind = ? AND subject_id = ?",
                (kind, subject_id),
            ).fetchone()
            return row is not None
        finally:
            conn.close()
    except Exception:
        # Fails OPEN, and the direction is deliberate: a ledger error must
        # never make an agent skip real work it has not actually done. The
        # cost is a possible duplicate; the cost of failing closed is
        # silently doing nothing, which is much harder to notice.
        logger.debug("ledger lookup failed for %s/%s", kind, subject_id, exc_info=True)
        return False


def claim(kind: str, subject_id: str, task_id: Optional[str] = None, detail: Any = None) -> bool:
    """Records this subject as handled. Returns True if THIS call claimed it
    and False if it was already claimed — so the check and the write are one
    atomic step, and two workers racing on the same subject cannot both
    believe they won."""
    try:
        conn = _connect()
        try:
            detail_json = None
            if detail is not None:
                try:
                    detail_json = json.dumps(detail)
                except Exception:
                    detail_json = json.dumps(str(detail))
            cur = conn.execute(
                "INSERT OR IGNORE INTO autonomous_ledger (kind, subject_id, first_seen, task_id, detail) "
                "VALUES (?, ?, ?, ?, ?)",
                (kind, subject_id, time.time(), task_id, detail_json),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
    except Exception:
        logger.debug("ledger claim failed for %s/%s", kind, subject_id, exc_info=True)
        return True   # same fail-open reasoning as above: prefer doing the work


def release(kind: str, subject_id: str) -> None:
    """Removes a claim so the subject can be picked up again. For the case
    where a claim was made and the downstream work then failed in a way that
    genuinely should be retried — the caller decides that, not this module."""
    try:
        conn = _connect()
        try:
            conn.execute(
                "DELETE FROM autonomous_ledger WHERE kind = ? AND subject_id = ?",
                (kind, subject_id),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.debug("ledger release failed for %s/%s", kind, subject_id, exc_info=True)


def handled_count(kind: str) -> int:
    try:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM autonomous_ledger WHERE kind = ?", (kind,)
            ).fetchone()
            return int(row["n"]) if row else 0
        finally:
            conn.close()
    except Exception:
        return 0
