"""actions/agent_orchestrator.py — restart recovery (persisted agent
status/task/event history survives a fresh AgentOrchestrator instance
against the same DB) and the single-instance scheduler lock (duplicate-run
prevention). DB/lock paths are isolated per-test by tests/conftest.py's
autouse fixture — never touches the real data/jarvis2.db.

Schedule-calculation tests (get_due_agents / _parse_schedule_minutes)
already exist in tests/test_agent_scheduler.py and aren't duplicated here.
"""
import time

from actions import agent_orchestrator as ao


def _agent(**overrides):
    defaults = dict(
        id="persist_test_agent", name="Persistence Test Agent", description="x", nucleus_id="system",
        permission_level=ao.PermissionLevel.OBSERVE, status=ao.AgentStatus.REGISTERED,
        schedule="60m", handler=lambda task: {"summary": "ran", "n": 1},
    )
    defaults.update(overrides)
    return ao.AgentDefinition(**defaults)


# ── restart recovery: agent status/last_run_ts survives a fresh instance ──

def test_started_agent_status_survives_a_new_orchestrator_instance():
    agent = _agent()
    orch1 = ao.AgentOrchestrator(agents={"persist_test_agent": agent})
    orch1.start_agent("persist_test_agent")
    assert orch1.get_agent("persist_test_agent").status == ao.AgentStatus.IDLE

    # Simulate a restart: a brand-new orchestrator, same underlying DB,
    # constructed from the same REGISTERED-by-default agent blueprint.
    fresh_agent = _agent()
    assert fresh_agent.status == ao.AgentStatus.REGISTERED
    orch2 = ao.AgentOrchestrator(agents={"persist_test_agent": fresh_agent})

    # Without persistence this would incorrectly be REGISTERED again,
    # silently stopping scheduled automation after every restart.
    assert orch2.get_agent("persist_test_agent").status == ao.AgentStatus.IDLE


def test_last_run_ts_survives_a_new_orchestrator_instance():
    agent = _agent(status=ao.AgentStatus.IDLE, last_run_ts=None)
    orch1 = ao.AgentOrchestrator(agents={"persist_test_agent": agent})
    orch1.run_due_agents()
    run_ts = orch1.get_agent("persist_test_agent").last_run_ts
    assert run_ts is not None

    orch2 = ao.AgentOrchestrator(agents={"persist_test_agent": _agent(status=ao.AgentStatus.IDLE)})
    assert orch2.get_agent("persist_test_agent").last_run_ts == run_ts
    # And it correctly isn't due again immediately after "restart":
    assert orch2.get_due_agents() == []


def test_last_error_survives_a_new_orchestrator_instance():
    def _failing_handler(task):
        raise RuntimeError("boom")

    agent = _agent(status=ao.AgentStatus.IDLE, handler=_failing_handler)
    orch1 = ao.AgentOrchestrator(agents={"persist_test_agent": agent})
    orch1.run_due_agents()
    assert orch1.get_agent("persist_test_agent").last_error == "boom"

    orch2 = ao.AgentOrchestrator(agents={"persist_test_agent": _agent(status=ao.AgentStatus.IDLE)})
    assert orch2.get_agent("persist_test_agent").last_error == "boom"


def test_stopped_agent_status_survives_a_new_orchestrator_instance():
    agent = _agent(status=ao.AgentStatus.IDLE)
    orch1 = ao.AgentOrchestrator(agents={"persist_test_agent": agent})
    orch1.stop_agent("persist_test_agent")

    orch2 = ao.AgentOrchestrator(agents={"persist_test_agent": _agent()})
    assert orch2.get_agent("persist_test_agent").status == ao.AgentStatus.STOPPED


def test_unknown_persisted_agent_id_is_silently_ignored():
    # A stale row for an agent_id that no longer exists in this instance's
    # agent set must not error or leak in.
    agent = _agent(id="agent_a", status=ao.AgentStatus.IDLE)
    orch1 = ao.AgentOrchestrator(agents={"agent_a": agent})
    orch1.stop_agent("agent_a")

    orch2 = ao.AgentOrchestrator(agents={"agent_b": _agent(id="agent_b")})
    assert orch2.get_agent("agent_b").status == ao.AgentStatus.REGISTERED
    assert orch2.get_agent("agent_a") is None


