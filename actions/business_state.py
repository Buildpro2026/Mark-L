"""What the businesses actually look like right now, and what moved.

Every number here is read from a system that already owns it — no new
database, no new source of truth, and nothing invented:

  * strategic_objective  — the revenue ledger (cumulative, target, progress)
  * opportunity_engine   — scored opportunities
  * buildpro_intelligence— candidates, jobs, matches
  * daily_deal_finders   — tracked products
  * agent_orchestrator   — live task state, including what awaits approval
  * business_intelligence— risks and observations
  * operating_memory     — the previous snapshot, for comparison

The point of gathering them in one place is the question the CEO cycle
could never answer before: WHAT CHANGED. A snapshot alone says "3 open
jobs"; two snapshots say "one new job since yesterday", which is the part
worth a human's attention.

Honesty rules, enforced in code rather than by convention:

  * A source that fails contributes `None`, never 0. "We could not read
    BuildPro" and "BuildPro has zero candidates" are different facts, and
    collapsing them into 0 would manufacture a decline that never happened.
  * Nothing is projected, forecast, or extrapolated. If a number is not in
    a system, it is absent and labelled absent.
  * `compare()` reports deltas only between two values that both exist.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("jarvis.business_state")

# Marks a metric whose source could not be read this cycle. Distinct from
# zero everywhere it is used.
UNAVAILABLE = None


def _safe(label: str, fn) -> Any:
    """Runs one source. A failure yields None (unknown), never a zero that
    would read as a real business number."""
    try:
        return fn()
    except Exception:
        logger.debug("business-state source %r failed", label, exc_info=True)
        return UNAVAILABLE


def _revenue() -> dict[str, Any]:
    from actions import strategic_objective as so
    status = so.get_objective_status()
    return {
        "cumulative_usd": status.get("cumulative_revenue_usd"),
        "target_usd": status.get("target_amount_usd"),
        "remaining_usd": status.get("remaining_usd"),
        "progress_pct": status.get("progress_pct"),
        "committed_deadline": status.get("committed_deadline"),
        # Everything above is logged fact from the revenue ledger. No
        # run-rate or projection is derived: with no dated revenue history
        # a projection would be a guess wearing a number's clothes.
        "basis": "logged revenue ledger (actuals only, no projection)",
    }


def _buildpro() -> dict[str, Any]:
    from actions import buildpro_intelligence
    data = buildpro_intelligence.generate_morning_report_data() or {}
    counts = data.get("counts") or {}
    matches = data.get("top_matches") or []
    strong = [m for m in matches if _score(m) >= 70]
    return {
        "candidates": counts.get("candidate_count"),
        "open_jobs": counts.get("active_jobs"),
        "qualified_matches": counts.get("qualified_matches"),
        "strong_matches": len(strong),
        "unmatched_open_jobs": len(data.get("unmatched_open_jobs") or []),
        # BuildPro computes its own follow-ups (e.g. "2 high-scoring
        # match(es) are still 'proposed' and ready to submit"). These are
        # recommendations, not work performed, and the report carries them
        # in RECOMMENDED NEXT ACTIONS where that distinction is explicit.
        "recommended_actions": [
            a for a in (data.get("recommended_actions") or [])
            if a != "No urgent recruiting follow-ups identified."
        ],
    }


def _score(match: dict) -> float:
    try:
        return float(match.get("score") or match.get("match_score") or 0)
    except (TypeError, ValueError):
        return 0.0


def _ddf() -> dict[str, Any]:
    from actions import daily_deal_finders as ddf
    top = ddf.get_top_products(limit=50) or []
    high_ticket = [p for p in top if _price(p) >= 100]
    return {
        "tracked_products": len(top),
        "high_ticket_products": len(high_ticket),
    }


def _price(product: dict) -> float:
    try:
        return float(product.get("price") or 0)
    except (TypeError, ValueError):
        return 0.0


def _opportunities() -> dict[str, Any]:
    from actions import opportunity_engine as oe
    ranked = oe.rank_opportunities(limit=25) or []
    return {
        "total_ranked": len(ranked),
        "top": [
            {"id": o.get("id"), "title": o.get("title"),
             "score": o.get("score"), "business": o.get("business"),
             "type": o.get("opp_type") or o.get("type")}
            for o in ranked[:5]
        ],
    }


def _tasks() -> dict[str, Any]:
    """Live task state. Note what each bucket means, because conflating them
    is exactly the error this phase exists to prevent: `awaiting_approval`
    is work JARVIS has NOT done and must not claim credit for."""
    from actions.agent_orchestrator import orchestrator, TaskStatus
    tasks = list(orchestrator._tasks.values())
    by_status: dict[str, int] = {}
    for t in tasks:
        key = t.status.value if hasattr(t.status, "value") else str(t.status)
        by_status[key] = by_status.get(key, 0) + 1
    awaiting = [t for t in tasks if t.status == TaskStatus.PENDING_APPROVAL]
    escalated = [t for t in tasks if getattr(t, "escalated", False)]
    return {
        "total": len(tasks),
        "by_status": by_status,
        "completed": by_status.get(TaskStatus.DONE.value, 0),
        "failed": by_status.get(TaskStatus.FAILED.value, 0),
        "awaiting_approval": len(awaiting),
        "escalated": len(escalated),
        "awaiting_approval_detail": [
            {"task_id": t.id, "agent_id": t.agent_id, "description": t.description[:200]}
            for t in awaiting[:10]
        ],
    }


def _risks() -> dict[str, Any]:
    from actions import business_intelligence as bi
    entries = bi.list_entries(category="risks", limit=25) or []
    return {"open_risks": len(entries),
            "recent": [{"title": e.get("title"), "business": e.get("business")} for e in entries[:5]]}


def snapshot() -> dict[str, Any]:
    """The current operating picture. Never raises; a failed source appears
    as None so the caller can say "unknown" rather than "zero"."""
    import time
    return {
        "ts": time.time(),
        "revenue": _safe("revenue", _revenue),
        "buildpro": _safe("buildpro", _buildpro),
        "ddf": _safe("ddf", _ddf),
        "opportunities": _safe("opportunities", _opportunities),
        "tasks": _safe("tasks", _tasks),
        "risks": _safe("risks", _risks),
    }


# ── cycle-to-cycle comparison ────────────────────────────────────────────

# The metrics worth reporting movement on, as (section, key, label).
TRACKED_METRICS = [
    ("revenue", "cumulative_usd", "cumulative revenue"),
    ("buildpro", "candidates", "candidates"),
    ("buildpro", "open_jobs", "open jobs"),
    ("buildpro", "strong_matches", "strong matches"),
    ("ddf", "tracked_products", "tracked products"),
    ("ddf", "high_ticket_products", "high-ticket products"),
    ("opportunities", "total_ranked", "ranked opportunities"),
    ("tasks", "completed", "completed tasks"),
    ("tasks", "awaiting_approval", "tasks awaiting approval"),
    ("risks", "open_risks", "open risks"),
]


def compare(current: dict[str, Any], previous: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Movement between two snapshots.

    A delta is reported only when BOTH values are real numbers. If either
    side is unknown — because a source was unreadable then or now — the
    metric is listed as unknown rather than differenced against a fabricated
    zero, which would invent a change that did not happen."""
    # A stored snapshot is not guaranteed to be well-formed: operating
    # memory falls back to json.dumps(str(data)) when a payload will not
    # serialise, an older row may predate the current shape, and a truncated
    # write can leave anything at all. Reading one must degrade to "no
    # comparison available", never raise — this crashed on a snapshot whose
    # sections were strings, and because the cycle isolates its stages the
    # failure was invisible: every cycle silently reported itself as the
    # first one while comparison quietly never happened.
    if not isinstance(previous, dict) or not isinstance(current, dict):
        return {"first_cycle": True, "changes": [], "unknown": [], "unchanged": [],
                "detail": "no usable previous snapshot"}
    if not previous:
        return {"first_cycle": True, "changes": [], "unknown": [], "unchanged": []}

    changes, unknown, unchanged = [], [], []
    for section, key, label in TRACKED_METRICS:
        cur_section = current.get(section)
        prev_section = previous.get(section)
        cur = cur_section.get(key) if isinstance(cur_section, dict) else None
        prev = prev_section.get(key) if isinstance(prev_section, dict) else None

        if not isinstance(cur, (int, float)) or not isinstance(prev, (int, float)):
            unknown.append(label)
            continue
        delta = cur - prev
        if delta == 0:
            unchanged.append(label)
        else:
            changes.append({
                "label": label, "section": section, "key": key,
                "previous": prev, "current": cur, "delta": delta,
                "direction": "up" if delta > 0 else "down",
            })
    return {"first_cycle": False, "changes": changes, "unknown": unknown, "unchanged": unchanged}


