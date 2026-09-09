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
from actions import business_state
from actions import ceo_report
from actions import ceo_decision
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
    """Whether a task actually finished clean. Delegates to
    agent_orchestrator.task_succeeded() — the same check now used when an
    approved EXECUTE task's outcome is recorded — rather than keeping a
    second copy of the DONE/error-vs-failure distinction here."""
    from actions.agent_orchestrator import task_succeeded
    return task_succeeded(task)


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


def _escalate_flagged_priorities(priorities: list[dict[str, Any]], dry_run: bool = False) -> list[dict[str, Any]]:
    """Turn a decision-layer escalation into an actual SMS.

    Only items ceo_decision.decide() has ALREADY flagged escalate=True are
    touched — this makes no new escalation judgement of its own, it only
    delivers the one that already exists. Isolated per item: one failing
    notification must not stop the others from being sent or block the
    rest of the cycle."""
    from actions import approval_notifier
    import hashlib

    sent: list[dict[str, Any]] = []
    for item in priorities or []:
        decision = item.get("decision") or {}
        if not decision.get("escalate"):
            continue
        title = str(item.get("title") or "an item")[:200]
        source = str(item.get("source") or "unknown")
        # Stable across cycles for the SAME item so notify_urgent_event's
        # own dedup (keyed on event_id) sends it once, not once per cycle.
        event_id = "ceo-escalation-" + hashlib.sha256(f"{source}:{title}".encode()).hexdigest()[:16]
        detail = (decision.get("why") or "repeated failures — needs a person to look at it")
        try:
            outcome = approval_notifier.notify_urgent_event(
                event_id=event_id, title=f"Needs your attention: {title}",
                detail=detail, level=3, dry_run=dry_run)
            sent.append({"source": source, "title": title, **outcome})
        except Exception as exc:
            logger.warning("could not deliver escalation for %s: %s", source, exc)
            sent.append({"source": source, "title": title, "action": "failed", "error": str(exc)[:200]})
    return sent


def _prioritize() -> list[dict[str, Any]]:
    """PRIORITIZE — real ranking, not a fixed severity tier.

    priorities_engine still produces the signals; replacing it would be a
    second source of the same facts. What changed is what orders them:
    get_todays_priorities() sorted by a fixed tier (risk=4, approval=3,
    stale=2, recommendation=1), so revenue, deadlines, dependencies and —
    most importantly — prior failures never entered the ordering, and an
    item that had failed four times ranked exactly where it did the first
    time. ceo_decision.plan() retrieves the relevant memory for each item
    and reranks on it, attaching the reasoning and the disposition.

    Falls back to the raw list if the decision layer fails: an unranked
    priority list is worse than a ranked one and far better than none."""
    raw = priorities_engine.get_todays_priorities(limit=20, min_severity=1)
    try:
        return ceo_decision.plan(raw, limit=20)
    except Exception:
        logger.exception("decision layer failed; using unranked priorities")
        return raw


def _decide_and_execute() -> dict[str, Any]:
    """DECIDE is implicit here, not a separate pass: get_due_agents()/
    get_stale_autonomous_agents() already ARE the decision of what's safe
    to run unattended (status IDLE + not EXECUTE-level) — re-deriving that
    logic here would be a second, driftable copy of the same policy. This
    just calls the two existing, already-tested dispatch functions and
    layers the one genuinely new autonomous action (DDF discovery, which
    is itself OBSERVE-class: a local DISCOVERED-status write, never a
    publish) alongside them."""
    # BuildPro matching runs FIRST, before the business-findings sweep below
    # reads its results. gather_and_dispatch()'s buildpro_findings() reads
    # bd.top_matches() — the table run_and_report() just discovered jobs
    # into and scored — so if the sweep ran first it dispatched outreach
    # tasks against YESTERDAY's matches while today's freshly-discovered
    # jobs sat unmatched-into-tasks until tomorrow's cycle. Isolated like
    # every other stage: a matching failure must not cost the rest.
    try:
        from actions import buildpro_daily
        matching = buildpro_daily.run_and_report()
    except Exception as exc:
        logger.exception("daily BuildPro matching failed")
        matching = {"ok": False, "state": "FAILED", "detail": str(exc)[:300],
                    "report": "", "strong_matches": 0}

    # Turn real business findings into real tasks. This is what connects
    # the integrations (Gmail, Calendar, HubSpot, DDF, BuildPro, research)
    # to the agent workforce; each source is isolated inside
    # gather_and_dispatch, and every finding is claimed once through
    # autonomous_ledger so a subject cannot generate the same work on
    # every sweep.
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
            "discovery_result": discovery_result, "business": business,
            "buildpro_matching": matching}


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

        # LEARN. The verification above establishes what actually happened;
        # this is what makes the next cycle any wiser for it.
        # record_outcome() drives operating_memory.failure_streak(), which
        # ceo_decision.score_item() reads to demote a repeatedly-failing
        # source and decide() reads to escalate one. Without this write the
        # loop verifies honestly and then forgets, which is why an agent
        # could fail every morning and rank identically every morning.
        try:
            ceo_decision.record_outcome(
                {"source": task.agent_id, "title": f"agent {task.agent_id}",
                 "kind": "agent_task", "business": "general"},
                ok=ok, detail=reason or f"task {task.id} completed", verified=ok)
        except Exception:
            logger.debug("could not record the outcome for task %s", task.id, exc_info=True)

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


