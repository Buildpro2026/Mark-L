from actions import agent_orchestrator as ao


# ── registration / builtin agents ──────────────────────────────────────

def test_buildpro_email_monitor_is_a_registered_builtin_agent():
    orch = ao.AgentOrchestrator()
    agent = orch.get_agent("buildpro_email_monitor")
    assert agent is not None
    assert agent.name == "BuildPro Email Monitor"
    # Found live 2026-08-19: this Render tier has no persistent disk, so
    # every deploy wipes agent status back to "nobody's ever started it" —
    # Lee's direct complaint was that scheduled automation just never
    # runs. A scheduled builtin agent with no persisted state now defaults
    # to IDLE (auto-running) instead of REGISTERED (inert until someone
    # manually re-starts it after every single restart) — see
    # AgentOrchestrator.__init__'s is_real_workforce comment.
    assert agent.status == ao.AgentStatus.IDLE
    assert agent.permission_level == ao.PermissionLevel.SUGGEST
    assert agent.nucleus_id == "buildpro"


def test_agents_cannot_be_registered_at_runtime():
    # No public method adds a new AgentDefinition — the only way agents enter
    # the system is BUILTIN_AGENTS in code. This is the guardrail against
    # uncontrolled self-replication.
    orch = ao.AgentOrchestrator()
    assert not hasattr(orch, "register_agent")


def test_to_public_dict_never_leaks_the_handler_callable():
    agent = ao.BUILTIN_AGENTS["buildpro_email_monitor"]
    d = agent.to_public_dict()
    assert "handler" not in d


# ── status / start / stop ────────────────────────────────────────────────

def test_start_and_stop_agent_transitions_status():
    orch = ao.AgentOrchestrator()
    # Scheduled builtin agents now default to IDLE with no persisted state
    # (see the comment on the previous test) — start_agent() on an
    # already-idle agent is still a legitimate no-op transition.
    assert orch.get_agent_status("buildpro_email_monitor") == ao.AgentStatus.IDLE

    orch.start_agent("buildpro_email_monitor")
    assert orch.get_agent_status("buildpro_email_monitor") == ao.AgentStatus.IDLE

    orch.stop_agent("buildpro_email_monitor")
    assert orch.get_agent_status("buildpro_email_monitor") == ao.AgentStatus.STOPPED


def test_unknown_agent_raises_key_error():
    orch = ao.AgentOrchestrator()
    try:
        orch.start_agent("does_not_exist")
        assert False, "expected KeyError"
    except KeyError:
        pass


# ── task assignment (hook — Task 3 added real execution on top of this) ──

def test_assign_task_to_suggest_level_agent_auto_runs(gmail_not_authorized):
    # Task 3 behavior: OBSERVE/SUGGEST agents run immediately on assignment
    # since neither level performs real side effects — see
    # test_agent_orchestrator_execution.py for the full execution-engine
    # test coverage (failures, EXECUTE approval gating, etc.).
    orch = ao.AgentOrchestrator()
    task = orch.assign_task("buildpro_email_monitor", "Check for new candidate emails")
    assert task.status == ao.TaskStatus.DONE
    assert task.result is not None


def test_assign_task_to_execute_level_agent_requires_approval_state():
    execute_agent = ao.AgentDefinition(
        id="test_execute_agent", name="Test Execute Agent", description="x",
        nucleus_id="system", permission_level=ao.PermissionLevel.EXECUTE,
    )
    orch = ao.AgentOrchestrator(agents={"test_execute_agent": execute_agent})
    task = orch.assign_task("test_execute_agent", "Do something real")
    assert task.status == ao.TaskStatus.PENDING_APPROVAL


def test_assign_task_generates_an_event(gmail_not_authorized):
    orch = ao.AgentOrchestrator()
    orch.assign_task("buildpro_email_monitor", "Check inbox")
    events = orch.list_events("buildpro_email_monitor")
    assert any(e.kind == "task_assigned" for e in events)


# ── results / events hooks ───────────────────────────────────────────────

def test_get_results_is_empty_for_an_agent_that_has_never_run():
    orch = ao.AgentOrchestrator()
    assert orch.get_results("buildpro_email_monitor") == []


def test_get_results_reflects_a_completed_task(gmail_not_authorized):
    orch = ao.AgentOrchestrator()
    orch.assign_task("buildpro_email_monitor", "Check inbox")
    assert orch.get_results("buildpro_email_monitor")   # SUGGEST-level auto-ran


def test_report_event_is_a_public_hook_for_agent_generated_events():
    orch = ao.AgentOrchestrator()
    orch.report_event("buildpro_email_monitor", "Found 3 new emails", kind="log")
    events = orch.list_events("buildpro_email_monitor")
    assert any(e.message == "Found 3 new emails" for e in events)


def test_summary_reports_agents_and_pending_approvals():
    execute_agent = ao.AgentDefinition(
        id="test_execute_agent", name="Test Execute Agent", description="x",
        nucleus_id="system", permission_level=ao.PermissionLevel.EXECUTE,
    )
    orch = ao.AgentOrchestrator(agents={
        "buildpro_email_monitor": ao.BUILTIN_AGENTS["buildpro_email_monitor"],
        "test_execute_agent": execute_agent,
    })
    orch.assign_task("test_execute_agent", "Send a real email")
    s = orch.summary()
    assert s["pending_approval_count"] == 1
    assert any(a["id"] == "buildpro_email_monitor" for a in s["agents"])


# ── module-level singleton ───────────────────────────────────────────────

def test_module_level_orchestrator_singleton_exists():
    assert isinstance(ao.orchestrator, ao.AgentOrchestrator)
    assert ao.orchestrator.get_agent("buildpro_email_monitor") is not None
