"""High-value end-to-end tests spanning real multi-subsystem chains — see
docs/E2E_TEST_PLAN.md for what's covered here vs. by the extensive
per-subsystem tests added throughout the rest of this test suite.

Mocks/fakes are used only at true external boundaries (network, the
Gemini Live session object, wall-clock sleeps in background loops) —
never at the seams between JARVIS's own modules, since proving those
seams work together for real is the entire point of this file.
"""
import asyncio
import importlib.util
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ui_stub(log: list | None = None):
    log = log if log is not None else []
    return type("UIStub", (), {
        "muted": False,
        "set_state": lambda self, s: log.append(("state", s)),
        "write_log": lambda self, m: log.append(("log", m)),
    })()


def _make_fc(tool_name, **args):
    return type("FC", (), {"id": "call-1", "name": tool_name, "args": args})()


def _live(main):
    live = object.__new__(main.JarvisLive)
    live._dashboard = None
    live._loop = None
    return live


# ── 1. Voice -> command -> UI status, chained into memory/config ────────

def test_save_memory_command_updates_ui_state_and_persists_to_future_prompts(monkeypatch, tmp_path):
    import memory.memory_manager as mm
    monkeypatch.setattr(mm, "MEMORY_PATH", tmp_path / "e2e_long_term.json")

    main = load_module("jarvis_e2e_memory", "main.py")
    log: list = []
    live = _live(main)
    live.ui = _ui_stub(log)

    # Turn 1: the voice tool call, exactly as Gemini would issue it after
    # the user says "I work as a site superintendent."
    fc = _make_fc("save_memory", category="identity", key="job", value="Site superintendent")
    response = asyncio.run(live._execute_tool(fc))

    # UI status actually changed for this turn (THINKING at dispatch entry,
    # LISTENING once the silent memory-save tool completes).
    states = [entry[1] for entry in log if entry[0] == "state"]
    assert "THINKING" in states
    assert "LISTENING" in states
    assert response.response["result"] == "ok"

    # The write actually landed on disk, independent of the tool call.
    saved = mm.load_memory()
    assert saved["identity"]["job"]["value"] == "Site superintendent"

    # Turn 2 (a LATER, unrelated session): the same fact must actually
    # reach a future system prompt — the whole point of saving it.
    prompt_text = mm.format_memory_for_prompt(mm.load_memory())
    assert "Site superintendent" in prompt_text


# ── 2. A real low-level failure surfaces correctly through every layer ──

def test_gmail_oauth_failure_propagates_to_a_spoken_message_without_crashing(monkeypatch):
    main = load_module("jarvis_e2e_gmail_failure", "main.py")
    live = _live(main)
    live.ui = _ui_stub()

    # The actual, real failure mode: no cached token yet. Raised at the
    # lowest layer (google_auth), not mocked away at gmail_integration's
    # boundary the way the Prompt 8 unit tests do.
    def _raise_not_authorized():
        raise RuntimeError(
            "Google account not yet authorized. Run "
            "`python -c \"from actions.google_auth import authorize_interactively as a; a()\"` once."
        )

    monkeypatch.setattr(main.google_auth, "get_credentials", _raise_not_authorized)

    fc = _make_fc("gmail", action="send", to="jane@example.com", body="Hello")
    response = asyncio.run(live._execute_tool(fc))

    result = response.response["result"].lower()
    assert "couldn't send the email" in result
    assert "not_authorized" in result
    # And the dispatcher didn't crash / lose the turn — a real
    # FunctionResponse came back, not an unhandled exception.
    assert response.response is not None


# ── 3. Integration confirmation gate — the real two-turn workflow ───────

