"""main.py's "airtable" voice tool — generic base/table/fields wiring into
actions/airtable_integration.py. Never makes a live Airtable call: every
airtable_integration.py function is monkeypatched.

Unlike Gmail/Calendar (pre-existing schemas: email, events), Airtable has
no fixed schema this codebase can assume — base_id/table_name/fields are
always explicit, per-call parameters, never hardcoded or guessed. 'create'
and 'update' require base_id, table_name, AND fields to already be present
before approved=True is ever passed — the same code-level gate pattern
used for gmail's 'send' and calendar's 'create'.
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


def _new_live(name="jarvis_main_airtable"):
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
    return type("FC", (), {"id": "call-1", "name": "airtable", "args": args})()


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
    assert "airtable" in names


# ── status ────────────────────────────────────────────────────────────

def test_status_reports_configured(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.airtable_integration, "get_status",
                         lambda: {"configured": True, "state": "CONFIGURED"})

    response = _run(live._execute_tool(_make_fc(action="status")))
    assert "connected" in response.response["result"].lower()


def test_status_reports_not_configured(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.airtable_integration, "get_status",
                         lambda: {"configured": False, "state": "NOT_CONFIGURED"})

    response = _run(live._execute_tool(_make_fc(action="status")))
    assert "isn't configured" in response.response["result"].lower()


# ── list (read) ───────────────────────────────────────────────────────

def test_list_requires_base_id_and_table_name(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    called = {"n": 0}
    monkeypatch.setattr(main.airtable_integration, "list_records",
                         lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    response = _run(live._execute_tool(_make_fc(action="list")))
    assert called["n"] == 0
    assert "base id" in response.response["result"].lower()


def test_list_reports_no_matching_records_when_empty(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.airtable_integration, "list_records",
                         lambda base_id, table_name, max_records, filter_by_formula="": {"ok": True, "records": []})

    response = _run(live._execute_tool(_make_fc(action="list", base_id="appXXX", table_name="Leads")))
    assert "no matching records" in response.response["result"].lower()


def test_list_summarizes_records(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.airtable_integration, "list_records", lambda base_id, table_name, max_records, filter_by_formula="": {
        "ok": True,
        "records": [{"id": "rec1", "fields": {"Name": "Jane"}}],
    })

    response = _run(live._execute_tool(_make_fc(action="list", base_id="appXXX", table_name="Leads")))
    result = response.response["result"]
    assert "rec1" in result
    assert "Jane" in result


def test_list_surfaces_a_failure_honestly(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.airtable_integration, "list_records",
                         lambda base_id, table_name, max_records, filter_by_formula="": {"ok": False, "state": "ERROR", "detail": "Table not found", "records": []})

    response = _run(live._execute_tool(_make_fc(action="list", base_id="appXXX", table_name="NoSuch")))
    result = response.response["result"].lower()
    assert "couldn't read airtable" in result
    assert "table not found" in result


# ── create (gated) ────────────────────────────────────────────────────

def test_create_without_base_or_table_is_refused(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    called = {"n": 0}
    monkeypatch.setattr(main.airtable_integration, "create_record",
                         lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    response = _run(live._execute_tool(_make_fc(action="create", fields='{"Name": "Jane"}')))
    assert called["n"] == 0


def test_create_without_fields_is_refused(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    called = {"n": 0}
    monkeypatch.setattr(main.airtable_integration, "create_record",
                         lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    response = _run(live._execute_tool(_make_fc(action="create", base_id="appXXX", table_name="Leads")))
    assert called["n"] == 0
    assert "fields" in response.response["result"].lower()


def test_create_with_invalid_json_fields_is_refused(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    called = {"n": 0}
    monkeypatch.setattr(main.airtable_integration, "create_record",
                         lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    response = _run(live._execute_tool(_make_fc(
        action="create", base_id="appXXX", table_name="Leads", fields="not valid json",
    )))
    assert called["n"] == 0


def test_create_with_full_details_passes_approved_true_and_parses_fields(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    captured = {}

    def _fake_create(base_id, table_name, fields, approved=False):
        captured.update(base_id=base_id, table_name=table_name, fields=fields, approved=approved)
        return {"ok": True, "record": {"id": "rec1"}}

    monkeypatch.setattr(main.airtable_integration, "create_record", _fake_create)

    response = _run(live._execute_tool(_make_fc(
        action="create", base_id="appXXX", table_name="Leads", fields='{"Name": "Jane Doe", "Status": "New"}',
    )))

    assert captured["base_id"] == "appXXX"
    assert captured["table_name"] == "Leads"
    assert captured["fields"] == {"Name": "Jane Doe", "Status": "New"}
    assert captured["approved"] is True
    assert "created" in response.response["result"].lower()


def test_create_failure_is_reported_not_fabricated(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.airtable_integration, "create_record",
                         lambda base_id, table_name, fields, approved=False: {"ok": False, "state": "ERROR", "detail": "Unknown field name"})

    response = _run(live._execute_tool(_make_fc(
        action="create", base_id="appXXX", table_name="Leads", fields='{"Bogus": "x"}',
    )))
    result = response.response["result"].lower()
    assert "couldn't create the record" in result
    assert "unknown field name" in result


# ── update (gated) ────────────────────────────────────────────────────

def test_update_without_record_id_is_refused(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    called = {"n": 0}
    monkeypatch.setattr(main.airtable_integration, "update_record",
                         lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    response = _run(live._execute_tool(_make_fc(
        action="update", base_id="appXXX", table_name="Leads", fields='{"Status": "Contacted"}',
    )))
    assert called["n"] == 0
    assert "record id" in response.response["result"].lower()


def test_update_success_passes_approved_true(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    captured = {}

    def _fake_update(base_id, table_name, record_id, fields, approved=False):
        captured.update(record_id=record_id, fields=fields, approved=approved)
        return {"ok": True, "record": {"id": record_id}}

    monkeypatch.setattr(main.airtable_integration, "update_record", _fake_update)

    response = _run(live._execute_tool(_make_fc(
        action="update", base_id="appXXX", table_name="Leads", record_id="rec1",
        fields='{"Status": "Contacted"}',
    )))

    assert captured == {"record_id": "rec1", "fields": {"Status": "Contacted"}, "approved": True}
    assert "updated" in response.response["result"].lower()


def test_unknown_airtable_action_reports_clearly():
    main, _ = _new_live()
    live = _live(main)

    response = _run(live._execute_tool(_make_fc(action="delete_everything")))
    assert "unknown airtable action" in response.response["result"].lower()
