import asyncio
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _new_live(name="jarvis_main_nav"):
    main = load_module(name, "main.py")
    live = object.__new__(main.JarvisLive)
    return main, live


def _ui_stub():
    return type("UIStub", (), {
        "muted": False,
        "set_state": lambda self, s: None,
        "write_log": lambda self, m: None,
    })()


class _FakeDashboard:
    """Mirrors DashboardServer.apply_navigation's persistent-state semantics
    closely enough (open/home change "current", status/back just report it)
    to exercise _execute_tool's dispatch without spinning up a real server."""

    _NAMES = {"buildpro": "BuildPro", "email": "Email", "jarvis": "Jarvis", "ddf": "Daily Deal Finders"}

    def __init__(self):
        self.apply_calls = []
        self.broadcast_calls = []
        self._current = "jarvis"

    def apply_navigation(self, action, nucleus_id=""):
        self.apply_calls.append((action, nucleus_id))
        if action == "open" and nucleus_id:
            self._current = nucleus_id
        elif action == "home":
            self._current = "jarvis"
        name = self._NAMES.get(self._current, self._current)
        return {"type": "navigate", "action": action, "nucleus_id": self._current, "name": name, "ts": 0}

    async def broadcast_nav(self, msg):
        self.broadcast_calls.append(msg)


def _make_fc(action=None, target=None):
    args = {}
    if action is not None:
        args["action"] = action
    if target is not None:
        args["target"] = target
    return type("FC", (), {"id": "call-1", "name": "navigate_command_center", "args": args})()


def _run(coro):
    return asyncio.run(coro)


# ── navigate_command_center is a real tool the model can call ──

def test_tool_declared_for_gemini():
    main, _ = _new_live()
    names = [t["name"] for t in main.TOOL_DECLARATIONS]
    assert "navigate_command_center" in names


# ── _execute_tool dispatch ──

def test_execute_tool_opens_a_nucleus_by_target_name():
    main, live = _new_live()
    live.ui = _ui_stub()
    live._dashboard = _FakeDashboard()
    live._loop = None   # keep the orb-state side-broadcast a no-op for this test

    fc = _make_fc(action="open", target="BuildPro")
    response = _run(live._execute_tool(fc))

    assert live._dashboard.apply_calls == [("open", "buildpro")]
    assert live._dashboard.broadcast_calls, "navigate event must be pushed to /3d/ws clients"
    assert "BuildPro" in response.response["result"]


def test_execute_tool_go_back():
    main, live = _new_live()
    live.ui = _ui_stub()
    live._dashboard = _FakeDashboard()
    live._loop = None

    fc = _make_fc(action=None, target="go back")
    response = _run(live._execute_tool(fc))

    assert live._dashboard.apply_calls == [("back", "")]
    assert "back" in response.response["result"].lower()


def test_execute_tool_go_home():
    main, live = _new_live()
    live.ui = _ui_stub()
    live._dashboard = _FakeDashboard()
    live._loop = None

    fc = _make_fc(action="home", target=None)
    response = _run(live._execute_tool(fc))

    assert live._dashboard.apply_calls == [("home", "")]
    assert "command center" in response.response["result"].lower()


def test_execute_tool_what_am_i_looking_at():
    main, live = _new_live()
    live.ui = _ui_stub()
    live._dashboard = _FakeDashboard()
    live._loop = None
    live._dashboard.apply_navigation("open", "email")   # pretend we're already on Email
    live._dashboard.apply_calls.clear()

    fc = _make_fc(action="status", target=None)
    response = _run(live._execute_tool(fc))

    assert live._dashboard.apply_calls == [("status", "")]
    assert "email" in response.response["result"].lower()


def test_execute_tool_unrecognized_target_reports_error_without_touching_dashboard():
    main, live = _new_live()
    live.ui = _ui_stub()
    live._dashboard = _FakeDashboard()
    live._loop = None

    fc = _make_fc(action="open", target="the moon base")
    response = _run(live._execute_tool(fc))

    assert live._dashboard.apply_calls == []   # never reached the dashboard
    assert "moon base" in response.response["result"]


def test_execute_tool_without_dashboard_running_gives_a_clear_message():
    main, live = _new_live()
    live.ui = _ui_stub()
    live._dashboard = None
    live._loop = None

    fc = _make_fc(action="open", target="BuildPro")
    response = _run(live._execute_tool(fc))

    assert "dashboard" in response.response["result"].lower()


# ── _broadcast_orb_state: cosmetic push, must never break the voice loop ──

def test_broadcast_orb_state_is_a_safe_noop_without_dashboard_or_loop():
    main, live = _new_live()
    live._dashboard = None
    live._loop = None
    live._broadcast_orb_state("listening")   # must not raise


def test_broadcast_orb_state_pushes_jarvis_state_when_dashboard_and_loop_exist():
    main, live = _new_live()
    dashboard = _FakeDashboard()

    async def _go():
        live._dashboard = dashboard
        live._loop = asyncio.get_running_loop()
        live._broadcast_orb_state("speaking")
        await asyncio.sleep(0.05)   # let the scheduled coroutine actually run

    asyncio.run(_go())

    assert {"type": "jarvis_state", "state": "speaking"} in dashboard.broadcast_calls
