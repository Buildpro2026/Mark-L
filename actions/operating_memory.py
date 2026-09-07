"""What JARVIS did, and what happened — the operating record.

This is NOT a second memory system, and the boundary matters:

  * memory/memory_manager.py owns CONVERSATIONAL memory — facts about Lee,
    preferences, session summaries. A small JSON file with a character
    budget, read into prompts. Untouched.
  * actions/business_intelligence.py owns BUSINESS OBSERVATIONS — risks,
    research, market signals. Things JARVIS noticed about the world.
    Untouched.
  * actions/autonomous_ledger.py owns IDENTITY — "have I already acted on
    this subject". A claim, not a narrative. Untouched.

None of them record what the autonomous system itself DID: which cycle ran,
what it produced, which agent failed and how often, whether the report
actually got delivered. That history existed only as log lines on an
ephemeral container, so JARVIS woke each morning unable to tell a first
failure from the fifth. That gap is what this fills, and only that.

Retention is bounded by construction: every write trims its own type back
to MAX_ENTRIES_PER_TYPE, so an unattended loop cannot grow the database
without limit. Writes never raise — recording that work happened must never
be the reason the work itself fails.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any, Optional

from core.headless import config

logger = logging.getLogger("jarvis.operating_memory")

# Module-level so tests can isolate it, matching agent_orchestrator/
# buildpro_data/business_intelligence/autonomous_ledger — they all name the
# same physical file and tests/conftest.py rebinds each by name.
DB_PATH = config.DB_PATH

# Per-type cap. Enough history to see a pattern (a fortnight of cycles, a
# long run of agent outcomes) without unbounded growth on a host with no
# persistent disk.
MAX_ENTRIES_PER_TYPE = 500

# Entry types. A closed vocabulary so a reader can filter reliably rather
# than guessing at free-text tags.
CYCLE_RUN = "cycle_run"
AGENT_OUTCOME = "agent_outcome"
INTEGRATION_STATE = "integration_state"
NOTIFICATION = "notification"
ESCALATION = "escalation"
RECOVERY = "recovery"


def _connect() -> sqlite3.Connection:
    config.ensure_data_dir()
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS operating_memory (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_type  TEXT NOT NULL,
            source      TEXT NOT NULL,
            subject     TEXT,
            summary     TEXT NOT NULL,
            data_json   TEXT,
            ok          INTEGER,
            ts          REAL NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_operating_memory_type_ts "
        "ON operating_memory (entry_type, ts DESC)"
    )
    conn.commit()
    return conn


def _trim(conn: sqlite3.Connection, entry_type: str) -> None:
    conn.execute(
        "DELETE FROM operating_memory WHERE entry_type = ? AND id NOT IN ("
        "  SELECT id FROM operating_memory WHERE entry_type = ? ORDER BY ts DESC LIMIT ?"
        ")",
        (entry_type, entry_type, MAX_ENTRIES_PER_TYPE),
    )


def record(entry_type: str, source: str, summary: str,
           subject: Optional[str] = None, data: Optional[dict] = None,
           ok: Optional[bool] = None, dedup_seconds: float = 0.0) -> dict[str, Any]:
    """Writes one operating-memory entry.

    dedup_seconds > 0 suppresses an identical (type, source, subject) entry
    seen within that window — for a loop that would otherwise record the
    same "Gmail is unauthorized" every fifteen minutes forever. It is off by
    default, because collapsing genuinely repeated events is exactly how a
    worsening problem gets hidden.

    Never raises. A memory write failing must not fail the business task
    that produced it, so the worst case is a returned {"recorded": False}
    that callers are free to ignore."""
    try:
        conn = _connect()
        try:
            now = time.time()
            if dedup_seconds > 0:
                row = conn.execute(
                    "SELECT id FROM operating_memory WHERE entry_type = ? AND source = ? "
                    "AND IFNULL(subject,'') = ? AND ts > ? ORDER BY ts DESC LIMIT 1",
                    (entry_type, source, subject or "", now - dedup_seconds),
                ).fetchone()
                if row is not None:
                    return {"recorded": False, "reason": "deduplicated"}

            data_json = None
            if data is not None:
                try:
                    data_json = json.dumps(data)
                except Exception:
                    data_json = json.dumps(str(data))
            cur = conn.execute(
                "INSERT INTO operating_memory (entry_type, source, subject, summary, data_json, ok, ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (entry_type, source, subject, summary[:2000], data_json,
                 None if ok is None else (1 if ok else 0), now),
            )
            _trim(conn, entry_type)
            conn.commit()
            return {"recorded": True, "id": cur.lastrowid}
        finally:
            conn.close()
    except Exception:
        logger.debug("operating memory write failed (%s/%s)", entry_type, source, exc_info=True)
        return {"recorded": False, "reason": "write_failed"}


def recall(entry_type: Optional[str] = None, source: Optional[str] = None,
           since_seconds: Optional[float] = None, limit: int = 20) -> list[dict[str, Any]]:
    """Most recent entries first. Returns [] on any failure — a caller
    reasoning over history must degrade to "no history", never crash."""
    try:
        conn = _connect()
        try:
            clauses, params = [], []
            if entry_type:
                clauses.append("entry_type = ?")
                params.append(entry_type)
            if source:
                clauses.append("source = ?")
                params.append(source)
            if since_seconds:
                clauses.append("ts > ?")
                params.append(time.time() - since_seconds)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            rows = conn.execute(
                f"SELECT * FROM operating_memory {where} ORDER BY ts DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        logger.debug("operating memory recall failed", exc_info=True)
        return []

    out = []
    for r in rows:
        try:
            data = json.loads(r["data_json"]) if r["data_json"] else None
        except Exception:
            data = None
        out.append({
            "id": r["id"], "entry_type": r["entry_type"], "source": r["source"],
            "subject": r["subject"], "summary": r["summary"], "data": data,
            "ok": None if r["ok"] is None else bool(r["ok"]), "ts": r["ts"],
        })
    return out


def failure_streak(source: str, entry_type: str = AGENT_OUTCOME, limit: int = 20) -> int:
    """How many times in a row `source` has most recently failed.

    This is the question self-healing actually needs — not "how many
    failures total", which never resets and eventually escalates everything
    forever. A single success anywhere in the recent run breaks the streak."""
    streak = 0
    for entry in recall(entry_type=entry_type, source=source, limit=limit):
        if entry["ok"] is False:
            streak += 1
        elif entry["ok"] is True:
            break
    return streak


def summarize(since_seconds: float = 86_400) -> dict[str, Any]:
    """A compact view of the last day's autonomous activity, for the CEO
    report and the monitoring sweep."""
    entries = recall(since_seconds=since_seconds, limit=MAX_ENTRIES_PER_TYPE)
    by_type: dict[str, int] = {}
    failures: list[dict[str, Any]] = []
    for e in entries:
        by_type[e["entry_type"]] = by_type.get(e["entry_type"], 0) + 1
        if e["ok"] is False:
            failures.append({"source": e["source"], "summary": e["summary"]})
    return {
        "window_seconds": since_seconds,
        "total": len(entries),
        "by_type": by_type,
        "failures": failures[:10],
        "failure_count": len(failures),
    }
