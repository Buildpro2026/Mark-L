"""navigate_command_center actually works from the headless web surfaces.

The bug this closes: JARVIS would claim to open a webpage ("Opened Google
in Chrome") when nothing anyone could see actually happened. Root cause —
navigate_command_center was miscategorized as SESSION_ONLY_TOOLS (main.py's
desktop-only tools), so it was invisible to /ui's chat and to
core.headless.dashboard_bridge's /3d typed-command relay, both of which run
turns through ToolExecutor. The only tool left available for "open a
website" was browser_control, which is honest about having no visible
browser on a headless server, but is not the Command Center workspace path
at all — so a request to "open Google" either got a confusing "URL
generated, not opened" result, or (via an interactive browser_control
action) a REAL server-side Playwright browser only the server process
could ever see.

dashboard.server.DashboardServer has no PyQt dependency and is mounted in
this same headless process (core/headless/app.py) — navigate_command_center
only ever needed that instance, not a live desktop/voice session, so this
suite gives it a real one throughout rather than a hand-rolled fake, to
exercise the actual resolve() -> execute_destination() -> broadcast_nav()
path, not a stand-in for it.
"""
import asyncio

import pytest

from core.headless.context import ToolContext
from core.headless.tool_executor import ToolExecutor
from core.headless.tool_registry import SESSION_ONLY_TOOLS, TOOL_DECLARATIONS
from dashboard.server import DashboardServer


def _run(coro):
    return asyncio.run(coro)


class _FakeSocket:
    """A connected /3d/ws client, minus the actual network — records every
    payload DashboardServer.broadcast_nav() sends it, the same shape a real
    browser tab's onmessage handler would receive."""

    def __init__(self):
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


def _connected_dashboard() -> DashboardServer:
    d = DashboardServer()
    d._3d_ws_clients.add(_FakeSocket())
    return d


def _ctx(dashboard=None) -> ToolContext:
    return ToolContext(dashboard_server=dashboard)


# ── the tool is genuinely reachable headlessly now ──────────────────────

def test_navigate_command_center_is_not_session_only():
    assert "navigate_command_center" not in SESSION_ONLY_TOOLS


def test_navigate_command_center_is_declared_for_headless_chat():
    from core.headless.ui import _chat_tool_declarations
    names = [t["name"] for t in _chat_tool_declarations()]
    assert "navigate_command_center" in names


def test_the_three_genuinely_desktop_only_tools_are_still_session_only():
    assert SESSION_ONLY_TOOLS == {"screen_process", "close_camera", "shutdown_jarvis"}


def test_navigate_command_center_still_a_real_declared_tool():
    names = {t["name"] for t in TOOL_DECLARATIONS}
    assert "navigate_command_center" in names


# ── open an external website: the reported bug, end to end ─────────────

def test_open_google_actually_reaches_a_connected_window_and_reports_honestly():
    dashboard = _connected_dashboard()
    result = _run(ToolExecutor(_ctx(dashboard)).execute(
        "navigate_command_center", {"action": "open", "target": "Google"}))
    # Google refuses framing (actions/workspace_navigation.py's
    # _FRAME_REFUSERS) — real navigation still happened (a tab, not the
    # workspace panel), and the reply says so rather than claiming embed.
    assert "google.com" in result.lower()
    assert "new tab" in result.lower()
    sent = list(dashboard._3d_ws_clients)[0].sent
    assert sent, "the connected /3d window never received the navigation"
    assert sent[0]["external_url"] == "https://google.com"
    assert sent[0]["nav_action"] == "open_external_tab"


def test_open_youtube_is_embeddable_and_opens_in_the_workspace():
    dashboard = _connected_dashboard()
    result = _run(ToolExecutor(_ctx(dashboard)).execute(
        "navigate_command_center", {"action": "open", "target": "YouTube"}))
    assert "YouTube" in result or "youtube" in result.lower()
    sent = list(dashboard._3d_ws_clients)[0].sent
    assert sent[0]["nav_action"] == "open_workspace"
    assert sent[0]["embeddable"] is True


def test_open_a_supported_business_destination():
    dashboard = _connected_dashboard()
    result = _run(ToolExecutor(_ctx(dashboard)).execute(
        "navigate_command_center", {"action": "open", "target": "BuildPro"}))
    assert "BuildPro" in result
    sent = list(dashboard._3d_ws_clients)[0].sent
    assert sent[0]["nucleus_id"] == "buildpro"
    assert sent[0]["nav_action"] == "open_nucleus"


def test_open_an_iframe_blocked_destination_reports_the_tab_fallback_honestly():
    dashboard = _connected_dashboard()
    result = _run(ToolExecutor(_ctx(dashboard)).execute(
        "navigate_command_center", {"action": "open", "target": "linkedin.com"}))
    assert "new tab" in result.lower()
    sent = list(dashboard._3d_ws_clients)[0].sent
    assert sent[0]["embeddable"] is False
    assert sent[0]["nav_action"] == "open_external_tab"


def test_go_back():
    dashboard = _connected_dashboard()
    dashboard.apply_navigation("open", "buildpro")
    result = _run(ToolExecutor(_ctx(dashboard)).execute(
        "navigate_command_center", {"target": "go back"}))
    assert "went back" in result.lower() or "back" in result.lower()
    assert dashboard._nucleus_id == "jarvis"