# ── restart recovery: task/event history survives ────────────────────────

def test_task_history_survives_a_new_orchestrator_instance():
    agent = _agent(status=ao.AgentStatus.IDLE)
    orch1 = ao.AgentOrchestrator(agents={"persist_test_agent": agent})
    task = orch1.assign_task("persist_test_agent", "do the thing")
    assert task.status == ao.TaskStatus.DONE

    orch2 = ao.AgentOrchestrator(agents={"persist_test_agent": _agent()})
    reloaded = orch2.get_task(task.id)
    assert reloaded is not None
    assert reloaded.status == ao.TaskStatus.DONE
    assert reloaded.result == {"summary": "ran", "n": 1}


def test_event_history_survives_a_new_orchestrator_instance():
    agent = _agent(status=ao.AgentStatus.IDLE)
    orch1 = ao.AgentOrchestrator(agents={"persist_test_agent": agent})
    orch1.assign_task("persist_test_agent", "do the thing")
    events_before = orch1.list_events("persist_test_agent")
    assert len(events_before) >= 2   # task_assigned + task_done

    orch2 = ao.AgentOrchestrator(agents={"persist_test_agent": _agent()})
    events_after = orch2.list_events("persist_test_agent")
    assert len(events_after) == len(events_before)


def test_report_event_is_persisted(monkeypatch):
    agent = _agent(status=ao.AgentStatus.IDLE)
    orch1 = ao.AgentOrchestrator(agents={"persist_test_agent": agent})
    orch1.report_event("persist_test_agent", "custom note", kind="log")

    orch2 = ao.AgentOrchestrator(agents={"persist_test_agent": _agent()})
    messages = [e.message for e in orch2.list_events("persist_test_agent")]
    assert "custom note" in messages


def test_rejected_task_status_is_persisted():
    execute_agent = _agent(id="execute_agent", permission_level=ao.PermissionLevel.EXECUTE)
    orch1 = ao.AgentOrchestrator(agents={"execute_agent": execute_agent})
    task = orch1.assign_task("execute_agent", "do something real")
    assert task.status == ao.TaskStatus.PENDING_APPROVAL
    orch1.reject_task(task.id)

    orch2 = ao.AgentOrchestrator(agents={"execute_agent": _agent(id="execute_agent")})
    assert orch2.get_task(task.id).status == ao.TaskStatus.REJECTED


def test_persistence_failure_does_not_break_normal_operation(monkeypatch, tmp_path):
    # A genuinely inaccessible DB path (a directory, not a file) must not
    # crash any mutator — the real safety property each _save_*'s own
    # try/except provides.
    bogus = tmp_path / "not_a_file.db"
    bogus.mkdir()
    monkeypatch.setattr(ao, "DB_PATH", bogus)

    agent = _agent(status=ao.AgentStatus.IDLE)
    orch = ao.AgentOrchestrator(agents={"persist_test_agent": agent})
    task = orch.assign_task("persist_test_agent", "do the thing")   # must not raise
    assert task.status == ao.TaskStatus.DONE
    orch.stop_agent("persist_test_agent")   # must not raise
    assert orch.get_agent("persist_test_agent").status == ao.AgentStatus.STOPPED


# ── single-instance scheduler lock: duplicate-run prevention ────────────

def test_acquire_scheduler_lock_succeeds_when_free():
    assert ao.acquire_scheduler_lock() is True
    assert ao.LOCK_PATH.exists()


def test_second_acquire_from_a_different_pid_is_refused(monkeypatch):
    # The lock file records the real test process's pid on the first
    # acquire. Simulate a second, different process attempting to acquire
    # by patching getpid + liveness for this second attempt only.
    assert ao.acquire_scheduler_lock() is True

    import os as _os
    monkeypatch.setattr(_os, "getpid", lambda: 999999)
    monkeypatch.setattr(ao, "_pid_is_running", lambda pid: True)

    assert ao.acquire_scheduler_lock() is False


