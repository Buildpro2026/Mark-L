import asyncio
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _new_live(name="jarvis_main_biz"):
    main = load_module(name, "main.py")
    live = object.__new__(main.JarvisLive)
    return main, live


def _ui_stub():
    return type("UIStub", (), {
        "muted": False,
        "set_state": lambda self, s: None,
        "write_log": lambda self, m: None,
    })()


def _run(coro):
    return asyncio.run(coro)


def _make_fc(tool_name, **args):
    return type("FC", (), {"id": "call-1", "name": tool_name, "args": args})()


def _isolate(monkeypatch, main, tmp_path):
    monkeypatch.setattr(main.biz_intel, "DB_PATH", tmp_path / "bi.db")
    monkeypatch.setattr(main.opp_engine, "DB_PATH", tmp_path / "opp.db")


def test_both_tools_declared_for_gemini():
    main, _ = _new_live()
    names = [t["name"] for t in main.TOOL_DECLARATIONS]
    assert "business_intelligence" in names
    assert "opportunity_engine" in names


# ── business_intelligence ────────────────────────────────────────────────

def test_bi_log_and_list(monkeypatch, tmp_path):
    main, live = _new_live()
    live.ui = _ui_stub()
    live._dashboard = None
    live._loop = None
    _isolate(monkeypatch, main, tmp_path)

    log_resp = _run(live._execute_tool(_make_fc(
        "business_intelligence", action="log", category="research", business="buildpro",
        title="Competitor pricing scan", content="Found 3 competitors under $200/mo",
    )))
    assert "Logged" in log_resp.response["result"]

    list_resp = _run(live._execute_tool(_make_fc(
        "business_intelligence", action="list", category="research", business="buildpro",
    )))
    assert "Competitor pricing scan" in list_resp.response["result"]


def test_bi_record_outcome_and_lessons(monkeypatch, tmp_path):
    main, live = _new_live()
    live.ui = _ui_stub()
    live._dashboard = None
    live._loop = None
    _isolate(monkeypatch, main, tmp_path)

    outcome_resp = _run(live._execute_tool(_make_fc(
        "business_intelligence", action="record_outcome", business="ddf",
        plan="Post weekend deals", result="4 sales", revenue=180, cost=15,
        lesson="Weekend timing works better", recommendation="Shift schedule",
    )))
    assert "recorded" in outcome_resp.response["result"].lower()

    lessons_resp = _run(live._execute_tool(_make_fc(
        "business_intelligence", action="lessons", business="ddf",
    )))
    assert "Weekend timing" in lessons_resp.response["result"]


def test_bi_summary_reports_totals(monkeypatch, tmp_path):
    main, live = _new_live()
    live.ui = _ui_stub()
    live._dashboard = None
    live._loop = None
    _isolate(monkeypatch, main, tmp_path)

    _run(live._execute_tool(_make_fc(
        "business_intelligence", action="record_outcome", business="ddf",
        plan="A", result="ok", revenue=100,
    )))
    resp = _run(live._execute_tool(_make_fc("business_intelligence", action="summary", business="ddf")))
    assert "$100" in resp.response["result"]


def test_bi_unknown_category_reports_error_not_crash(monkeypatch, tmp_path):
    main, live = _new_live()
    live.ui = _ui_stub()
    live._dashboard = None
    live._loop = None
    _isolate(monkeypatch, main, tmp_path)

    resp = _run(live._execute_tool(_make_fc(
        "business_intelligence", action="log", category="not_a_real_category", title="x",
    )))
    assert "Unknown BI category" in resp.response["result"]


# ── opportunity_engine ───────────────────────────────────────────────────

def test_opportunity_add_and_rank(monkeypatch, tmp_path):
    main, live = _new_live()
    live.ui = _ui_stub()
    live._dashboard = None
    live._loop = None
    _isolate(monkeypatch, main, tmp_path)

    add_resp = _run(live._execute_tool(_make_fc(
        "opportunity_engine", action="add", business="ddf", opp_type="quick_cash",
        title="Holiday deal blitz", revenue_potential=4, probability=4,
    )))
    assert "logged" in add_resp.response["result"].lower()

    rank_resp = _run(live._execute_tool(_make_fc(
        "opportunity_engine", action="rank", business="ddf", opp_type="quick_cash",
    )))
    assert "Holiday deal blitz" in rank_resp.response["result"]


def test_opportunity_update_status_requires_id(monkeypatch, tmp_path):
    main, live = _new_live()
    live.ui = _ui_stub()
    live._dashboard = None
    live._loop = None
    _isolate(monkeypatch, main, tmp_path)

    resp = _run(live._execute_tool(_make_fc("opportunity_engine", action="update_status", status="active")))
    assert "which opportunity" in resp.response["result"].lower()


def test_opportunity_update_status_success(monkeypatch, tmp_path):
    main, live = _new_live()
    live.ui = _ui_stub()
    live._dashboard = None
    live._loop = None
    _isolate(monkeypatch, main, tmp_path)

    add_resp = _run(live._execute_tool(_make_fc(
        "opportunity_engine", action="add", business="buildpro", opp_type="long_term", title="Retainer program",
    )))
    opp_id = main.opp_engine.list_opportunities(business="buildpro")[0]["id"]

    status_resp = _run(live._execute_tool(_make_fc(
        "opportunity_engine", action="update_status", opportunity_id=opp_id, status="active",
    )))
    assert "active" in status_resp.response["result"].lower()


def test_opportunity_invalid_type_reports_error_not_crash(monkeypatch, tmp_path):
    main, live = _new_live()
    live.ui = _ui_stub()
    live._dashboard = None
    live._loop = None
    _isolate(monkeypatch, main, tmp_path)

    resp = _run(live._execute_tool(_make_fc(
        "opportunity_engine", action="add", opp_type="medium_term", title="Bad type",
    )))
    assert "opp_type must be one of" in resp.response["result"]
