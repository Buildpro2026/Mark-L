"""Persistent North-Star business objective for Jarvis.

Stored as config (config/strategic_objective.json), not prompt text, so it
survives restarts and is the single source of truth Jarvis references when
evaluating significant recommendations/plans (see format_objective_for_prompt,
injected into main.py's system prompt) and when asked directly via the
strategic_objective tool.

Lee remains CEO/owner and final authority — this module only stores state and
reports on it. Nothing here executes anything; changes to cumulative_revenue_usd
only happen via an explicit call (see log_revenue in main.py), consistent with
the OBSERVE -> SUGGEST -> EXECUTE permission model.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = BASE_DIR / "config" / "strategic_objective.json"

DEFAULT_OBJECTIVE: dict[str, Any] = {
    "objective": "Reach $1,000,000 in cumulative revenue",
    "target_amount_usd": 1_000_000,
    "committed_horizon_months": 12,   # committed planning horizon
    "stretch_horizon_months": 6,      # aggressive/stretch target
    "start_date": None,               # set to today on first creation
    "owner": "Lee",
    "authority": (
        "Lee is CEO/owner and final authority. Jarvis may observe and suggest "
        "freely; execution against this objective follows the OBSERVE -> SUGGEST "
        "-> EXECUTE permission model and requires Lee's approval."
    ),
    "permission_model": "OBSERVE -> SUGGEST -> EXECUTE",
    "cumulative_revenue_usd": 0,
    "status": "active",
}

_DAYS_IN_MONTH = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _add_months(d: date, months: int) -> date:
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    max_day = _DAYS_IN_MONTH[month - 1]
    if month == 2 and _is_leap(year):
        max_day = 29
    return date(year, month, min(d.day, max_day))


def _ensure_config() -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        data = dict(DEFAULT_OBJECTIVE)
        data["start_date"] = date.today().isoformat()
        CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_strategic_objective() -> dict[str, Any]:
    _ensure_config()
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    merged = {**DEFAULT_OBJECTIVE, **data}
    if not merged.get("start_date"):
        merged["start_date"] = date.today().isoformat()
    return merged


def save_strategic_objective(updates: dict[str, Any]) -> dict[str, Any]:
    data = load_strategic_objective()
    data.update(updates)
    CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


def get_objective_status() -> dict[str, Any]:
    """Full status including computed deadlines and progress — deadlines are
    derived from start_date on every call rather than cached, so editing the
    horizon fields later stays consistent without a stale stored date."""
    data = load_strategic_objective()
    try:
        start = date.fromisoformat(data["start_date"])
    except Exception:
        start = date.today()

    committed_deadline = _add_months(start, int(data.get("committed_horizon_months", 12)))
    stretch_deadline = _add_months(start, int(data.get("stretch_horizon_months", 6)))
    target = float(data.get("target_amount_usd", 1_000_000) or 1_000_000)
    revenue = float(data.get("cumulative_revenue_usd", 0) or 0)
    progress_pct = round((revenue / target) * 100, 2) if target else 0.0

    return {
        **data,
        "committed_deadline": committed_deadline.isoformat(),
        "stretch_deadline": stretch_deadline.isoformat(),
        "progress_pct": progress_pct,
        "remaining_usd": max(0.0, target - revenue),
    }


def log_revenue(amount: float, note: str = "") -> dict[str, Any]:
    """Add `amount` to cumulative_revenue_usd. Only meant to be called from an
    explicit, Lee-stated instruction (see main.py's strategic_objective tool
    description) — this module has no autonomous trigger of its own."""
    s = get_objective_status()
    new_total = float(s.get("cumulative_revenue_usd", 0)) + float(amount)
    save_strategic_objective({"cumulative_revenue_usd": new_total})
    return get_objective_status()


def format_objective_for_prompt() -> str:
    """Short block injected into Gemini's system prompt (main.py::_build_config)
    so the objective is part of persistent context every session, not a
    one-off mention — this is the "minimum clean architecture" for Jarvis to
    reference it when evaluating significant recommendations or plans."""
    s = get_objective_status()
    return (
        f"[STRATEGIC OBJECTIVE]\n"
        f"North-star goal: {s['objective']} (${s['target_amount_usd']:,.0f}).\n"
        f"Stretch target: by {s['stretch_deadline']} "
        f"({s['stretch_horizon_months']} months, aggressive).\n"
        f"Committed horizon: by {s['committed_deadline']} "
        f"({s['committed_horizon_months']} months).\n"
        f"Progress so far: ${s['cumulative_revenue_usd']:,.0f} of "
        f"${s['target_amount_usd']:,.0f} ({s['progress_pct']}%).\n"
        f"Lee is CEO/owner and final authority. Weigh significant recommendations "
        f"and plans against this objective, but never assume approval to act on "
        f"it — follow OBSERVE -> SUGGEST -> EXECUTE: observe and suggest freely, "
        f"execution requires Lee's explicit approval.\n"
    )
