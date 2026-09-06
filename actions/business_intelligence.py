"""Persistent Business Intelligence storage for Jarvis.

"Jarvis getting smarter" means persistent business memory + outcome tracking
+ improved future decision-making — explicitly NOT self-modifying source
code. This module is that persistent memory: everything Jarvis observes,
decides, and learns about BuildPro Recruiting, CareerRocket Pro, Daily Deal
Finders, and future approved ventures lives here, queryable across sessions
and process restarts.

Categories (rows in `bi_entries`, distinguished by `category`):
    research, competitors, market_observations, experiments,
    decisions, outcomes, revenue, lessons_learned, recommendations
(Opportunities have their own richer, scored schema — see
actions/opportunity_engine.py — since Phase 4 needs ranking fields these
generic entries don't.)

Learning loop (Phase 8): record_outcome() is the
    PLAN -> EXECUTE -> RESULT -> REVENUE -> ANALYZE -> LESSON -> MEMORY
pipeline's persistence step — it logs an outcome and, if given, the lesson
and recommendation that came out of analyzing it, linked back to that
outcome so future decisions can look them up via get_lessons_for().
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from core.headless.config import DATA_DIR

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = DATA_DIR / "jarvis2.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

BUSINESSES = {"buildpro", "careerrocket", "ddf", "general"}
# Not enforced by add_entry() (that check is deliberately left to the
# 'general' catch-all above for anything without its own dedicated
# business — see actions/email_classification.py's company_id vocabulary,
# which is Lee's spec's own literal set and is intentionally broader than
# this historical 'business' tag).
CATEGORIES = {
    "research", "competitors", "market_observations", "experiments",
    "decisions", "outcomes", "revenue", "lessons_learned", "recommendations",
    "risks",
}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bi_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category   TEXT NOT NULL,
            business   TEXT NOT NULL,
            title      TEXT NOT NULL,
            content    TEXT,
            data       TEXT,            -- JSON blob for structured extras (cost, revenue, etc.)
            related_id INTEGER,         -- optional link to another bi_entries row
            ts         REAL NOT NULL
        )
    """)
    conn.commit()
    return conn


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    try:
        d["data"] = json.loads(d["data"]) if d["data"] else {}
    except Exception:
        d["data"] = {}
    return d


def add_entry(
    category: str, business: str, title: str, content: str = "",
    data: dict[str, Any] | None = None, related_id: int | None = None,
) -> int:
    category = (category or "").strip().lower()
    if category not in CATEGORIES:
        raise ValueError(f"Unknown BI category: {category!r}. Valid: {sorted(CATEGORIES)}")
    business = (business or "general").strip().lower()
    conn = _connect()
    cur = conn.execute(
        "INSERT INTO bi_entries (category, business, title, content, data, related_id, ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (category, business, title, content, json.dumps(data or {}), related_id, time.time()),
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def list_entries(category: str | None = None, business: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    conn = _connect()
    q = "SELECT * FROM bi_entries WHERE 1=1"
    params: list[Any] = []
    if category:
        q += " AND category = ?"
        params.append(category.strip().lower())
    if business:
        q += " AND business = ?"
        params.append(business.strip().lower())
    q += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def get_entry(entry_id: int) -> dict[str, Any] | None:
    conn = _connect()
    row = conn.execute("SELECT * FROM bi_entries WHERE id = ?", (entry_id,)).fetchone()
    conn.close()
    return _row_to_dict(row) if row else None


# ── Learning loop (Phase 8) ──────────────────────────────────────────────

def record_outcome(
    business: str,
    plan: str,
    result: str,
    revenue_usd: float = 0.0,
    cost_usd: float = 0.0,
    lesson: str = "",
    recommendation: str = "",
    decision_id: int | None = None,
) -> dict[str, Any]:
    """Persists PLAN -> EXECUTE -> RESULT -> REVENUE -> ANALYZE -> LESSON.
    Files the outcome, and (if given) the lesson/recommendation that came
    from analyzing it — each linked back to the outcome row via related_id
    so get_lessons_for() surfaces the full chain, not just the raw fact."""
    outcome_id = add_entry(
        "outcomes", business, title=plan[:120],
        content=result,
        data={"revenue_usd": revenue_usd, "cost_usd": cost_usd, "plan": plan},
        related_id=decision_id,
    )
    if lesson:
        add_entry("lessons_learned", business, title=plan[:120], content=lesson, related_id=outcome_id)
    if recommendation:
        add_entry("recommendations", business, title=plan[:120], content=recommendation, related_id=outcome_id)
    if revenue_usd:
        add_entry(
            "revenue", business, title=plan[:120], content=f"${revenue_usd:,.2f}",
            data={"amount_usd": revenue_usd}, related_id=outcome_id,
        )
    return {"outcome_id": outcome_id}


def get_lessons_for(business: str, limit: int = 10) -> list[dict[str, Any]]:
    return list_entries(category="lessons_learned", business=business, limit=limit)


def summary(business: str | None = None) -> dict[str, Any]:
    """Compact counts per category — consumed by the 3D Nucleus panels and
    the agent_orchestrator/system status summary."""
    conn = _connect()
    q = "SELECT category, COUNT(*) as n FROM bi_entries"
    params: list[Any] = []
    if business:
        q += " WHERE business = ?"
        params.append(business.strip().lower())
    q += " GROUP BY category"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    counts = {r["category"]: r["n"] for r in rows}
    total_revenue = sum(
        e["data"].get("amount_usd", 0) for e in list_entries(category="revenue", business=business, limit=1000)
    )
    return {"counts": counts, "total_entries": sum(counts.values()), "total_revenue_usd": total_revenue}
