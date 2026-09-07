"""Phase E — the full autonomous path, and what it does when things break.

This suite asserts STATE TRANSITIONS, not that functions returned. The
distinction is the whole point: a loop where every function returns cleanly
while nothing actually moves from "task created" to "verified complete" is
exactly the failure this project has been guarding against since Phase A.

Integrations are simulated at their own boundary. No real credential is
used anywhere, and nothing here reaches the network.
"""
import time

import pytest

from actions import agent_orchestrator as ao
from actions import business_pipeline as bp
from actions import business_state as bstate
from actions import ceo_operating_cycle as cycle
from actions import ceo_report as report
from actions import integration_health as ih
from actions import jarvis_brain as brain
from actions import notifications as notif
from actions import operating_memory as mem
from actions import self_healing as heal


# ── harness ──────────────────────────────────────────────────────────────

class Recorder:
    """Captures every outbound notification instead of sending one."""
    def __init__(self):
        self.sent = []

    def install(self, monkeypatch, fail=False):
        from actions import approval_notifier

        def _fake(event_id, title, detail="", level=2, dry_run=False):
            if fail:
                raise RuntimeError("simulated transport outage")
            self.sent.append({"event_id": event_id, "title": title, "level": level})
            return {"event_id": event_id, "action": "sms", "ok": True, "level": level}

        monkeypatch.setattr(approval_notifier, "notify_urgent_event", _fake)
        return self


def _observe_agent(aid, handler, perm=ao.PermissionLevel.OBSERVE, **kw):
    return ao.AgentDefinition(id=aid, name=f"Agent {aid}", description="x",
                              nucleus_id="system", permission_level=perm,
                              handler=handler, **kw)


# ══ THE FULL PATH ════════════════════════════════════════════════════════

def test_the_complete_autonomous_path_end_to_end(monkeypatch):
    """WAKE -> MEMORY -> BRAIN -> HEALTH -> STATE -> COMPARE -> PRIORITISE
    -> CREATE -> APPROVAL -> EXECUTE -> VERIFY -> MEMORY -> STATE -> REPORT
    -> NOTIFY, asserting the state at each transition."""
    notifier = Recorder().install(monkeypatch)

    executed = []
    safe = _observe_agent("researcher",
                          lambda t: executed.append(t.id) or {"summary": "3 signals gathered"})
    gated = _observe_agent("outreach",
                           lambda t: executed.append(t.id) or {"summary": "outreach sent"},
                           perm=ao.PermissionLevel.EXECUTE)
    orch = ao.AgentOrchestrator(agents={"researcher": safe, "outreach": gated})

    # 2. BRAIN provides real context from the in-repo vault.
    brain.reset_cache()
    assert brain.is_available(), "the Brain must be reachable on the autonomous path"
    assert brain.operating_context(["mandate"]), "the Brain returned no operating context"

    # 3. INTEGRATION HEALTH is established before any work is attempted.
    health = ih.check_all()
    assert "capabilities" in health

    # 4-6. DISCOVER -> CREATE SAFE TASKS (no execution yet for gated work).
    findings = [
        bp._finding("research_opportunity", "e2e-safe", "Research: market",
                    "researcher", "gather signals"),
        bp._finding("hubspot_opportunity", "e2e-gated", "Outreach: Acme",
                    "outreach", "contact Acme"),
    ]
    dispatched = bp.dispatch_findings(findings, orchestrator=orch)
    by_agent = {d["agent_id"]: d for d in dispatched}

    # OBSERVE work executed; EXECUTE work did NOT.
    assert by_agent["researcher"]["task_status"] == ao.TaskStatus.DONE.value
    assert by_agent["outreach"]["task_status"] == ao.TaskStatus.PENDING_APPROVAL.value
    gated_task_id = by_agent["outreach"]["task_id"]
    assert gated_task_id not in executed, "EXECUTE work ran before approval"

    # 7-8. APPROVAL REQUEST != APPROVAL.
    orch.request_approval(gated_task_id)
    assert any("APPROVAL NEEDED" in s["title"] for s in notifier.sent)
    assert orch.get_task(gated_task_id).status == ao.TaskStatus.PENDING_APPROVAL
    assert gated_task_id not in executed, "requesting approval executed the action"

    # 9-11. APPROVAL -> EXECUTION -> VERIFICATION.
    approved = orch.approve_task(gated_task_id)
    assert approved.status == ao.TaskStatus.DONE
    assert gated_task_id in executed
    assert approved.started_ts and approved.completed_ts and approved.attempt == 1

    # 12. MEMORY records the outcome.
    mem.record(mem.AGENT_OUTCOME, source="outreach", subject=gated_task_id,
               summary="outreach sent", ok=True)
    assert mem.recall(entry_type=mem.AGENT_OUTCOME, source="outreach", limit=1)

    # 13-14. BUSINESS STATE -> REPORT, with the action/creation distinction.
    snapshot = bstate.snapshot()
    built = report.build(
        state=snapshot, movement=bstate.compare(snapshot, None), health=health,
        execution={"due_tasks": [orch.get_task(gated_task_id)], "stale_tasks": [],
                   "business": {"dispatched": dispatched, "states": {"research": "SUCCESS"}}},
        verifications=[{"reference_id": gated_task_id, "success": True}],
        run_date="2026-09-07")

    assert [a["agent_id"] for a in built["actions_taken"]] == ["outreach"]
    assert len(built["tasks_created"]) == 2
    text = report.render_text(built)
    assert "ACTIONS TAKEN (verified)" in text
    assert "TASKS CREATED (not yet performed)" in text

    # 15. NOTIFY.
    result = notif.daily_report("ceo_cycle-2026-09-07", "Brief", text)
    assert result["ok"] is True


