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
    return {"brief": brief, "modules": modules}


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
    due_tasks = agent_orchestrator.run_due_agents()
    stale_tasks = agent_orchestrator.run_stale_autonomous_agents()

    discovery_result: dict[str, Any]
    try:
        discovery_result = ddf_discovery.discover_new_products()
    except Exception as exc:
        logger.exception("ddf_discovery.discover_new_products() raised")
        discovery_result = {"ok": False, "state": "ERROR", "detail": str(exc), "discovered": [], "saved": 0, "errors": [{"detail": str(exc)}]}

    return {"due_tasks": due_tasks, "stale_tasks": stale_tasks, "discovery_result": discovery_result}


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
    if failed_verifications:
        lines.append(f"{len(failed_verifications)} item(s) failed verification — filed as risks for follow-up.")
    unimplemented = [m["name"] for m in gathered["modules"].values() if not m["implemented"]]
    if unimplemented:
        lines.append(f"Not yet wired into this cycle: {', '.join(unimplemented)}.")
    return "\n".join(lines)


def _deliver_report(run_date: str, summary_text: str) -> dict[str, Any]:
    from actions import approval_notifier
    return approval_notifier.notify_urgent_event(
        event_id=f"ceo_cycle-{run_date}", title="JARVIS Morning Brief", detail=summary_text,
        level=2, priority="normal",
    )


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

    notification: dict[str, Any] = {"action": "skipped_dry_run"} if dry_run else _deliver_report(run_date, summary_text)

    result = {
        "ok": True, "state": "RAN", "run_date": run_date, "wake_ts": wake_ts,
        "priorities": priorities, "agents_run": len(execution["due_tasks"]) + len(execution["stale_tasks"]),
        "discovery": execution["discovery_result"], "verifications": verifications,
        "summary": summary_text, "notification": notification,
    }
    _mark_ran(run_date, summary_text, risk_count=len(gathered["brief"].get("risks", [])), agents_run=result["agents_run"])
    return result