def buildpro_daily_module():
    """Imported lazily so a BuildPro import problem cannot stop the whole
    module from loading — the report degrades, the cycle does not."""
    from actions import buildpro_daily
    return buildpro_daily


def _format_report(gathered: dict[str, Any], priorities: list[dict[str, Any]], execution: dict[str, Any], verifications: list[dict[str, Any]]) -> str:
    brief = gathered["brief"]
    risks = brief.get("risks", [])
    approvals = brief.get("pending_approvals", [])
    ddf_snapshot = brief.get("daily_deal_finders", {})
    failed_verifications = [v for v in verifications if not v["success"] and v.get("follow_up_required")]

    lines = [f"Morning cycle — {len(priorities)} item(s) need attention."]

    # BuildPro matches lead the report: they are the day's revenue
    # opportunity, and every number in this block is counted by
    # buildpro_daily rather than asserted here.
    matching = execution.get("buildpro_matching") or {}
    if matching.get("ok"):
        lines.append(
            f"BuildPro matching: {matching.get('strong_matches', 0)} strong match(es) "
            f"from {matching.get('jobs_evaluated', 0)} open job(s) against "
            f"{matching.get('candidates_evaluated', 0)} candidate(s).")
        top = matching.get("top")
        if top and top.get("score") is not None:
            lines.append(
                f"  Best: {top.get('candidate_name') or 'a candidate'} → "
                f"{top.get('job_title') or 'a job'} at {float(top['score']):.0f}% "
                f"— {buildpro_daily_module().recommended_action(top).lower()}.")
    elif matching:
        lines.append(f"BuildPro matching did not run: {matching.get('detail') or 'unknown error'}")
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
    tasks_snapshot = brief.get("tasks", {})
    if tasks_snapshot.get("available") and tasks_snapshot.get("overdue"):
        lines.append(f"Google Tasks: {len(tasks_snapshot['overdue'])} overdue.")
    hubspot_snapshot = brief.get("hubspot", {})
    if hubspot_snapshot.get("available") and hubspot_snapshot.get("companies"):
        lines.append(f"HubSpot: {len(hubspot_snapshot['companies'])} compan(y/ies) "
                     f"not yet assessed as a recruiting opportunity.")
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


