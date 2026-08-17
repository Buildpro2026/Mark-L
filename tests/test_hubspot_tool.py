"""main.py's "hubspot" voice tool — read/search/gated-upsert wiring into
actions/hubspot_integration.py. Never makes a live HubSpot call: every
hubspot_integration.py function is monkeypatched.

Like Gmail/Calendar/Airtable, 'upsert_contact'/'upsert_company' require
the dedup key (email/company_name) AND properties to already be present
before approved=True is ever passed — the same code-level gate pattern
used throughout this session's integration wiring.
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


def _new_live(name="jarvis_main_hubspot"):
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
    return type("FC", (), {"id": "call-1", "name": "hubspot", "args": args})()


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
    assert "hubspot" in names


# ── status ────────────────────────────────────────────────────────────

def test_status_reports_connected(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.hubspot_integration, "is_configured", lambda: True)

    response = _run(live._execute_tool(_make_fc(action="status")))
    assert "connected" in response.response["result"].lower()


def test_status_reports_not_configured(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.hubspot_integration, "is_configured", lambda: False)

    response = _run(live._execute_tool(_make_fc(action="status")))
    assert "isn't configured" in response.response["result"].lower()


# ── list / search (read) ─────────────────────────────────────────────

def test_list_contacts_reports_none_found_when_empty(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.hubspot_integration, "get_contacts", lambda limit: {"ok": True, "results": []})

    response = _run(live._execute_tool(_make_fc(action="list_contacts")))
    assert "no records found" in response.response["result"].lower()


def test_list_companies_summarizes_results(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.hubspot_integration, "get_companies", lambda limit: {
        "ok": True, "results": [{"id": "9", "properties": {"name": "Acme"}}],
    })

    response = _run(live._execute_tool(_make_fc(action="list_companies")))
    assert "Acme" in response.response["result"]


def test_search_contacts_requires_a_query(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    called = {"n": 0}
    monkeypatch.setattr(main.hubspot_integration, "search_contacts",
                         lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    response = _run(live._execute_tool(_make_fc(action="search_contacts")))
    assert called["n"] == 0
    assert "search term" in response.response["result"].lower()


def test_search_contacts_success(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.hubspot_integration, "search_contacts", lambda query, limit=20: {
        "ok": True, "results": [{"id": "1", "properties": {"email": "jane@x.com"}}],
    })

    response = _run(live._execute_tool(_make_fc(action="search_contacts", query="jane")))
    assert "jane@x.com" in response.response["result"]


def test_list_surfaces_a_failure_honestly(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.hubspot_integration, "get_contacts",
                         lambda limit: {"ok": False, "state": "NOT_CONFIGURED", "detail": "no token"})

    response = _run(live._execute_tool(_make_fc(action="list_contacts")))
    result = response.response["result"].lower()
    assert "couldn't read hubspot" in result
    assert "not_configured" in result


# ── upsert_contact (gated) ───────────────────────────────────────────

def test_upsert_contact_without_email_is_refused(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    called = {"n": 0}
    monkeypatch.setattr(main.hubspot_integration, "upsert_contact",
                         lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    response = _run(live._execute_tool(_make_fc(action="upsert_contact", properties='{"firstname": "Jane"}')))
    assert called["n"] == 0
    assert "email" in response.response["result"].lower()


def test_upsert_contact_without_properties_is_refused(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    called = {"n": 0}
    monkeypatch.setattr(main.hubspot_integration, "upsert_contact",
                         lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    response = _run(live._execute_tool(_make_fc(action="upsert_contact", email="jane@x.com")))
    assert called["n"] == 0


def test_upsert_contact_with_full_details_passes_approved_true(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    captured = {}

    def _fake_upsert(email, properties, approved=False):
        captured.update(email=email, properties=properties, approved=approved)
        return {"ok": True, "action": "created", "record": {"id": "1"}}

    monkeypatch.setattr(main.hubspot_integration, "upsert_contact", _fake_upsert)

    response = _run(live._execute_tool(_make_fc(
        action="upsert_contact", email="jane@x.com", properties='{"firstname": "Jane"}',
    )))

    assert captured["email"] == "jane@x.com"
    assert captured["properties"] == {"firstname": "Jane"}
    assert captured["approved"] is True
    assert "created" in response.response["result"].lower()


def test_upsert_contact_failure_is_reported(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.hubspot_integration, "upsert_contact",
                         lambda email, properties, approved=False: {"ok": False, "state": "NOT_CONFIGURED", "detail": "no token"})

    response = _run(live._execute_tool(_make_fc(
        action="upsert_contact", email="jane@x.com", properties='{"firstname": "Jane"}',
    )))
    assert "couldn't write the contact" in response.response["result"].lower()


# ── upsert_company (gated) ───────────────────────────────────────────

def test_upsert_company_without_name_is_refused(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    called = {"n": 0}
    monkeypatch.setattr(main.hubspot_integration, "upsert_company",
                         lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    response = _run(live._execute_tool(_make_fc(action="upsert_company", properties='{"industry": "construction"}')))
    assert called["n"] == 0


def test_upsert_company_with_full_details_passes_approved_true(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    captured = {}

    def _fake_upsert(name, properties, approved=False):
        captured.update(name=name, properties=properties, approved=approved)
        return {"ok": True, "action": "updated", "record": {"id": "co-1"}}

    monkeypatch.setattr(main.hubspot_integration, "upsert_company", _fake_upsert)

    response = _run(live._execute_tool(_make_fc(
        action="upsert_company", company_name="Acme", properties='{"industry": "construction"}',
    )))

    assert captured["name"] == "Acme"
    assert captured["approved"] is True
    assert "updated" in response.response["result"].lower()


def test_upsert_with_invalid_json_properties_is_refused(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    called = {"n": 0}
    monkeypatch.setattr(main.hubspot_integration, "upsert_contact",
                         lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    response = _run(live._execute_tool(_make_fc(
        action="upsert_contact", email="jane@x.com", properties="not json",
    )))
    assert called["n"] == 0


def test_unknown_hubspot_action_reports_clearly():
    main, _ = _new_live()
    live = _live(main)

    response = _run(live._execute_tool(_make_fc(action="delete_everything")))
    assert "unknown hubspot action" in response.response["result"].lower()
