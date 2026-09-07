"""The autonomous wake system as one system, end to end.

Covers what the Item #2/#3/#4 work actually changed, and deliberately not
what was already proven elsewhere: per-loop crash survival already has full
coverage in test_background_worker_resilience.py, and the CEO cycle's
scheduling gates in test_ceo_cycle_scheduling.py. What was missing, and is
tested here, is the wiring BETWEEN those pieces —

  * that exactly one scheduled owner runs the morning CEO cycle,
  * that a crashed loop is restarted rather than silently disappearing,
  * that the task engine retries bounded, safe work and escalates the rest,
  * that one failing agent no longer aborts the sweep, and
  * that a failed morning text can no longer erase a whole cycle's work.

Every test drives real code. External services are faked only where a real
call would leave this machine (Twilio, the LLM), never to manufacture a
passing result.
"""
import asyncio
import time

import pytest

from actions import agent_orchestrator as ao
from actions import ceo_operating_cycle as cycle
from core.headless import background as bg
from core.headless import config as hc

_real_sleep = asyncio.sleep


def _observe_agent(agent_id="obs", handler=None, **kw):
    return ao.AgentDefinition(
        id=agent_id, name=f"Agent {agent_id}", description="x", nucleus_id="system",
        permission_level=kw.pop("permission_level", ao.PermissionLevel.OBSERVE),
        handler=handler, **kw,
    )


# ══ WAKE: exactly one owner of the morning CEO cycle ═════════════════════

def test_web_service_does_not_start_the_ceo_cycle_loop_by_default():
    """The Render Cron owns the scheduled cycle. If the web service also
    started this loop, both would run it at 11:00 UTC — and because each
    container has its own ephemeral SQLite file, already_ran_today() cannot
    see the other's run and cannot suppress the duplicate."""
    assert hc.JARVIS_CEO_CYCLE_IN_WEB_SERVICE is False, "Cron must be the default owner"

    async def _run():
        worker = bg.BackgroundWorker()
        worker.start()
        try:
            names = {t.get_name() for t in worker._tasks}
            assert "ceo_operating_cycle" not in names, (
                "web service started the CEO cycle loop — that is the duplicate "
                "morning-brief wake the Cron already owns"
            )
            # every other worker must be untouched by this change
            assert names == {
                "agent_scheduler", "background_monitor", "proactive_observer",
                "objective_loop", "approval_notifier",
            }
        finally:
            await worker.stop()

    asyncio.run(_run())


def test_ceo_cycle_loop_can_be_handed_back_to_the_web_service(monkeypatch):
    """The loop is disabled, not deleted: if the Cron Job is ever removed,
    one env var restores web-service ownership."""
    monkeypatch.setattr(bg.headless_config, "JARVIS_CEO_CYCLE_IN_WEB_SERVICE", True)

    async def _run():
        worker = bg.BackgroundWorker()
        worker.start()
        try:
            assert "ceo_operating_cycle" in {t.get_name() for t in worker._tasks}
        finally:
            await worker.stop()

    asyncio.run(_run())


def test_starting_twice_does_not_create_duplicate_workers():
    async def _run():
        worker = bg.BackgroundWorker()
        worker.start()
        first = list(worker._tasks)
        worker.start()      # second startup event, or a double create_app()
        try:
            assert worker._tasks == first, "start() created a second set of loops"
        finally:
            await worker.stop()

    asyncio.run(_run())


def test_stop_cancels_every_worker_cleanly():
    async def _run():
        worker = bg.BackgroundWorker()
        worker.start()
        tasks = list(worker._tasks)
        await worker.stop()
        assert worker._tasks == []
        for t in tasks:
            assert t.done(), f"{t.get_name()} still running after stop()"

    asyncio.run(_run())