def test_go_home():
    dashboard = _connected_dashboard()
    dashboard.apply_navigation("open", "buildpro")
    result = _run(ToolExecutor(_ctx(dashboard)).execute(
        "navigate_command_center", {"action": "home"}))
    assert "command center" in result.lower() or "home" in result.lower()
    assert dashboard._nucleus_id == "jarvis"


# ── honesty: no false success ────────────────────────────────────────────

def test_invalid_destination_is_reported_not_silently_opened():
    dashboard = _connected_dashboard()
    result = _run(ToolExecutor(_ctx(dashboard)).execute(
        "navigate_command_center", {"action": "open", "target": "the moon base"}))
    assert "couldn't find" in result.lower() or "could not find" in result.lower()
    sent = list(dashboard._3d_ws_clients)[0].sent
    assert not sent, "an unresolved destination must never reach a Command Center window"


def test_missing_target_reports_current_location_not_an_error():
    dashboard = _connected_dashboard()
    result = _run(ToolExecutor(_ctx(dashboard)).execute("navigate_command_center", {}))
    assert "looking at" in result.lower()


def test_open_with_no_target_at_all_is_reported_not_silently_opened():
    # action="open" but nothing named — a genuinely missing destination,
    # distinct from omitting action/target entirely (which means "status").
    dashboard = _connected_dashboard()
    result = _run(ToolExecutor(_ctx(dashboard)).execute(
        "navigate_command_center", {"action": "open", "target": ""}))
    assert "couldn't find" in result.lower() or "could not find" in result.lower()
    sent = list(dashboard._3d_ws_clients)[0].sent
    assert not sent


def test_no_dashboard_server_is_reported_honestly_not_as_success():
    result = _run(ToolExecutor(_ctx(None)).execute(
        "navigate_command_center", {"action": "open", "target": "Google"}))
    assert "isn't running" in result.lower() or "not running" in result.lower()


def test_no_connected_command_center_window_is_reported_not_faked():
    # A real DashboardServer with genuinely nobody connected — apply_
    # navigation() still mutates server-side state, but nothing exists on
    # any screen for it to reach, and the reply must say so, not "Opened."
    dashboard = DashboardServer()
    result = _run(ToolExecutor(_ctx(dashboard)).execute(
        "navigate_command_center", {"action": "open", "target": "BuildPro"}))
    assert "no command center window is open" in result.lower()
    assert "opened buildpro in the command center" not in result.lower()


def test_the_headless_branch_never_bypasses_the_shared_resolver_or_executor():
    # Same guarantee dashboard/server.py's own execute_destination
    # docstring makes for the desktop path: one resolver, one executor,
    # checked as code so this can't silently drift into a second
    # implementation of "what does 'open Google' mean."
    import inspect
    src = inspect.getsource(ToolExecutor.execute)
    branch = src[src.index('name == "navigate_command_center"'):]
    branch = branch[:branch.index("elif name ==", 50) if "elif name ==" in branch[50:] else len(branch)]
    assert "workspace_navigation.resolve(" in branch
    assert "ctx.dashboard_server.execute_destination(" in branch
    assert "workspace_navigation.describe(" in branch


# ── voice/chat-originated: through the real tool-calling dispatch ───────

@pytest.fixture(autouse=True)
def _ollama_only(monkeypatch):
    from core.headless import ui as headless_ui
    monkeypatch.setattr(headless_ui.config, "OLLAMA_API_KEY", "fake-key-not-real")


def test_a_chat_turn_that_calls_the_tool_actually_navigates(monkeypatch):
    """The 'voice-originated navigation' case: the same dispatch path
    Gemini's function-calling (voice or text, /ui or /3d's typed-command
    relay) uses to reach any tool — not a direct ToolExecutor call, so this
    is the guarantee that a real conversational turn ending in "open
    Google" produces a real navigation, not just that the tool works when
    called directly."""
    from core.headless import ui as headless_ui
    from tests.ollama_fake import FakeFunctionCall, FakeResponse, install

    fc = FakeFunctionCall("navigate_command_center", {"action": "open", "target": "Google"})
    responses = [FakeResponse(function_calls=[fc]), FakeResponse(text="Done.")]
    install(monkeypatch, responses)

    dashboard = _connected_dashboard()
    monkeypatch.setattr(headless_ui, "_dashboard_server", dashboard)

    reply, calls = asyncio.run(headless_ui.run_chat_turn("open google", []))

    assert reply == "Done."
    assert calls and calls[0]["name"] == "navigate_command_center"
    sent = list(dashboard._3d_ws_clients)[0].sent
    assert sent and sent[0]["external_url"] == "https://google.com"


def test_set_dashboard_server_is_what_create_app_calls(monkeypatch):
    # core/headless/app.py's create_app() must actually call this, or the
    # whole chain above is wired to nothing in the real running process.
    from core.headless import ui as headless_ui
    from core.headless.app import create_app

    captured = {}
    monkeypatch.setattr(headless_ui, "set_dashboard_server", lambda s: captured.setdefault("server", s))
    create_app(start_background_worker=False)
    assert "server" in captured