def test_a_second_cycle_sees_the_first_and_does_not_duplicate_work(monkeypatch):
    """Steps 19-20: repeated cycles must be safe."""
    Recorder().install(monkeypatch)
    monkeypatch.setattr(cycle, "_deliver_report", lambda *a, **k: {"action": "none"})

    orch = ao.AgentOrchestrator(agents={
        "researcher": _observe_agent("researcher", lambda t: {"summary": "ok"})})
    finding = bp._finding("research_opportunity", "dupe-check", "Research: X",
                          "researcher", "do research")
    monkeypatch.setattr(bp, "SOURCES", {
        "research": lambda: {"state": bp.SUCCESS, "findings": [finding], "scanned": 1}})
    monkeypatch.setattr(cycle, "agent_orchestrator", orch)

    first = cycle.run_cycle(force=True)
    second = cycle.run_cycle(force=True)

    assert second["movement"]["first_cycle"] is False, "cycle 2 could not see cycle 1"
    created_titles = [t["title"] for t in (second["report"] or {}).get("tasks_created", [])]
    assert "Research: X" not in created_titles, "cycle 2 recreated an already-claimed subject"


def test_repeated_cycles_do_not_duplicate_the_notification(monkeypatch):
    notifier = Recorder().install(monkeypatch)
    notif.daily_report("ceo_cycle-2026-09-07", "Brief", "body")
    notif.daily_report("ceo_cycle-2026-09-07", "Brief", "body")
    assert len({s["event_id"] for s in notifier.sent}) == 1, (
        "a repeated report used a different event id, defeating transport dedup"
    )


# ══ FAILURE INJECTION ════════════════════════════════════════════════════

def test_an_integration_timeout_is_isolated(monkeypatch):
    def _timeout():
        raise TimeoutError("upstream timed out")
    ok = lambda: {"state": bp.SUCCESS, "findings": [], "scanned": 0}

    orch = ao.AgentOrchestrator(agents={})
    r = bp.gather_and_dispatch(sources={"slow": _timeout, "fine": ok}, orchestrator=orch)
    assert r["states"]["slow"] == bp.TRANSIENT_FAILURE
    assert r["states"]["fine"] == bp.SUCCESS