def test_supervisor_restarts_a_loop_that_crashes_outside_its_own_guard(monkeypatch):
    """A loop that raises before/around its inner try-except used to end the
    asyncio task silently — no traceback, because nothing awaits these tasks
    until shutdown. The supervisor must restart it and say so."""
    calls = {"n": 0}

    async def _crashy():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("crash outside the loop's own guard")
        await _real_sleep(3600)

    monkeypatch.setattr(bg, "_SUPERVISOR_RESTART_BACKOFF_SECS", 0.001)

    async def _run():
        worker = bg.BackgroundWorker()
        task = asyncio.create_task(worker._supervise("crashy", _crashy))
        try:
            for _ in range(2000):
                if calls["n"] >= 3:
                    break
                await _real_sleep(0.002)
            assert calls["n"] >= 3, f"supervisor stopped restarting after {calls['n']} attempt(s)"
            assert not task.done(), "supervised task died instead of being restarted"
        finally:
            worker._stopping = True
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(_run())


def test_scheduler_survives_a_lock_that_raises_on_startup(monkeypatch):
    """acquire_scheduler_lock() is documented as never raising, but it
    touches the filesystem and sat outside the loop's try. If it ever did
    raise, the one loop that runs all scheduled agents died permanently."""
    def _boom():
        raise OSError("simulated lock filesystem failure")

    monkeypatch.setattr(bg.agent_scheduler_lock, "acquire_scheduler_lock", _boom)
    monkeypatch.setattr(bg.agent_scheduler_lock, "refresh_scheduler_lock", lambda: None)
    monkeypatch.setattr(bg.agent_scheduler_lock, "release_scheduler_lock", lambda: None)
    monkeypatch.setattr(bg.agent_orchestrator, "get_due_agents", lambda: [])

    async def _run():
        worker = bg.BackgroundWorker()
        task = asyncio.create_task(worker._run_agent_scheduler())
        try:
            await _real_sleep(0.05)
            assert not task.done(), "scheduler died on a lock error instead of degrading"
        finally:
            worker._stopping = True
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(_run())


# ══ TASK ENGINE ══════════════════════════════════════════════════════════

def test_task_gets_a_unique_identity_and_recorded_execution_state():
    orch = ao.AgentOrchestrator(agents={"obs": _observe_agent(handler=lambda t: {"summary": "ok"})})
    a = orch.assign_task("obs", "first")
    b = orch.assign_task("obs", "second")

    assert a.id != b.id and len(a.id) >= 32, "task ids must be unique and non-guessable"
    assert a.status == ao.TaskStatus.DONE
    assert a.attempt == 1
    assert a.started_ts is not None and a.completed_ts is not None
    assert a.completed_ts >= a.started_ts


def test_a_completed_task_is_never_executed_again():
    """Re-running a finished task would repeat whatever its handler already
    did — the duplicate-execution risk a replayed tick creates."""
    runs = {"n": 0}

    def _handler(task):
        runs["n"] += 1
        return {"summary": "ok"}

    orch = ao.AgentOrchestrator(agents={"obs": _observe_agent(handler=_handler)})
    task = orch.assign_task("obs", "work")
    assert runs["n"] == 1

    again = orch.run_task(task.id)
    assert runs["n"] == 1, "a DONE task was executed a second time"
    assert again.status == ao.TaskStatus.DONE


def test_agent_cannot_run_two_tasks_at_once():
    orch = ao.AgentOrchestrator(agents={"obs": _observe_agent(handler=lambda t: {"summary": "ok"})})
    orch._agents["obs"].status = ao.AgentStatus.RUNNING
    t = orch.assign_task("obs", "work")
    assert t.status == ao.TaskStatus.REJECTED
    assert "already running" in (t.error or "")


