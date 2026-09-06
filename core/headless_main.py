"""Headless JARVIS process entry point — no PyQt6, no sounddevice, no
Gemini Live session required to start. Runs the FastAPI app (health check,
tool-execution API, Agent Orchestrator API) and the background worker
(agent scheduler, topic monitor, proactive observer) as one process.

Usage:
    python -m core.headless_main

Configuration is entirely via environment variables — see
core/headless/config.py and .env.example at the repo root. At minimum,
set JARVIS_API_TOKEN before exposing this beyond localhost; without it
every authenticated route returns 503, not open access.
"""
from __future__ import annotations

import logging
import platform
import sys
import types

import uvicorn

# Render/Linux is headless. Some legacy desktop actions import pyautogui at
# module import time even though those actions are not usable in the cloud.
# Provide a tiny compatibility module so one legacy import cannot crash the
# entire headless service. Actual desktop calls still fail explicitly rather
# than pretending a cloud container has a GUI.
if platform.system() == "Linux" and not __import__("os").environ.get("DISPLAY"):
    if "pyautogui" not in sys.modules:
        _headless_pyautogui = types.ModuleType("pyautogui")

        def _desktop_unavailable(*_args, **_kwargs):
            raise RuntimeError("Desktop automation is unavailable in the headless cloud runtime.")

        for _name in (
            "click", "doubleClick", "moveTo", "dragTo", "scroll",
            "press", "keyDown", "keyUp", "write", "hotkey",
            "screenshot", "position", "size", "locateOnScreen",
        ):
            setattr(_headless_pyautogui, _name, _desktop_unavailable)
        sys.modules["pyautogui"] = _headless_pyautogui

from actions.google_auth import ensure_credentials_from_env
from core.headless import config
from core.headless import app as _headless_app
from core.startup import print_startup_banner


def main() -> None:
    # See main.py's matching fix for why: the same emoji-laden debug prints
    # this codebase uses throughout actions/* raise UnicodeEncodeError under
    # Windows' default console code page. Render's Linux runtime is already
    # UTF-8, so this only matters when running headless_main.py locally on
    # Windows (e.g. for testing before deploy) — harmless no-op otherwise.
    if platform.system() == "Windows":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    print_startup_banner()
    if not config.API_TOKEN:
        logging.getLogger("jarvis.headless").warning(
            "JARVIS_API_TOKEN is not set — every authenticated route will return 503 "
            "until it's configured. The /health endpoint still works."
        )
    ensure_credentials_from_env()   # no-op unless GOOGLE_TOKEN_JSON/GOOGLE_CLIENT_SECRET_JSON are set

    app = _headless_app.create_app()
    uvicorn.run(app, host=config.HEADLESS_HOST, port=config.HEADLESS_PORT)


if __name__ == "__main__":
    main()