def test_an_authentication_failure_is_classified_not_treated_as_empty():
    from actions import gmail_integration
    import unittest.mock as m
    with m.patch.object(gmail_integration, "list_messages",
                        return_value={"ok": False, "state": "NOT_AUTHORIZED",
                                      "detail": "invalid_scope", "messages": []}):
        result = bp.gmail_findings()
    assert result["state"] == bp.AUTH_ERROR
    assert result["findings"] == []


def test_a_task_failure_does_not_stop_the_other_agents(monkeypatch):
    monkeypatch.setattr(ao, "_retry_sleep", lambda s: None)
    ran = []
    good = _observe_agent("good", lambda t: ran.append("good") or {"summary": "ok"}, schedule="1m")
    bad = _observe_agent("bad", lambda t: (_ for _ in ()).throw(ValueError("boom")), schedule="1m")
    orch = ao.AgentOrchestrator(agents={"bad": bad, "good": good})
    for a in orch._agents.values():
        a.status = ao.AgentStatus.IDLE

    tasks = orch.run_due_agents()
    assert "good" in ran, "a failing agent prevented an unrelated agent from running"
    failed = [t for t in tasks if t.status == ao.TaskStatus.FAILED]
    assert failed and failed[0].escalated is True


def test_a_notification_failure_never_marks_business_work_as_failed(monkeypatch):
    Recorder().install(monkeypatch, fail=True)
    orch = ao.AgentOrchestrator(agents={
        "worker": _observe_agent("worker", lambda t: {"summary": "real work done"})})
    task = orch.assign_task("worker", "do the work")
    assert task.status == ao.TaskStatus.DONE

    outcome = notif.business_alert("evt-x", "something happened")
    assert outcome["ok"] is False
    assert orch.get_task(task.id).status == ao.TaskStatus.DONE, (
        "a failed notification changed the outcome of completed business work"
    )


def test_malformed_memory_does_not_crash_the_cycle(monkeypatch):
    monkeypatch.setattr(mem, "_connect",
                        lambda: (_ for _ in ()).throw(RuntimeError("db gone")))
    monkeypatch.setattr(cycle, "_deliver_report", lambda *a, **k: {"action": "none"})
    assert cycle.run_cycle(force=True)["state"] == "RAN"


def test_a_malformed_business_snapshot_degrades_to_no_comparison():
    assert bstate.compare({"revenue": {"cumulative_usd": 1}}, "RAN")["first_cycle"] is True
    assert bstate.compare({"revenue": {"cumulative_usd": 1}},
                          {"revenue": "not-a-dict"})["changes"] == []


def test_an_expired_approval_never_executes():
    ran = {"n": 0}
    orch = ao.AgentOrchestrator(agents={"x": _observe_agent(
        "x", lambda t: ran.__setitem__("n", ran["n"] + 1) or {"summary": "sent"},
        perm=ao.PermissionLevel.EXECUTE)})
    task = orch.assign_task("x", "send")
    orch._tasks[task.id].created_ts = time.time() - (ao.APPROVAL_TTL_SECONDS + 60)

    orch.expire_stale_approvals()
    assert orch.approve_task(task.id).status == ao.TaskStatus.EXPIRED
    assert ran["n"] == 0


def test_a_worker_exception_does_not_take_down_the_others():
    import asyncio
    from core.headless import background as bg

    async def _run():
        worker = bg.BackgroundWorker()
        worker.start()
        try:
            names = {t.get_name() for t in worker._tasks}
            # Every supervised loop, whatever the current roster is. The
            # property under test is isolation, not the count.
            assert len(names) == len(worker._tasks) and len(names) >= 6
            # one supervised loop dying must leave the rest running
            victim = next(t for t in worker._tasks if t.get_name() == "proactive_observer")
            victim.cancel()
            await asyncio.sleep(0)
            alive = [t for t in worker._tasks if not t.done()]
            assert len(alive) >= len(names) - 1, "cancelling one worker took others with it"
        finally:
            await worker.stop()

    asyncio.run(_run())


