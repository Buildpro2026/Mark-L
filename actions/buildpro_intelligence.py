"""Morning recruiting intelligence — data foundation only.

Assembles structured data for the future morning recruiting report from the
real BuildPro data model (actions/buildpro_data.py): top matches,
highest-priority prospects, candidate/client follow-ups, new jobs, recent
activity, and a small set of rule-based recommended actions. Nothing here
sends anything (no email/SMS/social) — that's explicitly out of scope for
this module; generate_morning_report_data() just returns a dict a future
delivery step (voice briefing, email, dashboard) can consume.

Every figure comes directly from buildpro_data's public query functions —
"recommended actions" are simple, transparent rules over those real counts
(e.g. "N candidates need follow-up"), not generated/inferred text.
"""
from __future__ import annotations

import time
from typing import Any

from actions import buildpro_data as bd

_CONSTRUCTION_HINTS = ("construction", "building", "contractor", "engineering", "architecture")
_NEW_JOB_WINDOW_DAYS = 7.0


def _is_likely_construction(industry: str | None) -> bool:
    if not industry:
        return False
    industry_lower = industry.lower()
    return any(hint in industry_lower for hint in _CONSTRUCTION_HINTS)


def get_highest_priority_prospects(limit: int = 5) -> list[dict[str, Any]]:
    """Most-recently-identified prospects (status='prospect'), flagged with
    a real, re-derived (not stored/guessed) construction-industry signal
    from the persisted `industry` field — same signal
    buildpro_prospecting_agent uses when first identifying them."""
    prospects = bd.list_clients(status="prospect", limit=limit * 3)
    for p in prospects:
        p["likely_construction_related"] = _is_likely_construction(p.get("industry"))
    prospects.sort(key=lambda p: (not p["likely_construction_related"], -p["updated_ts"]))
    return prospects[:limit]


def _recommended_actions(
    candidate_followups: list[dict[str, Any]], client_followups: list[dict[str, Any]],
    top_matches: list[dict[str, Any]], unmatched_jobs: list[dict[str, Any]],
    new_jobs: list[dict[str, Any]],
) -> list[str]:
    """Small, transparent rule set over real counts already computed above
    — every action names the exact number driving it, nothing inferred."""
    actions: list[str] = []
    if candidate_followups:
        actions.append(f"{len(candidate_followups)} candidate(s) haven't been touched in "
                        f"{int(bd.FOLLOWUP_STALE_DAYS)}+ days and need follow-up.")
    if client_followups:
        actions.append(f"{len(client_followups)} client/prospect(s) haven't been touched in "
                        f"{int(bd.FOLLOWUP_STALE_DAYS)}+ days and need follow-up.")
    unsubmitted_high_scores = [
        m for m in top_matches
        if m["match_score"] is not None and m["match_score"] >= bd.QUALIFIED_MATCH_SCORE and m["status"] == "proposed"
    ]
    if unsubmitted_high_scores:
        actions.append(
            f"{len(unsubmitted_high_scores)} high-scoring match(es) (>= {bd.QUALIFIED_MATCH_SCORE}) "
            "are still 'proposed' and ready to submit."
        )
    if unmatched_jobs:
        actions.append(f"{len(unmatched_jobs)} open job(s) have no candidate matches scored yet.")
    if new_jobs:
        actions.append(f"{len(new_jobs)} new job(s) opened in the last {int(_NEW_JOB_WINDOW_DAYS)} days.")
    if not actions:
        actions.append("No urgent recruiting follow-ups identified.")
    return actions


def generate_morning_report_data() -> dict[str, Any]:
    """The single entry point for a future morning recruiting report —
    returns structured data only (no delivery of any kind). Reuses
    buildpro_data.summary() for counts and layers on the report-specific
    detail (top matches, priority prospects, new jobs, unmatched jobs,
    recommended actions) that summary() doesn't need to carry for the
    Command Center."""
    base = bd.summary()
    top_matches = bd.top_matches(limit=5)
    priority_prospects = get_highest_priority_prospects(limit=5)
    candidate_followups = bd.list_candidates_needing_followup(limit=10)
    client_followups = bd.list_clients_needing_followup(limit=10)
    new_jobs = bd.list_jobs(created_after=time.time() - _NEW_JOB_WINDOW_DAYS * 86400, limit=10)
    unmatched_jobs = bd.list_unmatched_open_jobs(limit=10)

    return {
        "generated_ts": time.time(),
        "counts": {
            "candidate_count": base["candidate_count"],
            "client_count": base["client_count"],
            "prospect_count": base["prospect_count"],
            "active_jobs": base["active_jobs"],
            "qualified_matches": base["qualified_matches"],
        },
        "top_matches": top_matches,
        "highest_priority_prospects": priority_prospects,
        "candidate_followups": candidate_followups,
        "client_followups": client_followups,
        "new_jobs": new_jobs,
        "unmatched_open_jobs": unmatched_jobs,
        "recent_activity": base["recent_activity"],
        "recommended_actions": _recommended_actions(
            candidate_followups, client_followups, top_matches, unmatched_jobs, new_jobs,
        ),
        "last_hubspot_sync": {
            "candidates": bd.get_last_sync("candidates"),
            "clients": bd.get_last_sync("clients"),
        },
    }
