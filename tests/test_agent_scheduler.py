import asyncio
import importlib.util
import time
from pathlib import Path

from actions import agent_orchestrator as ao

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _agent(**overrides):
    defaults = dict(
        id="sched_test_agent", name="Scheduled Test Agent", description="x", nucleus_id="system",
        permission_level=ao.PermissionLevel.OBSERVE, status=ao.AgentStatus.IDLE,
        schedule="60m", handler=lambda task: {"summary": "ran"},
    )
    defaults.update(overrides)
    return ao.AgentDefinition(**defaults)


# ── _parse_schedule_minutes ──────────────────────────────────────────────

def test_parse_schedule_minutes_variants():
    assert ao._parse_schedule_minutes("60m") == 60.0
    assert ao._parse_schedule_minutes("2h") == 120.0
    assert ao._parse_schedule_minutes("90") == 90.0
    assert ao._parse_schedule_minutes(None) is None
    assert ao._parse_schedule_minutes("") is None
    assert ao._parse_schedule_minutes("not-a-schedule") is None


# ── get_due_agents safety gates ──────────────────────────────────────────

def test_registered_agent_never_due_even_with_schedule():
    agent = _agent(status=ao.AgentStatus.REGISTERED)
    orch = ao.AgentOrchestrator(agents={"sched_test_agent": agent})
    assert orch.get_due_agents() == []


def test_stopped_agent_never_due():
    agent = _agent(status=ao.AgentStatus.STOPPED)
    orch = ao.AgentOrchestrator(agents={"sched_test_agent": agent})
    assert orch.get_due_agents() == []


def test_execute_level_agent_never_auto_runs_even_when_idle_and_scheduled():
    agent = _agent(permission_level=ao.PermissionLevel.EXECUTE)
    orch = ao.AgentOrchestrator(agents={"sched_test_agent": agent})
    assert orch.get_due_agents() == []


def test_idle_observe_agent_with_no_schedule_never_due():
    agent = _agent(schedule=None)
    orch = ao.AgentOrchestrator(agents={"sched_test_agent": agent})
    assert orch.get_due_agents() == []


def test_idle_agent_never_run_before_is_immediately_due():
    agent = _agent(last_run_ts=None)
    orch = ao.AgentOrchestrator(agents={"sched_test_agent": agent})
    due = orch.get_due_agents()
    assert len(due) == 1
    assert due[0].id == "sched_test_agent"


def test_idle_agent_within_interval_is_not_due():
    agent = _agent(last_run_ts=time.time() - 30 * 60)   # 30 min ago, schedule is 60m
    orch = ao.AgentOrchestrator(agents={"sched_test_agent": agent})
    assert orch.get_due_agents() == []


def test_idle_agent_past_interval_is_due():
    agent = _agent(last_run_ts=time.time() - 61 * 60)   # 61 min ago, schedule is 60m
    orch = ao.AgentOrchestrator(agents={"sched_test_agent": agent})
    due = orch.get_due_agents()
    assert len(due) == 1


def test_run_due_agents_executes_and_updates_last_run_ts():
    agent = _agent(last_run_ts=None)
    orch = ao.AgentOrchestrator(agents={"sched_test_agent": agent})
    tasks = orch.run_due_agents()
    assert len(tasks) == 1
    assert tasks[0].status == ao.TaskStatus.DONE
    assert orch.get_agent("sched_test_agent").last_run_ts is not None
    # now that it just ran, it should no longer be due
    assert orch.get_due_agents() == []


def test_default_builtin_agents_with_a_schedule_are_due_without_manual_start():
    # Changed 2026-08-19: BUILTIN_AGENTS with a schedule now default to
    # IDLE (auto-running) rather than REGISTERED when there's no persisted
    # state — see AgentOrchestrator.__init__'s is_real_workforce comment.
    # Found live: this Render tier has no persistent disk, so every
    # redeploy wiped agent status back to "never started," and Lee's
    # direct complaint was that scheduled automation (email monitoring,
    # DDF, etc.) simply never ran. A synthetic test-constructed
    # AgentOrchestrator (an explicit `agents=` dict) is NOT affected by
    # this — only the real, code-defined BUILTIN_AGENTS workforce
    # constructed via AgentOrchestrator() with no arguments.
    orch = ao.AgentOrchestrator()
    scheduled_ids = {a.id for a in orch.list_agents() if a.schedule}
    assert scheduled_ids   # sanity: some real agents do have schedules
    assert {a.id for a in orch.get_due_agents()} == scheduled_ids
    # EXECUTE-level agents never auto-run on a schedule even if one has a
    # schedule value set (none currently do, but this guards the invariant).
    execute_scheduled = {a.id for a in orch.list_agents() if a.schedule and a.permission_level == ao.PermissionLevel.EXECUTE}
    assert execute_scheduled.isdisjoint({a.id for a in orch.get_due_agents()})


# ── main.py wiring ────────────────────────────────────────────────────────

def test_run_agent_scheduler_is_wired_into_jarvis_live():
    main = load_module("jarvis_main_scheduler", "main.py")
    assert hasattr(main.JarvisLive, "_run_agent_scheduler")
    assert asyncio.iscoroutinefunction(main.JarvisLive._run_agent_scheduler)