def test_a_database_error_degrades_every_reader_without_raising(monkeypatch):
    monkeypatch.setattr(mem, "_connect", lambda: (_ for _ in ()).throw(RuntimeError("locked")))
    assert mem.recall() == []
    assert mem.failure_streak("anything") == 0
    assert mem.summarize()["total"] == 0
    assert mem.record(mem.CYCLE_RUN, "x", "y")["recorded"] is False

    from actions import autonomous_ledger as ledger
    monkeypatch.setattr(ledger, "_connect", lambda: (_ for _ in ()).throw(RuntimeError("locked")))
    # Fails OPEN: a ledger error must risk a duplicate, never silent inaction.
    assert ledger.already_handled("k", "s") is False
    assert ledger.claim("k", "s") is True


def test_self_healing_recovers_a_stuck_task_and_frees_the_agent():
    orch = ao.AgentOrchestrator(agents={"x": _observe_agent("x", lambda t: {"summary": "ok"})})
    task = orch.assign_task("x", "work")
    task.status = ao.TaskStatus.RUNNING
    task.started_ts = time.time() - (heal.STALE_TASK_SECONDS + 600)
    ao._save_task(task)
    orch._agents["x"].status = ao.AgentStatus.RUNNING

    recovered = heal.recover_stale_tasks(orchestrator=orch)
    assert [r["task_id"] for r in recovered] == [task.id]
    assert orch._agents["x"].status == ao.AgentStatus.IDLE


def test_the_cycle_always_terminates_and_records_even_when_stages_fail(monkeypatch):
    """Every stage broken at once: the cycle must still end cleanly and
    leave a record, not hang or raise."""
    for name in ("snapshot",):
        monkeypatch.setattr(bstate, name, lambda: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(cycle.integration_health, "check_all",
                        lambda: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(cycle.jarvis_brain, "operating_context",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(cycle, "_deliver_report",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))

    started = time.time()
    result = cycle.run_cycle(force=True)
    assert time.time() - started < 120, "the cycle did not terminate promptly"
    assert result["state"] == "RAN_REPORT_UNDELIVERED"
    assert cycle.already_ran_today(result["run_date"]) is True


# ══ THE SIX STATES ARE DISTINCT ══════════════════════════════════════════

def test_each_lifecycle_state_is_distinct_from_the_next(monkeypatch):
    """recommendation != task created != approval requested != approval
    != execution != verified completion."""
    Recorder().install(monkeypatch)
    ran = []
    orch = ao.AgentOrchestrator(agents={"x": _observe_agent(
        "x", lambda t: ran.append(t.id) or {"summary": "done"},
        perm=ao.PermissionLevel.EXECUTE)})

    # recommendation — nothing exists yet
    built = report.build(state={"tasks": {"awaiting_approval": 0}},
                         movement={"first_cycle": True, "changes": []},
                         health={}, execution={"due_tasks": [], "stale_tasks": [], "business": {}},
                         verifications=[])
    assert built["actions_taken"] == [] and built["tasks_created"] == []

    # task created != executed
    task = orch.assign_task("x", "send outreach")
    assert task.status == ao.TaskStatus.PENDING_APPROVAL and ran == []

    # approval requested != approved
    orch.request_approval(task.id)
    assert orch.get_task(task.id).status == ao.TaskStatus.PENDING_APPROVAL and ran == []

    # approved -> executed
    orch.approve_task(task.id)
    assert ran == [task.id]

    # executed != reported complete without verification
    execution = {"due_tasks": [orch.get_task(task.id)], "stale_tasks": [], "business": {}}
    unverified = report.build(state={}, movement={"first_cycle": True, "changes": []},
                              health={}, execution=execution, verifications=[])
    assert unverified["actions_taken"] == [], "completion was claimed without verification"
    assert unverified["actions_attempted_unverified"], "unverified work vanished from the report"

    verified = report.build(state={}, movement={"first_cycle": True, "changes": []},
                            health={}, execution=execution,
                            verifications=[{"reference_id": task.id, "success": True}])
    assert [a["agent_id"] for a in verified["actions_taken"]] == ["x"]
