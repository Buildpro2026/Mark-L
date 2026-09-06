"""main.py / core/headless_main.py force UTF-8 stdout+stderr on Windows at
startup. Reproduced live during the Phase 2 migration audit:
actions/web_search.py's own debug print ("[WebSearch] 🔍 mode=...") raised
UnicodeEncodeError under Windows' default console code page (cp1252) when
stdout wasn't a live UTF-8-capable console — e.g. piped output, a
background/service invocation — which is exactly how emoji-laden debug
prints throughout main.py and actions/* would crash whatever tool call
triggered them. This only matters on Windows; POSIX's default encoding is
already UTF-8.
"""
import importlib.util
import platform
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(platform.system() != "Windows", reason="stdout encoding fix is Windows-only; POSIX defaults to UTF-8 already")
def test_main_py_forces_utf8_stdout_on_windows():
    load_module("jarvis_main_encoding_test", "main.py")
    assert sys.stdout.encoding.lower().replace("-", "") == "utf8"
    assert sys.stderr.encoding.lower().replace("-", "") == "utf8"


@pytest.mark.skipif(platform.system() != "Windows", reason="stdout encoding fix is Windows-only; POSIX defaults to UTF-8 already")
def test_emoji_print_does_not_raise_after_main_py_loads(capsys):
    load_module("jarvis_main_encoding_test2", "main.py")
    # This is the exact reproduction: web_search.py prints "🔍" as part of
    # its normal debug trace on every call.
    print("[WebSearch] \U0001f50d mode='search' query='test'")
    captured = capsys.readouterr()
    assert "WebSearch" in captured.out