def test_transient_failure_retries_up_to_the_limit_then_succeeds(monkeypatch):
    monkeypatch.setattr(ao, "_retry_sleep", lambda s: None)
    attempts = {"n": 0}

    def _flaky(task):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise TimeoutError("upstream timed out")
        return {"summary": "recovered"}

    orch = ao.AgentOrchestrator(agents={"obs": _observe_agent(handler=_flaky)})
    task = orch.assign_task("obs", "work")

    assert attempts["n"] == 3
    assert task.status == ao.TaskStatus.DONE
    assert task.attempt == 3
    assert task.escalated is False
    assert task.error is None


def test_retry_is_bounded_and_then_escalates(monkeypatch):
    monkeypatch.setattr(ao, "_retry_sleep", lambda s: None)
    attempts = {"n": 0}

    def _always_down(task):
        attempts["n"] += 1
        raise ConnectionError("upstream is down")

    orch = ao.AgentOrchestrator(agents={"obs": _observe_agent(handler=_always_down)})
    task = orch.assign_task("obs", "work")

    assert attempts["n"] == ao._MAX_TASK_ATTEMPTS, "retry was not bounded"
    assert task.status == ao.TaskStatus.FAILED
    assert task.escalated is True
    assert "attempts" in (task.escalation_reason or "")


def test_a_deterministic_failure_is_not_retried(monkeypatch):
    """Retrying a ValueError would fail identically and only delay the
    escalation, so it must go straight to a human."""
    monkeypatch.setattr(ao, "_retry_sleep", lambda s: None)
    attempts = {"n": 0}

    def _bug(task):
        attempts["n"] += 1
        raise ValueError("deterministic defect")

    orch = ao.AgentOrchestrator(agents={"obs": _observe_agent(handler=_bug)})
    task = orch.assign_task("obs", "work")

    assert attempts["n"] == 1, "a non-transient failure was retried"
    assert task.status == ao.TaskStatus.FAILED
    assert task.escalated is True
    assert task.escalation_reason == "not a transient failure"


def test_work_with_side_effects_is_escalated_rather_than_repeated(monkeypatch):
    """A SUGGEST agent can leave a proposal behind. Even on a genuinely
    transient error it must not be repeated automatically — a duplicated
    external side effect is worse than a delayed one."""
    monkeypatch.setattr(ao, "_retry_sleep", lambda s: None)
    attempts = {"n": 0}

    def _flaky(task):
        attempts["n"] += 1
        raise TimeoutError("timed out after possibly sending")

    orch = ao.AgentOrchestrator(agents={
        "sug": _observe_agent("sug", handler=_flaky, permission_level=ao.PermissionLevel.SUGGEST)
    })
    task = orch.assign_task("sug", "work")

    assert attempts["n"] == 1, "side-effecting work was retried automatically"
    assert task.escalated is True
    assert "not safe to repeat" in (task.escalation_reason or "")


def test_an_agent_can_opt_into_retry_once_its_handler_is_idempotent(monkeypatch):
    monkeypatch.setattr(ao, "_retry_sleep", lambda s: None)
    attempts = {"n": 0}

    def _flaky(task):
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise TimeoutError("transient")
        return {"summary": "ok"}

    orch = ao.AgentOrchestrator(agents={
        "sug": _observe_agent("sug", handler=_flaky,
                              permission_level=ao.PermissionLevel.SUGGEST, retry_safe=True)
    })
    task = orch.assign_task("sug", "work")
    assert attempts["n"] == 2
    assert task.status == ao.TaskStatus.DONE


def test_agent_returns_to_a_usable_state_after_any_outcome(monkeypatch):
    monkeypatch.setattr(ao, "_retry_sleep", lambda s: None)
    for handler in (lambda t: {"summary": "ok"},
                    lambda t: (_ for _ in ()).throw(ValueError("boom"))):
        orch = ao.AgentOrchestrator(agents={"obs": _observe_agent(handler=handler)})
        orch.assign_task("obs", "work")
        assert orch._agents["obs"].status == ao.AgentStatus.IDLE


# ══ ORCHESTRATION ════════════════════════════════════════════════════════