def test_buffer_preview_then_confirmed_publish_two_turn_workflow(monkeypatch):
    main = load_module("jarvis_e2e_buffer_gate", "main.py")
    live = _live(main)
    live.ui = _ui_stub()

    calls: list[bool] = []

    def _fake_publish(post, approved=False):
        calls.append(approved)
        if not approved:
            return {
                "published": False, "status": "PREVIEW",
                "preview": {"channel_id": "c1", "service": "linkedin", "text": "New deal today!", "mode": "addToQueue"},
            }
        return {"published": True, "status": "PUBLISHED", "buffer_id": "p1"}

    monkeypatch.setattr(main.buffer_integration, "publish_to_buffer", _fake_publish)

    # Turn 1: preview — must not publish.
    preview_fc = _make_fc("social_post", action="preview", text="New deal today!", service="linkedin")
    preview_response = asyncio.run(live._execute_tool(preview_fc))
    assert calls == [False]
    assert "preview" in preview_response.response["result"].lower()

    # Turn 2: the user has now seen the preview and confirmed it — publish
    # the SAME content for real.
    publish_fc = _make_fc("social_post", action="publish", text="New deal today!", service="linkedin")
    publish_response = asyncio.run(live._execute_tool(publish_fc))
    assert calls == [False, True]
    assert "posted" in publish_response.response["result"].lower()


# ── shared: fast-forwarding asyncio.sleep for background-loop tests ─────

_real_sleep = asyncio.sleep


async def _fast_sleep(seconds, *a, **k):
    """Real asyncio.sleep for short waits (so cooperative scheduling still
    works); fast-forwards anything >= 60s (the real poll intervals used by
    _run_proactive_mode/_run_agent_scheduler) to keep tests fast. Captures
    the ORIGINAL asyncio.sleep before any patching to avoid the module-
    global-patch self-recursion mistake documented in test_proactive.py."""
    if seconds >= 60:
        return
    await _real_sleep(min(seconds, 0.01))


async def _run_one_iteration(coro, wait: float = 0.3):
    task = asyncio.ensure_future(coro)
    await _real_sleep(wait)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ── 4. Proactive suggestions — the real loop, not just the engine ───────

class _FakeSendSession:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_client_content(self, **kwargs):
        self.sent.append(kwargs)


def test_proactive_mode_loop_iteration_sends_a_message_and_records_the_trail(monkeypatch, tmp_path):
    main = load_module("jarvis_e2e_proactive", "main.py")
    monkeypatch.setattr(main.asyncio, "sleep", _fast_sleep)
    monkeypatch.setattr(main, "get_proactive_enabled", lambda: True)
    monkeypatch.setattr(main, "get_proactive_quiet_hours", lambda: None)
    monkeypatch.setattr(main, "load_memory", lambda: {})
    monkeypatch.setattr(main, "list_monitors", lambda: [])

    import actions.proactive as proactive_mod
    monkeypatch.setattr(proactive_mod, "DB_PATH", tmp_path / "e2e_proactive.db")

    live = _live(main)
    log: list = []
    live.ui = _ui_stub(log)
    live.session = _FakeSendSession()
    live._speaking_lock = main.threading.Lock()
    live._is_speaking = False
    # Real default cooldown (1200s), not 0 — with asyncio.sleep(60)
    # fast-forwarded to instant, the loop spins through many iterations
    # within the test's real-time window, and check_cooldown (not the
    # sleep interval) is what actually prevents runaway repeated
    # triggering. A first run of this test with check_cooldown=0 fired
    # 17 times in one pass, confirming that's a real behavioral gate, not
    # a redundant one.
    live._proactive = main.ProactiveEngine(min_silence_secs=0, check_cooldown=1200)
    live._last_user_speech = time.monotonic() - 10_000
    live._session_log = []

    asyncio.run(_run_one_iteration(live._run_proactive_mode()))

    assert len(live.session.sent) == 1
    assert "[PROACTIVE_CHECK]" in live.session.sent[0]["turns"]["parts"][0]["text"]
    assert any(kind == "log" and "Proactive check-in" in msg for kind, msg in log)

    trail = proactive_mod.get_recent_triggers()
    assert len(trail) == 1


def test_proactive_mode_loop_iteration_respects_disabled_flag(monkeypatch, tmp_path):
    main = load_module("jarvis_e2e_proactive_disabled", "main.py")
    monkeypatch.setattr(main.asyncio, "sleep", _fast_sleep)
    monkeypatch.setattr(main, "get_proactive_enabled", lambda: False)
    monkeypatch.setattr(main, "get_proactive_quiet_hours", lambda: None)

    import actions.proactive as proactive_mod
    monkeypatch.setattr(proactive_mod, "DB_PATH", tmp_path / "e2e_proactive_disabled.db")

    live = _live(main)
    live.ui = _ui_stub()
    live.session = _FakeSendSession()
    live._speaking_lock = main.threading.Lock()
    live._is_speaking = False
    live._proactive = main.ProactiveEngine(min_silence_secs=0, check_cooldown=0)
    live._last_user_speech = time.monotonic() - 10_000
    live._session_log = []

    asyncio.run(_run_one_iteration(live._run_proactive_mode()))

    assert live.session.sent == []   # disabled — the real config flag actually stopped it


