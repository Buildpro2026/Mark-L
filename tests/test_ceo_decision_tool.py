import asyncio
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _new_live(name="jarvis_main_ceo"):
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
    return type("FC", (), {"id": "call-1", "name": "ceo_decision", "args": args})()


def _run(coro):
    return asyncio.run(coro)


def test_tool_declared_for_gemini():
    main, _ = _new_live()
    names = [t["name"] for t in main.TOOL_DECLARATIONS]
    assert "ceo_decision" in names


def test_propose_reports_decision_id_and_objective_progress(monkeypatch, tmp_path):
    main, live = _new_live()
    live.ui = _ui_stub()
    live._dashboard = None
    live._loop = None
    monkeypatch.setattr(main.biz_intel, "DB_PATH", tmp_path / "bi.db")
    import actions.strategic_objective as so
    monkeypatch.setattr(so, "CONFIG_FILE", tmp_path / "objective.json")

    resp = _run(live._execute_tool(_make_fc(
        action="propose", business="ddf", title="Test ad spend",
        analysis="Looks promising", recommendation="Try it",
    )))
    assert "proposed" in resp.response["result"].lower()
    assert "$" in resp.response["result"]


def test_authorize_without_id_asks_for_one(monkeypatch, tmp_path):
    main, live = _new_live()
    live.ui = _ui_stub()
    live._dashboard = None
    live._loop = None
    monkeypatch.setattr(main.biz_intel, "DB_PATH", tmp_path / "bi.db")

    resp = _run(live._execute_tool(_make_fc(action="authorize")))
    assert "which decision" in resp.response["result"].lower()


def test_full_propose_authorize_record_outcome_flow(monkeypatch, tmp_path):
    main, live = _new_live()
    live.ui = _ui_stub()
    live._dashboard = None
    live._loop = None
    monkeypatch.setattr(main.biz_intel, "DB_PATH", tmp_path / "bi.db")
    import actions.strategic_objective as so
    monkeypatch.setattr(so, "CONFIG_FILE", tmp_path / "objective.json")

    propose_resp = _run(live._execute_tool(_make_fc(
        action="propose", business="ddf", title="Ad test", analysis="x",
    )))
    decision_id = main.biz_intel.list_entries(category="decisions", business="ddf")[-1]["id"]

    auth_resp = _run(live._execute_tool(_make_fc(action="authorize", decision_id=decision_id)))
    assert "authorized" in auth_resp.response["result"].lower()

    outcome_resp = _run(live._execute_tool(_make_fc(
        action="record_outcome", decision_id=decision_id, result="3 leads", revenue=150,
        lesson="Worked well",
    )))
    assert "outcome recorded" in outcome_resp.response["result"].lower()


def test_unknown_decision_id_reports_error_not_crash(monkeypatch, tmp_path):
    main, live = _new_live()
    live.ui = _ui_stub()
    live._dashboard = None
    live._loop = None
    monkeypatch.setattr(main.biz_intel, "DB_PATH", tmp_path / "bi.db")

    resp = _run(live._execute_tool(_make_fc(action="authorize", decision_id=999999)))
    assert "no decision found" in resp.response["result"].lower()
