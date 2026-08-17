"""_open_3d_command_center must never open a URL whose scheme/port it just
guessed — DashboardServer.get_url() is the only source of truth (it flips
to https:// once config/certs/jarvis.{crt,key} exist, and a hardcoded
http:// guess against an HTTPS-only port fails silently in the browser).

Runs the check in a subprocess: constructing ui.MainWindow (full QMainWindow,
not the lightweight overlays other UI tests build) crashes the pytest process
natively on this Windows/PyQt6 combo even with QT_QPA_PLATFORM=offscreen —
reproducible with no other tests running, unrelated to this fix. The same
construction and calls run fine as a plain `python script.py` process, so the
subprocess isolates pytest from whatever pytest-only state triggers it.
"""
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_SCRIPT = textwrap.dedent(r"""
    import os, sys, json
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.path.insert(0, {root!r})
    from PyQt6.QtWidgets import QApplication
    import ui as ui_module

    class _Fake:
        calls = []
        @staticmethod
        def openUrl(qurl):
            _Fake.calls.append(qurl.toString())

    app = QApplication.instance() or QApplication([])
    win = ui_module.MainWindow({icon!r})
    win.on_remote_clicked = {callback_src}
    ui_module.QDesktopServices = _Fake
    win._open_3d_command_center()
    print(json.dumps(_Fake.calls))
""")


def _run(callback_src: str) -> list[str]:
    script = _SCRIPT.format(
        root=str(ROOT), icon=str(ROOT / "config" / "jarvis.ico"), callback_src=callback_src
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    import json
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_opens_https_url_from_on_remote_clicked_not_a_hardcoded_http_guess():
    opened = _run("lambda: ('https://192.168.1.98:8000', 'KEY123', 'auto', 'manual')")
    assert opened == ["https://192.168.1.98:8000/3d"]


def test_opens_plain_http_url_when_dashboard_reports_plain_http():
    opened = _run("lambda: ('http://192.168.1.98:8002', 'KEY123', 'auto', 'manual')")
    assert opened == ["http://192.168.1.98:8002/3d"]


def test_does_not_open_a_url_when_dashboard_unavailable():
    opened = _run("lambda: None")
    assert opened == []
