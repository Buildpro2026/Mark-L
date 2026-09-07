"""The central CEO operating cycle (Lee's autonomous-CEO/COS spec, Section
THIRD): WAKE -> GATHER -> ANALYZE -> PRIORITIZE -> DECIDE -> EXECUTE ->
VERIFY -> FOLLOW-UP -> REPORT, as one independently schedulable function
that requires no chat/user prompt to run — see
core/headless/background.py's _run_ceo_cycle_loop for the scheduler that
calls this on a UTC-hour target, and run_cycle_once()/run_cycle(force=True)
for driving it directly (tests, a manual trigger).

This module does not reimplement anything that already works — it is
deliberately a thin orchestration layer over infrastructure this codebase
already has and already tests:
    GATHER      -> actions/executive_brief.py (Gmail/Calendar/BuildPro/DDF/
                   opportunities/business_intelligence/risks/pending
                   approvals) + actions/business_modules.py (adds the
                   CareerRocket/Airbnb honesty Section NINTH asks for)
    PRIORITIZE  -> actions/priorities_engine.py's get_todays_priorities()
    DECIDE/     -> actions/agent_orchestrator.py's run_due_agents()/
    EXECUTE        run_stale_autonomous_agents() (existing PermissionLevel
                   gates — OBSERVE/SUGGEST auto-run, EXECUTE always stays
                   PENDING_APPROVAL) plus actions/ddf_discovery.py (a new,
                   but equally safe, OBSERVE-class local write: discovered
                   products land at DISCOVERED status, never published)
    VERIFY      -> actions/verification.py's record_verification()
    FOLLOW-UP   -> a business_intelligence 'risks' entry for anything that
                   failed verification, so the next brief/priorities pass
                   surfaces it automatically (closes Section TWELFTH's
                   "must not merely log and forget" requirement)
    REPORT      -> actions/approval_notifier.py's notify_urgent_event() —
                   the SAME already-trusted self-notification channel
                   (SMS to Lee's own phone) approvals/escalations already
                   use, deliberately NOT gmail_integration.send_email(),
                   whose own code comment (core/headless/tool_executor.py,
                   the 'send_brief' gmail action) explicitly flagged that
                   an automatic daily send would need to either bypass the
                   approval gate or invent a new one. Notifying the owner
                   about his own business on his own phone is not a
                   third-party consequential act, so it doesn't need one —
                   it's the same authority basis every pending-approval
                   text already runs on.

Hard guardrail, enforced by construction, not just by comment: this module
never calls AgentOrchestrator.approve_task() or ddf.advance_to_published(...,
approved=True) or any other approval-gated send. It only ever runs
already-authorized OBSERVE/SUGGEST work and reports what still needs Lee."""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Optional

from core.headless import config
from actions.agent_orchestrator import orchestrator as agent_orchestrator
from actions import business_intelligence as biz_intel
from actions import business_modules
from actions import business_pipeline
from actions import integration_health
from actions import jarvis_brain
from actions import operating_memory
from actions import ddf_discovery
from actions import executive_brief
from actions import priorities_engine
from actions import verification

logger = logging.getLogger("jarvis.ceo_operating_cycle")


