"""Cloud/runtime compatibility hooks for JARVIS.

Python imports sitecustomize automatically during normal interpreter startup.
Render runs JARVIS headlessly on Linux with no X11 DISPLAY. Several legacy
Windows desktop actions import pyautogui at module import time; pyautogui
initializes mouseinfo immediately and crashes when DISPLAY is absent.

On a headless runtime, install a lightweight pyautogui compatibility stub so
those desktop-only modules can still be imported. Actual desktop operations
remain unavailable and fail clearly when invoked. On Windows/macOS desktop
runs, the real pyautogui package is untouched.
"""

from __future__ import annotations

import os
import platform
import sys
import types


_HEADLESS = platform.system() == "Linux" and not os.environ.get("DISPLAY")


if _HEADLESS and "pyautogui" not in sys.modules:
    class _HeadlessPyAutoGUI(types.ModuleType):
        FAILSAFE = True
        PAUSE = 0.05

        def __getattr__(self, name: str):
            def _unavailable(*args, **kwargs):
                raise RuntimeError(
                    f"pyautogui operation '{name}' is unavailable in JARVIS headless mode. "
                    "This capability requires the local desktop runtime."
                )
            return _unavailable

    _stub = _HeadlessPyAutoGUI("pyautogui")
    _stub.__file__ = "<headless-pyautogui-stub>"
    sys.modules["pyautogui"] = _stub
