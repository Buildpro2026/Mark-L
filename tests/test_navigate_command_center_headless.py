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


def test_no_connected_command_center_window_reports_honest_failure():
    # Production bug fixed here: with no /3d tab connected, this used to
    # say "Opening BuildPro in the command center now" — a claim that
    # depended entirely on an unconfirmed, later client-side window.open()
    # call, which browsers routinely block silently when it fires outside
    # a direct user click (exactly what happened in production: JARVIS
    # said it opened a page that never opened). delivered == 0 is the only
    # fact available at this point, and it means the navigation was not
    # confirmed — describe() must report that as a failure, not a promise.
    dashboard = DashboardServer()
    result = _run(ToolExecutor(_ctx(dashboard)).execute(
        "navigate_command_center", {"action": "open", "target": "BuildPro"}))
    assert "couldn't open" in result.lower()
    assert "opening" not in result.lower()
    assert "opened" not in result.lower()
    # Never instructs the user to go do it themselves either.
    assert "open the command center" not in result.lower()


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


# ── /ui IS the Command Center: a real, structured destination reaches the
# browser chat page, not just a spoken description — the actual fix for
# "the user must actually see the navigation happen" ───────────────────────

def test_navigate_leaves_a_structured_destination_on_the_context():
    # ToolExecutor.execute() stashes the resolved destination on
    # ctx.last_navigation so the caller (core/headless/ui.py's provider
    # loops, via _record_tool_call) can fold it into this turn's tool_calls
    # entry for the browser to act on.
    dashboard = _connected_dashboard()
    ctx = _ctx(dashboard)
    _run(ToolExecutor(ctx).execute(
        "navigate_command_center", {"action": "open", "target": "YouTube"}))
    assert ctx.last_navigation is not None
    assert ctx.last_navigation["delivered"] == 1
    assert ctx.last_navigation["destination_type"] in ("nucleus", "external")
    assert ctx.last_navigation["external_url"] or ctx.last_navigation["destination_route"]


def test_navigate_reports_zero_delivered_when_no_window_is_connected():
    ctx = _ctx(DashboardServer())   # no /3d client added — nothing connected
    _run(ToolExecutor(ctx).execute(
        "navigate_command_center", {"action": "open", "target": "BuildPro"}))
    assert ctx.last_navigation is not None
    assert ctx.last_navigation["delivered"] == 0


def test_status_and_control_actions_leave_no_navigation_to_auto_open():
    dashboard = _connected_dashboard()
    ctx = _ctx(dashboard)
    _run(ToolExecutor(ctx).execute("navigate_command_center", {"action": "status"}))
    assert ctx.last_navigation is None


def test_a_dashboard_less_process_leaves_no_navigation_either():
    ctx = _ctx(None)
    _run(ToolExecutor(ctx).execute(
        "navigate_command_center", {"action": "open", "target": "Google"}))
    assert ctx.last_navigation is None


def test_record_tool_call_folds_navigation_into_the_tool_calls_entry(monkeypatch):
    from core.headless import ui as headless_ui
    from core.headless.tool_executor import ToolExecutor

    dashboard = _connected_dashboard()
    ctx = _ctx(dashboard)
    executor = ToolExecutor(ctx)
    result = _run(executor.execute(
        "navigate_command_center", {"action": "open", "target": "YouTube"}))

    tool_calls_made = []
    headless_ui._record_tool_call(
        tool_calls_made, executor, "navigate_command_center",
        {"action": "open", "target": "YouTube"}, result)

    assert len(tool_calls_made) == 1
    assert "navigation" in tool_calls_made[0]
    assert tool_calls_made[0]["navigation"]["delivered"] == 1
    # Consumed — a later, unrelated tool call in the same turn must never
    # inherit a stale navigation payload from an earlier one.
    assert executor.ctx.last_navigation is None


def test_record_tool_call_does_not_attach_navigation_to_other_tools():
    from core.headless import ui as headless_ui
    from core.headless.context import ToolContext
    from core.headless.tool_executor import ToolExecutor

    executor = ToolExecutor(ToolContext())
    tool_calls_made = []
    headless_ui._record_tool_call(tool_calls_made, executor, "weather_report", {}, "Sunny.")
    assert "navigation" not in tool_calls_made[0]


# ── production-bug regression: honest confirmation, not an assumed one ────

def test_web_research_surfaced_url_navigates_when_a_window_is_connected():
    # A URL JARVIS learned about from web_research (never a known site
    # name) must resolve and navigate the same as any other external
    # destination — the resolver doesn't care where the URL came from.
    dashboard = _connected_dashboard()
    result = _run(ToolExecutor(_ctx(dashboard)).execute(
        "navigate_command_center",
        {"action": "open", "target": "https://www.apple.com/iphone-16/"}))
    assert "apple.com" in result.lower()
    sent = list(dashboard._3d_ws_clients)[0].sent
    assert sent, "the connected window never received the research-sourced URL"
    assert sent[0]["external_url"] == "https://www.apple.com/iphone-16/"


def test_web_research_surfaced_url_reports_honest_failure_with_no_window():
    # Same URL, no connected Command Center — must be reported as a
    # failure, never as an "opening now" promise.
    dashboard = DashboardServer()   # no /3d client connected
    result = _run(ToolExecutor(_ctx(dashboard)).execute(
        "navigate_command_center",
        {"action": "open", "target": "https://www.apple.com/iphone-16/"}))
    assert "couldn't open" in result.lower()
    assert "opened" not in result.lower()
    assert "opening" not in result.lower()


def test_navigation_immediately_followed_by_another_request_stays_independent():
    # A failed navigation must not corrupt or bleed into whatever the very
    # next tool call reports — each call gets its own honest result from
    # its own delivered count, and ctx.last_navigation always reflects
    # only the most recent call.
    dashboard = DashboardServer()   # starts with no /3d client connected
    ctx = _ctx(dashboard)
    executor = ToolExecutor(ctx)

    first = _run(executor.execute(
        "navigate_command_center", {"action": "open", "target": "BuildPro"}))
    assert "couldn't open" in first.lower()
    assert ctx.last_navigation["delivered"] == 0

    # A window connects between the two calls (e.g. the /3d tab that
    # opened from the first attempt finished loading and registered).
    dashboard._3d_ws_clients.add(_FakeSocket())

    second = _run(executor.execute(
        "navigate_command_center", {"action": "open", "target": "Candidates"}))
    assert "candidates" in second.lower()
    assert "couldn't open" not in second.lower()
    assert ctx.last_navigation["delivered"] == 1
    # The first call's failure must not linger into the second's report.
    sent = list(dashboard._3d_ws_clients)[0].sent
    assert sent and sent[-1]["nucleus_id"] == "candidates"