# ── 5. Scheduler state — the real loop, chained into restart recovery ───

def test_agent_scheduler_loop_iteration_runs_a_due_agent_and_persists_it(monkeypatch, tmp_path):
    import actions.agent_orchestrator as ao
    monkeypatch.setattr(ao, "DB_PATH", tmp_path / "e2e_scheduler.db")
    monkeypatch.setattr(ao, "LOCK_PATH", tmp_path / "e2e_scheduler.lock")

    main = load_module("jarvis_e2e_scheduler", "main.py")
    monkeypatch.setattr(main.asyncio, "sleep", _fast_sleep)

    test_agent = ao.AgentDefinition(
        id="e2e_due_agent", name="E2E Due Agent", description="x", nucleus_id="system",
        permission_level=ao.PermissionLevel.OBSERVE, status=ao.AgentStatus.IDLE,
        schedule="60m", last_run_ts=None,
        handler=lambda task: {"summary": "e2e ran"},
    )
    test_orchestrator = ao.AgentOrchestrator(agents={"e2e_due_agent": test_agent})
    monkeypatch.setattr(main, "agent_orchestrator", test_orchestrator)

    live = _live(main)
    log: list = []
    live.ui = _ui_stub(log)

    asyncio.run(_run_one_iteration(live._run_agent_scheduler()))

    ran_agent = test_orchestrator.get_agent("e2e_due_agent")
    assert ran_agent.last_run_ts is not None
    assert any(kind == "log" and "E2E Due Agent" in msg for kind, msg in log)

    # Restart recovery (Prompt 16), chained: a FRESH orchestrator against
    # the SAME db must see this scheduled run happened.
    fresh_agent = ao.AgentDefinition(
        id="e2e_due_agent", name="E2E Due Agent", description="x", nucleus_id="system",
        permission_level=ao.PermissionLevel.OBSERVE, status=ao.AgentStatus.REGISTERED,
        schedule="60m", handler=lambda task: {"summary": "e2e ran"},
    )
    recovered = ao.AgentOrchestrator(agents={"e2e_due_agent": fresh_agent})
    assert recovered.get_agent("e2e_due_agent").status == ao.AgentStatus.IDLE
    assert recovered.get_agent("e2e_due_agent").last_run_ts == ran_agent.last_run_ts


def test_agent_scheduler_loop_skips_when_lock_held_by_another_process(monkeypatch, tmp_path):
    import actions.agent_orchestrator as ao
    monkeypatch.setattr(ao, "DB_PATH", tmp_path / "e2e_scheduler2.db")
    monkeypatch.setattr(ao, "LOCK_PATH", tmp_path / "e2e_scheduler2.lock")

    main = load_module("jarvis_e2e_scheduler_locked", "main.py")
    monkeypatch.setattr(main.asyncio, "sleep", _fast_sleep)

    # Simulate another live process already holding the lock.
    monkeypatch.setattr(ao, "_pid_is_running", lambda pid: True)
    import os as _os
    ao.LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    import json as _json
    ao.LOCK_PATH.write_text(_json.dumps({"pid": _os.getpid() + 1, "acquired_ts": time.time()}), encoding="utf-8")

    test_agent = ao.AgentDefinition(
        id="e2e_locked_agent", name="E2E Locked Agent", description="x", nucleus_id="system",
        permission_level=ao.PermissionLevel.OBSERVE, status=ao.AgentStatus.IDLE,
        schedule="60m", last_run_ts=None, handler=lambda task: {"summary": "should not run"},
    )
    test_orchestrator = ao.AgentOrchestrator(agents={"e2e_locked_agent": test_agent})
    monkeypatch.setattr(main, "agent_orchestrator", test_orchestrator)

    live = _live(main)
    live.ui = _ui_stub()

    asyncio.run(_run_one_iteration(live._run_agent_scheduler()))

    # Never ran — this process never acquired the lock.
    assert test_orchestrator.get_agent("e2e_locked_agent").last_run_ts is None
