"""Google Tasks integration — read tasks; create/update/complete only when
explicitly approved. Uses actions/google_auth.py for OAuth — the same
shared Google credential/token as gmail_integration.py/calendar_integration.py,
not a second auth system.

create_task()/update_task()/complete_task() refuse to call the Tasks API
unless approved=True is passed by a caller acting on an explicit
instruction — the same gate pattern every other write in this codebase
uses. Everything here operates on the user's default task list
("@default") unless a different tasklist_id is given; most Google
accounts only ever have the one default list, and this codebase has no
concept of a UI to pick among several.
"""
from __future__ import annotations

from typing import Any

from actions import google_auth

DEFAULT_TASKLIST = "@default"


def _service():
    return google_auth.build_service("tasks", "v1")


def _normalize_task(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": task.get("id"),
        "title": task.get("title"),
        "notes": task.get("notes"),
        "status": task.get("status"),   # "needsAction" | "completed"
        "due": task.get("due"),
        "completed": task.get("completed"),
        "html_link": task.get("selfLink"),
    }


def list_tasks(
    max_results: int = 20, tasklist_id: str = DEFAULT_TASKLIST, show_completed: bool = False,
) -> dict[str, Any]:
    """Read-only: tasks on `tasklist_id`, incomplete-only by default (the
    common case — a completed-tasks dump isn't what 'what's on my list'
    means). Never fabricates results — an auth/API failure returns
    ok=False, not fake tasks."""
    try:
        service = _service()
        resp = service.tasks().list(
            tasklist=tasklist_id, maxResults=max_results,
            showCompleted=show_completed, showHidden=show_completed,
        ).execute()
        tasks = [_normalize_task(t) for t in resp.get("items", [])]
        return {"ok": True, "tasks": tasks}
    except RuntimeError as exc:
        return {"ok": False, "state": "NOT_AUTHORIZED", "detail": str(exc), "tasks": []}
    except Exception as exc:
        return {"ok": False, "state": "ERROR", "detail": str(exc), "tasks": []}


def get_task(task_id: str, tasklist_id: str = DEFAULT_TASKLIST) -> dict[str, Any]:
    """Read-only: one task's full detail by id."""
    try:
        service = _service()
        task = service.tasks().get(tasklist=tasklist_id, task=task_id).execute()
        return {"ok": True, "task": _normalize_task(task)}
    except RuntimeError as exc:
        return {"ok": False, "state": "NOT_AUTHORIZED", "detail": str(exc)}
    except Exception as exc:
        return {"ok": False, "state": "ERROR", "detail": str(exc)}


def create_task(
    title: str, notes: str = "", due_iso: str = "",
    tasklist_id: str = DEFAULT_TASKLIST, approved: bool = False,
) -> dict[str, Any]:
    """Refuses to create anything unless approved=True. due_iso, if given,
    must be an RFC3339 date/datetime (Google Tasks stores only the date
    part of `due` regardless of the time given — this passes the value
    through unchanged rather than silently reformatting it)."""
    if not approved:
        return {"ok": False, "state": "NOT_APPROVED", "detail": "Creating a task requires explicit approval."}
    body: dict[str, Any] = {"title": title}
    if notes:
        body["notes"] = notes
    if due_iso:
        body["due"] = due_iso
    try:
        service = _service()
        created = service.tasks().insert(tasklist=tasklist_id, body=body).execute()
        return {"ok": True, "task_id": created.get("id"), "html_link": created.get("selfLink")}
    except RuntimeError as exc:
        return {"ok": False, "state": "NOT_AUTHORIZED", "detail": str(exc)}
    except Exception as exc:
        return {"ok": False, "state": "ERROR", "detail": str(exc)}


def update_task(
    task_id: str, tasklist_id: str = DEFAULT_TASKLIST, approved: bool = False, **fields: Any,
) -> dict[str, Any]:
    """Refuses to update anything unless approved=True. Accepts any of
    title/notes/due_iso as keyword fields — only fields actually provided
    are patched, nothing else is touched."""
    if not approved:
        return {"ok": False, "state": "NOT_APPROVED", "detail": "Updating a task requires explicit approval."}
    patch: dict[str, Any] = {}
    if "title" in fields:
        patch["title"] = fields["title"]
    if "notes" in fields:
        patch["notes"] = fields["notes"]
    if "due_iso" in fields:
        patch["due"] = fields["due_iso"]
    try:
        service = _service()
        updated = service.tasks().patch(tasklist=tasklist_id, task=task_id, body=patch).execute()
        return {"ok": True, "task_id": updated.get("id")}
    except RuntimeError as exc:
        return {"ok": False, "state": "NOT_AUTHORIZED", "detail": str(exc)}
    except Exception as exc:
        return {"ok": False, "state": "ERROR", "detail": str(exc)}


def complete_task(task_id: str, tasklist_id: str = DEFAULT_TASKLIST, approved: bool = False) -> dict[str, Any]:
    """Marks a task done. Refuses unless approved=True — same gate as
    every other state-changing call here, even though completing a task
    is low-stakes, for one consistent contract across this module."""
    if not approved:
        return {"ok": False, "state": "NOT_APPROVED", "detail": "Completing a task requires explicit approval."}
    try:
        service = _service()
        updated = service.tasks().patch(
            tasklist=tasklist_id, task=task_id, body={"status": "completed"},
        ).execute()
        return {"ok": True, "task_id": updated.get("id")}
    except RuntimeError as exc:
        return {"ok": False, "state": "NOT_AUTHORIZED", "detail": str(exc)}
    except Exception as exc:
        return {"ok": False, "state": "ERROR", "detail": str(exc)}


def delete_task(task_id: str, tasklist_id: str = DEFAULT_TASKLIST, approved: bool = False) -> dict[str, Any]:
    """Permanently deletes a task. Refuses unless approved=True."""
    if not approved:
        return {"ok": False, "state": "NOT_APPROVED", "detail": "Deleting a task requires explicit approval."}
    try:
        service = _service()
        service.tasks().delete(tasklist=tasklist_id, task=task_id).execute()
        return {"ok": True, "task_id": task_id}
    except RuntimeError as exc:
        return {"ok": False, "state": "NOT_AUTHORIZED", "detail": str(exc)}
    except Exception as exc:
        return {"ok": False, "state": "ERROR", "detail": str(exc)}
