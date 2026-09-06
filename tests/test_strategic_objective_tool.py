import asyncio
import importlib.util
from pathlib import Path

from actions import strategic_objective as so

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _new_live(name="jarvis_main_strategic"):
    main = load_module(name, "main.py")
    live = object.__new__(main.JarvisLive)
    return main, live


def _ui_stub():
    return type("UIStub", (), {
        "muted": False,
        "set_state": lambda self, s: None,
        "write_log": lambda self, m: None,
    })()


def _make_fc(name, **args):
    return type("FC", (), {"id": "call-1", "name": name, "args": args})()


def _run(coro):
    return asyncio.run(coro)


def test_tool_declared_for_gemini():
    main, _ = _new_live()
    names = [t["name"] for t in main.TOOL_DECLARATIONS]
    assert "strategic_objective" in names


def test_execute_tool_status_reports_progress_toward_one_million(monkeypatch, tmp_path):
    main, live = _new_live()
    live.ui = _ui_stub()
    live._dashboard = None
    live._loop = None

    # Tool dispatch now runs through core/headless/tool_executor.py, which
    # calls strategic_objective.get_objective_status() on the real shared
    # module object — so isolating this test from real on-disk state means
    # redirecting that module's own CONFIG_FILE, not swapping a name in
    # main.py's namespace (that name isn't on the call path anymore).
    monkeypatch.setattr(so, "CONFIG_FILE", tmp_path / "strategic_objective.json")

    fc = _make_fc("strategic_objective", action="status")
    response = _run(live._execute_tool(fc))

    assert "$1,000,000" in response.response["result"]


def test_execute_tool_log_revenue_requires_a_positive_amount():
    main, live = _new_live()
    live.ui = _ui_stub()
    live._dashboard = None
    live._loop = None

    fc = _make_fc("strategic_objective", action="log_revenue")   # no amount
    response = _run(live._execute_tool(fc))

    assert "amount" in response.response["result"].lower()


def test_execute_tool_log_revenue_updates_persistent_state(monkeypatch, tmp_path):
    main, live = _new_live()
    live.ui = _ui_stub()
    live._dashboard = None
    live._loop = None

    monkeypatch.setattr(so, "CONFIG_FILE", tmp_path / "strategic_objective.json")

    fc = _make_fc("strategic_objective", action="log_revenue", amount=10000)
    response = _run(live._execute_tool(fc))

    assert "$10,000" in response.response["result"]
    assert so.get_objective_status()["cumulative_revenue_usd"] == 10000
