"""Detect what has gone wrong, fix what is safe to fix, escalate the rest.

Phase A made individual pieces survivable: loops restart, tasks retry within
bounds, agents return to IDLE. What no one did was LOOK. Nothing swept the
system asking whether a task had been RUNNING for six hours, whether an
agent had failed every attempt for two days, or whether the morning brief
had silently failed to send three times running. Each of those is invisible
precisely because the local error handling worked — the exception was
caught, logged, and the loop carried on, forever.

This is that sweep. Its rules:

  * It recovers only what is genuinely safe to recover — an interrupted
    task, an agent wedged in RUNNING. It never re-runs an external side
    effect, and never approves anything.
  * Escalation goes through the existing notification router, at a bounded
    rate. A system that texts about the same broken integration every
    fifteen minutes is a system that gets muted.
  * It never raises. A monitoring layer that can crash the thing it monitors
    is worse than no monitoring at all.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

logger = logging.getLogger("jarvis.self_healing")

# A task RUNNING longer than this was almost certainly interrupted — no
# handler here does minutes of work. Generous enough that a genuinely slow
# API call is never mistaken for a crash.
STALE_TASK_SECONDS = 3600

# Consecutive failures before an agent is treated as genuinely broken rather
# than unlucky. Three matches the task engine's own attempt budget.
FAILURE_STREAK_ESCALATE = 3

# Minimum gap between escalations about the same subject. The problem stays
# in the record and in the morning brief; this only bounds how often it
# interrupts Lee.
ESCALATION_COOLDOWN_SECONDS = 6 * 3600


def _record(entry_type: str, source: str, summary: str, ok: Optional[bool] = None,
            subject: Optional[str] = None, data: Optional[dict] = None) -> None:
    from actions import operating_memory
    operating_memory.record(entry_type, source=source, summary=summary,
                            subject=subject, data=data, ok=ok)


def escalate(subject: str, title: str, detail: str) -> dict[str, Any]:
    """Tells Lee about a persistent failure, at most once per cooldown.

    Rate limiting is checked against the operating record rather than an
    in-process timer, so a restart cannot reset it into a burst."""
    from actions import operating_memory
    from actions import notifications

    recent = operating_memory.recall(
        entry_type=operating_memory.ESCALATION, source=subject,
        since_seconds=ESCALATION_COOLDOWN_SECONDS, limit=1,
    )
    if recent:
        return {"ok": True, "action": "suppressed", "reason": "within cooldown"}

    _record(operating_memory.ESCALATION, source=subject, summary=title, ok=None)
    # Hourly event id so a genuinely persistent problem can still re-notify
    # after the cooldown, rather than being deduplicated away forever by the
    # transport's exactly-once contract.
    event_id = f"selfheal-{subject}-{int(time.time() // 3600)}"
    return notifications.failure(event_id, title, detail)


# ── detection + recovery ─────────────────────────────────────────────────

def recover_stale_tasks(orchestrator=None, now: Optional[float] = None) -> list[dict[str, Any]]:
    """Fails tasks stuck RUNNING/RETRYING past the threshold and frees their
    agent.

    The orchestrator already recovers stuck tasks at STARTUP. That only
    helps if the process restarts — a task wedged in a process that stays up
    holds its agent out of get_due_agents() indefinitely, which silently
    removes that agent from the workforce with no error anywhere."""
    from actions.agent_orchestrator import (
        orchestrator as _default, TaskStatus, AgentStatus, _save_task, _save_agent_state,
    )
    orchestrator = orchestrator or _default
    now = now if now is not None else time.time()
    recovered = []

    for task in list(orchestrator._tasks.values()):
        if task.status not in (TaskStatus.RUNNING, TaskStatus.RETRYING):
            continue
        started = task.started_ts or task.updated_ts or task.created_ts
        age = now - started
        if age < STALE_TASK_SECONDS:
            continue

        task.status = TaskStatus.FAILED
        task.error = f"Stuck in {task.status.value} for {age / 3600:.1f}h — recovered by the monitoring sweep."
        task.escalated = True
        task.escalation_reason = "stale task recovered"
        task.completed_ts = now
        task.updated_ts = now
        _save_task(task)

        agent = orchestrator._agents.get(task.agent_id)
        if agent is not None and agent.status == AgentStatus.RUNNING:
            agent.status = AgentStatus.IDLE
            agent.last_error = "Recovered from a stuck task by the monitoring sweep."
            _save_agent_state(agent)

        recovered.append({"task_id": task.id, "agent_id": task.agent_id, "age_hours": round(age / 3600, 1)})
        _record("recovery", source=task.agent_id,
                summary=f"Recovered stuck task {task.id} after {age / 3600:.1f}h",
                ok=True, subject=task.id)
    return recovered


def expire_stale_approvals(orchestrator=None) -> list[dict[str, Any]]:
    """Expires approval requests nobody has answered in time.

    AgentOrchestrator.expire_stale_approvals() has done exactly this since
    it was written and nothing ever called it — an EXECUTE-level task
    could sit PENDING_APPROVAL indefinitely and then run the moment
    someone approved a stale dashboard, acting on a situation that might
    be weeks out of date. This is the missing call, not new expiry logic."""
    from actions.agent_orchestrator import orchestrator as _default
    orchestrator = orchestrator or _default
    expired = orchestrator.expire_stale_approvals()
    for task in expired:
        _record("recovery", source=task.agent_id,
                summary=f"Expired unanswered approval for task {task.id}",
                ok=True, subject=task.id)
    return [{"task_id": t.id, "agent_id": t.agent_id} for t in expired]


def check_agent_failures(orchestrator=None) -> list[dict[str, Any]]:
    """Agents failing consistently. A single failure is noise; a streak is a
    broken agent, and only the streak is escalated."""
    from actions.agent_orchestrator import orchestrator as _default
    from actions import operating_memory
    orchestrator = orchestrator or _default

    broken = []
    for agent in orchestrator.list_agents():
        streak = operating_memory.failure_streak(agent.id)
        if streak < FAILURE_STREAK_ESCALATE:
            continue
        broken.append({"agent_id": agent.id, "name": agent.name, "streak": streak})
        escalate(
            subject=f"agent-{agent.id}",
            title=f"{agent.name} has failed {streak} runs in a row",
            detail=(f"Agent '{agent.name}' ({agent.id}) has failed its last {streak} "
                    f"attempts. Last error: {agent.last_error or 'unknown'}"),
        )
    return broken


def check_integrations() -> dict[str, Any]:
    """Integration health, recorded so a degradation is visible as a trend
    and not only as this moment's state.

    NOT_CONFIGURED is never escalated: a credential nobody has connected is
    an expected state, and paging about it daily is how real alerts get
    ignored. AUTH_ERROR and UNAVAILABLE on something that WAS working are
    the real signals."""
    from actions import integration_health as ih
    from actions import operating_memory

    report = ih.check_all()
    for name, result in report["integrations"].items():
        state = result["state"]
        operating_memory.record(
            operating_memory.INTEGRATION_STATE, source=name, subject=name,
            summary=f"{name}: {state}", data={"detail": result.get("detail")},
            ok=(state == ih.CONFIGURED),
            dedup_seconds=3600,   # one state entry per integration per hour
        )
        if state in (ih.AUTH_ERROR, ih.UNAVAILABLE):
            escalate(
                subject=f"integration-{name}",
                title=f"{name} is {state}",
                detail=ih.redact(result.get("detail") or state),
            )
    return report


def check_notification_failures() -> list[dict[str, Any]]:
    """Undelivered notifications — the failure mode that otherwise leaves no
    trace, because the thing that would have told you is the thing that
    broke. Not escalated through the same channel (it just failed); recorded
    so the morning brief and the dashboard can show it."""
    from actions import notifications
    failures = notifications.recent_failures(limit=10)
    if failures:
        _record("recovery", source="notifications",
                summary=f"{len(failures)} notification(s) failed to deliver recently",
                ok=False, data={"count": len(failures)})
    return failures


def check_cycle_health() -> dict[str, Any]:
    """Whether the autonomous cycle is actually running and reporting."""
    from actions import operating_memory
    runs = operating_memory.recall(entry_type=operating_memory.CYCLE_RUN, limit=5)
    if not runs:
        return {"state": "unknown", "detail": "no recorded cycle runs yet"}
    latest = runs[0]
    age_hours = (time.time() - latest["ts"]) / 3600
    # The cycle is daily, so a gap beyond ~36h means one was genuinely
    # missed rather than merely not due yet.
    if age_hours > 36:
        escalate(
            subject="ceo-cycle-missed",
            title="The daily CEO cycle has not run",
            detail=f"Last recorded run was {age_hours:.0f}h ago. Check the Render Cron Job.",
        )
        return {"state": "stale", "hours_since": round(age_hours, 1)}
    return {"state": "ok", "hours_since": round(age_hours, 1), "last_ok": latest["ok"]}


def run_sweep(orchestrator=None) -> dict[str, Any]:
    """One full monitoring pass. Every check is isolated: a failure in one
    must not stop the others, since the whole point is to be the thing that
    still works when something else does not."""
    report: dict[str, Any] = {"checks": {}, "errors": {}}

    for name, fn in (
        ("stale_tasks", lambda: recover_stale_tasks(orchestrator=orchestrator)),
        ("expired_approvals", lambda: expire_stale_approvals(orchestrator=orchestrator)),
        ("agent_failures", lambda: check_agent_failures(orchestrator=orchestrator)),
        ("integrations", check_integrations),
        ("notifications", check_notification_failures),
        ("cycle_health", check_cycle_health),
    ):
        try:
            report["checks"][name] = fn()
        except Exception as exc:
            logger.exception("self-healing check %r failed", name)
            report["errors"][name] = str(exc)

    report["recovered"] = len(report["checks"].get("stale_tasks") or [])
    report["broken_agents"] = len(report["checks"].get("agent_failures") or [])
    return report