def test_one_agent_failing_does_not_abort_the_rest_of_the_sweep(monkeypatch):
    """This was a list comprehension: an exception dispatching ONE agent
    aborted the whole sweep and every later agent silently never ran."""
    monkeypatch.setattr(ao, "_retry_sleep", lambda s: None)
    ran = []

    def _ok(task):
        ran.append(task.agent_id)
        return {"summary": "ok"}

    good_a = _observe_agent("a", handler=_ok, schedule="1m")
    bad = _observe_agent("b", handler=_ok, schedule="1m")
    good_c = _observe_agent("c", handler=_ok, schedule="1m")
    orch = ao.AgentOrchestrator(agents={"a": good_a, "b": bad, "c": good_c})
    for a in orch._agents.values():
        a.status = ao.AgentStatus.IDLE

    real_assign = orch.assign_task

    def _assign(agent_id, description):
        if agent_id == "b":
            raise RuntimeError("simulated dispatch failure")
        return real_assign(agent_id, description)

    monkeypatch.setattr(orch, "assign_task", _assign)

    results = orch.run_due_agents()

    assert "a" in ran and "c" in ran, f"sweep aborted early — only ran {ran}"
    assert len(results) == 2, "the failing agent should be skipped, not fatal"
    assert orch._agents["b"].last_error is not None, "the failure was not recorded"


def test_approval_gate_is_still_enforced_for_execute_agents():
    """The retry/escalation work must not have opened a path around the one
    gate that keeps irreversible work behind a human decision."""
    ran = {"n": 0}

    def _handler(task):
        ran["n"] += 1
        return {"summary": "did something irreversible"}

    orch = ao.AgentOrchestrator(agents={
        "exe": _observe_agent("exe", handler=_handler,
                              permission_level=ao.PermissionLevel.EXECUTE)
    })
    task = orch.assign_task("exe", "send the thing")

    assert task.status == ao.TaskStatus.PENDING_APPROVAL
    assert ran["n"] == 0, "an EXECUTE agent auto-ran without approval"
    with pytest.raises(PermissionError):
        orch.run_task(task.id)
    assert ran["n"] == 0

    approved = orch.approve_task(task.id)
    assert approved.status == ao.TaskStatus.DONE
    assert ran["n"] == 1


def test_stale_running_task_is_recovered_and_does_not_wedge_the_agent():
    """A crash mid-task leaves a persisted RUNNING status. Without recovery
    the agent is excluded from get_due_agents() forever."""
    agent = _observe_agent(handler=lambda t: {"summary": "ok"}, schedule="1m")
    orch = ao.AgentOrchestrator(agents={"obs": agent})
    task = orch.assign_task("obs", "work")

    # Simulate the crash: persist RUNNING for both agent and task.
    task.status = ao.TaskStatus.RUNNING
    ao._save_task(task)
    agent.status = ao.AgentStatus.RUNNING
    ao._save_agent_state(agent)

    revived = ao.AgentOrchestrator(agents={"obs": _observe_agent(
        handler=lambda t: {"summary": "ok"}, schedule="1m")})
    recovered = revived._agents["obs"]
    assert recovered.status == ao.AgentStatus.IDLE, "agent stayed wedged in RUNNING"
    assert "unclean restart" in (recovered.last_error or "").lower()


# ══ CEO CYCLE: the delivery failure path ═════════════════════════════════

def test_delivery_failure_still_records_the_cycle_and_never_fakes_an_sms(monkeypatch):
    """The bug this closes: _deliver_report() raised BEFORE _mark_ran(), so
    a Twilio outage meant every agent had run and none of it was recorded —
    and the loop then re-ran the whole cycle every 15 minutes."""
    def _boom(run_date, summary_text):
        raise RuntimeError("simulated Twilio outage")

    monkeypatch.setattr(cycle, "_deliver_report", _boom)

    result = cycle.run_cycle(force=True)

    assert result["state"] == "RAN_REPORT_UNDELIVERED"
    assert result["report_delivered"] is False
    assert result["notification"]["ok"] is False, "a failed SMS must never report ok"
    assert "Twilio outage" in result["notification"]["error"]
    assert cycle.already_ran_today(result["run_date"]) is True, (
        "completed autonomous work was not recorded — the cycle would re-run"
    )


