"""Shared execution context for tool calls, real (desktop) or headless.

main.py's JarvisLive builds one of these wrapping its real ui/speak/
proactive engine; the headless runtime builds one wrapping no-op/logging
stand-ins. Either way ToolExecutor (tool_executor.py) sees the same
interface and runs the same code.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger("jarvis.headless")


class NullPlayer:
    """No-op stand-in for ui.py's JarvisUI, for contexts with no desktop
    HUD (headless service, tests). Every action module that accepts a
    `player=` argument only ever calls write_log/show_content/
    start_camera_stream/stop_camera_stream/show_camera_frame on it and
    reads `.muted`/`.current_file` — this covers that whole surface.
    write_log goes to the logger rather than /dev/null, so headless tool
    activity is still observable (never silence errors/output, just
    don't require a GUI to see them)."""

    muted = False
    current_file: Optional[str] = None
    assistant_name = "JARVIS"

    def set_state(self, state: str) -> None:
        pass

    def write_log(self, text: str) -> None:
        logger.info(text)

    def show_content(self, title: str, text: str) -> None:
        logger.info("[content] %s: %s", title, text[:200])

    def show_camera_frame(self, img_bytes: bytes) -> None:
        pass

    def start_camera_stream(self) -> None:
        pass

    def stop_camera_stream(self) -> None:
        pass

    def start_speaking(self) -> None:
        pass

    def stop_speaking(self) -> None:
        pass


@dataclass
class ToolContext:
    """Everything ToolExecutor needs beyond (name, args). Optional fields
    default to headless-safe no-ops so ToolExecutor works the same whether
    it's driven by main.py's live JarvisLive instance or by the headless
    API/background worker."""

    ui: Any = field(default_factory=NullPlayer)
    speak: Callable[[str], None] = field(default=lambda text: None)
    proactive: Any = None   # actions.proactive.ProactiveEngine instance; built lazily if None
    # dashboard.server.DashboardServer instance, when one is actually
    # running in this process (core/headless/app.py mounts one at startup
    # whenever it can import cleanly — it has no PyQt dependency, so this
    # is normally set). None on a genuinely dashboard-less process (some
    # tests, or a deployment that failed to mount it) — navigate_command_
    # center reports that honestly rather than pretending it can navigate
    # somewhere with nothing listening.
    dashboard_server: Any = None
    # Set by ToolExecutor.execute()'s navigate_command_center branch to the
    # resolved destination (actions/workspace_navigation.py's shape) plus
    # how many /3d clients it was actually delivered to — None for every
    # other tool. The caller (core/headless/ui.py's provider loops) reads
    # this once right after the call and folds it into that tool call's own
    # entry in tool_calls_made, so the browser chat page can act on a REAL
    # destination instead of only the spoken description. One request/turn
    # per ToolExecutor instance (see tool_executor.py's class docstring),
    # so there is nothing to reset between unrelated turns.
    last_navigation: Optional[dict] = None

    def __post_init__(self) -> None:
        if self.proactive is None:
            from actions.proactive import ProactiveEngine
            self.proactive = ProactiveEngine()
