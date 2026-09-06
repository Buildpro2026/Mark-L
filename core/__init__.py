# core package

# Render runs JARVIS headlessly on Linux with no X11 DISPLAY. Some legacy
# desktop actions import pyautogui at module import time; pyautogui itself
# initializes mouseinfo and crashes before the headless service can start.
# Install a minimal stub before any headless submodule imports those actions.
import os
import platform
import sys
import types


if platform.system() == "Linux" and not os.environ.get("DISPLAY") and "pyautogui" not in sys.modules:
    class _HeadlessPyAutoGUI(types.ModuleType):
        FAILSAFE = True
        PAUSE = 0.05

        def __getattr__(self, name):
            def _unavailable(*args, **kwargs):
                raise RuntimeError(
                    f"Desktop GUI operation '{name}' is unavailable in headless cloud mode."
                )
            return _unavailable

    _stub = _HeadlessPyAutoGUI("pyautogui")
    _stub.__file__ = "<headless-pyautogui-stub>"
    sys.modules["pyautogui"] = _stub