# ── persistence, via Phase C operating memory (no new store) ─────────────

_SNAPSHOT_TYPE = "business_snapshot"


def save_snapshot(snap: dict[str, Any], run_date: str) -> None:
    from actions import operating_memory
    operating_memory.record(
        _SNAPSHOT_TYPE, source="business_state", subject=run_date,
        summary=f"Business snapshot for {run_date}", data=snap, ok=True,
    )


def previous_snapshot(before_ts: Optional[float] = None) -> Optional[dict[str, Any]]:
    """The most recent snapshot saved BEFORE `before_ts`.

    Selecting by time rather than by run_date matters: a cycle re-run on the
    same UTC date (a forced run, a manual re-trigger, a test) shares its
    run_date with the snapshot it is about to compare against, so excluding
    by date threw away the only prior state and reported every such cycle as
    the first one. A timestamp cutoff is the actual question being asked —
    "what did the world look like before this cycle started" — and it is
    correct for both the daily case and the same-day re-run."""
    from actions import operating_memory
    for entry in operating_memory.recall(entry_type=_SNAPSHOT_TYPE, limit=20):
        if before_ts is not None and entry.get("ts", 0) >= before_ts:
            continue
        data = entry.get("data")
        if isinstance(data, dict) and data:
            return data
    return None