def _safe_stage(name: str, fn, default):
    """Runs one lifecycle stage in isolation, and says so.

    A cycle is a chain of stages, and one broken subsystem must not cost the
    other twelve. A failing stage contributes its documented default and the
    cycle continues — which is why a snapshot failure produces {} (reported
    as unknown) rather than aborting before any agent has run.

    Isolation like that is invisible by default, which is its own hazard: a
    stage can fail silently for weeks while the cycle reports success. Every
    stage therefore emits one structured line saying which component ran,
    how long it took, and whether it succeeded or fell back. No payloads are
    logged — only the stage name, duration and outcome — so a log line can
    never carry a credential or customer data."""
    started = time.time()
    try:
        result = fn()
    except Exception:
        logger.exception(
            "ceo_cycle stage=%s outcome=failed duration_ms=%d action=fell_back_to_default",
            name, int((time.time() - started) * 1000),
        )
        return default
    logger.info(
        "ceo_cycle stage=%s outcome=ok duration_ms=%d",
        name, int((time.time() - started) * 1000),
    )
    return result


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
    logger.info("ceo_cycle event=start run_date=%s forced=%s dry_run=%s", run_date, force, dry_run)

    # WAKE -> LOAD MEMORY -> LOAD BRAIN -> CHECK HEALTH. _gather() already
    # performs the last three; the previous snapshot is what lets this cycle
    # answer "what changed" rather than only "what is".
    gathered = _gather()
    previous = _safe_stage("previous_snapshot",
                           lambda: business_state.previous_snapshot(before_ts=wake_ts), None)

    # ASSESS BUSINESS STATE -> COMPARE WITH PREVIOUS CYCLE
    snapshot = _safe_stage("business_snapshot", business_state.snapshot, {})
    movement = _safe_stage("compare", lambda: business_state.compare(snapshot, previous),
                           {"first_cycle": True, "changes": [], "unknown": [], "unchanged": []})

    # IDENTIFY -> PRIORITIZE -> CREATE SAFE TASKS -> EXECUTE AUTHORIZED WORK.
    # Task creation and execution both live in _decide_and_execute; the
    # approval gate inside assign_task is what keeps EXECUTE-level work at
    # PENDING_APPROVAL rather than running here.
    priorities = _prioritize()
    execution = _decide_and_execute()

    # VERIFY
    verifications = _verify_and_followup(execution)

    # ESCALATE. ceo_decision.decide() flags an item escalate=True when its
    # source has failed repeatedly (>= ESCALATION_STREAK) — a signal that
    # was computed and then never consulted anywhere: the CEO cycle could
    # know a source was broken and still say nothing to Lee about it. This
    # is the missing connection from decision to the existing Twilio
    # escalation path (approval_notifier.notify_urgent_event), not a new
    # notification system. notify_urgent_event's own event_id dedup means
    # calling this every cycle for the same failing item sends exactly one
    # SMS, not one per cycle.
    _escalate_flagged_priorities(priorities, dry_run=dry_run)

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
    # GENERATE CEO REPORT from what actually happened.
    report = _safe_stage(
        "ceo_report",
        lambda: ceo_report.build(state=snapshot, movement=movement,
                                 health=gathered.get("health") or {}, execution=execution,
                                 verifications=verifications, delivery_ok=True, run_date=run_date),
        {},
    )
    summary_text = ceo_report.render_text(report) if report else _format_report(
        gathered, priorities, execution, verifications)

    # NOTIFY WHEN WARRANTED. A cycle that ran cleanly and found nothing is a
    # normal outcome; texting about it daily trains its reader to ignore the
    # channel, and then a real alert lands in a muted thread.
    delivery_ok = True
    if dry_run:
        notification: dict[str, Any] = {"action": "skipped_dry_run"}
    elif report.get("quiet"):
        notification = {"action": "suppressed_quiet_cycle", "ok": True}
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
        "business_state": snapshot, "movement": movement, "report": report,
    }
    # Additive: Google Tasks and HubSpot snapshots (executive_brief) ride
    # alongside the structured report without touching ceo_report.py's
    # existing, already-tested build()/render_text() internals.
    if isinstance(result.get("report"), dict):
        result["report"].setdefault("google_tasks", gathered["brief"].get("tasks"))
        result["report"].setdefault("hubspot", gathered["brief"].get("hubspot"))
    _mark_ran(run_date, summary_text, risk_count=len(gathered["brief"].get("risks", [])), agents_run=result["agents_run"])
    _remember_cycle(run_date, result, execution, delivery_ok)
    _safe_stage("save_snapshot", lambda: business_state.save_snapshot(snapshot, run_date), None)
    logger.info(
        "ceo_cycle event=end run_date=%s state=%s agents_run=%d tasks_created=%d "
        "report_delivered=%s duration_ms=%d",
        run_date, state, result["agents_run"],
        len((execution.get("business") or {}).get("dispatched") or []),
        delivery_ok, int((time.time() - wake_ts) * 1000),
    )
    return result
