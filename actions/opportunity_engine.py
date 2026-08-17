"""Opportunity Discovery Engine.

Separates monetization opportunities into QUICK_CASH (revenue potential
within days/weeks) and LONG_TERM (recurring-revenue businesses/assets),
scored and ranked by revenue potential, time to revenue, probability, cost,
capital required, scalability, automation potential, competition, risk,
opportunity cost, and alignment with the $1,000,000 / 6-12 month strategic
objective (see actions/strategic_objective.py).

This is a scoring/ranking FRAMEWORK, not an autonomous web-crawling
discovery bot — Jarvis proposes candidate opportunities (via its own
reasoning plus the existing web_search tools) through the
opportunity_engine voice tool, which scores and ranks them here for Lee's
review. Nothing in this module contacts the network or invents data.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from core.headless.config import DATA_DIR

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = DATA_DIR / "jarvis2.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

OPPORTUNITY_TYPES = {"quick_cash", "long_term"}
STATUSES = {"proposed", "active", "paused", "closed"}

# 1-5 scale per factor. Weights reflect how directly each factor drives
# progress toward the $1M/6-12mo objective — tunable constants, not hidden
# logic. Five factors are "inverted" (lower raw value = better outcome:
# cheaper, faster, less capital, less competition, less risk) — scored so a
# 5 always means "most favorable" for every factor, inverted or not.
_WEIGHTS: dict[str, float] = {
    "revenue_potential":    0.20,
    "probability":          0.15,
    "time_to_revenue":      0.13,   # 5 = fastest
    "scalability":          0.11,
    "alignment":            0.10,   # 5 = strongly advances the $1M objective
    "automation_potential": 0.09,
    "opportunity_cost":     0.06,   # 5 = lowest opportunity cost (least given up elsewhere)
    "cost":                 0.06,   # 5 = cheapest
    "capital_required":     0.05,   # 5 = least capital needed
    "competition":          0.03,   # 5 = least competition
    "risk":                 0.02,   # 5 = lowest risk
}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS opportunities (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            business              TEXT NOT NULL,
            opp_type              TEXT NOT NULL,   -- 'quick_cash' | 'long_term'
            title                 TEXT NOT NULL,
            description           TEXT,
            revenue_potential     INTEGER,
            time_to_revenue       INTEGER,
            probability           INTEGER,
            cost                  INTEGER,
            capital_required      INTEGER,
            scalability           INTEGER,
            automation_potential  INTEGER,
            competition           INTEGER,
            risk                  INTEGER,
            opportunity_cost      INTEGER,
            alignment             INTEGER,
            status                TEXT NOT NULL DEFAULT 'proposed',
            ts                    REAL NOT NULL
        )
    """)
    conn.commit()
    return conn


def _clamp_scores(scores: dict[str, Any]) -> dict[str, int]:
    out = {}
    for factor in _WEIGHTS:
        val = scores.get(factor, 3)
        try:
            val = int(val)
        except (TypeError, ValueError):
            val = 3
        out[factor] = max(1, min(5, val))
    return out


def score_opportunity(fields: dict[str, Any]) -> float:
    """Weighted 0-100 score. Missing/invalid factors default to 3 (neutral)."""
    scores = _clamp_scores(fields)
    total = sum((scores[f] / 5.0) * w for f, w in _WEIGHTS.items())
    return round(total * 100, 1)


def add_opportunity(business: str, opp_type: str, title: str, description: str = "", **scores) -> dict[str, Any]:
    opp_type = (opp_type or "").strip().lower()
    if opp_type not in OPPORTUNITY_TYPES:
        raise ValueError(f"opp_type must be one of {sorted(OPPORTUNITY_TYPES)}, got {opp_type!r}")
    fields = _clamp_scores(scores)
    conn = _connect()
    cur = conn.execute(
        "INSERT INTO opportunities (business, opp_type, title, description, "
        "revenue_potential, time_to_revenue, probability, cost, capital_required, "
        "scalability, automation_potential, competition, risk, opportunity_cost, "
        "alignment, status, ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            (business or "general").strip().lower(), opp_type, title, description,
            fields["revenue_potential"], fields["time_to_revenue"], fields["probability"],
            fields["cost"], fields["capital_required"], fields["scalability"],
            fields["automation_potential"], fields["competition"], fields["risk"],
            fields["opportunity_cost"], fields["alignment"], "proposed", time.time(),
        ),
    )
    conn.commit()
    opp_id = cur.lastrowid
    conn.close()
    return {"id": opp_id, "score": score_opportunity(fields)}


def _rows(q: str, params: list[Any]) -> list[dict[str, Any]]:
    conn = _connect()
    rows = [dict(r) for r in conn.execute(q, params).fetchall()]
    conn.close()
    for r in rows:
        r["score"] = score_opportunity(r)
    return rows


def list_opportunities(opp_type: str | None = None, business: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    q = "SELECT * FROM opportunities WHERE 1=1"
    params: list[Any] = []
    if opp_type:
        q += " AND opp_type = ?"
        params.append(opp_type.strip().lower())
    if business:
        q += " AND business = ?"
        params.append(business.strip().lower())
    q += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    return _rows(q, params)


def rank_opportunities(opp_type: str | None = None, business: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
    """Highest-scoring opportunities first — the practical answer to
    'does this move us toward the revenue objective?' for a given slice."""
    opps = list_opportunities(opp_type=opp_type, business=business, limit=1000)
    opps.sort(key=lambda o: o["score"], reverse=True)
    return opps[:limit]


def update_status(opp_id: int, status: str) -> dict[str, Any]:
    status = (status or "").strip().lower()
    if status not in STATUSES:
        raise ValueError(f"status must be one of {sorted(STATUSES)}, got {status!r}")
    conn = _connect()
    conn.execute("UPDATE opportunities SET status = ? WHERE id = ?", (status, opp_id))
    conn.commit()
    conn.close()
    return {"id": opp_id, "status": status}
