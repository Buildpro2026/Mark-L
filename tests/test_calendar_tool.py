"""main.py's "calendar" voice tool — read/gated-create/gated-update wiring
into actions/calendar_integration.py. Never makes a live Calendar/OAuth
call: every calendar_integration.py / google_auth.py function is
monkeypatched.

Like Gmail (see test_gmail_tool.py), calendar_integration.py was fully
built, tested, and OAuth-authorized, but wired into nothing. 'create'
requires a title, start, AND end to already be present in the same tool
call (the code-level gate, independent of the LLM's own judgment) before
approved=True is ever passed, mirroring the same pattern used for
gmail's 'send' and the 'communications' tool's send_sms/call.
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


def _new_live(name="jarvis_main_calendar"):
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
    return type("FC", (), {"id": "call-1", "name": "calendar", "args": args})()


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
    assert "calendar" in names


# ── status ────────────────────────────────────────────────────────────

def test_status_reports_authorized(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.google_auth, "get_credential_status",
                         lambda: {"credential_file": "present", "token_cached": True, "authorized": True})

    response = _run(live._execute_tool(_make_fc(action="status")))
    assert "connected and authorized" in response.response["result"].lower()


def test_status_reports_missing_client_secret(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.google_auth, "get_credential_status",
                         lambda: {"credential_file": "missing", "token_cached": False, "authorized": False})

    response = _run(live._execute_tool(_make_fc(action="status")))
    assert "isn't set up" in response.response["result"].lower()


# ── list (read) ───────────────────────────────────────────────────────

def test_list_reports_no_upcoming_events_when_empty(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.calendar_integration, "list_upcoming_events",
                         lambda max_results: {"ok": True, "events": []})

    response = _run(live._execute_tool(_make_fc(action="list")))
    assert "no upcoming events" in response.response["result"].lower()


def test_list_summarizes_title_and_start_time(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.calendar_integration, "list_upcoming_events", lambda max_results: {
        "ok": True,
        "events": [{"summary": "Board meeting", "start": "2026-08-15T09:00:00-05:00"}],
    })

    response = _run(live._execute_tool(_make_fc(action="list")))
    result = response.response["result"]
    assert "Board meeting" in result
    assert "2026-08-15T09:00:00-05:00" in result


def test_list_surfaces_a_failure_honestly(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.calendar_integration, "list_upcoming_events",
                         lambda max_results: {"ok": False, "state": "NOT_AUTHORIZED", "detail": "no token", "events": []})

    response = _run(live._execute_tool(_make_fc(action="list")))
    result = response.response["result"].lower()
    assert "couldn't read the calendar" in result
    assert "not_authorized" in result


# ── create (gated) — the critical safety path ───────────────────────

def test_create_without_title_or_times_is_refused_and_never_calls_the_real_api(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    called = {"n": 0}
    monkeypatch.setattr(main.calendar_integration, "create_event",
                         lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    response = _run(live._execute_tool(_make_fc(action="create")))
    assert called["n"] == 0
    assert "title" in response.response["result"].lower() or "time" in response.response["result"].lower()


def test_create_missing_end_time_only_is_also_refused(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    called = {"n": 0}
    monkeypatch.setattr(main.calendar_integration, "create_event",
                         lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    response = _run(live._execute_tool(_make_fc(
        action="create", summary="Standup", start_iso="2026-08-15T09:00:00-05:00",
    )))
    assert called["n"] == 0


def test_create_with_full_details_passes_approved_true(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    captured = {}

    def _fake_create(summary, start_iso, end_iso, description="", location="", approved=False, ignore_conflicts=False):
        captured.update(summary=summary, start_iso=start_iso, end_iso=end_iso, approved=approved, ignore_conflicts=ignore_conflicts)
        return {"ok": True, "event_id": "e1"}

    monkeypatch.setattr(main.calendar_integration, "create_event", _fake_create)

    response = _run(live._execute_tool(_make_fc(
        action="create", summary="Standup", start_iso="2026-08-15T09:00:00-05:00", end_iso="2026-08-15T09:30:00-05:00",
    )))

    assert captured["approved"] is True
    assert captured["ignore_conflicts"] is False
    assert "created" in response.response["result"].lower()
    assert "standup" in response.response["result"].lower()


def test_create_conflict_is_reported_not_silently_double_booked(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.calendar_integration, "create_event", lambda *a, **k: {
        "ok": False, "state": "CONFLICT", "detail": "1 existing event(s) overlap this time.",
        "conflicts": [{"summary": "Existing dentist appointment"}],
    })

    response = _run(live._execute_tool(_make_fc(
        action="create", summary="New thing", start_iso="2026-08-15T09:00:00-05:00", end_iso="2026-08-15T09:30:00-05:00",
    )))
    result = response.response["result"]
    assert "conflicts" in result.lower()
    assert "Existing dentist appointment" in result


def test_create_ignore_conflicts_flag_is_forwarded(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    captured = {}

    def _fake_create(summary, start_iso, end_iso, description="", location="", approved=False, ignore_conflicts=False):
        captured["ignore_conflicts"] = ignore_conflicts
        return {"ok": True, "event_id": "e1"}

    monkeypatch.setattr(main.calendar_integration, "create_event", _fake_create)

    _run(live._execute_tool(_make_fc(
        action="create", summary="Standup", start_iso="2026-08-15T09:00:00-05:00",
        end_iso="2026-08-15T09:30:00-05:00", ignore_conflicts=True,
    )))
    assert captured["ignore_conflicts"] is True


# ── update (gated) ────────────────────────────────────────────────────

def test_update_without_event_id_is_refused(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    called = {"n": 0}
    monkeypatch.setattr(main.calendar_integration, "update_event",
                         lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    response = _run(live._execute_tool(_make_fc(action="update", summary="New title")))
    assert called["n"] == 0
    assert "event id" in response.response["result"].lower()


def test_update_without_any_fields_is_refused(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    called = {"n": 0}
    monkeypatch.setattr(main.calendar_integration, "update_event",
                         lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    response = _run(live._execute_tool(_make_fc(action="update", event_id="e1")))
    assert called["n"] == 0
    assert "change" in response.response["result"].lower()


def test_update_success_passes_approved_true_and_only_provided_fields(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    captured = {}

    def _fake_update(event_id, approved=False, **fields):
        captured.update(event_id=event_id, approved=approved, fields=fields)
        return {"ok": True, "event_id": event_id}

    monkeypatch.setattr(main.calendar_integration, "update_event", _fake_update)

    response = _run(live._execute_tool(_make_fc(action="update", event_id="e1", summary="Rescheduled")))

    assert captured == {"event_id": "e1", "approved": True, "fields": {"summary": "Rescheduled"}}
    assert "updated" in response.response["result"].lower()


def test_unknown_calendar_action_reports_clearly():
    main, _ = _new_live()
    live = _live(main)

    response = _run(live._execute_tool(_make_fc(action="delete_everything")))
    assert "unknown calendar action" in response.response["result"].lower()
