"""main.py's "github" voice/chat tool — read-only wiring into
actions/github_integration.py, through the shared ToolExecutor
(core/headless/tool_executor.py). Never makes a live GitHub call: every
github_integration.py function is monkeypatched.
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


def _new_live(name="jarvis_main_github"):
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
    return type("FC", (), {"id": "call-1", "name": "github", "args": args})()


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
    assert "github" in names


# ── status ────────────────────────────────────────────────────────────

def test_status_reports_connected(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.github_integration, "is_configured", lambda: True)
    monkeypatch.setattr(main.github_integration, "verify_github", lambda: {
        "configured": True, "verified": True, "status": "VERIFIED",
        "repo": {"full_name": "buildpro2026/mark-l"},
    })

    response = _run(live._execute_tool(_make_fc(action="status")))
    assert "connected" in response.response["result"].lower()
    assert "buildpro2026/mark-l" in response.response["result"]


def test_status_reports_not_configured(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.github_integration, "is_configured", lambda: False)

    response = _run(live._execute_tool(_make_fc(action="status")))
    assert "isn't configured" in response.response["result"].lower()
    assert "GITHUB_TOKEN" in response.response["result"]


# ── repo / commits / branches ────────────────────────────────────────

def test_repo_summarizes_result(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.github_integration, "get_repo", lambda: {
        "ok": True, "state": "OK",
        "data": {"full_name": "x/y", "default_branch": "main", "open_issues_count": 2, "pushed_at": "t"},
    })
    response = _run(live._execute_tool(_make_fc(action="repo")))
    assert "x/y" in response.response["result"]
    assert "main" in response.response["result"]


def test_commits_reports_none_found_when_empty(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.github_integration, "list_commits", lambda **k: {"ok": True, "state": "OK", "results": []})
    response = _run(live._execute_tool(_make_fc(action="commits")))
    assert "no commits" in response.response["result"].lower()


def test_commits_surfaces_a_failure_honestly_not_as_empty(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.github_integration, "list_commits",
                         lambda **k: {"ok": False, "state": "ERROR", "detail": "boom", "results": []})
    response = _run(live._execute_tool(_make_fc(action="commits")))
    assert "boom" in response.response["result"]


def test_branches_lists_names(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.github_integration, "list_branches",
                         lambda **k: {"ok": True, "state": "OK", "results": ["main", "dev"]})
    response = _run(live._execute_tool(_make_fc(action="branches")))
    assert "main" in response.response["result"] and "dev" in response.response["result"]


# ── issues / pull_requests ────────────────────────────────────────────

def test_issues_reports_none_when_empty(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.github_integration, "list_issues", lambda **k: {"ok": True, "state": "OK", "results": []})
    response = _run(live._execute_tool(_make_fc(action="issues")))
    assert "no open issues" in response.response["result"].lower()


def test_pull_requests_summarizes_results(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.github_integration, "list_pull_requests",
                         lambda **k: {"ok": True, "state": "OK", "results": [{"number": 5, "title": "Add feature"}]})
    response = _run(live._execute_tool(_make_fc(action="pull_requests")))
    assert "#5" in response.response["result"] and "Add feature" in response.response["result"]


def test_unknown_github_action_reports_clearly():
    main, _ = _new_live()
    live = _live(main)
    response = _run(live._execute_tool(_make_fc(action="delete_everything")))
    assert "unknown github action" in response.response["result"].lower()