def test_acquire_from_the_same_pid_again_succeeds_idempotently():
    assert ao.acquire_scheduler_lock() is True
    assert ao.acquire_scheduler_lock() is True   # same process re-acquiring its own lock


def test_stale_lock_from_a_dead_pid_is_reclaimed(monkeypatch):
    assert ao.acquire_scheduler_lock() is True

    import os as _os
    monkeypatch.setattr(_os, "getpid", lambda: 424242)
    monkeypatch.setattr(ao, "_pid_is_running", lambda pid: False)   # the recorded pid is dead

    assert ao.acquire_scheduler_lock() is True   # reclaimed despite being "held"


def test_stale_lock_by_age_is_reclaimed_even_if_pid_check_would_say_alive(monkeypatch):
    assert ao.acquire_scheduler_lock() is True

    import os as _os
    import json as _json
    # Backdate the lock file well past the staleness window.
    data = _json.loads(ao.LOCK_PATH.read_text(encoding="utf-8"))
    data["acquired_ts"] = time.time() - (ao._STALE_LOCK_SECONDS + 60)
    ao.LOCK_PATH.write_text(_json.dumps(data), encoding="utf-8")

    monkeypatch.setattr(_os, "getpid", lambda: 424243)
    monkeypatch.setattr(ao, "_pid_is_running", lambda pid: True)   # would say "alive" if age were checked first

    assert ao.acquire_scheduler_lock() is True   # age-based staleness wins regardless


def test_refresh_scheduler_lock_updates_the_timestamp():
    ao.acquire_scheduler_lock()
    import json as _json
    original = _json.loads(ao.LOCK_PATH.read_text(encoding="utf-8"))
    time.sleep(0.01)
    ao.refresh_scheduler_lock()
    refreshed = _json.loads(ao.LOCK_PATH.read_text(encoding="utf-8"))
    assert refreshed["acquired_ts"] > original["acquired_ts"]


def test_release_scheduler_lock_removes_the_file_when_owned():
    ao.acquire_scheduler_lock()
    assert ao.LOCK_PATH.exists()
    ao.release_scheduler_lock()
    assert not ao.LOCK_PATH.exists()


def test_release_scheduler_lock_does_not_remove_a_lock_owned_by_another_pid(monkeypatch):
    ao.acquire_scheduler_lock()

    import os as _os
    monkeypatch.setattr(_os, "getpid", lambda: 555555)
    ao.release_scheduler_lock()   # different "pid" than the one that owns the file

    assert ao.LOCK_PATH.exists()   # untouched — not this process's lock to release


def test_corrupt_lock_file_is_treated_as_stale_and_reclaimed():
    ao.LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    ao.LOCK_PATH.write_text("not valid json", encoding="utf-8")
    assert ao.acquire_scheduler_lock() is True


def test_pid_is_running_returns_false_on_a_bogus_pid():
    # A very unlikely-to-exist PID — real liveness check via psutil.
    assert ao._pid_is_running(999_999_999) is False


def test_agent_stuck_at_running_from_an_unclean_restart_is_recovered_to_idle():
    """A process kill (crash, redeploy, Render free-tier sleep) between
    run_task() setting agent.status = RUNNING and its own finally block
    setting it back to IDLE leaves RUNNING persisted. Without recovery,
    get_due_agents() (which only ever considers IDLE agents) would exclude
    this agent forever — stuck, not idle, and never scheduled again."""
    stuck_agent = _agent(status=ao.AgentStatus.RUNNING)
    orch1 = ao.AgentOrchestrator(agents={stuck_agent.id: stuck_agent})
    ao._save_agent_state(stuck_agent)

    stuck_task = ao.AgentTask(
        id="stuck-task", agent_id=stuck_agent.id,
        description="in flight when the process died", status=ao.TaskStatus.RUNNING,
    )
    ao._save_task(stuck_task)

    # "Restart": a fresh AgentOrchestrator against the same persisted state.
    orch2 = ao.AgentOrchestrator(agents={stuck_agent.id: _agent(status=ao.AgentStatus.REGISTERED)})

    recovered = orch2.get_agent(stuck_agent.id)
    assert recovered.status == ao.AgentStatus.IDLE
    assert "restart" in (recovered.last_error or "").lower()

    recovered_task = orch2.get_task("stuck-task")
    assert recovered_task.status == ao.TaskStatus.FAILED
    assert "restart" in recovered_task.error.lower()

    events = orch2.list_events(stuck_agent.id)
    assert any("recovered" in e.message.lower() for e in events)


