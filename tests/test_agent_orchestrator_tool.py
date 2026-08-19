import asyncio
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _new_live(name="jarvis_main_agents"):
    main = load_module(name, "main.py")
    live = object.__new__(main.JarvisLive)
    return main, live


def _ui_stub():
    return type("UIStub", (), {
        "muted": False,
        "set_state": lambda self, s: None,
        "write_log": lambda self, m: None,
    })()


def _make_fc(**args):
    return type("FC", (), {"id": "call-1", "name": "agent_orchestrator", "args": args})()


def _run(coro):
    return asyncio.run(coro)


def test_tool_declared_for_gemini():
    main, _ = _new_live()
    names = [t["name"] for t in main.TOOL_DECLARATIONS]
    assert "agent_orchestrator" in names


def test_list_action_mentions_the_builtin_agent():
    main, live = _new_live()
    live.ui = _ui_stub()
    live._dashboard = None
    live._loop = None

    fc = _make_fc(action="list")
    response = _run(live._execute_tool(fc))
    assert "BuildPro Email Monitor" in response.response["result"]


def test_assign_to_suggest_agent_runs_and_reports_completion(gmail_not_authorized):
    main, live = _new_live()
    live.ui = _ui_stub()
    live._dashboard = None
    live._loop = None

    fc = _make_fc(action="assign", agent_id="buildpro_email_monitor", task="Check the inbox")
    response = _run(live._execute_tool(fc))
    assert "completed" in response.response["result"].lower()


def test_assign_to_execute_agent_requires_approval_and_never_autoruns():
    main, live = _new_live()
    live.ui = _ui_stub()
    live._dashboard = None
    live._loop = None

    # Register a throwaway EXECUTE-level agent on the shared singleton for
    # this test only, so the tool path is exercised against a real EXECUTE
    # agent without touching the real BuildPro Email Monitor's permission level.
    ao = main.agent_orchestrator
    calls = []

    from actions.agent_orchestrator import AgentDefinition, PermissionLevel
    ao._agents["test_execute_via_tool"] = AgentDefinition(
        id="test_execute_via_tool", name="Test Execute Via Tool", description="x",
        nucleus_id="system", permission_level=PermissionLevel.EXECUTE,
        handler=lambda task: calls.append(task.id) or {"ok": True},
    )
    try:
        fc = _make_fc(action="assign", agent_id="test_execute_via_tool", task="Do something real")
        response = _run(live._execute_tool(fc))
        assert "approval" in response.response["result"].lower()
        assert calls == []
    finally:
        del ao._agents["test_execute_via_tool"]


def test_assign_reports_honest_failure_not_task_completed(gmail_not_authorized):
    # Phase 2 fix: a handler that returns normally (task.status == DONE)
    # while its result dict says the underlying action actually failed
    # (e.g. buildpro_email_responder's real Gmail send failing) used to be
    # phrased to Lee/Gemini as "Task completed: <failure text>" — a
    # contradiction that reads as success. Must say it did not succeed.
    main, live = _new_live()
    live.ui = _ui_stub()
    live._dashboard = None
    live._loop = None

    ao = main.agent_orchestrator
    from actions.agent_orchestrator import AgentDefinition, PermissionLevel
    ao._agents["test_soft_failure_via_tool"] = AgentDefinition(
        id="test_soft_failure_via_tool", name="Test Soft Failure", description="x",
        nucleus_id="system", permission_level=PermissionLevel.SUGGEST,
        handler=lambda task: {"summary": "Couldn't send draft (ERROR): not found", "sent": False},
    )
    try:
        fc = _make_fc(action="assign", agent_id="test_soft_failure_via_tool", task="send it")
        response = _run(live._execute_tool(fc))
        result = response.response["result"]
        assert "did not succeed" in result.lower()
        assert "task completed:" not in result.lower()
    finally:
        del ao._agents["test_soft_failure_via_tool"]


def test_unknown_agent_id_reports_a_clear_error_not_a_crash():
    main, live = _new_live()
    live.ui = _ui_stub()
    live._dashboard = None
    live._loop = None

    fc = _make_fc(action="assign", agent_id="does_not_exist", task="Do a thing")
    response = _run(live._execute_tool(fc))
    assert "does_not_exist" in response.response["result"] or "unknown" in response.response["result"].lower()