def _connect() -> sqlite3.Connection:
    config.ensure_data_dir()
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ceo_cycle_runs (
            run_date    TEXT PRIMARY KEY,
            run_ts      REAL NOT NULL,
            summary     TEXT,
            risk_count  INTEGER,
            agents_run  INTEGER
        )
    """)
    conn.commit()
    return conn


def _utc_date(now: Optional[float] = None) -> str:
    ts = now if now is not None else time.time()
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def already_ran_today(run_date: Optional[str] = None) -> bool:
    run_date = run_date or _utc_date()
    try:
        conn = _connect()
        try:
            row = conn.execute("SELECT 1 FROM ceo_cycle_runs WHERE run_date = ?", (run_date,)).fetchone()
            return row is not None
        finally:
            conn.close()
    except Exception:
        return False  # a persistence hiccup must not permanently block the cycle from ever running


def _mark_ran(run_date: str, summary: str, risk_count: int, agents_run: int) -> None:
    try:
        conn = _connect()
        conn.execute(
            "INSERT OR REPLACE INTO ceo_cycle_runs (run_date, run_ts, summary, risk_count, agents_run) VALUES (?,?,?,?,?)",
            (run_date, time.time(), summary, risk_count, agents_run),
        )
        conn.commit()
        conn.close()
    except Exception:
        logger.debug("could not persist ceo_cycle_runs row for %s", run_date, exc_info=True)


def _task_ok(task) -> bool:
    """A task counts as verified-success only if it actually finished
    clean — matches AgentOrchestrator.run_task's own DONE/error-vs-failure
    distinction (see its 2026-09-02 reliability-audit comment) rather than
    treating any non-exception result as success."""
    if task.status.value != "done":
        return False
    result = task.result or {}
    if isinstance(result, dict) and (result.get("error") or result.get("failed")):
        return False
    return True


def _gather() -> dict[str, Any]:
    brief = executive_brief.generate_brief()
    modules = business_modules.gather_all()

    # What is actually possible this morning, established BEFORE any work is
    # attempted. Previously the cycle tried everything and learned the
    # answer through failures, which made an unset credential look identical
    # to a broken one.
    try:
        health = integration_health.check_all()
    except Exception as exc:
        logger.warning("integration health check failed: %s", exc)
        health = {"integrations": {}, "healthy": [], "degraded": {}, "capabilities": {}}

    # The Brain, on the autonomous path. Retrieval is query-scoped and
    # budgeted; a missing or unreadable vault yields {} and the cycle runs
    # exactly as before — knowledge informs decisions, it is not a
    # prerequisite for making them.
    try:
        knowledge = jarvis_brain.operating_context(["mandate", "approval"])
    except Exception:
        logger.debug("brain context unavailable", exc_info=True)
        knowledge = {}

    return {"brief": brief, "modules": modules, "health": health, "knowledge": knowledge}


def _prioritize() -> list[dict[str, Any]]:
    return priorities_engine.get_todays_priorities(limit=20, min_severity=1)


def _decide_and_execute() -> dict[str, Any]:
    """DECIDE is implicit here, not a separate pass: get_due_agents()/
    get_stale_autonomous_agents() already ARE the decision of what's safe
    to run unattended (status IDLE + not EXECUTE-level) — re-deriving that
    logic here would be a second, driftable copy of the same policy. This
    just calls the two existing, already-tested dispatch functions and
    layers the one genuinely new autonomous action (DDF discovery, which
    is itself OBSERVE-class: a local DISCOVERED-status write, never a
    publish) alongside them."""
    # Turn real business findings into real tasks BEFORE the sweep runs, so
    # anything discovered this morning is executed in the same cycle rather
    # than waiting a day. This is what connects the integrations (Gmail,
    # Calendar, HubSpot, DDF, BuildPro, research) to the agent workforce;
    # each source is isolated inside gather_and_dispatch, and every finding
    # is claimed once through autonomous_ledger so a subject cannot generate
    # the same work on every sweep.
    try:
        business = business_pipeline.gather_and_dispatch()
    except Exception as exc:
        logger.exception("business pipeline pass failed")
        business = {"sources": {}, "states": {}, "dispatched": [], "total_dispatched": 0,
                    "healthy_sources": 0, "error": str(exc)}

    due_tasks = agent_orchestrator.run_due_agents()
    stale_tasks = agent_orchestrator.run_stale_autonomous_agents()

    discovery_result: dict[str, Any]
    try:
        discovery_result = ddf_discovery.discover_new_products()
    except Exception as exc:
        logger.exception("ddf_discovery.discover_new_products() raised")
        discovery_result = {"ok": False, "state": "ERROR", "detail": str(exc), "discovered": [], "saved": 0, "errors": [{"detail": str(exc)}]}

    return {"due_tasks": due_tasks, "stale_tasks": stale_tasks,
            "discovery_result": discovery_result, "business": business}


def _verify_and_followup(execution: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    for task in [*execution["due_tasks"], *execution["stale_tasks"]]:
        ok = _task_ok(task)
        reason = "" if ok else (task.error or (task.result or {}).get("error") or "agent task did not complete cleanly")
        rec = verification.record_verification(
            "ceo_cycle_agent_run", intended=f"run agent {task.agent_id}", actual=f"task {task.id} -> {task.status.value}",
            success=ok, provider_response=task.result, reference_id=task.id,
            external_system="agent_orchestrator", follow_up_required=not ok, follow_up_reason=reason,
        )
        records.append(rec)
        if not ok:
            try:
                biz_intel.add_entry(
                    "risks", "general", title=f"CEO cycle: agent {task.agent_id} needs attention",
                    content=reason, data={"task_id": task.id, "agent_id": task.agent_id},
                )
            except Exception:
                logger.debug("could not file follow-up risk entry for task %s", task.id, exc_info=True)

    discovery = execution["discovery_result"]
    discovery_ok = bool(discovery.get("ok")) and not discovery.get("errors")
    discovery_reason = "" if discovery_ok else (
        discovery.get("detail") or (discovery.get("errors") or [{}])[0].get("detail", "discovery run reported errors")
    )
    disc_rec = verification.record_verification(
        "ceo_cycle_ddf_discovery", intended="discover new DDF product candidates",
        actual=f"provider={discovery.get('provider')} state={discovery.get('state')} saved={discovery.get('saved', 0)}",
        success=discovery_ok, provider_response=discovery,
        external_system="ddf_discovery",
        # NOT_CONFIGURED is an honest, expected state (no credential yet),
        # not a failure that needs a follow-up risk entry every single day.
        follow_up_required=discovery_ok is False and discovery.get("state") not in ("NOT_CONFIGURED",),
        follow_up_reason=discovery_reason,
    )
    records.append(disc_rec)
    if disc_rec["follow_up_required"]:
        try:
            biz_intel.add_entry(
                "risks", "ddf", title="CEO cycle: DDF product discovery failed",
                content=discovery_reason, data={"provider": discovery.get("provider")},
            )
        except Exception:
            logger.debug("could not file DDF discovery follow-up risk entry", exc_info=True)

    return records


def _format_report(gathered: dict[str, Any], priorities: list[dict[str, Any]], execution: dict[str, Any], verifications: list[dict[str, Any]]) -> str:
    brief = gathered["brief"]
    risks = brief.get("risks", [])
    approvals = brief.get("pending_approvals", [])
    ddf_snapshot = brief.get("daily_deal_finders", {})
    failed_verifications = [v for v in verifications if not v["success"] and v.get("follow_up_required")]

    lines = [f"Morning cycle — {len(priorities)} item(s) need attention."]
    buildpro = brief.get("buildpro", {})
    bp_counts = buildpro.get("counts", {})
    if bp_counts:
        lines.append(
            f"BuildPro: {bp_counts.get('candidate_count', 0)} candidate(s), "
            f"{bp_counts.get('active_jobs', 0)} open job(s), "
            f"{bp_counts.get('qualified_matches', 0)} qualified match(es)."
        )
    bp_actions = [a for a in buildpro.get("recommended_actions", []) if a != "No urgent recruiting follow-ups identified."]
    if bp_actions:
        lines.append("BuildPro follow-up: " + " ".join(bp_actions[:3]))
    if risks:
        lines.append(f"RISKS: {len(risks)} — top: {risks[0]['detail'][:140]}")
    if approvals:
        lines.append(f"APPROVALS WAITING: {len(approvals)}")
    if ddf_snapshot.get("high_ticket_picks"):
        names = ", ".join(p.get("name", "") for p in ddf_snapshot["high_ticket_picks"])
        lines.append(f"DDF high-ticket picks ready: {names}")
    discovery = execution["discovery_result"]
    if discovery.get("state") == "NOT_CONFIGURED":
        lines.append("DDF discovery: no product-data API key configured yet.")
    elif discovery.get("saved"):
        lines.append(f"DDF discovery: {discovery['saved']} new candidate(s) found (not yet published).")
    ran = len(execution["due_tasks"]) + len(execution["stale_tasks"])
    lines.append(f"Ran {ran} agent task(s) autonomously.")

    # What the business sources actually produced this cycle. Degraded
    # sources are named rather than quietly omitted — a morning brief that
    # hides a broken Gmail connection is worse than one that says so.
    business = execution.get("business") or {}
    if business.get("total_dispatched"):
        by_kind: dict[str, int] = {}
        for d in business["dispatched"]:
            by_kind[d["kind"]] = by_kind.get(d["kind"], 0) + 1
        detail = ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in sorted(by_kind.items()))
        lines.append(f"Created {business['total_dispatched']} new task(s) from business findings: {detail}.")
    unavailable = [c for c, ok in (gathered.get("health", {}).get("capabilities") or {}).items() if not ok]
    if unavailable:
        lines.append("Capabilities unavailable this cycle: " + ", ".join(sorted(unavailable)) + ".")

    degraded = [n for n, st in (business.get("states") or {}).items() if st != business_pipeline.SUCCESS]
    if degraded:
        states = ", ".join(f"{n}={business['states'][n]}" for n in sorted(degraded))
        lines.append(f"Business sources needing attention: {states}.")
    if failed_verifications:
        lines.append(f"{len(failed_verifications)} item(s) failed verification — filed as risks for follow-up.")
    unimplemented = [m["name"] for m in gathered["modules"].values() if not m["implemented"]]
    if unimplemented:
        lines.append(f"Not yet wired into this cycle: {', '.join(unimplemented)}.")
    return "\n".join(lines)


def _deliver_report(run_date: str, summary_text: str) -> dict[str, Any]:
    from actions import notifications
    # level=2 is the whole severity statement: send the morning brief as an
    # SMS, but don't escalate it to a phone call the way a level-3 incident
    # does (see approval_notifier's 0=log 1=dashboard 2=SMS 3=SMS+call scale).
    result = notifications.daily_report(
        event_id=f"ceo_cycle-{run_date}", title="JARVIS Morning Brief", detail=summary_text,
    )
    # The router returns its own envelope; the cycle has always reported the
    # transport's result, so unwrap rather than change what callers see.
    return result.get("delivery", result)


def _record_delivery_failure(run_date: str, detail: str) -> None:
    """Files a failed morning-brief delivery through the same verification
    and risk mechanisms the cycle already uses for a failed agent run, so it
    surfaces in tomorrow's brief instead of vanishing into a log line.
    Deliberately does NOT try to notify about the notification failing —
    the delivery channel is the thing that just broke."""
    try:
        verification.record_verification(
            "ceo_cycle_report_delivery", intended="deliver the morning brief to Lee",
            actual=f"delivery raised: {detail}", success=False,
            external_system="approval_notifier", reference_id=f"ceo_cycle-{run_date}",
            follow_up_required=True, follow_up_reason=detail,
        )
    except Exception:
        logger.debug("could not record delivery-failure verification", exc_info=True)
    try:
        biz_intel.add_entry(
            "risks", "general", title="CEO cycle: morning brief was not delivered",
            content=detail, data={"run_date": run_date},
        )
    except Exception:
        logger.debug("could not file delivery-failure risk entry", exc_info=True)


def _remember_cycle(run_date: str, result: dict[str, Any],
                    execution: dict[str, Any], delivery_ok: bool) -> None:
    """Writes this cycle and each agent outcome into operating memory.

    Wrapped end to end: recording that the work happened must never be the
    reason the work is reported as failed. Per-agent outcomes are what
    self_healing.failure_streak() later reads to tell a broken agent from an
    unlucky one."""
    try:
        operating_memory.record(
            operating_memory.CYCLE_RUN, source="ceo_operating_cycle",
            subject=run_date,
            summary=(f"Cycle {run_date}: {result['agents_run']} agent task(s), "
                     f"{(result.get('notification') or {}).get('action', 'n/a')} delivery"),
            data={
                "agents_run": result["agents_run"],
                "state": result["state"],
                "business_dispatched": (execution.get("business") or {}).get("total_dispatched", 0),
            },
            ok=delivery_ok,
        )
        for task in [*execution.get("due_tasks", []), *execution.get("stale_tasks", [])]:
            operating_memory.record(
                operating_memory.AGENT_OUTCOME, source=task.agent_id, subject=task.id,
                summary=(task.error or (task.result or {}).get("summary") or task.status.value)[:300],
                ok=_task_ok(task),
            )
    except Exception:
        logger.debug("could not write cycle outcome to operating memory", exc_info=True)


def run_cycle(force: bool = False, dry_run: bool = False) -> dict[str, Any]:
    """The full WAKE->REPORT pass. Runs at most once per UTC calendar date
    unless force=True (tests, or a manual re-run Lee explicitly asks for).
    dry_run=True runs every read/decide/execute/verify step for real but
    skips only the final notification send — useful for testing the whole
    pipeline without texting Lee's phone every time."""
    run_date = _utc_date()
    if not force and already_ran_today(run_date):
        return {"ok": True, "state": "ALREADY_RAN_TODAY", "run_date": run_date}

    wake_ts = time.time()
    gathered = _gather()
    priorities = _prioritize()
    execution = _decide_and_execute()
    verifications = _verify_and_followup(execution)
    summary_text = _format_report(gathered, priorities, execution, verifications)

    # REPORT delivery is the last stage, and it is the only one whose
    # failure must not erase the four that already succeeded. Previously a
    # raise here propagated out of run_cycle BEFORE _mark_ran(), so a Twilio
    # outage meant: every agent had run, discovery had run, verifications
    # were filed — and none of it was recorded as having happened. The
    # background loop then saw already_ran_today() == False and re-ran the
    # entire cycle, repeating all that work, every 15 minutes.
    #
    # A delivery failure is now its own distinct outcome. The work is
    # recorded, the failure is recorded honestly through the same
    # verification + risk mechanisms every other cycle failure uses (so
    # tomorrow's brief surfaces it), and nothing pretends an SMS was sent.
    delivery_ok = True
    if dry_run:
        notification: dict[str, Any] = {"action": "skipped_dry_run"}
    else:
        try:
            notification = _deliver_report(run_date, summary_text)
        except Exception as exc:
            delivery_ok = False
            logger.exception("CEO cycle report delivery failed")
            notification = {"action": "failed", "ok": False, "error": str(exc)}
            _record_delivery_failure(run_date, str(exc))

    state = "RAN" if delivery_ok else "RAN_REPORT_UNDELIVERED"
    result = {
        "ok": True, "state": state, "run_date": run_date, "wake_ts": wake_ts,
        "priorities": priorities, "agents_run": len(execution["due_tasks"]) + len(execution["stale_tasks"]),
        "discovery": execution["discovery_result"], "verifications": verifications,
        "summary": summary_text, "notification": notification,
        "report_delivered": delivery_ok,
    }
    _mark_ran(run_date, summary_text, risk_count=len(gathered["brief"].get("risks", [])), agents_run=result["agents_run"])
    _remember_cycle(run_date, result, execution, delivery_ok)
    return result
