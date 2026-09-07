"""The daily CEO report, built from what the cycle actually did.

The distinction this module exists to enforce, and the reason it is a
module rather than an f-string:

    recommendation != task created != approval requested
    != executed != verified != completed

Those are six different states, and a report that blurs them is worse than
no report — it tells Lee that candidates were contacted when a task to
prepare outreach was created, and he stops trusting the whole thing. Each
section below draws from exactly one of those states, and ACTIONS TAKEN
draws only from tasks that reached DONE with a passing verification.

Failures are classified rather than lumped together, because "HubSpot has
no token" and "BuildPro's matcher crashed" demand completely different
responses and only one of them is a business problem.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("jarvis.ceo_report")

# Failure classes. The first three are configuration/infrastructure facts,
# not business outcomes, and the report says so explicitly so an unset
# credential never reads as a failing business.
NOT_CONFIGURED = "NOT_CONFIGURED"
AUTH_ERROR = "AUTH_ERROR"
UNAVAILABLE = "UNAVAILABLE"
TASK_FAILURE = "TASK_FAILURE"
DELIVERY_FAILURE = "DELIVERY_FAILURE"
APPROVAL_REQUIRED = "APPROVAL_REQUIRED"

_INFRASTRUCTURE = {NOT_CONFIGURED, AUTH_ERROR, UNAVAILABLE}

# A bounded list. An executive report with forty priorities has none.
MAX_PRIORITIES = 5
MAX_OPPORTUNITIES = 5
MAX_PER_SECTION = 8


def _money(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "unknown"
    return f"${value:,.0f}"


# ── section builders ─────────────────────────────────────────────────────

def _blockers(health: dict, business: dict, execution: dict,
              delivery_ok: bool, tasks: dict) -> list[dict[str, Any]]:
    """Everything standing in the way, each labelled with WHY."""
    out: list[dict[str, Any]] = []

    for name, result in (health.get("integrations") or {}).items():
        state = result.get("state")
        if state in ("NOT_CONFIGURED", "AUTH_ERROR", "UNAVAILABLE"):
            out.append({
                "kind": state, "subject": name,
                "detail": result.get("detail") or state,
                "infrastructure": True,
            })

    for source, state in (business.get("states") or {}).items():
        if state in ("NOT_CONFIGURED", "AUTH_ERROR", "UNAVAILABLE"):
            # Already covered by the integration probe above when the names
            # line up; keep the business-source view only when it adds a
            # source the health check does not know about.
            if not any(b["subject"] == source for b in out):
                out.append({"kind": state, "subject": source,
                            "detail": f"business source {source}", "infrastructure": True})

    failed = [t for t in _all_tasks(execution) if _task_failed(t)]
    for t in failed[:MAX_PER_SECTION]:
        out.append({
            "kind": TASK_FAILURE, "subject": t.agent_id,
            "detail": (t.error or "task did not complete cleanly")[:200],
            "infrastructure": False,
            "escalated": bool(getattr(t, "escalated", False)),
        })

    if not delivery_ok:
        out.append({"kind": DELIVERY_FAILURE, "subject": "morning brief",
                    "detail": "the report could not be delivered", "infrastructure": True})

    awaiting = (tasks or {}).get("awaiting_approval") or 0
    if awaiting:
        out.append({"kind": APPROVAL_REQUIRED, "subject": "owner",
                    "detail": f"{awaiting} action(s) waiting on your decision",
                    "infrastructure": False})
    return out


def _all_tasks(execution: dict) -> list:
    return [*(execution.get("due_tasks") or []), *(execution.get("stale_tasks") or [])]


def _task_failed(task) -> bool:
    status = getattr(task.status, "value", task.status)
    return status in ("failed", "expired")


# A handler that ran cleanly but had nothing to work on because its
# integration is unconfigured did NOT take an action. Those tasks legitimately
# reach DONE and verify successfully — the agent behaved correctly — but
# reporting them under ACTIONS TAKEN said "JARVIS did 8 things" on a cycle
# where it did none, which is the exact overstatement this module exists to
# prevent. They get their own section instead: visible, never hidden, never
# counted as work.
_NO_OP_MARKERS = (
    "isn't configured", "is not configured", "not configured",
    "isn't authorized", "is not authorized", "not authorized",
    "nothing to sync", "nothing to scan", "nothing to process",
    "nothing to route", "nothing to do", "no urgent events",
    "nothing pending", "no products", "0 top product",
)


def _is_no_op(task) -> bool:
    result = task.result if isinstance(task.result, dict) else {}
    state = str(result.get("state") or "").upper()
    if state in ("NOT_CONFIGURED", "NOT_AUTHORIZED", "AUTH_ERROR"):
        return True
    summary = str(result.get("summary") or "").lower()
    return any(marker in summary for marker in _NO_OP_MARKERS)


def _no_ops(execution: dict) -> list[dict[str, Any]]:
    out = []
    for t in _all_tasks(execution):
        status = getattr(t.status, "value", t.status)
        if status == "done" and _is_no_op(t):
            summary = ((t.result or {}).get("summary") if isinstance(t.result, dict) else None) or ""
            out.append({"agent_id": t.agent_id, "reason": str(summary)[:160]})
    return out[:MAX_PER_SECTION]


def _actions_taken(execution: dict, verifications: list[dict]) -> list[dict[str, Any]]:
    """ONLY work that finished and verified.

    A task that ran and returned a handler-level error is not an action
    taken; nor is a task that produced a draft awaiting approval. Anything
    that executed without a passing verification record is reported as
    attempted, not completed, in _attempted()."""
    verified_ids = {
        v.get("reference_id") for v in (verifications or [])
        if v.get("success") is True
    }
    out = []
    for t in _all_tasks(execution):
        status = getattr(t.status, "value", t.status)
        if status != "done" or t.id not in verified_ids:
            continue
        if _is_no_op(t):
            continue   # ran correctly, but performed no work — see _no_ops()
        summary = ((t.result or {}).get("summary") if isinstance(t.result, dict) else None) or "completed"
        out.append({"agent_id": t.agent_id, "task_id": t.id, "result": str(summary)[:200],
                    "verified": True})
    return out[:MAX_PER_SECTION]


def _attempted(execution: dict, verifications: list[dict]) -> list[dict[str, Any]]:
    """Ran, but the outcome is not confirmed. Honest middle ground between
    "did it" and "failed": an external call with no safe verification path
    belongs here, never in ACTIONS TAKEN."""
    verified_ids = {v.get("reference_id") for v in (verifications or []) if v.get("success") is True}
    out = []
    for t in _all_tasks(execution):
        status = getattr(t.status, "value", t.status)
        if status == "done" and t.id not in verified_ids and not _is_no_op(t):
            out.append({"agent_id": t.agent_id, "task_id": t.id, "verified": False})
    return out[:MAX_PER_SECTION]


def _tasks_created(business: dict) -> list[dict[str, Any]]:
    """Created this cycle — created, not performed."""
    return [
        {"kind": d.get("kind"), "title": d.get("title"),
         "agent_id": d.get("agent_id"), "task_id": d.get("task_id"),
         "state": d.get("task_status")}
        for d in (business.get("dispatched") or [])
    ][:MAX_PER_SECTION]


def _opportunities(state: dict) -> list[dict[str, Any]]:
    """Ranked by the signals that already exist, capped so one noisy source
    cannot fill the whole section."""
    opps = ((state.get("opportunities") or {}).get("top")) or []
    bp = state.get("buildpro") or {}
    out = [{"source": "opportunity_engine", "title": o.get("title"),
            "score": o.get("score"), "business": o.get("business")} for o in opps]
    if isinstance(bp.get("strong_matches"), int) and bp["strong_matches"] > 0:
        out.insert(0, {
            "source": "buildpro", "title": f"{bp['strong_matches']} strong candidate/job match(es) ready",
            "score": None, "business": "buildpro",
        })
    ddf = state.get("ddf") or {}
    if isinstance(ddf.get("high_ticket_products"), int) and ddf["high_ticket_products"] > 0:
        out.append({"source": "ddf", "title": f"{ddf['high_ticket_products']} high-ticket product(s) tracked",
                    "score": None, "business": "ddf"})
    return out[:MAX_OPPORTUNITIES]


def _priorities(blockers: list[dict], state: dict, movement: dict,
                approvals: int) -> list[dict[str, Any]]:
    """A bounded, ordered list of what deserves attention next.

    Ordering is deliberate rather than by raw count: a decision only Lee can
    make outranks anything JARVIS could do itself, and a broken credential
    outranks new opportunities because it is what is preventing them from
    being found."""
    out: list[dict[str, Any]] = []

    if approvals:
        out.append({"priority": "approve or reject pending actions",
                    "why": f"{approvals} action(s) cannot proceed without your decision",
                    "kind": APPROVAL_REQUIRED})

    auth = [b for b in blockers if b["kind"] == AUTH_ERROR]
    if auth:
        out.append({"priority": f"restore access: {', '.join(sorted({b['subject'] for b in auth}))}",
                    "why": "a working credential stopped authenticating — capabilities are offline",
                    "kind": AUTH_ERROR})

    escalated = [b for b in blockers if b["kind"] == TASK_FAILURE and b.get("escalated")]
    if escalated:
        out.append({"priority": "review escalated task failures",
                    "why": f"{len(escalated)} failure(s) could not be retried safely",
                    "kind": TASK_FAILURE})

    bp = state.get("buildpro") or {}
    if isinstance(bp.get("strong_matches"), int) and bp["strong_matches"] > 0:
        out.append({"priority": f"act on {bp['strong_matches']} strong recruiting match(es)",
                    "why": "highest-value revenue signal currently available",
                    "kind": "revenue"})

    for change in (movement.get("changes") or [])[:2]:
        if change["label"] == "cumulative revenue" and change["delta"] > 0:
            out.insert(0, {"priority": "confirm new revenue is recorded correctly",
                           "why": f"cumulative revenue moved by {_money(change['delta'])}",
                           "kind": "revenue"})

    missing = [b for b in blockers if b["kind"] == NOT_CONFIGURED]
    if missing and len(out) < MAX_PRIORITIES:
        out.append({"priority": f"connect: {', '.join(sorted({b['subject'] for b in missing})[:4])}",
                    "why": "these capabilities are unavailable until credentials are set",
                    "kind": NOT_CONFIGURED})

    return out[:MAX_PRIORITIES]


def _recommendations(state: dict, blockers: list[dict], actions: list) -> list[str]:
    """Explicitly NOT things JARVIS did. Phrased as proposals so no reader
    can mistake this section for an activity log."""
    recs = []
    tasks = state.get("tasks") or {}
    if tasks.get("awaiting_approval"):
        recs.append(f"Review the {tasks['awaiting_approval']} action(s) awaiting approval — none will run until you decide.")
    bp = state.get("buildpro") or {}
    for action in (bp.get("recommended_actions") or [])[:3]:
        recs.append(action)
    if isinstance(bp.get("unmatched_open_jobs"), int) and bp["unmatched_open_jobs"] > 0:
        recs.append(f"{bp['unmatched_open_jobs']} open job(s) have no qualified match — consider sourcing.")
    if any(b["kind"] == AUTH_ERROR for b in blockers):
        recs.append("Re-authorize the failing integration(s); dependent work is not running.")
    if not actions:
        recs.append("No verified actions completed this cycle — check whether the agents have real work available.")
    return recs[:MAX_PER_SECTION]


# ── assembly ─────────────────────────────────────────────────────────────

def build(state: dict, movement: dict, health: dict, execution: dict,
          verifications: list[dict], delivery_ok: bool = True,
          run_date: str = "") -> dict[str, Any]:
    """The structured report. Returns data, not text, so the same content
    can be rendered for SMS, a dashboard, or a test assertion without three
    divergent formatters."""
    business = execution.get("business") or {}
    tasks = state.get("tasks") or {}
    blockers = _blockers(health, business, execution, delivery_ok, tasks)
    actions = _actions_taken(execution, verifications)
    attempted = _attempted(execution, verifications)
    no_ops = _no_ops(execution)
    created = _tasks_created(business)
    opportunities = _opportunities(state)
    approvals = tasks.get("awaiting_approval") or 0
    priorities = _priorities(blockers, state, movement, approvals)

    infra = [b for b in blockers if b["infrastructure"]]
    business_failures = [b for b in blockers if not b["infrastructure"]]

    return {
        "run_date": run_date,
        "executive_summary": _summary(state, movement, actions, created, approvals, infra),
        # Named explicitly because the brief must state BuildPro's real
        # operating counts, not only risks and approvals.
        "buildpro_counts": state.get("buildpro") or {},
        "business_source_states": business.get("states") or {},
        "business_movement": movement,
        "revenue_opportunities": opportunities,
        "actions_taken": actions,
        "actions_attempted_unverified": attempted,
        "no_op_agents": no_ops,
        "tasks_created": created,
        "approvals_required": tasks.get("awaiting_approval_detail") or [],
        "blockers": blockers,
        "infrastructure_blockers": infra,
        "business_blockers": business_failures,
        "recommended_next_actions": _recommendations(state, blockers, actions),
        "ceo_priorities": priorities,
        "quiet": _is_quiet(actions, created, movement, business_failures, approvals, infra),
    }


def _summary(state, movement, actions, created, approvals, infra) -> str:
    revenue = state.get("revenue") or {}
    bits = [f"Revenue {_money(revenue.get('cumulative_usd'))} of {_money(revenue.get('target_usd'))}"]
    if movement.get("first_cycle"):
        bits.append("first recorded cycle — no comparison available yet")
    else:
        n = len(movement.get("changes") or [])
        bits.append(f"{n} metric(s) moved since the last cycle" if n else "no measured change since the last cycle")
    bits.append(f"{len(actions)} verified action(s), {len(created)} task(s) created")
    if approvals:
        bits.append(f"{approvals} awaiting your approval")
    if infra:
        bits.append(f"{len(infra)} capability blocker(s) — configuration, not business")
    return ". ".join(bits) + "."


def _is_quiet(actions, created, movement, business_failures, approvals,
              infrastructure_blockers=()) -> bool:
    """True when nothing happened worth interrupting Lee for.

    A cycle that ran cleanly and found nothing is a normal outcome, not
    something to text about — a daily message that always arrives and never
    matters trains its reader to ignore it, and then a real alert lands in a
    muted thread.

    A BROKEN capability is never quiet, though. "Nothing to report" and
    "five integrations cannot authenticate" are opposite situations, and
    suppressing the second to avoid noise is how an outage goes unnoticed
    for a week."""
    return not (actions or created or approvals or business_failures
                or infrastructure_blockers or (movement.get("changes") or []))


def render_text(report: dict[str, Any]) -> str:
    """Plain-text rendering for SMS delivery. Sections with no content are
    omitted rather than printed empty."""
    L: list[str] = []
    if report.get("run_date"):
        L.append(f"JARVIS CEO REPORT — {report['run_date']}")
    L.append(report["executive_summary"])

    bp = report.get("buildpro_counts") or {}
    if bp:
        L.append(
            f"BuildPro: {bp.get('candidates', 0)} candidate(s), "
            f"{bp.get('open_jobs', 0)} open job(s), "
            f"{bp.get('qualified_matches', 0)} qualified match(es)."
        )

    movement = report.get("business_movement") or {}
    if movement.get("changes"):
        L.append("\nBUSINESS MOVEMENT")
        for c in movement["changes"][:MAX_PER_SECTION]:
            sign = "+" if c["delta"] > 0 else ""
            L.append(f"- {c['label']}: {c['previous']} -> {c['current']} ({sign}{c['delta']})")
    elif movement.get("first_cycle"):
        L.append("\nBUSINESS MOVEMENT\n- first recorded cycle; next cycle can compare")

    if report.get("revenue_opportunities"):
        L.append("\nREVENUE OPPORTUNITIES")
        for o in report["revenue_opportunities"]:
            score = f" (score {o['score']})" if o.get("score") is not None else ""
            L.append(f"- [{o['source']}] {o['title']}{score}")

    if report.get("actions_taken"):
        L.append("\nACTIONS TAKEN (verified)")
        for a in report["actions_taken"]:
            L.append(f"- {a['agent_id']}: {a['result']}")
    if report.get("actions_attempted_unverified"):
        L.append("\nATTEMPTED (not verified)")
        for a in report["actions_attempted_unverified"]:
            L.append(f"- {a['agent_id']}: ran, outcome unconfirmed")

    if report.get("no_op_agents"):
        L.append("\nRAN BUT NO WORK AVAILABLE")
        for n in report["no_op_agents"]:
            L.append(f"- {n['agent_id']}: {n['reason']}")

    if report.get("tasks_created"):
        created_list = report["tasks_created"]
        by_kind: dict[str, int] = {}
        for t in created_list:
            by_kind[t["kind"]] = by_kind.get(t["kind"], 0) + 1
        detail = ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in sorted(by_kind.items()))
        L.append(f"\nCreated {len(created_list)} new task(s) from business findings: {detail}.")
        L.append("TASKS CREATED (not yet performed)")
        for t in created_list:
            L.append(f"- {t['title']} [{t['state']}] -> {t['agent_id']}")

    degraded = {n: st for n, st in (report.get("business_source_states") or {}).items()
                if st != "SUCCESS"}
    if degraded:
        L.append("\nBusiness sources needing attention: "
                 + ", ".join(f"{n}={st}" for n, st in sorted(degraded.items())) + ".")

    if report.get("approvals_required"):
        L.append("\nAPPROVALS REQUIRED")
        for a in report["approvals_required"]:
            L.append(f"- {a['agent_id']}: {a['description']} (task {a['task_id']})")

    if report.get("infrastructure_blockers"):
        L.append("\nCAPABILITY BLOCKERS (configuration, not business)")
        for b in report["infrastructure_blockers"]:
            L.append(f"- {b['kind']}: {b['subject']} — {b['detail']}")
    if report.get("business_blockers"):
        L.append("\nBUSINESS BLOCKERS")
        for b in report["business_blockers"]:
            L.append(f"- {b['kind']}: {b['subject']} — {b['detail']}")

    if report.get("recommended_next_actions"):
        L.append("\nRECOMMENDED NEXT ACTIONS (proposals, not performed)")
        for r in report["recommended_next_actions"]:
            L.append(f"- {r}")

    if report.get("ceo_priorities"):
        L.append("\nCEO PRIORITIES")
        for i, p in enumerate(report["ceo_priorities"], 1):
            L.append(f"{i}. {p['priority']} — {p['why']}")

    return "\n".join(L)
