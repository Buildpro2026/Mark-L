"""core/headless/tool_executor.py's "google_tasks" dispatch. Tested
directly through ToolExecutor (the same shared dispatcher both main.py/
desktop and the headless FastAPI service call), NOT through main.py —
main.py can't be imported in this sandbox (PyQt6/libEGL), but ToolExecutor
itself has no such dependency, matching test_headless_core.py's own
pattern for testing this exact module headless-only.

Never makes a live Google Tasks call: every google_tasks_integration.py
function used here is monkeypatched.
"""
import asyncio

from actions import google_tasks_integration
from core.headless.context import ToolContext
from core.headless.tool_executor import ToolExecutor


def _executor() -> ToolExecutor:
    return ToolExecutor(ToolContext())


def _run(coro):
    return asyncio.run(coro)


# ── list / get ────────────────────────────────────────────

def test_list_reports_no_open_tasks_when_empty(monkeypatch):
    monkeypatch.setattr(google_tasks_integration, "list_tasks", lambda max_results: {"ok": True, "tasks": []})
    result = _run(_executor().execute("google_tasks", {"action": "list"}))
    assert "no open tasks" in result.lower()


def test_list_summarizes_title_and_due(monkeypatch):
    monkeypatch.setattr(google_tasks_integration, "list_tasks", lambda max_results: {
        "ok": True, "tasks": [{"id": "t1", "title": "Follow up with candidate", "due": "2026-08-20"}],
    })
    result = _run(_executor().execute("google_tasks", {"action": "list"}))
    assert "Follow up with candidate" in result
    assert "2026-08-20" in result


def test_list_surfaces_a_failure_honestly(monkeypatch):
    monkeypatch.setattr(google_tasks_integration, "list_tasks", lambda max_results: {
        "ok": False, "state": "NOT_AUTHORIZED", "detail": "not authorized", "tasks": [],
    })
    result = _run(_executor().execute("google_tasks", {"action": "list"}))
    assert "couldn't read" in result.lower()
    assert "not authorized" in result.lower()


def test_get_without_task_id_is_refused(monkeypatch):
    calls = []
    monkeypatch.setattr(google_tasks_integration, "get_task", lambda *a, **k: calls.append(1))
    result = _run(_executor().execute("google_tasks", {"action": "get"}))
    assert calls == []
    assert "task id" in result.lower()


def test_get_reports_title_notes_and_status(monkeypatch):
    monkeypatch.setattr(google_tasks_integration, "get_task", lambda task_id: {
        "ok": True, "task": {"id": task_id, "title": "Follow up", "notes": "call about offer", "status": "needsAction"},
    })
    result = _run(_executor().execute("google_tasks", {"action": "get", "task_id": "t1"}))
    assert "Follow up" in result
    assert "call about offer" in result


# ── create ───────────────────────────────────────────

def test_create_without_title_is_refused_and_never_calls_the_real_api(monkeypatch):
    calls = []
    monkeypatch.setattr(google_tasks_integration, "create_task", lambda *a, **k: calls.append(1))
    result = _run(_executor().execute("google_tasks", {"action": "create"}))
    assert calls == []
    assert "title" in result.lower()


def test_create_with_full_details_passes_approved_true(monkeypatch):
    captured = {}

    def fake_create_task(title, notes="", due_iso="", approved=False):
        captured["title"] = title
        captured["notes"] = notes
        captured["due_iso"] = due_iso
        captured["approved"] = approved
        return {"ok": True, "task_id": "t1"}

    monkeypatch.setattr(google_tasks_integration, "create_task", fake_create_task)
    result = _run(_executor().execute("google_tasks", {
        "action": "create", "title": "Follow up with candidate", "notes": "call about offer", "due_iso": "2026-08-20",
    }))
    assert captured["approved"] is True
    assert captured["title"] == "Follow up with candidate"
    assert captured["notes"] == "call about offer"
    assert "Follow up with candidate" in result


def test_create_failure_is_reported_not_fabricated(monkeypatch):
    monkeypatch.setattr(google_tasks_integration, "create_task", lambda title, notes="", due_iso="", approved=False: {
        "ok": False, "state": "ERROR", "detail": "insufficient scope",
    })
    result = _run(_executor().execute("google_tasks", {"action": "create", "title": "x"}))
    assert "couldn't create" in result.lower()
    assert "insufficient scope" in result


# ── update ───────────────────────────────────────────

def test_update_without_task_id_is_refused(monkeypatch):
    calls = []
    monkeypatch.setattr(google_tasks_integration, "update_task", lambda *a, **k: calls.append(1))
    result = _run(_executor().execute("google_tasks", {"action": "update", "title": "New title"}))
    assert calls == []
    assert "task id" in result.lower()


def test_update_without_any_fields_is_refused(monkeypatch):
    calls = []
    monkeypatch.setattr(google_tasks_integration, "update_task", lambda *a, **k: calls.append(1))
    result = _run(_executor().execute("google_tasks", {"action": "update", "task_id": "t1"}))
    assert calls == []
    assert "need at least one thing" in result.lower()


def test_update_passes_only_provided_fields_and_approved_true(monkeypatch):
    captured = {}

    def fake_update_task(task_id, approved=False, **fields):
        captured["task_id"] = task_id
        captured["approved"] = approved
        captured["fields"] = fields
        return {"ok": True, "task_id": task_id}

    monkeypatch.setattr(google_tasks_integration, "update_task", fake_update_task)
    result = _run(_executor().execute("google_tasks", {"action": "update", "task_id": "t1", "notes": "rescheduled"}))
    assert captured["approved"] is True
    assert captured["fields"] == {"notes": "rescheduled"}
    assert "updated" in result.lower()


# ── complete / delete ────────────────────────────────

def test_complete_without_task_id_is_refused(monkeypatch):
    calls = []
    monkeypatch.setattr(google_tasks_integration, "complete_task", lambda *a, **k: calls.append(1))
    result = _run(_executor().execute("google_tasks", {"action": "complete"}))
    assert calls == []
    assert "task id" in result.lower()


def test_complete_passes_approved_true(monkeypatch):
    captured = {}

    def fake_complete_task(task_id, approved=False):
        captured["task_id"] = task_id
        captured["approved"] = approved
        return {"ok": True, "task_id": task_id}

    monkeypatch.setattr(google_tasks_integration, "complete_task", fake_complete_task)
    result = _run(_executor().execute("google_tasks", {"action": "complete", "task_id": "t1"}))
    assert captured["approved"] is True
    assert "complete" in result.lower()


def test_delete_without_task_id_is_refused(monkeypatch):
    calls = []
    monkeypatch.setattr(google_tasks_integration, "delete_task", lambda *a, **k: calls.append(1))
    result = _run(_executor().execute("google_tasks", {"action": "delete"}))
    assert calls == []
    assert "task id" in result.lower()


def test_delete_passes_approved_true_and_reports_failure_honestly(monkeypatch):
    monkeypatch.setattr(google_tasks_integration, "delete_task", lambda task_id, approved=False: {
        "ok": False, "state": "ERROR", "detail": "not found",
    })
    result = _run(_executor().execute("google_tasks", {"action": "delete", "task_id": "t1"}))
    assert "couldn't delete" in result.lower()
    assert "not found" in result


def test_unknown_google_tasks_action_reports_clearly(monkeypatch):
    result = _run(_executor().execute("google_tasks", {"action": "not_a_real_action"}))
    assert "unknown google_tasks action" in result.lower()