def test_agent_persisted_as_idle_is_not_touched_by_crash_recovery():
    """Crash recovery must only ever act on agents actually stuck at
    RUNNING — a normally-idle agent's state/events must pass through
    untouched."""
    idle_agent = _agent(status=ao.AgentStatus.IDLE, last_error=None)
    ao._save_agent_state(idle_agent)

    orch = ao.AgentOrchestrator(agents={idle_agent.id: _agent(status=ao.AgentStatus.REGISTERED)})
    restored = orch.get_agent(idle_agent.id)

    assert restored.status == ao.AgentStatus.IDLE
    assert restored.last_error is None
    assert orch.list_events(idle_agent.id) == []


def test_run_task_refuses_to_double_run_an_already_running_agent():
    """Two overlapping attempts to run a task for the same agent must not
    execute the handler twice concurrently — the second is refused, not
    silently queued behind or raced against the first."""
    calls = []

    def _slow_handler(task):
        calls.append(task.id)
        return {"summary": "ran"}

    agent = _agent(status=ao.AgentStatus.IDLE, handler=_slow_handler)
    orch = ao.AgentOrchestrator(agents={agent.id: agent})

    first = orch.assign_task(agent.id, "first task")
    assert first.status == ao.TaskStatus.DONE  # OBSERVE-level runs immediately, then returns to IDLE

    # Manually force the agent back to RUNNING to simulate a task genuinely
    # still in flight (assign_task's own real run happens synchronously in
    # this codebase, so this is how a would-be second concurrent call is
    # reproduced deterministically without real threads).
    live_agent = orch.get_agent(agent.id)
    live_agent.status = ao.AgentStatus.RUNNING

    second = orch.assign_task(agent.id, "second task, should be refused")
    # assign_task() for an OBSERVE agent calls run_task() synchronously,
    # which must refuse rather than execute the handler a second time.
    assert second.status == ao.TaskStatus.REJECTED
    assert "already running" in second.error.lower()
    assert calls == [first.id]   # handler ran exactly once, not twice


# ── Autonomous objective loop (get_stale_autonomous_agents) ──────────────

def test_get_stale_autonomous_agents_only_includes_opted_in_idle_non_execute_agents():
    calls = []

    def _handler(task):
        calls.append(task.agent_id)
        return {"summary": "surveyed"}

    autonomous_agent = ao.AgentDefinition(
        id="auto_agent", name="Autonomous Test Agent", description="x", nucleus_id="system",
        permission_level=ao.PermissionLevel.OBSERVE, status=ao.AgentStatus.IDLE,
        schedule=None, autonomous_ok=True, handler=_handler,
    )
    # Needs a real topic per call — must NOT be swept into a generic trigger.
    topic_agent = ao.AgentDefinition(
        id="topic_agent", name="Needs A Real Topic", description="x", nucleus_id="system",
        permission_level=ao.PermissionLevel.OBSERVE, status=ao.AgentStatus.IDLE,
        schedule=None, autonomous_ok=False, handler=_handler,
    )
    # Even opted in, EXECUTE-level must never auto-run unattended.
    execute_agent = ao.AgentDefinition(
        id="execute_agent", name="Dangerous", description="x", nucleus_id="system",
        permission_level=ao.PermissionLevel.EXECUTE, status=ao.AgentStatus.IDLE,
        schedule=None, autonomous_ok=True, handler=_handler,
    )
    # Opted in but not started yet — must not run before start_agent().
    not_started = ao.AgentDefinition(
        id="not_started", name="Not Started", description="x", nucleus_id="system",
        permission_level=ao.PermissionLevel.OBSERVE, status=ao.AgentStatus.REGISTERED,
        schedule=None, autonomous_ok=True, handler=_handler,
    )

    orch = ao.AgentOrchestrator(agents={
        "auto_agent": autonomous_agent, "topic_agent": topic_agent,
        "execute_agent": execute_agent, "not_started": not_started,
    })

    stale = orch.get_stale_autonomous_agents()
    assert [a.id for a in stale] == ["auto_agent"]

    ran = orch.run_stale_autonomous_agents()
    assert calls == ["auto_agent"]
    assert len(ran) == 1


