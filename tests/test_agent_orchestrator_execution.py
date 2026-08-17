from actions import agent_orchestrator as ao


def _execute_agent(agent_id="test_execute_agent", handler=None):
    return ao.AgentDefinition(
        id=agent_id, name="Test Execute Agent", description="x",
        nucleus_id="system", permission_level=ao.PermissionLevel.EXECUTE,
        handler=handler,
    )


# ── OBSERVE/SUGGEST agents auto-run — no side-effect risk at those levels ──

def test_suggest_level_task_runs_immediately_and_produces_a_result(gmail_not_authorized):
    orch = ao.AgentOrchestrator()
    task = orch.assign_task("buildpro_email_monitor", "Check for new candidate emails")
    assert task.status == ao.TaskStatus.DONE
    # J3: the handler is now real (actions/buildpro_email_monitor.py) —
    # this specific assertion isolates Gmail as NOT_AUTHORIZED via the
    # gmail_not_authorized fixture so the result stays honest/deterministic
    # without a live network call; see test_buildpro_email_monitor.py for
    # coverage of the real authorized-and-scanning path.
    assert task.result["configured"] is False


def test_observe_level_task_auto_runs_too():
    handler_calls = []
    observe_agent = ao.AgentDefinition(
        id="observer", name="Observer", description="x", nucleus_id="system",
        permission_level=ao.PermissionLevel.OBSERVE,
        handler=lambda task: handler_calls.append(task.id) or {"summary": "observed"},
    )
    orch = ao.AgentOrchestrator(agents={"observer": observe_agent})
    task = orch.assign_task("observer", "Watch something")
    assert task.status == ao.TaskStatus.DONE
    assert handler_calls == [task.id]


# ── EXECUTE agents require Lee's explicit approval before running ──────────

def test_execute_level_task_does_not_run_until_approved():
    handler_calls = []
    agent = _execute_agent(handler=lambda task: handler_calls.append(task.id) or {"ok": True})
    orch = ao.AgentOrchestrator(agents={"test_execute_agent": agent})

    task = orch.assign_task("test_execute_agent", "Send a real email")
    assert task.status == ao.TaskStatus.PENDING_APPROVAL
    assert handler_calls == []   # must NOT have run yet


def test_run_task_refuses_a_pending_approval_task_directly():
    agent = _execute_agent(handler=lambda task: {"ok": True})
    orch = ao.AgentOrchestrator(agents={"test_execute_agent": agent})
    task = orch.assign_task("test_execute_agent", "Send a real email")

    try:
        orch.run_task(task.id)
        assert False, "expected PermissionError"
    except PermissionError:
        pass
    assert orch.get_task(task.id).status == ao.TaskStatus.PENDING_APPROVAL


def test_approve_task_runs_it_and_stores_the_result():
    handler_calls = []
    agent = _execute_agent(handler=lambda task: handler_calls.append(task.id) or {"sent": True})
    orch = ao.AgentOrchestrator(agents={"test_execute_agent": agent})
    task = orch.assign_task("test_execute_agent", "Send a real email")

    approved = orch.approve_task(task.id)

    assert approved.status == ao.TaskStatus.DONE
    assert approved.result == {"sent": True}
    assert handler_calls == [task.id]
    assert orch.get_results("test_execute_agent") == [{"sent": True}]


def test_reject_task_leaves_it_rejected_and_never_runs():
    handler_calls = []
    agent = _execute_agent(handler=lambda task: handler_calls.append(task.id) or {"sent": True})
    orch = ao.AgentOrchestrator(agents={"test_execute_agent": agent})
    task = orch.assign_task("test_execute_agent", "Send a real email")

    rejected = orch.reject_task(task.id)

    assert rejected.status == ao.TaskStatus.REJECTED
    assert handler_calls == []
    assert orch.get_results("test_execute_agent") == []


# ── failures are captured, not raised, and agent status recovers ──────────

def test_a_failing_handler_marks_the_task_failed_not_the_whole_orchestrator():
    def boom(task):
        raise ValueError("simulated failure")

    agent = ao.AgentDefinition(
        id="flaky", name="Flaky Agent", description="x", nucleus_id="system",
        permission_level=ao.PermissionLevel.OBSERVE, handler=boom,
    )
    orch = ao.AgentOrchestrator(agents={"flaky": agent})
    task = orch.assign_task("flaky", "Do a thing")

    assert task.status == ao.TaskStatus.FAILED
    assert "simulated failure" in task.error
    assert orch.get_agent_status("flaky") == ao.AgentStatus.IDLE   # recovered, not stuck RUNNING
    events = orch.list_events("flaky")
    assert any(e.kind == "task_failed" for e in events)


def test_agent_with_no_handler_reports_a_clear_result_instead_of_crashing():
    agent = ao.AgentDefinition(
        id="no_handler", name="No Handler", description="x", nucleus_id="system",
        permission_level=ao.PermissionLevel.OBSERVE,
    )
    orch = ao.AgentOrchestrator(agents={"no_handler": agent})
    task = orch.assign_task("no_handler", "Do a thing")
    assert task.status == ao.TaskStatus.DONE
    assert "No handler" in task.result["summary"]


# ── the module-level singleton behaves the same way ────────────────────────

def test_singleton_buildpro_email_monitor_end_to_end(gmail_not_authorized):
    task = ao.orchestrator.assign_task("buildpro_email_monitor", "Check inbox now")
    assert task.status == ao.TaskStatus.DONE
    results = ao.orchestrator.get_results("buildpro_email_monitor")
    assert results and results[-1]["configured"] is False
