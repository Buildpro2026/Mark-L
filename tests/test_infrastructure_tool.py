"""main.py's "infrastructure" voice/chat tool — read-only wiring into
actions/infrastructure_status.py, through the shared ToolExecutor.
Never makes a live Render call: infrastructure_status.py is monkeypatched.
"""
import asyncio
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _new_live(name="jarvis_main_infra"):
    main = load_module(name, "main.py")
    live = object.__new__(main.JarvisLive)
    return main, live


def _ui_stub():
    return type("UIStub", (), {
        "muted": False,
        "set_state": lambda self, s: None,
        "write_log": lambda self, m: None,
    })()


def _make_fc(**args):
    return type("FC", (), {"id": "call-1", "name": "infrastructure", "args": args})()


def _run(coro):
    return asyncio.run(coro)


def _live(main):
    live = object.__new__(main.JarvisLive)
    live.ui = _ui_stub()
    live._dashboard = None
    live._loop = None
    return live


def test_tool_declared_for_gemini():
    main, _ = _new_live()
    names = [t["name"] for t in main.TOOL_DECLARATIONS]
    assert "infrastructure" in names


def test_status_reports_render_live_and_oracle_planned(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.infrastructure_status, "get_infrastructure_overview", lambda: {
        "render": {"configured": True, "state": "OK",
                   "service": {"name": "jarvis-headless-core", "status": "active"},
                   "latest_deploy": {"status": "live"}},
        "oracle": {"configured": False, "state": "PLANNED"},
    })
    response = _run(live._execute_tool(_make_fc(action="status")))
    result = response.response["result"].lower()
    assert "jarvis-headless-core" in result and "active" in result
    assert "oracle" in result and ("planned" in result or "not connected" in result)


def test_status_reports_render_not_configured_honestly(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.infrastructure_status, "get_infrastructure_overview", lambda: {
        "render": {"configured": False, "state": "NOT_CONFIGURED",
                    "detail": "Render isn't configured — missing: RENDER_API_KEY, RENDER_SERVICE_ID."},
        "oracle": {"configured": False, "state": "PLANNED"},
    })
    response = _run(live._execute_tool(_make_fc(action="status")))
    result = response.response["result"]
    assert "not configured" in result.lower()
    assert "RENDER_API_KEY" in result


def test_unknown_infrastructure_action_reports_clearly():
    main, _ = _new_live()
    live = _live(main)
    response = _run(live._execute_tool(_make_fc(action="delete_everything")))
    assert "unknown infrastructure action" in response.response["result"].lower()