def test_get_stale_autonomous_agents_respects_staleness_window():
    def _handler(task):
        return {"summary": "surveyed"}

    agent = ao.AgentDefinition(
        id="auto_agent", name="Autonomous Test Agent", description="x", nucleus_id="system",
        permission_level=ao.PermissionLevel.OBSERVE, status=ao.AgentStatus.IDLE,
        schedule=None, autonomous_ok=True, last_run_ts=time.time(), handler=_handler,
    )
    orch = ao.AgentOrchestrator(agents={"auto_agent": agent})

    # Just ran moments ago — not stale yet under the default 6h window.
    assert orch.get_stale_autonomous_agents() == []
    # But long enough ago that it is.
    time.sleep(0.01)
    assert [a.id for a in orch.get_stale_autonomous_agents(staleness_secs=0.005)] == ["auto_agent"]


# ── Autonomous objective loop: topic rotation for research-style agents ──

def test_run_stale_autonomous_agents_rotates_through_autonomous_topics():
    calls = []

    def _handler(task):
        calls.append(task.description)
        return {"summary": "researched"}

    agent = ao.AgentDefinition(
        id="topic_agent", name="Topic Agent", description="x", nucleus_id="system",
        permission_level=ao.PermissionLevel.OBSERVE, status=ao.AgentStatus.IDLE, schedule=None,
        autonomous_ok=True, autonomous_topics=("topic A", "topic B", "topic C"), handler=_handler,
    )
    orch = ao.AgentOrchestrator(agents={"topic_agent": agent})

    for _ in range(4):
        for a in orch._agents.values():
            a.last_run_ts = 0.0
            a.status = ao.AgentStatus.IDLE
        orch.run_stale_autonomous_agents()

    assert calls == ["topic A", "topic B", "topic C", "topic A"]


def test_autonomous_topic_rotation_survives_a_restart():
    def _handler(task):
        return {"summary": "researched"}

    def _fresh_agent(status):
        return ao.AgentDefinition(
            id="topic_agent", name="Topic Agent", description="x", nucleus_id="system",
            permission_level=ao.PermissionLevel.OBSERVE, status=status, schedule=None,
            autonomous_ok=True, autonomous_topics=("topic A", "topic B", "topic C"), handler=_handler,
        )

    orch1 = ao.AgentOrchestrator(agents={"topic_agent": _fresh_agent(ao.AgentStatus.IDLE)})
    orch1.run_stale_autonomous_agents()  # -> "topic A"

    # "Restart": a brand new orchestrator against the same persisted DB.
    orch2 = ao.AgentOrchestrator(agents={"topic_agent": _fresh_agent(ao.AgentStatus.REGISTERED)})
    for a in orch2._agents.values():
        a.last_run_ts = 0.0
        a.status = ao.AgentStatus.IDLE
    tasks = orch2.run_stale_autonomous_agents()

    assert tasks[0].description == "topic B"


def test_agent_without_autonomous_topics_still_uses_the_generic_description():
    def _handler(task):
        return {"summary": "surveyed"}

    agent = ao.AgentDefinition(
        id="survey_agent", name="Survey Agent", description="x", nucleus_id="system",
        permission_level=ao.PermissionLevel.OBSERVE, status=ao.AgentStatus.IDLE,
        schedule=None, autonomous_ok=True, autonomous_topics=None, handler=_handler,
    )
    orch = ao.AgentOrchestrator(agents={"survey_agent": agent})
    tasks = orch.run_stale_autonomous_agents()
    assert tasks[0].description == "Autonomous objective check"
