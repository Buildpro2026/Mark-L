"""CEO Decision Engine — Jarvis's structured decision pipeline.

Observe -> Research -> Identify opportunity -> Analyze -> Compare alternatives
-> Recommend -> Explain upside/downside -> Request authorization (if needed)
-> Deploy agents -> Monitor -> Measure -> Learn -> Update memory -> Recommend next.

This module does not reason on its own — Gemini's live conversation does the
actual observing/analyzing/comparing/recommending (using web_search,
business_intelligence, and opportunity_engine along the way). What this
module provides is the STRUCTURE: a persistent, authorization-gated decision
log (built on business_intelligence's "decisions" category) so decisions are
traceable end to end — proposed, (if needed) authorized by Lee, and later
closed out with a recorded outcome — instead of being talked about and
forgotten. Agent deployment itself goes through actions/agent_orchestrator.py
(a separate call from Jarvis) — this module doesn't duplicate that.

Guiding question for every proposal: "Will this move us closer to the
$1,000,000 revenue objective?" — see actions/strategic_objective.py.
"""
from __future__ import annotations

from typing import Any

from actions.business_intelligence import add_entry, get_entry, list_entries, record_outcome as _bi_record_outcome
from actions.strategic_objective import get_objective_status


def propose_decision(
    business: str,
    title: str,
    analysis: str,
    alternatives: str = "",
    recommendation: str = "",
    upside: str = "",
    downside: str = "",
    requires_authorization: bool = True,
) -> dict[str, Any]:
    """Files a decision proposal. Always allowed — proposing/analyzing is not
    a consequential action by itself. Returns the objective's current status
    alongside the logged id so the proposal is framed against real progress,
    not vibes."""
    decision_id = add_entry(
        "decisions", business, title=title, content=analysis,
        data={
            "alternatives": alternatives,
            "recommendation": recommendation,
            "upside": upside,
            "downside": downside,
            "requires_authorization": bool(requires_authorization),
            "authorized": not requires_authorization,
        },
    )
    return {"decision_id": decision_id, "objective": get_objective_status()}


def authorize_decision(decision_id: int) -> dict[str, Any]:
    """Lee's explicit authorization for a decision that required it. Does
    NOT itself deploy anything — deployment goes through
    actions/agent_orchestrator.py as a separate step — this only flips the
    recorded authorization flag. The caller (main.py's ceo_decision tool)
    must only invoke this on Lee's explicit instruction, never inferred."""
    entry = get_entry(decision_id)
    if not entry or entry["category"] != "decisions":
        raise ValueError(f"No decision found with id {decision_id}")
    data = dict(entry["data"])
    data["authorized"] = True
    add_entry(
        "decisions", entry["business"], title=f"[AUTHORIZED] {entry['title']}",
        content="Lee authorized this decision.", data=data, related_id=decision_id,
    )
    return {"decision_id": decision_id, "authorized": True}


def is_authorized(decision_id: int) -> bool:
    entry = get_entry(decision_id)
    if not entry:
        return False
    if entry.get("data", {}).get("authorized"):
        return True
    # bi_entries is append-only (no UPDATE) — authorize_decision() files a
    # linked follow-up entry rather than mutating the original row, so an
    # authorization also has to be looked up via related_id, not just the
    # original entry's own (permanently stale) data.
    for candidate in list_entries(category="decisions", business=entry["business"], limit=500):
        if candidate.get("related_id") == decision_id and candidate.get("data", {}).get("authorized"):
            return True
    return False


def record_decision_outcome(
    decision_id: int, result: str, revenue_usd: float = 0.0, cost_usd: float = 0.0,
    lesson: str = "", recommendation: str = "",
) -> dict[str, Any]:
    """Closes the loop — MEASURE -> LEARN -> MEMORY — via business_
    intelligence's shared outcome/lesson/revenue tracking, linked back to
    the originating decision."""
    entry = get_entry(decision_id)
    if not entry:
        raise ValueError(f"No decision found with id {decision_id}")
    return _bi_record_outcome(
        business=entry["business"], plan=entry["title"], result=result,
        revenue_usd=revenue_usd, cost_usd=cost_usd, lesson=lesson,
        recommendation=recommendation, decision_id=decision_id,
    )