def test_delivery_failure_is_filed_as_a_risk_for_the_next_brief(monkeypatch):
    filed = []
    monkeypatch.setattr(cycle, "_deliver_report",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no route to host")))
    real_add = cycle.biz_intel.add_entry

    def _add(category, business, title, content, data=None, **kw):
        filed.append((category, title))
        return real_add(category, business, title, content, data=data, **kw)

    monkeypatch.setattr(cycle.biz_intel, "add_entry", _add)

    cycle.run_cycle(force=True)

    assert any(c == "risks" and "not delivered" in t.lower() for c, t in filed), (
        f"delivery failure was not filed through the existing risk mechanism: {filed}"
    )


def test_a_successful_cycle_still_reports_ran(monkeypatch):
    monkeypatch.setattr(cycle, "_deliver_report",
                        lambda run_date, summary: {"action": "none", "configured": False})
    result = cycle.run_cycle(force=True)
    assert result["state"] == "RAN"
    assert result["report_delivered"] is True


def test_execution_failure_still_fails_the_whole_cycle(monkeypatch):
    """A delivery failure is survivable; a failure to do the WORK is not,
    and must still surface as a non-zero exit for the Cron."""
    monkeypatch.setattr(cycle, "_gather",
                        lambda: (_ for _ in ()).throw(RuntimeError("state unavailable")))
    with pytest.raises(RuntimeError):
        cycle.run_cycle(force=True)


# ══ END TO END ═══════════════════════════════════════════════════════════

def test_wake_to_recorded_outcome_end_to_end(monkeypatch):
    """WAKE -> CEO CYCLE -> TASK -> ORCHESTRATOR -> AGENT -> EXECUTE ->
    VERIFY -> RECORD, driven through the real cycle with one real scheduled
    agent. Only the SMS is faked; everything else is production code."""
    executed = []

    def _handler(task):
        executed.append(task.id)
        return {"summary": "gathered 3 signals"}

    agent = _observe_agent("e2e", handler=_handler, schedule="1m")
    agent.status = ao.AgentStatus.IDLE
    orch = ao.AgentOrchestrator(agents={"e2e": agent})
    monkeypatch.setattr(cycle, "agent_orchestrator", orch)

    sent = {}

    def _fake_deliver(run_date, summary_text):
        sent["run_date"] = run_date
        sent["summary"] = summary_text
        return {"action": "sms", "ok": True, "level": 2}

    monkeypatch.setattr(cycle, "_deliver_report", _fake_deliver)

    result = cycle.run_cycle(force=True)

    # WAKE + EXECUTE
    assert result["state"] == "RAN"
    assert executed, "the wake never reached real agent execution"
    # the task carries real execution state
    task = orch.get_task(executed[0])
    assert task.status == ao.TaskStatus.DONE
    assert task.attempt == 1 and task.started_ts and task.completed_ts
    # VERIFY
    assert result["verifications"], "no verification records were produced"
    agent_check = next(
        (v for v in result["verifications"] if v.get("reference_id") == task.id), None
    )
    assert agent_check is not None, (
        "the agent's run was never verified — VERIFY did not see EXECUTE's output"
    )
    assert agent_check["success"] is True
    assert agent_check["verification_status"] == "verified_success"
    assert agent_check["follow_up_required"] is False
    # REPORT + RECORD
    assert sent["run_date"] == result["run_date"]
    assert cycle.already_ran_today(result["run_date"]) is True
    assert orch._agents["e2e"].status == ao.AgentStatus.IDLE
